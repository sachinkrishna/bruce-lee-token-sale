import logging
from datetime import datetime, timezone
from typing import Optional

from app.database import purchase_wallets_col
from app.utils.keypair import generate_keypair

logger = logging.getLogger(__name__)

POOL_MIN_FREE = 20
POOL_GENERATE_BATCH = 50


async def ensure_wallet_pool() -> None:
    col = purchase_wallets_col()
    free_count = await col.count_documents({"status": "free"})
    if free_count >= POOL_MIN_FREE:
        logger.info(f"Wallet pool OK: {free_count} free wallets")
        return

    to_generate = POOL_GENERATE_BATCH - free_count
    logger.info(f"Generating {to_generate} purchase wallets (current free: {free_count})")

    wallets = []
    for _ in range(to_generate):
        pub, priv = generate_keypair()
        wallets.append(
            {
                "public_key": pub,
                "private_key": priv,
                "assigned_to_purchase": None,
                "status": "free",
                "created_at": datetime.now(timezone.utc),
            }
        )

    if wallets:
        await col.insert_many(wallets)
        logger.info(f"Generated {len(wallets)} purchase wallets")


async def lock_wallet(purchase_id) -> Optional[dict]:
    col = purchase_wallets_col()
    result = await col.find_one_and_update(
        {"status": "free"},
        {"$set": {"status": "locked", "assigned_to_purchase": purchase_id}},
        return_document=True,
    )
    return result


async def mark_wallet_used(purchase_id) -> None:
    col = purchase_wallets_col()
    await col.update_one(
        {"assigned_to_purchase": purchase_id},
        {"$set": {"status": "used"}},
    )


async def release_wallet(purchase_id) -> None:
    """Mark a locked wallet as used (wallets are never reused)."""
    await mark_wallet_used(purchase_id)


async def get_free_wallet_count() -> int:
    return await purchase_wallets_col().count_documents({"status": "free"})
