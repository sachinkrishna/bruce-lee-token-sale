from typing import Optional

from fastapi import APIRouter

from app.config import settings
from app.database import purchases_col
from app.services.sol_price import get_sol_price
from app.utils.economics import POWER_STAKE_MULTIPLIER
from app.utils.level import LEVEL_THRESHOLDS

router = APIRouter(prefix="/api/v1/stats", tags=["stats"])


@router.get("/levels")
async def get_levels():
    return [
        {
            "level": level,
            "commission_rate": rate,
            "commission_percent": round(rate * 100, 1),
            "qualification_usd": threshold,
        }
        for level, threshold, rate in sorted(LEVEL_THRESHOLDS, key=lambda x: x[0])
    ]


@router.get("/global")
async def global_stats():
    pipeline = [
        {"$match": {"status": "completed"}},
        {"$group": {
            "_id": None,
            "tokens_sold": {"$sum": "$xfee_amount"},
            "total_purchases": {"$sum": 1},
        }},
    ]

    tokens_sold = 0
    total_purchases = 0

    async for doc in purchases_col().aggregate(pipeline):
        tokens_sold = doc.get("tokens_sold", 0)
        total_purchases = doc.get("total_purchases", 0)

    # When xfee_total_supply <= 0, treat the sale as uncapped (unlimited).
    tokens_remaining: Optional[int]
    total_supply: Optional[int]
    if settings.xfee_total_supply <= 0:
        tokens_remaining = None
        total_supply = None
    else:
        tokens_remaining = max(0, settings.xfee_total_supply - tokens_sold)
        total_supply = settings.xfee_total_supply

    try:
        sol_price = await get_sol_price()
    except Exception:
        sol_price = 0.0

    return {
        "tokens_sold": tokens_sold,
        "tokens_remaining": tokens_remaining,
        "total_supply": total_supply,
        "total_purchases": total_purchases,
        "sol_price": sol_price,
    }


@router.get("/power")
async def power_stats():
    """Total POWER distributed (and pending) from confirmed purchases.

    Only `status: "completed"` purchases are counted. A purchase contributes
    to `total_distributed` if its `power_distribution_status` is `staked` or
    `already_staked`; it contributes to `total_pending_delayed_stake` if it
    is `pending_delayed_stake` (POWER was deferred while distribution was
    disabled, to be staked later with an optional bonus multiplier).
    """
    pipeline = [
        {"$match": {"status": "completed"}},
        {
            "$group": {
                "_id": "$power_distribution_status",
                "count": {"$sum": 1},
                "power_total": {
                    "$sum": {
                        "$ifNull": ["$power_amount_staked", {"$ifNull": ["$power_base_amount", 0]}]
                    }
                },
            }
        },
    ]

    by_status: dict[str, dict[str, int]] = {}
    async for doc in purchases_col().aggregate(pipeline):
        status_key = doc.get("_id") or "unknown"
        by_status[str(status_key)] = {
            "count": int(doc.get("count") or 0),
            "power_total": int(doc.get("power_total") or 0),
        }

    distributed_keys = ("staked", "already_staked")
    pending_keys = ("pending_delayed_stake",)

    total_distributed = sum(by_status.get(k, {}).get("power_total", 0) for k in distributed_keys)
    distributed_purchase_count = sum(by_status.get(k, {}).get("count", 0) for k in distributed_keys)
    total_pending_delayed_stake = sum(by_status.get(k, {}).get("power_total", 0) for k in pending_keys)
    pending_purchase_count = sum(by_status.get(k, {}).get("count", 0) for k in pending_keys)

    return {
        "total_distributed": total_distributed,
        "distributed_purchase_count": distributed_purchase_count,
        "total_pending_delayed_stake": total_pending_delayed_stake,
        "pending_purchase_count": pending_purchase_count,
        "by_status": by_status,
        "power_stake_multiplier": POWER_STAKE_MULTIPLIER,
        "power_distribution_enabled": settings.power_distribution_enabled,
        "power_delayed_stake_bonus_multiplier": settings.power_delayed_stake_bonus_multiplier,
    }
