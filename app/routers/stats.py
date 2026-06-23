from typing import Optional

from fastapi import APIRouter

from app.config import settings
from app.database import purchases_col
from app.services.sol_price import get_sol_price
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
