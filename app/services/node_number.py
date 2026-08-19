"""Sequential node numbering for users with at least one completed purchase.

Every wallet that has ever completed a purchase gets a `node_number` — a
monotonically increasing integer starting at 1, assigned at most once, never
changed thereafter.

Ordering rule: chronological by the wallet's *first* completed purchase
(`min(confirmed_at)`), with ties broken alphabetically by `wallet_address` so
the assignment is fully deterministic across replays.

State model
-----------
* `users.node_number` — sparse unique int on each user document. Set once and
  never unset.
* `users.node_number_assigned_at` — timestamp of assignment (audit only).
* `system_meta._id="node_number_state"` — atomic counter: `{next: N}` where
  `next` is the next value to hand out.
* `system_meta._id="node_number_backfill"` — completion marker for the
  chronological startup backfill.

Concurrency
-----------
`assign_node_number` uses a two-step commit:

  1. `find_one_and_update` on the counter to atomically consume a number.
  2. `update_one` on the user with `node_number: {"$exists": False}` filter
     so a lost race just gets discarded without stomping an existing value.

Under a lost race we intentionally "burn" the number (leaves a small gap).
Gaps don't break monotonicity or uniqueness; races on the same wallet are
extremely rare (the hook fires exactly once per purchase completion inside
`process_completed_purchase`, which is itself serialized per purchase).
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from pymongo import ReturnDocument

from app.database import purchases_col, system_meta_col, users_col

logger = logging.getLogger(__name__)

NODE_NUMBER_STATE_ID: str = "node_number_state"
NODE_NUMBER_BACKFILL_ID: str = "node_number_backfill"


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _consume_next_number() -> int:
    """Atomically increment the counter and return the newly assigned value."""
    state = await system_meta_col().find_one_and_update(
        {"_id": NODE_NUMBER_STATE_ID},
        {
            "$inc": {"next": 1},
            "$set": {"updated_at": _now()},
            "$setOnInsert": {"created_at": _now()},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return int(state["next"])


async def _has_completed_purchase(wallet: str) -> bool:
    doc = await purchases_col().find_one(
        {"user_wallet": wallet, "status": "completed"},
        projection={"_id": 1},
    )
    return doc is not None


async def get_node_number(wallet: str) -> Optional[int]:
    """Read-only lookup: returns the node number for a wallet or None."""
    u = await users_col().find_one(
        {"wallet_address": wallet},
        projection={"node_number": 1},
    )
    if not u:
        return None
    n = u.get("node_number")
    return int(n) if isinstance(n, int) else None


async def assign_node_number(wallet: str) -> Optional[int]:
    """Assign a node number to `wallet` if eligible and not already assigned.

    Idempotent: returns the existing number if the wallet is already assigned.
    Returns None if the wallet has no completed purchase yet (defer) or if
    the user document doesn't exist.
    """
    user = await users_col().find_one(
        {"wallet_address": wallet},
        projection={"node_number": 1},
    )
    if not user:
        return None
    existing = user.get("node_number")
    if isinstance(existing, int):
        return existing

    if not await _has_completed_purchase(wallet):
        return None

    n = await _consume_next_number()
    result = await users_col().update_one(
        {"wallet_address": wallet, "node_number": {"$exists": False}},
        {"$set": {"node_number": n, "node_number_assigned_at": _now()}},
    )
    if result.modified_count == 1:
        logger.info("Assigned node_number=%s to %s", n, wallet)
        return n

    # Race: someone else assigned in parallel. The number `n` we consumed is
    # now a gap in the sequence. Return whatever ended up on the doc.
    fresh = await users_col().find_one(
        {"wallet_address": wallet},
        projection={"node_number": 1},
    )
    if fresh and isinstance(fresh.get("node_number"), int):
        return int(fresh["node_number"])
    return None


async def ensure_node_number_backfill() -> dict:
    """One-shot chronological backfill for all existing eligible users.

    Idempotent — a marker in `system_meta` prevents re-application on
    redeploys. Safe to invoke on every startup.
    """
    marker = await system_meta_col().find_one({"_id": NODE_NUMBER_BACKFILL_ID})
    if marker and marker.get("applied"):
        return {
            "ran": False,
            "reason": "already_applied",
            "assigned_count": int(marker.get("assigned_count", 0)),
            "applied_at": marker.get("applied_at"),
        }

    # Seed the counter to max(existing) so any partial state from a prior run
    # (crash mid-backfill, hand-edits, etc.) doesn't produce duplicates.
    max_existing = 0
    async for u in users_col().find(
        {"node_number": {"$exists": True, "$type": "number"}},
        projection={"node_number": 1},
    ).sort("node_number", -1).limit(1):
        max_existing = int(u["node_number"])

    await system_meta_col().update_one(
        {"_id": NODE_NUMBER_STATE_ID},
        {
            "$max": {"next": max_existing},
            "$set": {"updated_at": _now()},
            "$setOnInsert": {"created_at": _now()},
        },
        upsert=True,
    )

    pipeline = [
        {"$match": {"status": "completed", "confirmed_at": {"$ne": None}}},
        {
            "$group": {
                "_id": "$user_wallet",
                "first_confirmed_at": {"$min": "$confirmed_at"},
            }
        },
        {
            "$lookup": {
                "from": users_col().name,
                "localField": "_id",
                "foreignField": "wallet_address",
                "as": "user",
                "pipeline": [{"$project": {"node_number": 1}}],
            }
        },
        {
            "$match": {
                "$or": [
                    {"user": {"$size": 0}},
                    {"user.node_number": {"$exists": False}},
                ]
            }
        },
        {"$sort": {"first_confirmed_at": 1, "_id": 1}},
    ]

    assigned_count = 0
    async for row in purchases_col().aggregate(pipeline):
        wallet = row["_id"]
        first_confirmed_at = row.get("first_confirmed_at")

        # `user` array is empty when there's no matching user doc (shouldn't
        # normally happen — a completed purchase implies a registered user,
        # but be defensive).
        user_arr = row.get("user") or []
        if user_arr and isinstance(user_arr[0].get("node_number"), int):
            continue

        n = await _consume_next_number()
        result = await users_col().update_one(
            {"wallet_address": wallet, "node_number": {"$exists": False}},
            {
                "$set": {
                    "node_number": n,
                    "node_number_assigned_at": _now(),
                }
            },
        )
        if result.modified_count == 1:
            assigned_count += 1
            logger.info(
                "Backfill: node_number=%s → %s (first_completed=%s)",
                n,
                wallet,
                first_confirmed_at.isoformat() if first_confirmed_at else "?",
            )

    now = _now()
    await system_meta_col().update_one(
        {"_id": NODE_NUMBER_BACKFILL_ID},
        {
            "$set": {
                "applied": True,
                "applied_at": now,
                "assigned_count": int(assigned_count),
            }
        },
        upsert=True,
    )
    logger.info(
        "Node-number backfill: assigned %s new number(s); counter next=%s",
        assigned_count,
        (await system_meta_col().find_one({"_id": NODE_NUMBER_STATE_ID}) or {}).get(
            "next"
        ),
    )
    return {
        "ran": True,
        "assigned_count": assigned_count,
        "applied_at": now,
    }
