"""Public founder-eligibility endpoints."""
import logging

from fastapi import APIRouter

from app.config import settings
from app.services.founder import (
    FOUNDER_ELIGIBLE_CAP_USD,
    FOUNDER_ELIGIBLE_MIN_PURCHASE_USD,
    get_founder_state,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/founder", tags=["founder"])


@router.get("/progress")
async def get_founder_progress():
    """Current cumulative USD toward the founder cap plus round / mining state.

    Fields are derived as follows:
      * `round_closed` - True once cumulative sales reach the $1M cap
        (i.e. the founder-eligibility state doc has `closed_at` set).
      * `mining_live` - True whenever POWER staking is enabled on the
        server (`POWER_DISTRIBUTION_ENABLED=true`). Independent of the
        round; staking can be toggled on/off at any time.
      * `phase` - three-state label:
          - `"round"`      - round is still open (regardless of staking flag)
          - `"pre_mining"` - round closed but staking not yet enabled
          - `"mining"`     - round closed and staking enabled
    """
    state = await get_founder_state()

    cap = float(state.get("cap_usd", FOUNDER_ELIGIBLE_CAP_USD))
    cumulative = float(state.get("cumulative_usd", 0.0))
    min_usd = float(state.get("min_purchase_usd", FOUNDER_ELIGIBLE_MIN_PURCHASE_USD))
    remaining = max(cap - cumulative, 0.0)
    is_open = state.get("closed_at") is None
    round_closed = not is_open
    mining_live = bool(settings.power_distribution_enabled)
    if not round_closed:
        phase = "round"
    elif mining_live:
        phase = "mining"
    else:
        phase = "pre_mining"

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
