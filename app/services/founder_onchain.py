"""Founder on-chain writes — mirrors the POWER-stake reliability pattern.

Each founder-eligible purchase gets its USD value written to the Revenue Split
program via `add_power(pool, user_wallet, external_ref=purchase_id, power_delta=xfee_amount)`.

Reliability model (same as `app/services/stake_repair.py`):

  1. Live path — after `maybe_mark_founder_eligible` sets `founder_eligible=true`,
     we set `founder_onchain_status="pending"` and kick a fire-and-forget task.
     A single failure never blocks the buyer's response.
  2. Repair worker — a periodic background loop scans purchases with
     `founder_onchain_status="pending"` that are at least
     FOUNDER_ONCHAIN_REPAIR_MIN_AGE_MINUTES old and retries them.
  3. On-chain dedup — `external_ref = str(purchase_id)`. The program's
     `power_grant` PDA is unique per (pool, sha256(external_ref)); replaying
     the same ref reverts. We probe the PDA before every write and, on tx
     failure, we re-probe to detect races.
  4. Kill switch — FOUNDER_ONCHAIN_ENABLED=false disables both the live path
     and the worker while leaving pending markers in place. Turning it back on
     resumes writes without any manual backfill.
  5. Backfill — on startup, any purchase with `founder_eligible=true` and no
     `founder_onchain_status` is stamped `pending`, so historic marks get
     replayed by the worker without touching Mongo counters.

The value we write is the USD dollar count (`xfee_amount`, since $1 XFEE = $1).
It's a u64, so it fits comfortably.

Purchase-doc fields set here:
  founder_onchain_status:          "pending" | "written" | "already_written"
                                  | "not_eligible" | "disabled" | "skipped"
  founder_onchain_tx:              tx signature (on "written")
  founder_onchain_amount:          u64 power_delta actually written
  founder_onchain_external_ref:    the purchase_id string used
  founder_onchain_written_at:      utc datetime of success
  founder_onchain_last_attempt_at: utc datetime of most recent attempt
  founder_onchain_last_error:      last error string (cleared on success)
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

from bson import ObjectId
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solders.pubkey import Pubkey

from app.config import founder_onchain_rpc_url, settings
from app.database import purchases_col, system_meta_col
from app.services.founder_onchain_sdk import (
    add_power,
    power_grant_exists,
)
from app.utils.keypair import keypair_from_private_key

logger = logging.getLogger(__name__)

FOUNDER_ONCHAIN_BACKFILL_MARKER: str = "founder_onchain_backfill"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _preflight() -> Optional[str]:
    """Return an error string if we're not configured to run, else None."""
    if not settings.founder_onchain_enabled:
        return "founder_onchain_disabled"
    if not settings.founder_onchain_signer_private_key:
        return "FOUNDER_ONCHAIN_SIGNER_PRIVATE_KEY not configured"
    if not settings.founder_onchain_pool_address:
        return "FOUNDER_ONCHAIN_POOL_ADDRESS not configured"
    if not founder_onchain_rpc_url():
        return "FOUNDER_ONCHAIN_RPC_URL / QUICKNODE_RPC_URL not configured"
    return None


async def _mark_result(
    pid: ObjectId,
    *,
    status: str,
    tx: Optional[str] = None,
    amount: Optional[int] = None,
    external_ref: Optional[str] = None,
    error: Optional[str] = None,
    written: bool = False,
) -> None:
    updates: Dict[str, Any] = {
        "founder_onchain_status": status,
        "founder_onchain_last_attempt_at": _now(),
    }
    if error is None:
        updates["founder_onchain_last_error"] = None
    else:
        updates["founder_onchain_last_error"] = error[:800]
    if tx is not None:
        updates["founder_onchain_tx"] = tx
    if amount is not None:
        updates["founder_onchain_amount"] = int(amount)
    if external_ref is not None:
        updates["founder_onchain_external_ref"] = external_ref
    if written:
        updates["founder_onchain_written_at"] = _now()
    await purchases_col().update_one({"_id": pid}, {"$set": updates})


async def write_founder_power_onchain(
    purchase: dict,
    *,
    force: bool = False,
    pool_address: Optional[str] = None,
    rpc_url: Optional[str] = None,
) -> dict:
    """Attempt one on-chain write for a single purchase.

    - Returns a structured result dict (safe to expose over admin API).
    - Never raises: failures are captured on the purchase document and
      returned in the result.
    - Idempotent: probes the on-chain `power_grant` PDA before writing.

    `force=True` skips the "already-written" fast path in Mongo (still probes
    on-chain). Use only for manual admin re-writes.
    """
    pid: ObjectId = purchase["_id"]
    purchase_id_str = str(pid)

    if not purchase.get("founder_eligible"):
        await _mark_result(pid, status="not_eligible", error="purchase is not founder_eligible")
        return {
            "success": False,
            "purchase_id": purchase_id_str,
            "error": "purchase is not founder_eligible",
            "status": "not_eligible",
        }

    err = _preflight()
    if err:
        await _mark_result(pid, status="disabled", error=err)
        return {
            "success": False,
            "skipped": True,
            "reason": err,
            "purchase_id": purchase_id_str,
            "status": "disabled",
        }

    if not force and purchase.get("founder_onchain_status") == "written":
        return {
            "success": True,
            "skipped": True,
            "reason": "already_written_in_db",
            "purchase_id": purchase_id_str,
            "tx": purchase.get("founder_onchain_tx"),
            "amount": purchase.get("founder_onchain_amount"),
            "status": "written",
        }

    amount = int(purchase.get("xfee_amount") or 0)
    if amount <= 0:
        await _mark_result(pid, status="skipped", error=f"invalid xfee_amount={amount}")
        return {
            "success": False,
            "purchase_id": purchase_id_str,
            "error": "invalid xfee_amount",
            "status": "skipped",
        }

    pool_str = pool_address or settings.founder_onchain_pool_address
    rpc_str = rpc_url or founder_onchain_rpc_url()
    try:
        pool_pubkey = Pubkey.from_string(pool_str)
        user_pubkey = Pubkey.from_string(purchase["user_wallet"])
    except Exception as e:
        err_msg = f"invalid pool or user_wallet pubkey: {e}"
        await _mark_result(pid, status="pending", error=err_msg)
        return {"success": False, "purchase_id": purchase_id_str, "error": err_msg}

    try:
        signer = keypair_from_private_key(settings.founder_onchain_signer_private_key)
    except Exception as e:
        err_msg = f"failed to load founder-onchain signer keypair: {e}"
        await _mark_result(pid, status="pending", error=err_msg)
        return {"success": False, "purchase_id": purchase_id_str, "error": err_msg}

    connection = AsyncClient(rpc_str, commitment=Confirmed)
    try:
        try:
            already = await power_grant_exists(connection, pool_pubkey, purchase_id_str)
        except Exception as e:
            already = False
            logger.warning(
                "founder-onchain: pre-write PDA probe failed for %s (%s); attempting write anyway",
                purchase_id_str,
                e,
            )
        if already:
            await _mark_result(
                pid,
                status="already_written",
                amount=amount,
                external_ref=purchase_id_str,
                written=True,
            )
            return {
                "success": True,
                "skipped": True,
                "reason": "already_written_on_chain",
                "purchase_id": purchase_id_str,
                "amount": amount,
                "status": "already_written",
            }

        try:
            sig = await add_power(
                connection=connection,
                signer=signer,
                pool=pool_pubkey,
                wallet=user_pubkey,
                external_ref=purchase_id_str,
                power_delta=amount,
            )
        except Exception as e:
            err_msg = str(e)
            races = False
            try:
                races = await power_grant_exists(connection, pool_pubkey, purchase_id_str)
            except Exception:
                races = False
            if races:
                await _mark_result(
                    pid,
                    status="already_written",
                    amount=amount,
                    external_ref=purchase_id_str,
                    written=True,
                    error=None,
                )
                logger.info(
                    "founder-onchain: tx failed but PDA exists — treating as already_written (purchase=%s)",
                    purchase_id_str,
                )
                return {
                    "success": True,
                    "skipped": True,
                    "reason": "already_written_on_chain_post_send",
                    "purchase_id": purchase_id_str,
                    "amount": amount,
                    "status": "already_written",
                }

            await _mark_result(pid, status="pending", error=err_msg)
            logger.warning(
                "founder-onchain: write failed for purchase=%s pool=%s user=%s amount=%s: %s",
                purchase_id_str,
                pool_str,
                purchase["user_wallet"],
                amount,
                err_msg,
            )
            return {
                "success": False,
                "purchase_id": purchase_id_str,
                "error": err_msg,
                "status": "pending",
            }

        await _mark_result(
            pid,
            status="written",
            tx=sig,
            amount=amount,
            external_ref=purchase_id_str,
            written=True,
        )
        logger.info(
            "founder-onchain: wrote purchase=%s user=%s amount=%s tx=%s",
            purchase_id_str,
            purchase["user_wallet"],
            amount,
            sig,
        )
        return {
            "success": True,
            "purchase_id": purchase_id_str,
            "tx": sig,
            "amount": amount,
            "status": "written",
        }
    finally:
        try:
            await connection.close()
        except Exception:
            pass


async def try_write_founder_power_onchain(purchase_id: str) -> None:
    """Fire-and-forget wrapper for the live path.

    Fetches the freshest purchase doc and attempts one write. All exceptions
    are swallowed so a background task failure cannot break the caller.
    """
    if not settings.founder_onchain_enabled:
        return
    try:
        pid = ObjectId(purchase_id)
    except Exception:
        return
    try:
        doc = await purchases_col().find_one({"_id": pid})
        if not doc:
            return
        await write_founder_power_onchain(doc)
    except Exception:
        logger.exception(
            "founder-onchain: background write raised for purchase=%s", purchase_id
        )


async def run_founder_onchain_repair_scan() -> dict:
    """One pass of the repair worker.

    Picks up founder-eligible purchases that:
      * still have `founder_onchain_status` in {"pending", "disabled"} (the
        latter so re-enabling the kill switch resumes work automatically), and
      * were confirmed at least FOUNDER_ONCHAIN_REPAIR_MIN_AGE_MINUTES ago
        (so the live-path async task has a fair chance first), and
      * were confirmed at/after `founder_onchain_repair_since_unix`.
    """
    if settings.test_mode:
        return {"ran": False, "reason": "test_mode"}

    err = _preflight()
    if err:
        return {"ran": False, "reason": err}

    try:
        signer = keypair_from_private_key(settings.founder_onchain_signer_private_key)
        logger.info(
            "founder-onchain repair signer: %s (pool=%s)",
            signer.pubkey(),
            settings.founder_onchain_pool_address,
        )
    except Exception:
        logger.warning(
            "founder-onchain repair: failed to derive signer pubkey for log line",
            exc_info=True,
        )

    cutoff = _now() - timedelta(minutes=settings.founder_onchain_repair_min_age_minutes)
    since = datetime.fromtimestamp(
        settings.founder_onchain_repair_since_unix, tz=timezone.utc
    )

    query: dict = {
        "founder_eligible": True,
        "founder_onchain_status": {"$in": ["pending", "disabled"]},
        "confirmed_at": {"$ne": None, "$gte": since, "$lte": cutoff},
    }

    stats: Dict[str, Any] = {
        "ran": True,
        "checked": 0,
        "written": 0,
        "already_written": 0,
        "failed": 0,
        "errors": [],
    }

    cursor = (
        purchases_col()
        .find(query)
        .sort("confirmed_at", 1)
        .limit(settings.founder_onchain_repair_batch_size)
    )
    async for purchase in cursor:
        stats["checked"] += 1
        try:
            out = await write_founder_power_onchain(purchase)
            if out.get("success") and out.get("tx"):
                stats["written"] += 1
            elif out.get("success") and out.get("skipped"):
                stats["already_written"] += 1
            elif out.get("success"):
                stats["already_written"] += 1
            else:
                stats["failed"] += 1
                stats["errors"].append(
                    {
                        "purchase_id": out.get("purchase_id"),
                        "error": out.get("error") or str(out),
                    }
                )
        except Exception as e:
            stats["failed"] += 1
            stats["errors"].append(
                {"purchase_id": str(purchase.get("_id")), "error": str(e)}
            )
            logger.exception(
                "founder-onchain repair error for purchase %s", purchase.get("_id")
            )

    if stats["checked"]:
        logger.info(
            "founder-onchain repair: checked=%s written=%s already=%s failed=%s",
            stats["checked"],
            stats["written"],
            stats["already_written"],
            stats["failed"],
        )
        if stats["failed"] and stats["errors"]:
            sample = stats["errors"][0]
            logger.warning(
                "founder-onchain repair: %s failure(s). First: purchase=%s error=%s",
                stats["failed"],
                sample.get("purchase_id"),
                sample.get("error"),
            )

    return stats


async def founder_onchain_repair_worker_loop() -> None:
    """Periodic scan until cancelled (see main lifespan)."""
    while True:
        try:
            await run_founder_onchain_repair_scan()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("founder-onchain repair scan failed")
        await asyncio.sleep(settings.founder_onchain_repair_interval_seconds)


async def ensure_founder_onchain_backfill() -> dict:
    """Mark existing founder-eligible purchases as `pending` so the worker picks them up.

    One-shot, idempotent — a marker in `system_meta` prevents re-application on
    redeploys. Safe to run regardless of the FOUNDER_ONCHAIN_ENABLED kill
    switch (we're only writing DB status; no on-chain calls).
    """
    marker = await system_meta_col().find_one({"_id": FOUNDER_ONCHAIN_BACKFILL_MARKER})
    if marker and marker.get("applied"):
        return {
            "ran": False,
            "reason": "already_applied",
            "marked_count": int(marker.get("marked_count", 0)),
            "applied_at": marker.get("applied_at"),
        }

    result = await purchases_col().update_many(
        {
            "founder_eligible": True,
            "founder_onchain_status": {"$exists": False},
        },
        {"$set": {"founder_onchain_status": "pending"}},
    )

    now = _now()
    await system_meta_col().update_one(
        {"_id": FOUNDER_ONCHAIN_BACKFILL_MARKER},
        {
            "$set": {
                "applied": True,
                "applied_at": now,
                "marked_count": int(result.modified_count),
            }
        },
        upsert=True,
    )
    logger.info(
        "founder-onchain backfill: marked %s founder-eligible purchases as pending",
        result.modified_count,
    )
    return {
        "ran": True,
        "marked_count": int(result.modified_count),
        "applied_at": now,
    }
