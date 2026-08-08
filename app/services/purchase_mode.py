"""System-wide purchase-mode toggle (SOL vs XFEE).

The active mode is persisted in Mongo (`system_meta._id = "purchase_mode"`)
so it survives redeploys and is shared across replicas. The env var
`DEFAULT_PURCHASE_MODE` is only used the first time a fresh deployment reads
the mode — after that, the DB value is authoritative.

Each write appends to a bounded `history` array so we can audit who
switched the mode and when.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from app.config import settings
from app.database import system_meta_col

logger = logging.getLogger(__name__)

MODE_DOC_ID = "purchase_mode"
VALID_MODES = ("SOL", "XFEE")
HISTORY_LIMIT = 100


def _normalize(mode: str) -> str:
    m = (mode or "").strip().upper()
    if m not in VALID_MODES:
        raise ValueError(f"Invalid purchase mode: {mode!r}. Must be one of {VALID_MODES}")
    return m


async def get_purchase_mode() -> dict:
    """Return the current mode doc.

    Shape:
        {
            "mode": "SOL" | "XFEE",
            "updated_at": datetime,
            "updated_by": str,          # optional; who last set it
        }

    Auto-initializes from `DEFAULT_PURCHASE_MODE` when the doc doesn't exist.
    """
    doc = await system_meta_col().find_one({"_id": MODE_DOC_ID})
    if doc:
        return {
            "mode": doc.get("mode", "SOL"),
            "updated_at": doc.get("updated_at"),
            "updated_by": doc.get("updated_by"),
        }

    default = _normalize(settings.default_purchase_mode or "SOL")
    now = datetime.now(timezone.utc)
    await system_meta_col().update_one(
        {"_id": MODE_DOC_ID},
        {
            "$setOnInsert": {
                "_id": MODE_DOC_ID,
                "mode": default,
                "updated_at": now,
                "updated_by": "bootstrap",
                "history": [
                    {
                        "mode": default,
                        "changed_at": now,
                        "changed_by": "bootstrap",
                        "reason": "initialized from DEFAULT_PURCHASE_MODE",
                    }
                ],
            }
        },
        upsert=True,
    )
    return {"mode": default, "updated_at": now, "updated_by": "bootstrap"}


async def set_purchase_mode(mode: str, *, changed_by: str = "admin", reason: Optional[str] = None) -> dict:
    """Set the active purchase mode. Idempotent. Records history."""
    normalized = _normalize(mode)
    now = datetime.now(timezone.utc)

    current = await system_meta_col().find_one({"_id": MODE_DOC_ID})
    prev_mode = current.get("mode") if current else None
    if prev_mode == normalized:
        return {
            "mode": normalized,
            "updated_at": current.get("updated_at") if current else now,
            "updated_by": current.get("updated_by") if current else changed_by,
            "unchanged": True,
        }

    history_entry = {
        "from": prev_mode,
        "to": normalized,
        "changed_at": now,
        "changed_by": changed_by,
        "reason": reason or "",
    }
    await system_meta_col().update_one(
        {"_id": MODE_DOC_ID},
        {
            "$set": {
                "mode": normalized,
                "updated_at": now,
                "updated_by": changed_by,
            },
            "$push": {
                "history": {
                    "$each": [history_entry],
                    "$slice": -HISTORY_LIMIT,
                }
            },
        },
        upsert=True,
    )
    logger.warning(
        "Purchase mode changed: %s -> %s (by=%s, reason=%s)",
        prev_mode, normalized, changed_by, reason,
    )
    return {
        "mode": normalized,
        "previous_mode": prev_mode,
        "updated_at": now,
        "updated_by": changed_by,
        "unchanged": False,
    }


async def get_mode_history(limit: int = 50) -> list[dict]:
    doc = await system_meta_col().find_one({"_id": MODE_DOC_ID})
    if not doc:
        return []
    hist = doc.get("history") or []
    return hist[-limit:]
