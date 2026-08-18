"""Founder eligibility — early-buyer marker capped at total USD sales.

Every completed purchase whose USD value is at least the minimum threshold
is a candidate to be marked `founder_eligible`. Marking is atomic against a
single `system_meta._id="founder_state"` document that tracks cumulative USD
progress toward the cap. Once cumulative reaches the cap, no further purchases
are marked; the pool is closed.

Semantics:
  * Cap is a soft floor — the purchase that crosses the cap is included in
    full (its USD "over-runs" the cap by up to one purchase's value).
  * Once granted, founder status is monotonic — nothing ever unsets it, even
    if a purchase were later invalidated.
  * A user is `founder=true` iff at least one of their purchases is
    `founder_eligible=true`.

The live path (`maybe_mark_founder_eligible`) uses a `find_one_and_update`
with an `$expr` gate for concurrency safety. The startup backfill processes
existing completed purchases chronologically by `confirmed_at`.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from pymongo import ReturnDocument

from app.config import settings
from app.database import purchases_col, system_meta_col, users_col

logger = logging.getLogger(__name__)

FOUNDER_ELIGIBLE_CAP_USD: float = 1_000_000.0
FOUNDER_ELIGIBLE_MIN_PURCHASE_USD: float = 100.0
FOUNDER_STATE_ID: str = "founder_state"


async def _ensure_state_doc() -> dict:
    """Create the founder state document if missing. Returns the current doc."""
    existing = await system_meta_col().find_one({"_id": FOUNDER_STATE_ID})
    if existing is not None:
        return existing

    doc = {
        "_id": FOUNDER_STATE_ID,
        "cap_usd": FOUNDER_ELIGIBLE_CAP_USD,
        "min_purchase_usd": FOUNDER_ELIGIBLE_MIN_PURCHASE_USD,
        "cumulative_usd": 0.0,
        "founder_count": 0,
        "closed_at": None,
        "backfill_completed_at": None,
        "last_purchase_at": None,
        "created_at": datetime.now(timezone.utc),
    }
    try:
        await system_meta_col().insert_one(doc)
    except Exception:
        # Racing init — another worker inserted first. Re-read.
        existing = await system_meta_col().find_one({"_id": FOUNDER_STATE_ID})
        if existing is not None:
            return existing
        raise
    return doc


async def get_founder_state() -> dict:
    """Fetch the current founder-state doc, initializing if needed."""
    return await _ensure_state_doc()


async def maybe_mark_founder_eligible(purchase: dict) -> bool:
    """Mark this completed purchase founder-eligible if the cap isn't exhausted.

    Idempotent: safe to call multiple times on the same purchase.

    Returns True iff the purchase ends up marked founder_eligible (either now
    or from a prior call). Returns False if the purchase was skipped due to
    the min-purchase floor or cap exhaustion.
    """
    if purchase.get("status") != "completed":
        return False

    sale_usd = float(purchase.get("xfee_amount") or 0)
    if sale_usd < FOUNDER_ELIGIBLE_MIN_PURCHASE_USD:
        return False

    if purchase.get("founder_eligible"):
        return True

    await _ensure_state_doc()

    now = datetime.now(timezone.utc)

    # Atomic check-and-increment. The `$expr` gate ensures we only take the
    # slot if the cap wasn't already reached when this update was applied.
    state = await system_meta_col().find_one_and_update(
        {
            "_id": FOUNDER_STATE_ID,
            "$expr": {"$lt": ["$cumulative_usd", "$cap_usd"]},
        },
        {
            "$inc": {"cumulative_usd": sale_usd},
            "$set": {"last_purchase_at": now},
        },
        return_document=ReturnDocument.AFTER,
    )
    if state is None:
        logger.info(
            "Founder cap already reached; purchase %s (%.2f USD) not marked",
            purchase.get("_id"),
            sale_usd,
        )
        return False

    # If this increment tipped us at/over the cap, record the close time
    # exactly once.
    if state["cumulative_usd"] >= state["cap_usd"]:
        await system_meta_col().update_one(
            {"_id": FOUNDER_STATE_ID, "closed_at": None},
            {"$set": {"closed_at": now}},
        )

    # Mark the purchase. The filter ensures idempotency — if another worker
    # marked it in parallel, `modified_count` will be 0 and we roll back the
    # increment we just applied. We also stamp `founder_onchain_status=pending`
    # so the founder-onchain worker will pick it up (either now, if enabled,
    # or later when the kill switch is flipped on).
    result = await purchases_col().update_one(
        {"_id": purchase["_id"], "founder_eligible": {"$ne": True}},
        {
            "$set": {
                "founder_eligible": True,
                "founder_eligible_at": now,
                "founder_onchain_status": "pending",
            }
        },
    )
    if result.modified_count == 0:
        # Race: someone else already claimed this purchase. Roll back the USD
        # we just added so the counter doesn't double-count.
        await system_meta_col().update_one(
            {"_id": FOUNDER_STATE_ID},
            {"$inc": {"cumulative_usd": -sale_usd}},
        )
        return True

    # Flip the user to founder if this is their first eligible purchase.
    # Filter ensures we only increment `founder_count` once per user.
    user_res = await users_col().update_one(
        {
            "wallet_address": purchase["user_wallet"],
            "founder": {"$ne": True},
        },
        {
            "$set": {
                "founder": True,
                "founder_since": purchase.get("confirmed_at") or now,
            }
        },
    )
    if user_res.modified_count == 1:
        await system_meta_col().update_one(
            {"_id": FOUNDER_STATE_ID},
            {"$inc": {"founder_count": 1}},
        )

    logger.info(
        "Marked purchase %s founder_eligible (%.2f USD; cumulative=%.2f/%.0f)",
        purchase.get("_id"),
        sale_usd,
        state["cumulative_usd"],
        state["cap_usd"],
    )

    if settings.founder_onchain_enabled:
        try:
            from app.services.founder_onchain import try_write_founder_power_onchain

            asyncio.create_task(try_write_founder_power_onchain(str(purchase["_id"])))
        except Exception:
            logger.exception(
                "Failed to schedule founder-onchain write for purchase %s",
                purchase.get("_id"),
            )

    return True


async def ensure_founder_backfill() -> dict:
    """One-shot backfill of existing completed purchases in chronological order.

    Runs during lifespan startup. Idempotent: a `backfill_completed_at` marker
    on the state doc prevents re-application on redeploys.

    Returns a summary of what happened.
    """
    state = await _ensure_state_doc()

    if state.get("backfill_completed_at"):
        return {
            "ran": False,
            "reason": "already_backfilled",
            "cumulative_usd": float(state.get("cumulative_usd", 0.0)),
            "founder_count": int(state.get("founder_count", 0)),
            "closed_at": state.get("closed_at"),
        }

    cap = float(state.get("cap_usd", FOUNDER_ELIGIBLE_CAP_USD))
    cumulative = float(state.get("cumulative_usd", 0.0))
    min_usd = float(state.get("min_purchase_usd", FOUNDER_ELIGIBLE_MIN_PURCHASE_USD))
    now = datetime.now(timezone.utc)

    purchases_marked = 0
    new_founders = 0

    if cumulative < cap:
        cursor = (
            purchases_col()
            .find(
                {
                    "status": "completed",
                    "xfee_amount": {"$gte": min_usd},
                    "founder_eligible": {"$ne": True},
                    "confirmed_at": {"$ne": None},
                }
            )
            .sort("confirmed_at", 1)
        )

        async for p in cursor:
            if cumulative >= cap:
                break
            sale_usd = float(p["xfee_amount"])
            cumulative += sale_usd

            await purchases_col().update_one(
                {"_id": p["_id"]},
                {
                    "$set": {
                        "founder_eligible": True,
                        "founder_eligible_at": now,
                        "founder_onchain_status": "pending",
                    }
                },
            )
            purchases_marked += 1

            user_res = await users_col().update_one(
                {
                    "wallet_address": p["user_wallet"],
                    "founder": {"$ne": True},
                },
                {
                    "$set": {
                        "founder": True,
                        "founder_since": p.get("confirmed_at") or now,
                    }
                },
            )
            if user_res.modified_count == 1:
                new_founders += 1

    closed_at: Optional[datetime] = now if cumulative >= cap else None
    await system_meta_col().update_one(
        {"_id": FOUNDER_STATE_ID},
        {
            "$set": {
                "cumulative_usd": cumulative,
                "backfill_completed_at": now,
                "closed_at": closed_at,
            },
            "$inc": {"founder_count": new_founders},
        },
    )

    logger.info(
        "Founder backfill: marked=%s new_founders=%s cumulative=%.2f/%.0f closed=%s",
        purchases_marked,
        new_founders,
        cumulative,
        cap,
        bool(closed_at),
    )
    return {
        "ran": True,
        "purchases_marked": purchases_marked,
        "new_founders": new_founders,
        "cumulative_usd": cumulative,
        "cap_usd": cap,
        "closed_at": closed_at,
    }
