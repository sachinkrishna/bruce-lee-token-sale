import asyncio
import logging
from datetime import datetime, timezone

from app.database import purchase_wallets_col, purchases_col
from app.services.purchase_flow import process_completed_purchase
from app.services.solana_rpc import get_balance
from app.services.wallet_pool import release_wallet

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5


async def poll_purchase_wallet(
    purchase_id: str,
    pubkey: str,
    expected_sol: float,
    expires_at: datetime,
) -> None:
    """Poll a purchase wallet for incoming SOL until payment arrives or expiry."""
    try:
        min_sol = expected_sol * 0.95

        logger.info(
            f"Started polling {pubkey} for purchase {purchase_id} "
            f"(expected={expected_sol:.6f} SOL, min={min_sol:.6f} SOL, expires={expires_at.isoformat()})"
        )

        while datetime.now(timezone.utc) < expires_at:
            try:
                balance_lamports = await get_balance(pubkey)
                balance_sol = balance_lamports / 1e9

                if balance_sol >= min_sol:
                    logger.info(
                        f"Payment detected on {pubkey}: {balance_sol:.6f} SOL "
                        f"(purchase {purchase_id})"
                    )
                    await process_completed_purchase(purchase_id, balance_sol)
                    return

            except Exception:
                logger.exception(f"Error polling balance for {pubkey}")

            await asyncio.sleep(POLL_INTERVAL_SECONDS)

        logger.info(f"Purchase {purchase_id} expired, marking...")
        try:
            balance_lamports = await get_balance(pubkey)
            await purchase_wallets_col().update_one(
                {"public_key": pubkey},
                {"$set": {"remaining_balance_sol": balance_lamports / 1e9}},
            )
        except Exception:
            logger.exception(f"Failed to record remaining balance for {pubkey}")
        await mark_purchase_expired(purchase_id)
        await release_wallet(purchase_id)

    except asyncio.CancelledError:
        logger.info(f"Polling cancelled for purchase {purchase_id}")
        raise
    except Exception:
        logger.exception(f"Fatal error in poller for purchase {purchase_id}")


async def mark_purchase_expired(purchase_id: str) -> None:
    from bson import ObjectId

    await purchases_col().update_one(
        {"_id": ObjectId(purchase_id), "status": "pending"},
        {"$set": {"status": "expired"}},
    )
