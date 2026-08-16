"""Public founder-eligibility endpoints."""
import logging

from fastapi import APIRouter

from app.services.founder import (
    FOUNDER_ELIGIBLE_CAP_USD,
    FOUNDER_ELIGIBLE_MIN_PURCHASE_USD,
    get_founder_state,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/founder", tags=["founder"])


@router.get("/progress")
async def get_founder_progress():
    """Current cumulative USD toward the founder cap, and whether it's still open.

    The `phase` field is derived: while the founder round is open, phase == "round";
    once the $1M cap closes, phase == "mining" and mining_live == True. Callers
    that prefer booleans can use `round_closed` + `mining_live` directly.
    """
    state = await get_founder_state()

    cap = float(state.get("cap_usd", FOUNDER_ELIGIBLE_CAP_USD))
    cumulative = float(state.get("cumulative_usd", 0.0))
    min_usd = float(state.get("min_purchase_usd", FOUNDER_ELIGIBLE_MIN_PURCHASE_USD))
    remaining = max(cap - cumulative, 0.0)
    is_open = state.get("closed_at") is None
    round_closed = not is_open
    mining_live = round_closed
    phase = "round" if is_open else "mining"

    return {
        "cap_usd": cap,
        "min_purchase_usd": min_usd,
        "cumulative_usd": cumulative,
        "remaining_usd": remaining,
        "is_open": is_open,
        "round_closed": round_closed,
        "mining_live": mining_live,
        "phase": phase,
        "closed_at": state.get("closed_at"),
        "founder_count": int(state.get("founder_count", 0)),
        "last_purchase_at": state.get("last_purchase_at"),
        "backfill_completed_at": state.get("backfill_completed_at"),
    }
