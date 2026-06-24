import asyncio
import logging
from datetime import datetime, timezone

from bson import ObjectId
from solders.pubkey import Pubkey

from app.config import settings, staking_signer_private_key
from app.database import (
    allocs_col,
    purchase_wallets_col,
    purchases_col,
    relationship_tree_col,
    transactions_col,
    users_col,
)
from app.services.commission import TOTAL_COMMISSION_RATE, distribute_commissions
from app.services.global_pool import resolve_active_pool
from app.services.indexer import run_dir_indir_index_batch, run_indexer_batch, run_level_index_batch, run_self_purchase_index
from app.services.sol_price import get_sol_price
from app.services.solana_rpc import get_balance_stable, transfer_sol, confirm_transaction
from app.services.staking_sdk import check_purchase_id, stake_with_purchase_id
from app.services.wallet_pool import ensure_wallet_pool, mark_wallet_used
from app.utils.economics import calculate_power_amount
from app.utils.keypair import keypair_from_private_key
from app.utils.tranche import tranche_deduction_usd

logger = logging.getLogger(__name__)


async def process_completed_purchase(
    purchase_id: str, balance_sol: float, skip_token_dispatch: bool = False, force_process: bool = False
) -> None:
    """Execute the full post-payment flow for a confirmed purchase."""
    pid = ObjectId(purchase_id)
    purchase = await purchases_col().find_one({"_id": pid})
    if not purchase:
        logger.error(f"Purchase {purchase_id} not found")
        return

    if purchase["status"] != "pending" and not force_process:
        logger.warning(f"Purchase {purchase_id} already processed (status={purchase['status']})")
        return

    expected_sol = purchase.get("sol_amount_expected", 0.0)
    if balance_sol < expected_sol * 0.95:
        logger.warning(
            f"Purchase {purchase_id}: insufficient payment "
            f"(received {balance_sol:.6f} SOL, expected {expected_sol:.6f} SOL). Skipping."
        )
        return

    sol_price = await get_sol_price()

    # 1. Mark purchase confirmed
    await purchases_col().update_one(
        {"_id": pid},
        {
            "$set": {
                "status": "completed",
                "confirmed_at": datetime.now(timezone.utc),
                "sol_amount_received": balance_sol,
                "sol_price_at_confirmation": sol_price,
            }
        },
    )
    logger.info(f"Purchase {purchase_id} confirmed: {balance_sol:.6f} SOL at ${sol_price}")
    if settings.global_pool_enabled:
        await resolve_active_pool(datetime.now(timezone.utc))

    buyer_wallet = purchase["user_wallet"]
    purchase_wallet_pubkey = purchase["purchase_wallet_pubkey"]
    xfee_amount = purchase["xfee_amount"]
    # 1 XFEE = 1 USD
    sale_usd = float(xfee_amount)
    purchase_value_sol = purchase.get("purchase_value_sol")
    if purchase_value_sol is None:
        purchase_value_sol = sale_usd / sol_price
        await purchases_col().update_one(
            {"_id": pid},
            {
                "$set": {
                    "purchase_value_sol": round(purchase_value_sol, 6),
                    "gas_buffer_usd": round(max((expected_sol * sol_price) - sale_usd, 0.0), 2),
                }
            },
        )

    # 2. Dispatch POWER to buyer
    power_amount = calculate_power_amount(sale_usd)
    
    if skip_token_dispatch:
        logger.info(f"POWER staking skipped for purchase {purchase_id}: skip_token_dispatch=True")
    elif settings.test_mode:
        fake_tx = f"test_stake_{pid}"
        logger.info(f"[TEST] Skipping real stake for purchase {purchase_id}; using fake tx {fake_tx}")
        await purchases_col().update_one(
            {"_id": pid},
            {
                "$set": {
                    "token_dispatch_tx": fake_tx,
                    "power_distribution_status": "staked",
                    "power_distribution_bonus_eligible": False,
                    "power_base_amount": power_amount,
                    "power_amount_staked": power_amount,
                    "power_staked_at": datetime.now(timezone.utc),
                }
            },
        )
    elif not settings.power_distribution_enabled:
        logger.warning(f"POWER staking disabled by config for purchase {purchase_id}; purchase flow will continue")
        await purchases_col().update_one(
            {"_id": pid},
            {
                "$set": {
                    "power_distribution_skipped": True,
                    "power_distribution_status": "pending_delayed_stake",
                    "power_distribution_skip_reason": "disabled_by_config",
                    "power_distribution_skipped_at": datetime.now(timezone.utc),
                    "power_distribution_bonus_eligible": True,
                    "power_base_amount": power_amount,
                }
            },
        )
    else:
        try:
            info = await asyncio.to_thread(
                check_purchase_id,
                pool_address=settings.pool_address,
                purchase_id=str(pid),
                rpc_url=settings.quicknode_rpc_url
            )
            
            if info["staked"]:
                logger.info(f"Purchase {purchase_id} already staked on-chain.")
                token_tx = "already_staked"
                await purchases_col().update_one(
                    {"_id": pid},
                    {
                        "$set": {
                            "token_dispatch_tx": token_tx,
                            "power_distribution_status": "already_staked",
                            "power_distribution_bonus_eligible": False,
                            "power_base_amount": power_amount,
                        }
                    },
                )
            else:
                result = await asyncio.to_thread(
                    stake_with_purchase_id,
                    admin_private_key_b58=staking_signer_private_key(),
                    pool_address=settings.pool_address,
                    user_address=buyer_wallet,
                    amount=power_amount,
                    purchase_id=str(pid),
                    rpc_url=settings.quicknode_rpc_url
                )
                
                if result["success"]:
                    token_tx = result["signature"]
                    await purchases_col().update_one(
                        {"_id": pid},
                        {
                            "$set": {
                                "token_dispatch_tx": token_tx,
                                "power_distribution_status": "staked",
                                "power_distribution_bonus_eligible": False,
                                "power_base_amount": power_amount,
                                "power_amount_staked": power_amount,
                                "power_staked_at": datetime.now(timezone.utc),
                            }
                        },
                    )

                    await transactions_col().insert_one(
                        {
                            "purchase_id": pid,
                            "tx_type": "power_stake",
                            "from_wallet": settings.master_wallet_address,
                            "to_wallet": buyer_wallet,
                            "amount_sol": 0.0,
                            "tx_signature": token_tx,
                            "created_at": datetime.now(timezone.utc),
                        }
                    )
                elif result["already_staked"]:
                    logger.info(f"Purchase {purchase_id} was already staked (caught by SDK).")
                    await purchases_col().update_one(
                        {"_id": pid},
                        {
                            "$set": {
                                "token_dispatch_tx": "already_staked",
                                "power_distribution_status": "already_staked",
                                "power_distribution_bonus_eligible": False,
                                "power_base_amount": power_amount,
                            }
                        },
                    )
                else:
                    raise Exception(result["error"])
        except Exception:
            logger.exception(f"Power staking failed for purchase {purchase_id}")
            await purchases_col().update_one({"_id": pid}, {"$set": {"status": "failed"}})
            return

    deduction_usd = tranche_deduction_usd(sale_usd)
    commissionable_sol = purchase_value_sol - (deduction_usd / sol_price)

    # 3 & 4. Distribute commissions (creates allocs for ALL ancestors)
    if sale_usd >= 10.0:
        try:
            total_distributed = await distribute_commissions(
                pid,
                buyer_wallet,
                commissionable_sol,
                purchase_wallet_pubkey,
                sale_usd,
                xfee_amount,
                purchase_value_sol,
                sol_price,
            )

            await purchases_col().update_one(
                {"_id": pid}, {"$set": {"commission_distributed": True}}
            )
        except Exception:
            logger.exception(f"Commission distribution failed for purchase {purchase_id}")
            total_distributed = 0.0
    else:
        logger.info(f"Purchase {purchase_id} is < $10 ({sale_usd:.2f} USD). Skipping commissions.")
        total_distributed = 0.0
        await purchases_col().update_one(
            {"_id": pid}, {"$set": {"commission_distributed": True}}
        )

    # 5 & 6. Sweep remaining SOL to master wallet
    try:
        pw_doc = await purchase_wallets_col().find_one({"public_key": purchase_wallet_pubkey})
        if not pw_doc:
            raise Exception(f"Purchase wallet {purchase_wallet_pubkey} not found for sweep")

        pw_keypair = keypair_from_private_key(pw_doc["private_key"])

        # Use stable reads so balance reflects commission debits before sweep math / chain edge RPCs.
        current_balance_lamports = await get_balance_stable(purchase_wallet_pubkey)

        # Reserve the actual gas buffer applied to this purchase (or fallback to $2.00)
        gas_buffer_usd = purchase.get("gas_buffer_usd", 2.0)
        reserve_lamports = int((gas_buffer_usd / sol_price) * 1e9)
        rent_lamports = 890_880
        fee_buffer_lamports = 20_000
        target_sweep_lamports = int((commissionable_sol - total_distributed) * 1e9)
        available_lamports = current_balance_lamports - reserve_lamports - rent_lamports - fee_buffer_lamports
        sweep_lamports = min(target_sweep_lamports, available_lamports)

        if sweep_lamports > 0:
            sweep_amount_sol = sweep_lamports / 1e9
            sig = await transfer_sol(
                pw_keypair,
                Pubkey.from_string(settings.master_wallet_address),
                sweep_lamports,
            )
            confirmed = await confirm_transaction(sig)

            alloc_doc = {
                "purchase_id": pid,
                "recipient_wallet": settings.master_wallet_address,
                "sol_amount": sweep_amount_sol,
                "sale_usd": 0.0,
                "alloc_type": "master_sweep",
                "ancestor_level_tier": 0,
                "differential_rate": 0.0,
                "on_chain_tx": sig if confirmed else None,
                "status": "sent" if confirmed else "failed",
                "indexed": True,
                "created_at": datetime.now(timezone.utc),
            }
            await allocs_col().insert_one(alloc_doc)

            await transactions_col().insert_one(
                {
                    "purchase_id": pid,
                    "tx_type": "master_sweep",
                    "from_wallet": purchase_wallet_pubkey,
                    "to_wallet": settings.master_wallet_address,
                    "amount_sol": sweep_amount_sol,
                    "tx_signature": sig,
                    "created_at": datetime.now(timezone.utc),
                }
            )
            commission_pool = commissionable_sol * TOTAL_COMMISSION_RATE
            undistributed = commission_pool - total_distributed
            logger.info(
                f"Swept {sweep_amount_sol:.6f} SOL to master "
                f"(undistributed commission: {undistributed:.6f} SOL)"
            )
    except Exception:
        logger.exception(f"Master sweep failed for purchase {purchase_id}")

    # 7. Record remaining balance and mark purchase wallet as used
    try:
        final_balance_lamports = await get_balance_stable(purchase_wallet_pubkey)
        final_balance_sol = final_balance_lamports / 1e9
        await purchase_wallets_col().update_one(
            {"public_key": purchase_wallet_pubkey},
            {"$set": {"remaining_balance_sol": final_balance_sol}},
        )
    except Exception:
        logger.exception(f"Failed to record remaining balance for {purchase_wallet_pubkey}")

    await mark_wallet_used(pid)

    # 8. Index: update self_purchase for buyer, then batch-index all ancestors
    try:
        await run_self_purchase_index(buyer_wallet)

        tree_doc = await relationship_tree_col().find_one({"wallet_address": buyer_wallet})
        if tree_doc and tree_doc.get("ancestors"):
            await run_indexer_batch(tree_doc["ancestors"])
            await run_level_index_batch(tree_doc["ancestors"])
            await run_dir_indir_index_batch(tree_doc["ancestors"])
    except Exception:
        logger.exception(f"Indexer update failed for purchase {purchase_id}")

    # 9. Mark buyer as valid referrer
    await users_col().update_one(
        {"wallet_address": buyer_wallet},
        {"$set": {"is_valid_referrer": True}},
    )

    # 10. Top up wallet pool if needed
    await ensure_wallet_pool()

    logger.info(f"Purchase {purchase_id} fully processed")
