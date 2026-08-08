import logging
from datetime import datetime, timezone

from bson import ObjectId
from solders.pubkey import Pubkey

from app.config import settings
from app.database import allocs_col, relationship_tree_col, transactions_col, users_col, purchase_wallets_col
from app.services.global_pool import record_missed_commission_points
from app.services.solana_rpc import confirm_transaction, transfer_sol, transfer_spl_token
from app.utils.keypair import keypair_from_private_key
from app.utils.level import MAX_COMMISSION_LEVEL, get_rate_for_level

logger = logging.getLogger(__name__)

TOTAL_COMMISSION_RATE = 1.00


async def distribute_commissions(
    purchase_id: ObjectId,
    buyer_wallet: str,
    total_sol_received: float,
    purchase_wallet_pubkey: str,
    sale_usd: float,
    sale_tokens: int = 0,
    sale_sol: float = 0.0,
    sol_price: float = 0.0,
) -> float:
    """
    Walk the FULL ancestor tree, create an alloc for every ancestor
    (zero-commission included), and distribute differential commissions on-chain.
    Returns total SOL actually distributed as commissions.
    """
    tree_doc = await relationship_tree_col().find_one({"wallet_address": buyer_wallet})
    if not tree_doc or not tree_doc.get("ancestors"):
        logger.info(f"No ancestors for {buyer_wallet}, no commissions to distribute")
        return 0.0

    existing_alloc = await allocs_col().find_one({"purchase_id": purchase_id, "alloc_type": "commission"})
    if existing_alloc:
        logger.warning(f"Allocs already exist for purchase {purchase_id}, skipping duplicate distribution")
        return 0.0

    pw_doc = await purchase_wallets_col().find_one({"public_key": purchase_wallet_pubkey})
    if not pw_doc:
        raise Exception(f"Purchase wallet {purchase_wallet_pubkey} not found")
    pw_keypair = keypair_from_private_key(pw_doc["private_key"])

    ancestors = tree_doc["ancestors"]
    direct_parent = ancestors[0] if ancestors else None

    # Batch-fetch all ancestor user docs in one query
    ancestor_users = {}
    async for user in users_col().find({"wallet_address": {"$in": ancestors}}):
        ancestor_users[user["wallet_address"]] = user

    highest_level_paid_so_far = 0
    total_distributed = 0.0
    max_level_reached = False
    paid_commission_by_level = {}
    pending_allocs = []
    now = datetime.now(timezone.utc)

    for ancestor_wallet in ancestors:
        ancestor_user = ancestor_users.get(ancestor_wallet)
        if not ancestor_user:
            continue

        ancestor_level = ancestor_user.get("level", 1)

        if max_level_reached or ancestor_level <= highest_level_paid_so_far:
            alloc_doc = {
                "_id": ObjectId(),
                "purchase_id": purchase_id,
                "recipient_wallet": ancestor_wallet,
                "sol_amount": 0.0,
                "sale_usd": sale_usd,
                "sale_sol": sale_sol,
                "sale_tokens": sale_tokens,
                "alloc_type": "commission",
                "ancestor_level_tier": ancestor_level,
                "differential_rate": 0.0,
                "on_chain_tx": None,
                "status": "zero",
                "indexed": False,
                "level_indexed": False,
                "is_direct_sale": ancestor_wallet == direct_parent,
                "dir_indir_indexed": False,
                "created_at": now,
            }
            peer_commission_sol = paid_commission_by_level.get(ancestor_level)
            if settings.global_pool_enabled and peer_commission_sol and peer_commission_sol > 0:
                alloc_doc["global_pool_points_pending"] = True
                alloc_doc["global_pool_points_sol"] = peer_commission_sol
            pending_allocs.append(alloc_doc)
            continue

        differential_rate = get_rate_for_level(ancestor_level) - get_rate_for_level(highest_level_paid_so_far)
        commission_sol = total_sol_received * differential_rate
        if commission_sol > 0 and ancestor_level not in paid_commission_by_level:
            paid_commission_by_level[ancestor_level] = commission_sol

        try:
            lamports = int(commission_sol * 1e9)
            sig = None
            status = "zero"

            if lamports > 0:
                sig = await transfer_sol(
                    pw_keypair,
                    Pubkey.from_string(ancestor_wallet),
                    lamports,
                )
                confirmed = await confirm_transaction(sig)
                status = "sent" if confirmed else "failed"

                if confirmed:
                    total_distributed += commission_sol

                await transactions_col().insert_one({
                    "purchase_id": purchase_id,
                    "tx_type": "commission",
                    "from_wallet": purchase_wallet_pubkey,
                    "to_wallet": ancestor_wallet,
                    "amount_sol": commission_sol,
                    "tx_signature": sig,
                    "created_at": now,
                })

            pending_allocs.append({
                "_id": ObjectId(),
                "purchase_id": purchase_id,
                "recipient_wallet": ancestor_wallet,
                "sol_amount": commission_sol,
                "sale_usd": sale_usd,
                "sale_sol": sale_sol,
                "sale_tokens": sale_tokens,
                "alloc_type": "commission",
                "ancestor_level_tier": ancestor_level,
                "differential_rate": differential_rate,
                "on_chain_tx": sig,
                "status": status,
                "indexed": False,
                "level_indexed": False,
                "is_direct_sale": ancestor_wallet == direct_parent,
                "dir_indir_indexed": False,
                "created_at": now,
            })

            highest_level_paid_so_far = ancestor_level
            logger.info(
                f"Commission: {commission_sol:.6f} SOL to {ancestor_wallet} "
                f"(L{ancestor_level}, diff_rate={differential_rate})"
            )

            if highest_level_paid_so_far >= MAX_COMMISSION_LEVEL:
                max_level_reached = True

        except Exception:
            logger.exception(f"Failed to send commission to {ancestor_wallet}")
            pending_allocs.append({
                "_id": ObjectId(),
                "purchase_id": purchase_id,
                "recipient_wallet": ancestor_wallet,
                "sol_amount": commission_sol,
                "sale_usd": sale_usd,
                "sale_sol": sale_sol,
                "sale_tokens": sale_tokens,
                "alloc_type": "commission",
                "ancestor_level_tier": ancestor_level,
                "differential_rate": differential_rate,
                "on_chain_tx": None,
                "status": "failed",
                "indexed": False,
                "level_indexed": False,
                "is_direct_sale": ancestor_wallet == direct_parent,
                "dir_indir_indexed": False,
                "created_at": now,
            })

    # Batch insert all allocs in one operation
    if pending_allocs:
        await allocs_col().insert_many(pending_allocs)
        for alloc in pending_allocs:
            if not alloc.get("global_pool_points_pending"):
                continue
            try:
                points_sol = float(alloc["global_pool_points_sol"])
                await record_missed_commission_points(
                    alloc_id=alloc["_id"],
                    wallet_address=alloc["recipient_wallet"],
                    purchase_id=purchase_id,
                    event_time=now,
                    points_usd=points_sol * sol_price if sol_price > 0 else 0.0,
                    currency="SOL",
                    points_native=points_sol,
                )
            except Exception:
                logger.exception(
                    "Failed to record global pool points for alloc %s",
                    alloc["_id"],
                )

    commission_pool_sol = total_sol_received * TOTAL_COMMISSION_RATE
    logger.info(f"Total commission distributed: {total_distributed:.6f} SOL out of pool {commission_pool_sol:.6f} SOL")
    return total_distributed


async def distribute_commissions_xfee(
    purchase_id: ObjectId,
    buyer_wallet: str,
    commissionable_xfee_raw: int,
    purchase_wallet_pubkey: str,
    sale_usd: float,
    sale_tokens: int,
    xfee_price_usd: float,
    xfee_decimals: int,
    xfee_mint: str,
) -> int:
    """XFEE-mode commission distribution.

    Same cumulative-differential rules as the SOL branch, but transfers happen
    as SPL token transfers (with idempotent ATA creation, master pays SOL rent
    so the buyer's tiny gas buffer isn't blown on ATA creation).

    Returns the total raw XFEE actually distributed.
    """
    tree_doc = await relationship_tree_col().find_one({"wallet_address": buyer_wallet})
    if not tree_doc or not tree_doc.get("ancestors"):
        logger.info("No ancestors for %s, no XFEE commissions to distribute", buyer_wallet)
        return 0

    existing_alloc = await allocs_col().find_one({"purchase_id": purchase_id, "alloc_type": "commission"})
    if existing_alloc:
        logger.warning("Allocs already exist for purchase %s, skipping duplicate distribution", purchase_id)
        return 0

    pw_doc = await purchase_wallets_col().find_one({"public_key": purchase_wallet_pubkey})
    if not pw_doc:
        raise Exception(f"Purchase wallet {purchase_wallet_pubkey} not found")
    pw_keypair = keypair_from_private_key(pw_doc["private_key"])
    master_kp = keypair_from_private_key(settings.master_wallet_private_key)
    mint_pubkey = Pubkey.from_string(xfee_mint)

    ancestors = tree_doc["ancestors"]
    direct_parent = ancestors[0] if ancestors else None

    ancestor_users: dict[str, dict] = {}
    async for user in users_col().find({"wallet_address": {"$in": ancestors}}):
        ancestor_users[user["wallet_address"]] = user

    highest_level_paid_so_far = 0
    total_distributed_raw = 0
    max_level_reached = False
    paid_commission_by_level: dict[int, int] = {}
    pending_allocs: list[dict] = []
    now = datetime.now(timezone.utc)

    def _raw_to_ui(raw: int) -> float:
        return raw / (10 ** xfee_decimals)

    def _raw_to_usd(raw: int) -> float:
        return _raw_to_ui(raw) * xfee_price_usd

    for ancestor_wallet in ancestors:
        ancestor_user = ancestor_users.get(ancestor_wallet)
        if not ancestor_user:
            continue

        ancestor_level = ancestor_user.get("level", 1)

        if max_level_reached or ancestor_level <= highest_level_paid_so_far:
            alloc_doc = {
                "_id": ObjectId(),
                "purchase_id": purchase_id,
                "recipient_wallet": ancestor_wallet,
                "sol_amount": 0.0,
                "xfee_amount_raw": 0,
                "xfee_amount_ui": 0.0,
                "sale_usd": sale_usd,
                "sale_sol": 0.0,
                "sale_tokens": sale_tokens,
                "alloc_type": "commission",
                "currency": "XFEE",
                "ancestor_level_tier": ancestor_level,
                "differential_rate": 0.0,
                "on_chain_tx": None,
                "status": "zero",
                "indexed": False,
                "level_indexed": False,
                "is_direct_sale": ancestor_wallet == direct_parent,
                "dir_indir_indexed": False,
                "created_at": now,
            }
            peer_commission_raw = paid_commission_by_level.get(ancestor_level)
            if settings.global_pool_enabled and peer_commission_raw and peer_commission_raw > 0:
                alloc_doc["global_pool_points_pending"] = True
                alloc_doc["global_pool_points_xfee_raw"] = peer_commission_raw
            pending_allocs.append(alloc_doc)
            continue

        differential_rate = get_rate_for_level(ancestor_level) - get_rate_for_level(highest_level_paid_so_far)
        commission_raw = int(commissionable_xfee_raw * differential_rate)
        if commission_raw > 0 and ancestor_level not in paid_commission_by_level:
            paid_commission_by_level[ancestor_level] = commission_raw

        try:
            sig = None
            status = "zero"

            if commission_raw > 0:
                sig = await transfer_spl_token(
                    pw_keypair,
                    Pubkey.from_string(ancestor_wallet),
                    mint_pubkey,
                    commission_raw,
                    xfee_decimals,
                    fee_payer_keypair=master_kp,
                )
                confirmed = await confirm_transaction(sig)
                status = "sent" if confirmed else "failed"

                if confirmed:
                    total_distributed_raw += commission_raw

                await transactions_col().insert_one({
                    "purchase_id": purchase_id,
                    "tx_type": "commission_xfee",
                    "from_wallet": purchase_wallet_pubkey,
                    "to_wallet": ancestor_wallet,
                    "amount_sol": 0.0,
                    "xfee_amount_raw": commission_raw,
                    "xfee_amount_ui": _raw_to_ui(commission_raw),
                    "tx_signature": sig,
                    "created_at": now,
                })

            pending_allocs.append({
                "_id": ObjectId(),
                "purchase_id": purchase_id,
                "recipient_wallet": ancestor_wallet,
                "sol_amount": 0.0,
                "xfee_amount_raw": commission_raw,
                "xfee_amount_ui": _raw_to_ui(commission_raw),
                "sale_usd": sale_usd,
                "sale_sol": 0.0,
                "sale_tokens": sale_tokens,
                "alloc_type": "commission",
                "currency": "XFEE",
                "ancestor_level_tier": ancestor_level,
                "differential_rate": differential_rate,
                "on_chain_tx": sig,
                "status": status,
                "indexed": False,
                "level_indexed": False,
                "is_direct_sale": ancestor_wallet == direct_parent,
                "dir_indir_indexed": False,
                "created_at": now,
            })

            highest_level_paid_so_far = ancestor_level
            logger.info(
                "Commission [XFEE]: %s raw (%.6f) to %s (L%s, diff_rate=%s)",
                commission_raw, _raw_to_ui(commission_raw),
                ancestor_wallet, ancestor_level, differential_rate,
            )

            if highest_level_paid_so_far >= MAX_COMMISSION_LEVEL:
                max_level_reached = True

        except Exception:
            logger.exception("Failed to send XFEE commission to %s", ancestor_wallet)
            pending_allocs.append({
                "_id": ObjectId(),
                "purchase_id": purchase_id,
                "recipient_wallet": ancestor_wallet,
                "sol_amount": 0.0,
                "xfee_amount_raw": commission_raw,
                "xfee_amount_ui": _raw_to_ui(commission_raw),
                "sale_usd": sale_usd,
                "sale_sol": 0.0,
                "sale_tokens": sale_tokens,
                "alloc_type": "commission",
                "currency": "XFEE",
                "ancestor_level_tier": ancestor_level,
                "differential_rate": differential_rate,
                "on_chain_tx": None,
                "status": "failed",
                "indexed": False,
                "level_indexed": False,
                "is_direct_sale": ancestor_wallet == direct_parent,
                "dir_indir_indexed": False,
                "created_at": now,
            })

    if pending_allocs:
        await allocs_col().insert_many(pending_allocs)
        for alloc in pending_allocs:
            if not alloc.get("global_pool_points_pending"):
                continue
            try:
                xfee_raw = int(alloc["global_pool_points_xfee_raw"])
                await record_missed_commission_points(
                    alloc_id=alloc["_id"],
                    wallet_address=alloc["recipient_wallet"],
                    purchase_id=purchase_id,
                    event_time=now,
                    points_usd=_raw_to_usd(xfee_raw),
                    currency="XFEE",
                    points_native=xfee_raw,
                )
            except Exception:
                logger.exception(
                    "Failed to record global pool points for alloc %s",
                    alloc["_id"],
                )

    commission_pool_raw = int(commissionable_xfee_raw * TOTAL_COMMISSION_RATE)
    logger.info(
        "Total commission distributed [XFEE]: %s raw (%.6f) out of pool %s raw",
        total_distributed_raw, _raw_to_ui(total_distributed_raw), commission_pool_raw,
    )
    return total_distributed_raw
