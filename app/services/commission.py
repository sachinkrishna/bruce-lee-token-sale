import logging
from datetime import datetime, timezone

from bson import ObjectId
from solders.pubkey import Pubkey

from app.database import allocs_col, relationship_tree_col, transactions_col, users_col, purchase_wallets_col
from app.services.solana_rpc import transfer_sol, confirm_transaction
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
    pending_allocs = []
    now = datetime.now(timezone.utc)

    for ancestor_wallet in ancestors:
        ancestor_user = ancestor_users.get(ancestor_wallet)
        if not ancestor_user:
            continue

        ancestor_level = ancestor_user.get("level", 1)

        if max_level_reached or ancestor_level <= highest_level_paid_so_far:
            pending_allocs.append({
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
            })
            continue

        differential_rate = get_rate_for_level(ancestor_level) - get_rate_for_level(highest_level_paid_so_far)
        commission_sol = total_sol_received * differential_rate

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

    commission_pool_sol = total_sol_received * TOTAL_COMMISSION_RATE
    logger.info(f"Total commission distributed: {total_distributed:.6f} SOL out of pool {commission_pool_sol:.6f} SOL")
    return total_distributed
