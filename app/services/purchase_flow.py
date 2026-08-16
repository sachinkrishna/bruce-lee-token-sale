"""Post-payment flow for confirmed purchases.

`process_completed_purchase` is the single entrypoint. Internally it
dispatches on `payment_mode` (snapshot at initiate time on the purchase doc):

  - SOL mode → `_process_sol_mode` (legacy behavior: SOL commissions +
    SOL sweep to master, gas buffer stays on ephemeral wallet).
  - XFEE mode → `_process_xfee_mode` (SPL commissions + SPL sweep, all
    signed by ephemeral wallet with master as ATA-rent fee-payer).

Everything after the currency-specific block (POWER staking, indexer,
wallet pool top-up, etc.) is shared and executed once at the tail.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

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
from app.services.commission import (
    TOTAL_COMMISSION_RATE,
    distribute_commissions,
    distribute_commissions_xfee,
)
from app.services.global_pool import resolve_active_pool
from app.services.indexer import (
    run_dir_indir_index_batch,
    run_indexer_batch,
    run_level_index_batch,
    run_self_purchase_index,
)
from app.services.sol_price import get_sol_price
from app.services.solana_rpc import (
    confirm_transaction,
    get_balance_stable,
    get_spl_balance_stable,
    transfer_sol,
    transfer_spl_token,
)
from app.services.staking_sdk import check_purchase_id, stake_with_purchase_id
from app.services.wallet_pool import ensure_wallet_pool, mark_wallet_used
from app.services.xfee_price import get_xfee_price
from app.utils.economics import calculate_power_amount
from app.utils.keypair import keypair_from_private_key
from app.utils.tranche import tranche_deduction_usd

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Public entrypoint
# ─────────────────────────────────────────────────────────────────────────────


async def process_completed_purchase(
    purchase_id: str,
    balance_sol: float,
    skip_token_dispatch: bool = False,
    force_process: bool = False,
    *,
    xfee_amount_raw: Optional[int] = None,
) -> None:
    """Execute the full post-payment flow for a confirmed purchase.

    `xfee_amount_raw` is only used when the purchase is in XFEE mode; the
    poller passes the observed XFEE ATA balance.
    """
    pid = ObjectId(purchase_id)
    purchase = await purchases_col().find_one({"_id": pid})
    if not purchase:
        logger.error(f"Purchase {purchase_id} not found")
        return

    if purchase["status"] != "pending" and not force_process:
        logger.warning(f"Purchase {purchase_id} already processed (status={purchase['status']})")
        return

    mode = purchase.get("payment_mode", "SOL")
    if mode == "XFEE":
        result = await _process_xfee_mode(
            pid, purchase, balance_sol=balance_sol, balance_xfee_raw=int(xfee_amount_raw or 0)
        )
    else:
        result = await _process_sol_mode(pid, purchase, balance_sol=balance_sol)

    if result is None:
        # Non-recoverable early exit (insufficient payment, staking failed, …).
        return

    # Shared tail: indexing, valid-referrer flip, wallet-pool top-up.
    buyer_wallet = purchase["user_wallet"]
    await _post_flow_shared(pid, buyer_wallet, purchase["purchase_wallet_pubkey"])
    logger.info(f"Purchase {purchase_id} fully processed [mode={mode}]")


# ─────────────────────────────────────────────────────────────────────────────
# SOL-mode flow (unchanged behavior)
# ─────────────────────────────────────────────────────────────────────────────


async def _process_sol_mode(pid: ObjectId, purchase: dict, *, balance_sol: float) -> Optional[dict]:
    expected_sol = purchase.get("sol_amount_expected", 0.0)
    if balance_sol < expected_sol * 0.95:
        logger.warning(
            f"Purchase {pid}: insufficient SOL payment "
            f"(received {balance_sol:.6f}, expected {expected_sol:.6f}). Skipping."
        )
        return None

    sol_price = await get_sol_price()

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
    logger.info(f"Purchase {pid} confirmed [SOL]: {balance_sol:.6f} SOL at ${sol_price}")
    if settings.global_pool_enabled:
        await resolve_active_pool(datetime.now(timezone.utc))

    buyer_wallet = purchase["user_wallet"]
    purchase_wallet_pubkey = purchase["purchase_wallet_pubkey"]
    xfee_amount = purchase["xfee_amount"]
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

    power_ok = await _dispatch_power_stake(
        pid, buyer_wallet, sale_usd, skip=False
    )
    if not power_ok:
        return None

    deduction_usd = tranche_deduction_usd(sale_usd)
    commissionable_sol = purchase_value_sol - (deduction_usd / sol_price)

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
            logger.exception(f"Commission distribution failed for purchase {pid}")
            total_distributed = 0.0
    else:
        logger.info(f"Purchase {pid} is < $10 ({sale_usd:.2f} USD). Skipping commissions.")
        total_distributed = 0.0
        await purchases_col().update_one(
            {"_id": pid}, {"$set": {"commission_distributed": True}}
        )

    # Sweep remaining SOL to master.
    try:
        pw_doc = await purchase_wallets_col().find_one({"public_key": purchase_wallet_pubkey})
        if not pw_doc:
            raise Exception(f"Purchase wallet {purchase_wallet_pubkey} not found for sweep")

        pw_keypair = keypair_from_private_key(pw_doc["private_key"])
        current_balance_lamports = await get_balance_stable(purchase_wallet_pubkey)

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

            await allocs_col().insert_one(
                {
                    "purchase_id": pid,
                    "recipient_wallet": settings.master_wallet_address,
                    "sol_amount": sweep_amount_sol,
                    "sale_usd": 0.0,
                    "alloc_type": "master_sweep",
                    "currency": "SOL",
                    "ancestor_level_tier": 0,
                    "differential_rate": 0.0,
                    "on_chain_tx": sig if confirmed else None,
                    "status": "sent" if confirmed else "failed",
                    "indexed": True,
                    "created_at": datetime.now(timezone.utc),
                }
            )
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
        logger.exception(f"Master sweep failed for purchase {pid}")

    # Record remaining balance and mark wallet used.
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
    return {"mode": "SOL"}


# ─────────────────────────────────────────────────────────────────────────────
# XFEE-mode flow (new)
# ─────────────────────────────────────────────────────────────────────────────


async def _process_xfee_mode(
    pid: ObjectId,
    purchase: dict,
    *,
    balance_sol: float,
    balance_xfee_raw: int,
) -> Optional[dict]:
    """Confirm and distribute an XFEE-mode purchase.

    In XFEE mode:
      - SOL received on the ephemeral wallet is the buyer's gas buffer (kept
        on the ephemeral wallet to pay tx fees for commissions + sweep).
      - `balance_xfee_raw` is what the buyer paid in XFEE (raw, integer).
    """
    expected_xfee_raw = int(purchase.get("xfee_amount_expected_raw") or 0)
    tolerance = float(settings.xfee_mode_receive_tolerance or 0.95)
    if expected_xfee_raw <= 0:
        logger.error("Purchase %s in XFEE mode has no xfee_amount_expected_raw", pid)
        return None
    min_xfee_raw = int(expected_xfee_raw * tolerance)
    if balance_xfee_raw < min_xfee_raw:
        logger.warning(
            "Purchase %s: insufficient XFEE payment (received %s raw, expected >= %s raw). Skipping.",
            pid, balance_xfee_raw, min_xfee_raw,
        )
        return None

    xfee_mint = purchase.get("xfee_mint") or settings.xfee_payment_token_mint
    decimals = int(purchase.get("xfee_decimals") or settings.xfee_payment_token_decimals or 9)
    if not xfee_mint:
        logger.error("Purchase %s in XFEE mode but no xfee_mint set anywhere", pid)
        return None

    xfee_price = await get_xfee_price()
    now = datetime.now(timezone.utc)

    await purchases_col().update_one(
        {"_id": pid},
        {
            "$set": {
                "status": "completed",
                "confirmed_at": now,
                "sol_amount_received": balance_sol,
                "xfee_amount_received_raw": balance_xfee_raw,
                "xfee_price_at_confirmation": xfee_price,
            }
        },
    )
    logger.info(
        "Purchase %s confirmed [XFEE]: %s raw XFEE at $%s (+ %.6f SOL gas)",
        pid, balance_xfee_raw, xfee_price, balance_sol,
    )
    if settings.global_pool_enabled:
        await resolve_active_pool(now)

    buyer_wallet = purchase["user_wallet"]
    purchase_wallet_pubkey = purchase["purchase_wallet_pubkey"]
    xfee_amount = purchase["xfee_amount"]
    sale_usd = float(xfee_amount)

    power_ok = await _dispatch_power_stake(pid, buyer_wallet, sale_usd, skip=False)
    if not power_ok:
        return None

    # Commissionable XFEE = received XFEE minus the tranche-deduction converted
    # to XFEE at the confirmation price. Deduction is expressed in USD.
    deduction_usd = tranche_deduction_usd(sale_usd)
    deduction_xfee_raw = int((deduction_usd / xfee_price) * (10 ** decimals)) if xfee_price > 0 else 0
    commissionable_xfee_raw = max(0, balance_xfee_raw - deduction_xfee_raw)

    total_distributed_raw = 0
    if sale_usd >= 10.0:
        try:
            total_distributed_raw = await distribute_commissions_xfee(
                pid,
                buyer_wallet,
                commissionable_xfee_raw,
                purchase_wallet_pubkey,
                sale_usd,
                xfee_amount,
                xfee_price,
                decimals,
                xfee_mint,
            )
            await purchases_col().update_one(
                {"_id": pid}, {"$set": {"commission_distributed": True}}
            )
        except Exception:
            logger.exception("XFEE commission distribution failed for purchase %s", pid)
    else:
        logger.info("Purchase %s is < $10 ($%.2f). Skipping commissions.", pid, sale_usd)
        await purchases_col().update_one(
            {"_id": pid}, {"$set": {"commission_distributed": True}}
        )

    # Sweep remaining XFEE to master's XFEE ATA (master pays SOL fee since the
    # ephemeral wallet's gas buffer is small).
    try:
        pw_doc = await purchase_wallets_col().find_one({"public_key": purchase_wallet_pubkey})
        if not pw_doc:
            raise Exception(f"Purchase wallet {purchase_wallet_pubkey} not found for XFEE sweep")

        pw_keypair = keypair_from_private_key(pw_doc["private_key"])
        current_xfee_raw = await get_spl_balance_stable(purchase_wallet_pubkey, xfee_mint)

        # Leave a small buffer so tiny rounding errors don't cause the sweep to
        # over-request. `commissionable_xfee_raw - total_distributed_raw` is the
        # target; cap at what's actually there.
        target_sweep_raw = commissionable_xfee_raw - total_distributed_raw
        sweep_raw = min(target_sweep_raw, current_xfee_raw)

        if sweep_raw > 0:
            master_pubkey = Pubkey.from_string(settings.master_wallet_address)
            mint_pubkey = Pubkey.from_string(xfee_mint)
            master_kp = keypair_from_private_key(settings.master_wallet_private_key)

            sig = await transfer_spl_token(
                pw_keypair,
                master_pubkey,
                mint_pubkey,
                sweep_raw,
                decimals,
                fee_payer_keypair=master_kp,
            )
            confirmed = await confirm_transaction(sig)

            sweep_ui = sweep_raw / (10 ** decimals)
            await allocs_col().insert_one(
                {
                    "purchase_id": pid,
                    "recipient_wallet": settings.master_wallet_address,
                    "sol_amount": 0.0,
                    "xfee_amount_raw": sweep_raw,
                    "xfee_amount_ui": sweep_ui,
                    "sale_usd": 0.0,
                    "alloc_type": "master_sweep",
                    "currency": "XFEE",
                    "ancestor_level_tier": 0,
                    "differential_rate": 0.0,
                    "on_chain_tx": sig if confirmed else None,
                    "status": "sent" if confirmed else "failed",
                    "indexed": True,
                    "created_at": datetime.now(timezone.utc),
                }
            )
            await transactions_col().insert_one(
                {
                    "purchase_id": pid,
                    "tx_type": "master_sweep_xfee",
                    "from_wallet": purchase_wallet_pubkey,
                    "to_wallet": settings.master_wallet_address,
                    "amount_sol": 0.0,
                    "xfee_amount_raw": sweep_raw,
                    "xfee_amount_ui": sweep_ui,
                    "tx_signature": sig,
                    "created_at": datetime.now(timezone.utc),
                }
            )
            logger.info(
                "Swept %s raw XFEE (%.6f) to master (sig=%s, confirmed=%s)",
                sweep_raw, sweep_ui, sig, confirmed,
            )
    except Exception:
        logger.exception("XFEE master sweep failed for purchase %s", pid)

    # Record remaining balances and mark wallet used.
    try:
        final_sol_lamports = await get_balance_stable(purchase_wallet_pubkey)
        final_xfee_raw = await get_spl_balance_stable(purchase_wallet_pubkey, xfee_mint)
        await purchase_wallets_col().update_one(
            {"public_key": purchase_wallet_pubkey},
            {
                "$set": {
                    "remaining_balance_sol": final_sol_lamports / 1e9,
                    "remaining_xfee_raw": final_xfee_raw,
                }
            },
        )
    except Exception:
        logger.exception("Failed to record remaining balances for %s", purchase_wallet_pubkey)

    await mark_wallet_used(pid)
    return {"mode": "XFEE"}


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────


async def _dispatch_power_stake(
    pid: ObjectId,
    buyer_wallet: str,
    sale_usd: float,
    *,
    skip: bool,
) -> bool:
    """Stake POWER for the buyer. Returns False iff the call raised.

    Currency-agnostic: the POWER amount is computed from `sale_usd` regardless
    of whether the buyer paid in SOL or XFEE.
    """
    power_amount = calculate_power_amount(sale_usd)

    if skip:
        logger.info(f"POWER staking skipped for purchase {pid}: skip_token_dispatch=True")
        return True

    if settings.test_mode:
        fake_tx = f"test_stake_{pid}"
        logger.info(f"[TEST] Skipping real stake for purchase {pid}; using fake tx {fake_tx}")
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
        return True

    if not settings.power_distribution_enabled:
        logger.warning(
            "POWER staking disabled by config for purchase %s; flow will continue", pid
        )
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
        return True

    try:
        info = await asyncio.to_thread(
            check_purchase_id,
            pool_address=settings.pool_address,
            purchase_id=str(pid),
            rpc_url=settings.quicknode_rpc_url,
        )

        if info["staked"]:
            logger.info(f"Purchase {pid} already staked on-chain.")
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
            return True

        result = await asyncio.to_thread(
            stake_with_purchase_id,
            admin_private_key_b58=staking_signer_private_key(),
            pool_address=settings.pool_address,
            user_address=buyer_wallet,
            amount=power_amount,
            purchase_id=str(pid),
            rpc_url=settings.quicknode_rpc_url,
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
            return True

        if result["already_staked"]:
            logger.info(f"Purchase {pid} was already staked (caught by SDK).")
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
            return True

        raise Exception(result["error"])
    except Exception:
        logger.exception(f"Power staking failed for purchase {pid}")
        await purchases_col().update_one({"_id": pid}, {"$set": {"status": "failed"}})
        return False


async def _post_flow_shared(pid: ObjectId, buyer_wallet: str, purchase_wallet_pubkey: str) -> None:
    """Indexer updates, valid-referrer flip, wallet-pool top-up.

    Currency-agnostic — runs identically for SOL and XFEE mode.
    """
    try:
        await run_self_purchase_index(buyer_wallet)
        tree_doc = await relationship_tree_col().find_one({"wallet_address": buyer_wallet})
        if tree_doc and tree_doc.get("ancestors"):
            await run_indexer_batch(tree_doc["ancestors"])
            await run_level_index_batch(tree_doc["ancestors"])
            await run_dir_indir_index_batch(tree_doc["ancestors"])
    except Exception:
        logger.exception(f"Indexer update failed for purchase {pid}")

    await users_col().update_one(
        {"wallet_address": buyer_wallet},
        {"$set": {"is_valid_referrer": True}},
    )

    try:
        from app.services.founder import maybe_mark_founder_eligible

        fresh = await purchases_col().find_one({"_id": pid})
        if fresh is not None:
            await maybe_mark_founder_eligible(fresh)
    except Exception:
        logger.exception(f"Founder eligibility check failed for purchase {pid}")

    await ensure_wallet_pool()
