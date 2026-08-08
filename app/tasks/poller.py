import asyncio
import logging
from datetime import datetime, timezone

from app.database import purchase_wallets_col, purchases_col
from app.services.purchase_flow import process_completed_purchase
from app.services.solana_rpc import get_balance, get_spl_balance_raw
from app.services.wallet_pool import release_wallet

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5


async def poll_purchase_wallet(
    purchase_id: str,
    pubkey: str,
    expected_sol: float,
    expires_at: datetime,
) -> None:
    """Poll a purchase wallet until payment arrives or expiry.

    The purchase doc holds `payment_mode` (snapshot at initiate time). In SOL
    mode we only check the SOL balance; in XFEE mode we check both the SOL
    gas buffer and the XFEE ATA balance and only trigger completion once both
    are present.
    """
    try:
        # Load the mode + expected amounts once — they're immutable for the
        # lifetime of a purchase (mode was snapshotted on initiate).
        from bson import ObjectId

        purchase = await purchases_col().find_one({"_id": ObjectId(purchase_id)})
        if not purchase:
            logger.error("Poller: purchase %s not found; aborting", purchase_id)
            return

        mode = purchase.get("payment_mode", "SOL")
        min_sol = expected_sol * 0.95

        xfee_expected_raw = int(purchase.get("xfee_amount_expected_raw") or 0)
        xfee_mint = purchase.get("xfee_mint") or ""
        xfee_tolerance = float(purchase.get("xfee_receive_tolerance") or 0.95)
        min_xfee = int(xfee_expected_raw * xfee_tolerance) if mode == "XFEE" else 0

        logger.info(
            "Poller started [mode=%s] wallet=%s expected_sol=%.6f min_sol=%.6f "
            "xfee_expected_raw=%s min_xfee_raw=%s expires=%s",
            mode, pubkey, expected_sol, min_sol,
            xfee_expected_raw or "-", min_xfee or "-", expires_at.isoformat(),
        )

        while datetime.now(timezone.utc) < expires_at:
            try:
                sol_lamports = await get_balance(pubkey)
                sol_balance = sol_lamports / 1e9

                if mode == "XFEE":
                    xfee_raw = await get_spl_balance_raw(pubkey, xfee_mint) if xfee_mint else 0
                    if sol_balance >= min_sol and xfee_raw >= min_xfee and min_xfee > 0:
                        logger.info(
                            "XFEE payment detected on %s: %.6f SOL + %s XFEE (raw)",
                            pubkey, sol_balance, xfee_raw,
                        )
                        await process_completed_purchase(
                            purchase_id,
                            sol_balance,
                            xfee_amount_raw=xfee_raw,
                        )
                        return
                else:
                    if sol_balance >= min_sol:
                        logger.info(
                            "SOL payment detected on %s: %.6f SOL", pubkey, sol_balance
                        )
                        await process_completed_purchase(purchase_id, sol_balance)
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
