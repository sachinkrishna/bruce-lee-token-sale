"""Per-purchase POWER entitlement — the shadow ledger.

Every `status="completed"` purchase gets a `power_entitlement` field recording
the total POWER that wallet is owed for that purchase under the current tiered
scheme:

    entitlement = xfee_amount × POWER_STAKE_MULTIPLIER
                  + (FOUNDER_TIER_BONUS_TABLE[xfee_amount] if founder_eligible)

The tier bonus is exact-match only (the frontend enforces the allowed purchase
sizes). Post-cap or non-founder-eligible purchases get flat 20× base. This
column is intentionally decoupled from the on-chain stake state — reconciling
what a wallet is entitled to versus what has actually been staked on-chain is
a separate operation handled elsewhere.

Live path: `_stamp_power_entitlement` is invoked from `process_completed_purchase`
after `maybe_mark_founder_eligible` so the founder flag is accurate.

Backfill: `ensure_power_entitlement_backfill` runs on startup and stamps every
completed purchase that hasn't been stamped yet, using the freshest
`founder_eligible` value. Idempotent via a marker in `system_meta`.

Force rerun: an admin endpoint clears the marker and calls this again. Safe
because the underlying update is a set-if-unset (with an optional
`force_recompute=True` to overwrite existing values).
"""
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from bson import ObjectId

from app.database import purchases_col, system_meta_col
from app.utils.economics import calculate_power_entitlement

logger = logging.getLogger(__name__)

POWER_ENTITLEMENT_BACKFILL_MARKER: str = "power_entitlement_backfill"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _entitlement_for_doc(purchase: dict) -> int:
    return calculate_power_entitlement(
        purchase.get("xfee_amount", 0),
        founder_eligible=bool(purchase.get("founder_eligible", False)),
    )


async def stamp_power_entitlement(purchase_id) -> Optional[int]:
    """Compute and persist `power_entitlement` for a single purchase.

    Idempotent — always writes the currently-correct value (so a purchase
    that transitions from non-founder to founder-eligible ends up with the
    higher value).
    """
    pid = purchase_id if isinstance(purchase_id, ObjectId) else ObjectId(str(purchase_id))
    purchase = await purchases_col().find_one({"_id": pid})
    if not purchase or purchase.get("status") != "completed":
        return None

    ent = _entitlement_for_doc(purchase)
    await purchases_col().update_one(
        {"_id": pid},
        {
            "$set": {
                "power_entitlement": ent,
                "power_entitlement_updated_at": _now(),
            }
        },
    )
    return ent


async def ensure_power_entitlement_backfill(*, force_recompute: bool = False) -> dict:
    """Stamp every completed purchase with `power_entitlement`.

    Runs on startup. Idempotent — a marker in `system_meta` short-circuits
    subsequent calls unless `force_recompute=True` (used by the admin
    rerun endpoint after clearing the marker).

    `force_recompute=True` recomputes and overwrites `power_entitlement` on
    every completed purchase regardless of whether the field is already set.
    Useful when the tier table or bonus rules change.
    """
    marker = await system_meta_col().find_one({"_id": POWER_ENTITLEMENT_BACKFILL_MARKER})
    if marker and marker.get("applied") and not force_recompute:
        return {
            "ran": False,
            "reason": "already_applied",
            "stamped_count": int(marker.get("stamped_count", 0)),
            "applied_at": marker.get("applied_at"),
        }

    match: Dict[str, Any] = {"status": "completed"}
    if not force_recompute:
        match["power_entitlement"] = {"$exists": False}

    now = _now()
    stamped_count = 0
    ops = []
    from pymongo import UpdateOne  # local import to avoid module-load cost when unused

    cursor = purchases_col().find(
        match,
        projection={
            "_id": 1,
            "xfee_amount": 1,
            "founder_eligible": 1,
            "power_entitlement": 1,
        },
    )
    async for p in cursor:
        ent = _entitlement_for_doc(p)
        if not force_recompute and p.get("power_entitlement") == ent:
            continue
        ops.append(
            UpdateOne(
                {"_id": p["_id"]},
                {
                    "$set": {
                        "power_entitlement": ent,
                        "power_entitlement_updated_at": now,
                    }
                },
            )
        )
        stamped_count += 1
        if len(ops) >= 500:
            await purchases_col().bulk_write(ops, ordered=False)
            ops = []

    if ops:
        await purchases_col().bulk_write(ops, ordered=False)

    await system_meta_col().update_one(
        {"_id": POWER_ENTITLEMENT_BACKFILL_MARKER},
        {
            "$set": {
                "applied": True,
                "applied_at": now,
                "stamped_count": int(stamped_count),
                "force_recompute": bool(force_recompute),
            }
        },
        upsert=True,
    )
    logger.info(
        "power-entitlement backfill: stamped=%s force_recompute=%s",
        stamped_count,
        force_recompute,
    )
    return {
        "ran": True,
        "stamped_count": stamped_count,
        "force_recompute": force_recompute,
        "applied_at": now,
    }


async def total_entitlement_for_wallet(wallet: str) -> int:
    """Sum of `power_entitlement` across a wallet's completed purchases."""
    pipeline = [
        {"$match": {"user_wallet": wallet, "status": "completed"}},
        {"$group": {"_id": None, "total": {"$sum": {"$ifNull": ["$power_entitlement", 0]}}}},
    ]
    async for row in purchases_col().aggregate(pipeline):
        return int(row.get("total", 0))
    return 0


async def total_entitlement_system_wide() -> int:
    """Sum of `power_entitlement` across every completed purchase in the system."""
    pipeline = [
        {"$match": {"status": "completed"}},
        {"$group": {"_id": None, "total": {"$sum": {"$ifNull": ["$power_entitlement", 0]}}}},
    ]
    async for row in purchases_col().aggregate(pipeline):
        return int(row.get("total", 0))
    return 0
