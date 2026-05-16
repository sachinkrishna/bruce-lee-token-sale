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

    # Cap at 0: sales are not blocked at initiate; over-cap shows as 0 remaining
    tokens_remaining = max(0, settings.xfee_total_supply - tokens_sold)

    try:
        sol_price = await get_sol_price()
    except Exception:
        sol_price = 0.0

    return {
        "tokens_sold": tokens_sold,
        "tokens_remaining": tokens_remaining,
        "total_purchases": total_purchases,
        "sol_price": sol_price,
    }
