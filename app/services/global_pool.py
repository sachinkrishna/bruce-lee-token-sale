"""
Global Pool — direct auto-settlement from a funding wallet, split by currency.

Two independent payout tracks share one accrual timeline:

  * SOL track — funded by the funding wallet's SOL balance. Points accrue in
    USD terms whenever a SOL-mode purchase would have paid an ancestor a
    non-zero commission that got zero-alloc'd by the differential rules.
  * XFEE track — funded by the funding wallet's XFEE ATA balance. Points
    accrue in USD terms whenever the same happens for an XFEE-mode purchase.

At finalization:
  1. Freeze pool, snapshot BOTH funding balances (SOL + XFEE), and compute
     per-user owed amounts on each side proportional to their per-currency
     point share.
  2. Acquire a wallet-level lock so only one pool settles from the funding
     wallet at a time.
  3. For each user row: run the SOL payout state machine, then the XFEE
     payout state machine. Both use SPL-memo-based idempotency.
  4. Mark pool settled once BOTH sides finish for every user.

Idempotency keys:
  - DB unique index (pool_id, wallet_address) ensures one row per (pool, user).
  - On-chain memos "GP:{pool_index}:{sid}:{prefix}" (SOL) and
    "GP-X:{pool_index}:{sid}:{prefix}" (XFEE) survive backend restarts —
    any historical payout from the funding wallet is recoverable by scanning
    the funding wallet's recent signatures for those memos.
"""
import asyncio
import base58
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from bson import ObjectId
from pymongo.errors import DuplicateKeyError
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from app.config import settings
from app.database import allocs_col, global_pools_col, pool_points_col, system_meta_col
from app.services.solana_rpc import (
    confirm_transaction,
    find_signature_by_memo,
    find_spl_signature_by_memo,
    get_balance,
    get_spl_balance_raw,
    transfer_sol_with_memo,
    transfer_spl_token,
)

logger = logging.getLogger(__name__)

LAMPORTS_PER_SOL = 1_000_000_000
WORKER_ID = f"worker-{uuid.uuid4().hex[:8]}"

# Backfill marker: applied once at startup to fold pre-bifurcation points into
# the SOL bucket (all historical accrual was SOL-only).
BUCKET_BACKFILL_MARKER = "pool_points_bucket_backfill_v1"


# ── Time + config helpers ─────────────────────────────────────────────────────

def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _duration() -> timedelta:
    return timedelta(days=settings.global_pool_duration_days)


def _funding_keypair() -> Keypair:
    if not settings.global_pool_funding_wallet_private_key:
        raise RuntimeError("GLOBAL_POOL_FUNDING_WALLET_PRIVATE_KEY is not configured")
    return Keypair.from_bytes(base58.b58decode(settings.global_pool_funding_wallet_private_key))


def _funding_pubkey_str() -> str:
    return str(_funding_keypair().pubkey())


def _memo_for(pool_index: int, settlement_id: str, wallet: str, *, side: str = "SOL") -> str:
    """Compact on-chain memo, well under the 566-byte SPL memo cap.

    Side is encoded via the prefix so a single memo scan can distinguish
    SOL from XFEE payouts on the same funding wallet.
    """
    prefix = "GP" if side == "SOL" else "GP-X"
    return f"{prefix}:{pool_index}:{settlement_id}:{wallet[:8]}"


def _xfee_mint() -> str:
    return settings.xfee_payment_token_mint


def _xfee_decimals() -> int:
    return int(settings.xfee_payment_token_decimals or 9)


async def sum_master_commissions_in_window(start_at: datetime, end_at: datetime) -> float:
    """Sum master's `sent` SOL commission allocs (SOL amount) within [start, end).

    Used only by /global-pool/funds to give a window-scoped SOL collection
    figure. XFEE-mode commissions live in a different currency and aren't
    summed here.
    """
    if not settings.master_wallet_address:
        return 0.0
    pipeline = [
        {
            "$match": {
                "recipient_wallet": settings.master_wallet_address,
                "alloc_type": "commission",
                "status": "sent",
                "$or": [
                    {"currency": {"$exists": False}},
                    {"currency": "SOL"},
                ],
                "created_at": {"$gte": start_at, "$lt": end_at},
            }
        },
        {"$group": {"_id": None, "total_sol": {"$sum": "$sol_amount"}}},
    ]
    async for doc in allocs_col().aggregate(pipeline):
        return float(doc.get("total_sol") or 0.0)
    return 0.0


async def sum_master_commissions_xfee_in_window(start_at: datetime, end_at: datetime) -> int:
    """Sum master's `sent` XFEE commission allocs (raw amount) within [start, end)."""
    if not settings.master_wallet_address:
        return 0
    pipeline = [
        {
            "$match": {
                "recipient_wallet": settings.master_wallet_address,
                "alloc_type": "commission",
                "status": "sent",
                "currency": "XFEE",
                "created_at": {"$gte": start_at, "$lt": end_at},
            }
        },
        {"$group": {"_id": None, "total_raw": {"$sum": "$xfee_amount_raw"}}},
    ]
    async for doc in allocs_col().aggregate(pipeline):
        return int(doc.get("total_raw") or 0)
    return 0


# ── One-time migration: fold legacy points_usd into SOL bucket ────────────────

async def ensure_pool_points_bucket_backfill() -> None:
    """Idempotently fold pre-bifurcation `points_usd` into `points_usd_sol`.

    All historical accrual was SOL-only, so we can safely mirror the
    existing per-user and per-pool totals into the new SOL bucket.
    """
    marker = await system_meta_col().find_one({"_id": BUCKET_BACKFILL_MARKER})
    if marker and marker.get("applied"):
        return

    rows_updated = 0
    async for row in pool_points_col().find({"points_usd_sol": {"$exists": False}}):
        pts = float(row.get("points_usd", 0.0) or 0.0)
        await pool_points_col().update_one(
            {"_id": row["_id"]},
            {"$set": {"points_usd_sol": pts, "points_usd_xfee": 0.0}},
        )
        rows_updated += 1

    pools_updated = 0
    async for pool in global_pools_col().find({"total_points_usd_sol": {"$exists": False}}):
        tot = float(pool.get("total_points_usd", 0.0) or 0.0)
        await global_pools_col().update_one(
            {"_id": pool["_id"]},
            {"$set": {"total_points_usd_sol": tot, "total_points_usd_xfee": 0.0}},
        )
        pools_updated += 1

    await system_meta_col().update_one(
        {"_id": BUCKET_BACKFILL_MARKER},
        {
            "$set": {
                "applied": True,
                "applied_at": datetime.now(timezone.utc),
                "rows_updated": rows_updated,
                "pools_updated": pools_updated,
            }
        },
        upsert=True,
    )
    logger.info(
        "Pool-points bucket backfill applied: %s rows, %s pools",
        rows_updated, pools_updated,
    )


# ── Pool lifecycle (window management) ────────────────────────────────────────

async def resolve_active_pool(event_time: datetime) -> Optional[dict]:
    if not settings.global_pool_enabled:
        return None

    event_time = _utc(event_time)
    existing = await global_pools_col().find_one(
        {"start_at": {"$lte": event_time}, "end_at": {"$gt": event_time}}
    )
    if existing:
        return existing

    last = await global_pools_col().find_one(sort=[("pool_index", -1)])
    if not last:
        doc = _new_pool_doc(pool_index=1, start_at=event_time)
        try:
            result = await global_pools_col().insert_one(doc)
            doc["_id"] = result.inserted_id
            return doc
        except DuplicateKeyError:
            return await global_pools_col().find_one({"pool_index": 1})

    last_start = _utc(last["start_at"])
    last_end = _utc(last["end_at"])
    if event_time < last_start:
        return last

    if event_time >= last_end:
        windows_after_last = int((event_time - last_end) // _duration())
        start_at = last_end + (windows_after_last * _duration())
        pool_index = int(last["pool_index"]) + windows_after_last + 1
    else:
        start_at = last_start
        pool_index = int(last["pool_index"])

    doc = _new_pool_doc(pool_index=pool_index, start_at=start_at)
    try:
        result = await global_pools_col().insert_one(doc)
        doc["_id"] = result.inserted_id
        return doc
    except DuplicateKeyError:
        return await global_pools_col().find_one({"pool_index": pool_index})


async def ensure_next_pool(ended_pool: dict) -> dict:
    next_index = int(ended_pool["pool_index"]) + 1
    existing = await global_pools_col().find_one({"pool_index": next_index})
    if existing:
        return existing

    start_at = _utc(ended_pool["end_at"])
    doc = _new_pool_doc(pool_index=next_index, start_at=start_at)
    try:
        result = await global_pools_col().insert_one(doc)
        doc["_id"] = result.inserted_id
        logger.info("Started global pool %s at %s", next_index, start_at.isoformat())
        return doc
    except DuplicateKeyError:
        return await global_pools_col().find_one({"pool_index": next_index})


def _new_pool_doc(*, pool_index: int, start_at: datetime) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "pool_index": pool_index,
        "start_at": start_at,
        "end_at": start_at + _duration(),
        "status": "active",
        "total_points_usd": 0.0,
        "total_points_usd_sol": 0.0,
        "total_points_usd_xfee": 0.0,
        "created_at": now,
        "updated_at": now,
    }


# ── Point accrual ─────────────────────────────────────────────────────────────

async def record_missed_commission_points(
    *,
    alloc_id: ObjectId,
    wallet_address: str,
    purchase_id: ObjectId,
    event_time: datetime,
    points_usd: float,
    currency: str = "SOL",
    points_native: Optional[float] = None,
) -> Optional[dict]:
    """Attribute missed-commission USD-value points to a user in the active pool.

    `currency` selects which bucket to increment. Both SOL and XFEE origins
    push into the same pool row so a user with mixed history gets a single
    payout entry with two side-labeled owed amounts at settlement time.
    """
    if not settings.global_pool_enabled or points_usd <= 0:
        return None
    if currency not in ("SOL", "XFEE"):
        raise ValueError(f"currency must be 'SOL' or 'XFEE', got {currency!r}")

    pool = await resolve_active_pool(event_time)
    if not pool:
        return None
    if pool.get("status") != "active":
        return None

    now = datetime.now(timezone.utc)
    alloc_set = {
        "global_pool_points_recorded": True,
        "global_pool_points_usd": points_usd,
        "global_pool_points_currency": currency,
        "global_pool_index": pool["pool_index"],
        "global_pool_id": pool["_id"],
    }
    if points_native is not None:
        # Preserves currency-native amount alongside USD (e.g. SOL amount for
        # SOL-mode, raw XFEE for XFEE-mode) for audit / breakdown endpoints.
        if currency == "SOL":
            alloc_set["global_pool_points_sol"] = float(points_native)
        else:
            alloc_set["global_pool_points_xfee_raw"] = int(points_native)
    alloc_update = await allocs_col().update_one(
        {"_id": alloc_id, "global_pool_points_recorded": {"$ne": True}},
        {"$set": alloc_set},
    )
    if alloc_update.modified_count == 0:
        return None

    inc_fields = {"points_usd": points_usd, "event_count": 1}
    if currency == "SOL":
        inc_fields["points_usd_sol"] = points_usd
    else:
        inc_fields["points_usd_xfee"] = points_usd

    await pool_points_col().update_one(
        {"pool_id": pool["_id"], "wallet_address": wallet_address},
        {
            "$setOnInsert": {
                "pool_id": pool["_id"],
                "pool_index": pool["pool_index"],
                "wallet_address": wallet_address,
                "settle_status": "pending",
                "xfee_settle_status": "pending",
                "created_at": now,
            },
            "$inc": inc_fields,
            "$set": {"updated_at": now},
            "$addToSet": {"alloc_ids": alloc_id, "purchase_ids": purchase_id},
        },
        upsert=True,
    )

    pool_inc = {"total_points_usd": points_usd}
    if currency == "SOL":
        pool_inc["total_points_usd_sol"] = points_usd
    else:
        pool_inc["total_points_usd_xfee"] = points_usd
    await global_pools_col().update_one(
        {"_id": pool["_id"]},
        {"$inc": pool_inc, "$set": {"updated_at": now}},
    )
    return {"pool_index": pool["pool_index"], "points_usd": points_usd, "currency": currency}


# ── Standings / queries ───────────────────────────────────────────────────────

async def get_pool_standings(
    pool_index: int,
    limit: int = 100,
    skip: int = 0,
) -> list[dict]:
    cursor = (
        pool_points_col()
        .find({"pool_index": pool_index})
        .sort("points_usd", -1)
        .skip(skip)
        .limit(limit)
    )
    standings = []
    async for row in cursor:
        row["id"] = str(row.pop("_id"))
        row["pool_id"] = str(row["pool_id"])
        row["alloc_ids"] = [str(v) for v in row.get("alloc_ids", [])]
        row["purchase_ids"] = [str(v) for v in row.get("purchase_ids", [])]
        standings.append(row)
    return standings


async def get_pool_user_count(pool_index: int) -> int:
    return await pool_points_col().count_documents({"pool_index": pool_index})


async def list_pools(
    *,
    status: Optional[str] = None,
    page: int = 1,
    limit: int = 20,
) -> dict:
    query: dict[str, Any] = {}
    if status:
        query["status"] = status
    skip = (page - 1) * limit
    cursor = (
        global_pools_col()
        .find(query)
        .sort("pool_index", -1)
        .skip(skip)
        .limit(limit)
    )
    items = [row async for row in cursor]
    for row in items:
        row["user_count"] = await pool_points_col().count_documents({"pool_id": row["_id"]})
        sol_collected = await sum_master_commissions_in_window(row["start_at"], row["end_at"])
        row["sol_collected"] = sol_collected
        row["sol_collected_lamports"] = int(sol_collected * LAMPORTS_PER_SOL)
        xfee_collected_raw = await sum_master_commissions_xfee_in_window(row["start_at"], row["end_at"])
        row["xfee_collected_raw"] = xfee_collected_raw
        row["xfee_collected_ui"] = xfee_collected_raw / (10 ** _xfee_decimals())
        snapshot = row.get("snapshot") or {}
        if "distributable_lamports" in snapshot:
            row["distributable_lamports"] = int(snapshot.get("distributable_lamports") or 0)
            row["distributable_sol"] = row["distributable_lamports"] / LAMPORTS_PER_SOL
        if "distributable_xfee_raw" in snapshot:
            row["distributable_xfee_raw"] = int(snapshot.get("distributable_xfee_raw") or 0)
            row["distributable_xfee_ui"] = row["distributable_xfee_raw"] / (10 ** _xfee_decimals())
    total = await global_pools_col().count_documents(query)
    return {"items": items, "total": total, "page": page, "limit": limit}


async def get_user_pool_entry(wallet_address: str, pool_index: int) -> Optional[dict]:
    return await pool_points_col().find_one(
        {"wallet_address": wallet_address, "pool_index": pool_index}
    )


async def get_user_pool_history(
    wallet_address: str,
    *,
    page: int = 1,
    limit: int = 20,
) -> dict:
    skip = (page - 1) * limit
    cursor = (
        pool_points_col()
        .find({"wallet_address": wallet_address})
        .sort("pool_index", -1)
        .skip(skip)
        .limit(limit)
    )
    items = [row async for row in cursor]
    total = await pool_points_col().count_documents({"wallet_address": wallet_address})
    return {"items": items, "total": total, "page": page, "limit": limit}


async def get_current_pool() -> Optional[dict]:
    now = datetime.now(timezone.utc)
    pool = await global_pools_col().find_one(
        {"start_at": {"$lte": now}, "end_at": {"$gt": now}}
    )
    if pool:
        pool["id"] = str(pool.pop("_id"))
    return pool


async def get_global_pool_summary() -> dict:
    now = datetime.now(timezone.utc)
    total_pools = await global_pools_col().count_documents({})
    settled = await global_pools_col().count_documents({"status": "settled"})
    active = await global_pools_col().count_documents({"status": "active"})
    in_progress = await global_pools_col().count_documents(
        {"status": {"$in": ["settling", "ready_to_settle"]}}
    )

    points_pipeline = [
        {
            "$group": {
                "_id": None,
                "total_points_usd": {"$sum": "$total_points_usd"},
                "total_points_usd_sol": {"$sum": "$total_points_usd_sol"},
                "total_points_usd_xfee": {"$sum": "$total_points_usd_xfee"},
            }
        }
    ]
    total_points_usd = 0.0
    total_points_usd_sol = 0.0
    total_points_usd_xfee = 0.0
    async for doc in global_pools_col().aggregate(points_pipeline):
        total_points_usd = float(doc.get("total_points_usd") or 0.0)
        total_points_usd_sol = float(doc.get("total_points_usd_sol") or 0.0)
        total_points_usd_xfee = float(doc.get("total_points_usd_xfee") or 0.0)

    # SOL side owed by settle_status
    sol_pipeline = [
        {"$match": {"owed_lamports": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$settle_status",
                "total_owed_lamports": {"$sum": "$owed_lamports"},
                "count": {"$sum": 1},
            }
        },
    ]
    sol_by_status: dict[str, dict[str, int]] = {}
    async for doc in pool_points_col().aggregate(sol_pipeline):
        sol_by_status[doc["_id"]] = {
            "total_owed_lamports": int(doc.get("total_owed_lamports") or 0),
            "count": int(doc.get("count") or 0),
        }

    xfee_pipeline = [
        {"$match": {"owed_xfee_raw": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$xfee_settle_status",
                "total_owed_xfee_raw": {"$sum": "$owed_xfee_raw"},
                "count": {"$sum": 1},
            }
        },
    ]
    xfee_by_status: dict[str, dict[str, int]] = {}
    async for doc in pool_points_col().aggregate(xfee_pipeline):
        xfee_by_status[doc["_id"]] = {
            "total_owed_xfee_raw": int(doc.get("total_owed_xfee_raw") or 0),
            "count": int(doc.get("count") or 0),
        }

    current_pool = await global_pools_col().find_one(
        {"start_at": {"$lte": now}, "end_at": {"$gt": now}}
    )
    return {
        "total_pools": total_pools,
        "active": active,
        "in_progress": in_progress,
        "settled": settled,
        "total_points_usd_all_pools": total_points_usd,
        "total_points_usd_sol_all_pools": total_points_usd_sol,
        "total_points_usd_xfee_all_pools": total_points_usd_xfee,
        "settlement_counts_sol": sol_by_status,
        "settlement_counts_xfee": xfee_by_status,
        "current_pool_index": int(current_pool["pool_index"]) if current_pool else None,
    }


# ── Settlement snapshot + lock ────────────────────────────────────────────────

async def _snapshot_pool(pool: dict) -> dict:
    """Freeze the pool, compute distributable amounts on both sides, and write
    per-user owed amounts.

    Idempotent: if the pool is already snapshotted, this is a no-op.
    """
    if pool.get("snapshot", {}).get("snapshot_at") is not None:
        return pool

    total_sol = float(pool.get("total_points_usd_sol", 0.0) or 0.0)
    total_xfee = float(pool.get("total_points_usd_xfee", 0.0) or 0.0)
    total_all = total_sol + total_xfee
    if total_all <= 0:
        now = datetime.now(timezone.utc)
        await global_pools_col().update_one(
            {"_id": pool["_id"]},
            {
                "$set": {
                    "status": "settled",
                    "snapshot": {
                        "funding_balance_lamports": 0,
                        "buffer_lamports": 0,
                        "distributable_lamports": 0,
                        "distributable_xfee_raw": 0,
                        "funding_wallet": _funding_pubkey_str(),
                        "snapshot_at": now,
                        "reason": "no_points",
                    },
                    "settled_at": now,
                    "updated_at": now,
                }
            },
        )
        return await global_pools_col().find_one({"_id": pool["_id"]})

    funding_pub = _funding_pubkey_str()
    balance_lamports = await get_balance(funding_pub)
    buffer_lamports = int(settings.global_pool_funding_buffer_sol * LAMPORTS_PER_SOL)
    distributable_lamports = max(0, balance_lamports - buffer_lamports)

    distributable_xfee_raw = 0
    if total_xfee > 0 and _xfee_mint():
        distributable_xfee_raw = await get_spl_balance_raw(funding_pub, _xfee_mint())

    # Enforce that whichever side has points has non-zero funding. If neither
    # can be funded, that's an operator issue and we surface it loudly.
    if total_sol > 0 and distributable_lamports <= 0 and (total_xfee <= 0 or distributable_xfee_raw <= 0):
        raise RuntimeError(
            f"Global pool funding wallet SOL balance ({balance_lamports} lamports) is below the "
            f"configured buffer ({buffer_lamports} lamports); no XFEE points either. Cannot settle."
        )

    settlement_id = secrets.token_hex(6)
    now = datetime.now(timezone.utc)
    decimals = _xfee_decimals()

    cursor = pool_points_col().find({"pool_id": pool["_id"]})
    total_users = 0
    async for row in cursor:
        sol_pts = float(row.get("points_usd_sol", 0.0) or 0.0)
        xfee_pts = float(row.get("points_usd_xfee", 0.0) or 0.0)

        owed_sol = (
            int((sol_pts / total_sol) * distributable_lamports)
            if total_sol > 0 and sol_pts > 0 else 0
        )
        owed_xfee = (
            int((xfee_pts / total_xfee) * distributable_xfee_raw)
            if total_xfee > 0 and xfee_pts > 0 else 0
        )
        memo_sol = _memo_for(int(pool["pool_index"]), settlement_id, row["wallet_address"], side="SOL")
        memo_xfee = _memo_for(int(pool["pool_index"]), settlement_id, row["wallet_address"], side="XFEE")
        sol_status = "pending" if owed_sol > 0 else "skipped_zero"
        xfee_status = "pending" if owed_xfee > 0 else "skipped_zero"

        await pool_points_col().update_one(
            {"_id": row["_id"]},
            {
                "$set": {
                    "owed_lamports": owed_sol,
                    "owed_sol": owed_sol / LAMPORTS_PER_SOL,
                    "owed_xfee_raw": owed_xfee,
                    "owed_xfee_ui": owed_xfee / (10 ** decimals),
                    "settle_status": sol_status,
                    "xfee_settle_status": xfee_status,
                    "memo": memo_sol,
                    "xfee_memo": memo_xfee,
                    "updated_at": now,
                },
                "$setOnInsert": {"attempts": 0, "xfee_attempts": 0},
            },
        )
        total_users += 1

    await global_pools_col().update_one(
        {"_id": pool["_id"]},
        {
            "$set": {
                "status": "ready_to_settle",
                "snapshot": {
                    "funding_balance_lamports": balance_lamports,
                    "funding_balance_xfee_raw": distributable_xfee_raw,
                    "buffer_lamports": buffer_lamports,
                    "distributable_lamports": distributable_lamports,
                    "distributable_xfee_raw": distributable_xfee_raw,
                    "funding_wallet": funding_pub,
                    "snapshot_at": now,
                    "total_users": total_users,
                    "settlement_id": settlement_id,
                },
                "updated_at": now,
            }
        },
    )
    return await global_pools_col().find_one({"_id": pool["_id"]})


async def _acquire_wallet_lock(pool: dict, ttl_seconds: int = 600) -> bool:
    """Wallet-level lock: ensures only one pool settles from the funding wallet at a time."""
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(seconds=ttl_seconds)
    funding = pool["snapshot"]["funding_wallet"]

    busy = await global_pools_col().find_one(
        {
            "_id": {"$ne": pool["_id"]},
            "status": "settling",
            "snapshot.funding_wallet": funding,
            "settlement.lock_until": {"$gt": now},
        }
    )
    if busy:
        logger.info(
            "Funding wallet %s busy with pool %s — deferring pool %s",
            funding, busy["pool_index"], pool["pool_index"],
        )
        return False

    result = await global_pools_col().update_one(
        {
            "_id": pool["_id"],
            "$or": [
                {"settlement.lock_owner": {"$exists": False}},
                {"settlement.lock_owner": WORKER_ID},
                {"settlement.lock_until": {"$lte": now}},
            ],
        },
        {
            "$set": {
                "status": "settling",
                "settlement.lock_owner": WORKER_ID,
                "settlement.lock_until": expiry,
                "settlement.started_at": pool.get("settlement", {}).get("started_at") or now,
                "updated_at": now,
            }
        },
    )
    return result.modified_count > 0


async def _release_wallet_lock(pool: dict) -> None:
    await global_pools_col().update_one(
        {"_id": pool["_id"], "settlement.lock_owner": WORKER_ID},
        {"$unset": {"settlement.lock_until": ""}, "$set": {"updated_at": datetime.now(timezone.utc)}},
    )


# ── Per-user payout state machines ────────────────────────────────────────────

async def _process_row_sol(pool: dict, row: dict) -> str:
    """SOL payout state machine for one user. Returns final settle_status."""
    if int(row.get("owed_lamports", 0)) <= 0:
        return row.get("settle_status", "skipped_zero")

    funding_kp = _funding_keypair()
    funding_pub = str(funding_kp.pubkey())
    to_pubkey = Pubkey.from_string(row["wallet_address"])
    memo = row["memo"]
    owed = int(row["owed_lamports"])
    now = datetime.now(timezone.utc)

    existing_sig = row.get("tx_signature")
    if existing_sig:
        ok = await confirm_transaction(existing_sig, max_retries=settings.global_pool_confirm_retries)
        if ok:
            await pool_points_col().update_one(
                {"_id": row["_id"]},
                {"$set": {"settle_status": "confirmed", "confirmed_at": now, "updated_at": now}},
            )
            return "confirmed"

    reconciled_sig = await find_signature_by_memo(funding_pub, memo)
    if reconciled_sig:
        await pool_points_col().update_one(
            {"_id": row["_id"]},
            {
                "$set": {
                    "settle_status": "confirmed",
                    "tx_signature": reconciled_sig,
                    "confirmed_at": now,
                    "reconciled": True,
                    "updated_at": now,
                }
            },
        )
        logger.info(
            "Reconciled existing SOL payout for %s via memo %s -> %s",
            row["wallet_address"], memo, reconciled_sig,
        )
        return "confirmed"

    sending_update = await pool_points_col().update_one(
        {
            "_id": row["_id"],
            "settle_status": {"$in": ["pending", "failed", "sending"]},
        },
        {
            "$set": {
                "settle_status": "sending",
                "sending_at": now,
                "updated_at": now,
            },
            "$inc": {"attempts": 1},
        },
    )
    if sending_update.modified_count == 0:
        latest = await pool_points_col().find_one({"_id": row["_id"]})
        return latest.get("settle_status", "unknown") if latest else "unknown"

    try:
        signature = await transfer_sol_with_memo(funding_kp, to_pubkey, owed, memo)
    except Exception as exc:
        await pool_points_col().update_one(
            {"_id": row["_id"]},
            {
                "$set": {
                    "settle_status": "failed",
                    "last_error": str(exc),
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        logger.exception("SOL transfer failed for %s (pool %s)", row["wallet_address"], pool["pool_index"])
        return "failed"

    await pool_points_col().update_one(
        {"_id": row["_id"]},
        {
            "$set": {
                "settle_status": "sent",
                "tx_signature": signature,
                "sent_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            },
            "$unset": {"last_error": ""},
        },
    )

    ok = await confirm_transaction(signature, max_retries=settings.global_pool_confirm_retries)
    final_status = "confirmed" if ok else "sent"
    await pool_points_col().update_one(
        {"_id": row["_id"]},
        {
            "$set": {
                "settle_status": final_status,
                "confirmed_at": datetime.now(timezone.utc) if ok else None,
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )
    return final_status


async def _process_row_xfee(pool: dict, row: dict) -> str:
    """XFEE payout state machine for one user. Returns final xfee_settle_status."""
    if int(row.get("owed_xfee_raw", 0)) <= 0:
        return row.get("xfee_settle_status", "skipped_zero")

    xfee_mint = _xfee_mint()
    if not xfee_mint:
        logger.error("XFEE payout requested but XFEE_PAYMENT_TOKEN_MINT unset")
        return row.get("xfee_settle_status", "failed")

    funding_kp = _funding_keypair()
    funding_pub = str(funding_kp.pubkey())
    to_pubkey = Pubkey.from_string(row["wallet_address"])
    mint_pubkey = Pubkey.from_string(xfee_mint)
    memo = row["xfee_memo"]
    owed_raw = int(row["owed_xfee_raw"])
    decimals = _xfee_decimals()
    now = datetime.now(timezone.utc)

    existing_sig = row.get("xfee_tx_signature")
    if existing_sig:
        ok = await confirm_transaction(existing_sig, max_retries=settings.global_pool_confirm_retries)
        if ok:
            await pool_points_col().update_one(
                {"_id": row["_id"]},
                {"$set": {"xfee_settle_status": "confirmed", "xfee_confirmed_at": now, "updated_at": now}},
            )
            return "confirmed"

    reconciled_sig = await find_spl_signature_by_memo(funding_pub, memo)
    if reconciled_sig:
        await pool_points_col().update_one(
            {"_id": row["_id"]},
            {
                "$set": {
                    "xfee_settle_status": "confirmed",
                    "xfee_tx_signature": reconciled_sig,
                    "xfee_confirmed_at": now,
                    "xfee_reconciled": True,
                    "updated_at": now,
                }
            },
        )
        logger.info(
            "Reconciled existing XFEE payout for %s via memo %s -> %s",
            row["wallet_address"], memo, reconciled_sig,
        )
        return "confirmed"

    sending_update = await pool_points_col().update_one(
        {
            "_id": row["_id"],
            "xfee_settle_status": {"$in": ["pending", "failed", "sending"]},
        },
        {
            "$set": {
                "xfee_settle_status": "sending",
                "xfee_sending_at": now,
                "updated_at": now,
            },
            "$inc": {"xfee_attempts": 1},
        },
    )
    if sending_update.modified_count == 0:
        latest = await pool_points_col().find_one({"_id": row["_id"]})
        return latest.get("xfee_settle_status", "unknown") if latest else "unknown"

    try:
        signature = await transfer_spl_token(
            funding_kp,
            to_pubkey,
            mint_pubkey,
            owed_raw,
            decimals,
            memo=memo,
        )
    except Exception as exc:
        await pool_points_col().update_one(
            {"_id": row["_id"]},
            {
                "$set": {
                    "xfee_settle_status": "failed",
                    "xfee_last_error": str(exc),
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        logger.exception("XFEE transfer failed for %s (pool %s)", row["wallet_address"], pool["pool_index"])
        return "failed"

    await pool_points_col().update_one(
        {"_id": row["_id"]},
        {
            "$set": {
                "xfee_settle_status": "sent",
                "xfee_tx_signature": signature,
                "xfee_sent_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            },
            "$unset": {"xfee_last_error": ""},
        },
    )

    ok = await confirm_transaction(signature, max_retries=settings.global_pool_confirm_retries)
    final_status = "confirmed" if ok else "sent"
    await pool_points_col().update_one(
        {"_id": row["_id"]},
        {
            "$set": {
                "xfee_settle_status": final_status,
                "xfee_confirmed_at": datetime.now(timezone.utc) if ok else None,
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )
    return final_status


async def _process_row(pool: dict, row: dict) -> dict:
    """Run both currency payouts for one row and return combined outcome."""
    sol_status = await _process_row_sol(pool, row)
    xfee_status = await _process_row_xfee(pool, row)
    return {"sol": sol_status, "xfee": xfee_status}


# ── Public settlement entrypoint ──────────────────────────────────────────────

async def settle_pool(pool_index: int, *, force: bool = False) -> dict:
    """Settle a global pool by direct auto-transfer from the funding wallet.

    `force=true` ends an active pool early before settling it. Runs both the
    SOL and XFEE payout tracks per user; the pool is marked settled once no
    user has an outstanding non-terminal status on either side.
    """
    now = datetime.now(timezone.utc)
    pool = await global_pools_col().find_one({"pool_index": pool_index})
    if not pool:
        raise ValueError(f"Global pool {pool_index} not found")

    if pool.get("status") == "settled":
        return {"success": True, "pool_index": pool_index, "status": "settled", "already_settled": True}

    if _utc(pool["end_at"]) > now:
        if not force:
            raise ValueError("Pool is not due yet. Pass force=true to settle early.")
        await global_pools_col().update_one(
            {"_id": pool["_id"]},
            {"$set": {"end_at": now, "forced_finalized": True, "updated_at": now}},
        )
        pool["end_at"] = now
        pool["forced_finalized"] = True

    pool = await _snapshot_pool(pool)
    if pool["status"] == "settled":
        return {"success": True, "pool_index": pool_index, "status": "settled", "reason": "no_points"}

    await ensure_next_pool(pool)

    if not await _acquire_wallet_lock(pool):
        return {
            "success": False,
            "pool_index": pool_index,
            "status": pool["status"],
            "reason": "funding_wallet_busy_with_other_pool",
        }

    stats = {
        "sol_confirmed": 0, "sol_sent": 0, "sol_failed": 0, "sol_skipped": 0,
        "xfee_confirmed": 0, "xfee_sent": 0, "xfee_failed": 0, "xfee_skipped": 0,
    }
    try:
        cursor = (
            pool_points_col()
            .find(
                {
                    "pool_id": pool["_id"],
                    "$or": [
                        {"owed_lamports": {"$gt": 0}, "settle_status": {"$nin": ["confirmed", "skipped_zero"]}},
                        {"owed_xfee_raw": {"$gt": 0}, "xfee_settle_status": {"$nin": ["confirmed", "skipped_zero"]}},
                    ],
                }
            )
            .sort("points_usd", -1)
        )
        rows: list[dict] = [row async for row in cursor]

        sem = asyncio.Semaphore(max(1, settings.global_pool_settlement_concurrency))

        async def _bounded(row):
            async with sem:
                return await _process_row(pool, row)

        results = await asyncio.gather(*[_bounded(r) for r in rows], return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                stats["sol_failed"] += 1
                stats["xfee_failed"] += 1
                continue
            sol_s = result.get("sol")
            xfee_s = result.get("xfee")
            if sol_s == "confirmed":
                stats["sol_confirmed"] += 1
            elif sol_s == "sent":
                stats["sol_sent"] += 1
            elif sol_s == "failed":
                stats["sol_failed"] += 1
            else:
                stats["sol_skipped"] += 1
            if xfee_s == "confirmed":
                stats["xfee_confirmed"] += 1
            elif xfee_s == "sent":
                stats["xfee_sent"] += 1
            elif xfee_s == "failed":
                stats["xfee_failed"] += 1
            else:
                stats["xfee_skipped"] += 1

        outstanding = await pool_points_col().count_documents(
            {
                "pool_id": pool["_id"],
                "$or": [
                    {"owed_lamports": {"$gt": 0}, "settle_status": {"$nin": ["confirmed", "skipped_zero"]}},
                    {"owed_xfee_raw": {"$gt": 0}, "xfee_settle_status": {"$nin": ["confirmed", "skipped_zero"]}},
                ],
            }
        )
        if outstanding == 0:
            await global_pools_col().update_one(
                {"_id": pool["_id"]},
                {
                    "$set": {
                        "status": "settled",
                        "settled_at": datetime.now(timezone.utc),
                        "settlement.completed_at": datetime.now(timezone.utc),
                        "updated_at": datetime.now(timezone.utc),
                    }
                },
            )
            final_status = "settled"
        else:
            final_status = "settling"
            await global_pools_col().update_one(
                {"_id": pool["_id"]},
                {
                    "$set": {
                        "settlement.outstanding": outstanding,
                        "updated_at": datetime.now(timezone.utc),
                    }
                },
            )
    finally:
        await _release_wallet_lock(pool)

    return {
        "success": True,
        "pool_index": pool_index,
        "status": final_status,
        "stats": stats,
    }


async def process_due_pools(now: Optional[datetime] = None) -> dict:
    if not settings.global_pool_enabled:
        return {"ran": False, "reason": "global_pool_disabled"}
    now = _utc(now or datetime.now(timezone.utc))
    out: dict[str, Any] = {"ran": True, "settled": [], "failed": []}
    cursor = global_pools_col().find(
        {"status": {"$in": ["active", "ready_to_settle", "settling"]}, "end_at": {"$lte": now}}
    ).sort("pool_index", 1)
    async for pool in cursor:
        try:
            result = await settle_pool(int(pool["pool_index"]))
            out["settled"].append(result)
        except Exception as exc:
            logger.exception("Global pool settlement failed for pool %s", pool.get("pool_index"))
            out["failed"].append({"pool_index": pool.get("pool_index"), "error": str(exc)})
    return out


async def global_pool_worker_loop() -> None:
    while True:
        try:
            await process_due_pools()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Global pool worker failed")
        await asyncio.sleep(settings.global_pool_finalize_interval_seconds)
