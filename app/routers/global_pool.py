from datetime import datetime
from typing import Any, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, Header, HTTPException, Query

from app.config import settings
from app.database import allocs_col, global_pools_col, pool_points_col
from app.services.global_pool import (
    get_current_pool,
    get_global_pool_summary,
    get_pool_standings,
    get_pool_user_count,
    get_user_pool_entry,
    get_user_pool_history,
    list_pools,
    process_due_pools,
    settle_pool,
)
from app.services.sol_price import get_sol_price
from app.services.solana_rpc import get_balance


async def _sum_master_commissions_in_window(start_at: datetime, end_at: datetime) -> float:
    """Sum of master's `commission` alloc payouts within a time window.

    This represents funds that legitimately accrued to the funding wallet
    during the given pool's window, independent of the wallet's gross
    balance (which may include residuals from prior pools).
    """
    if not settings.master_wallet_address:
        return 0.0
    pipeline = [
        {
            "$match": {
                "recipient_wallet": settings.master_wallet_address,
                "alloc_type": "commission",
                "status": "sent",
                "created_at": {"$gte": start_at, "$lt": end_at},
            }
        },
        {"$group": {"_id": None, "total_sol": {"$sum": "$sol_amount"}}},
    ]
    async for doc in allocs_col().aggregate(pipeline):
        return float(doc.get("total_sol") or 0.0)
    return 0.0


def _jsonable(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


async def verify_admin_key(x_admin_key: str = Header(..., alias="X-Admin-Key")):
    if not settings.admin_api_key:
        raise HTTPException(status_code=500, detail="Admin API key not configured on server")
    if x_admin_key != settings.admin_api_key:
        raise HTTPException(status_code=401, detail="Invalid admin API key")


router = APIRouter(prefix="/api/v1/global-pool", tags=["global-pool"])
admin_router = APIRouter(
    prefix="/api/v1/admin/global-pool",
    tags=["admin", "global-pool"],
    dependencies=[Depends(verify_admin_key)],
)


@router.get("/summary")
async def global_pool_summary():
    return _jsonable(await get_global_pool_summary())


@router.get("/funds")
async def global_pool_funds():
    """Live snapshot of the current global pool.

    Pool-scoped numbers (`pool_collected_*`, `pool_points_usd`) measure what
    accrued during the current window — derived from master's `commission`
    allocs created within `[start_at, end_at)` and the pool doc's running
    `total_points_usd`. These are immune to wallet residuals from prior
    pools or unrelated transfers.

    Wallet-scoped numbers (`funding_wallet_balance_*`,
    `available_for_settlement_*`) are the live on-chain state and may
    therefore include carry-over from prior pools or external deposits.
    They reflect what would actually be paid out if the pool settled now.
    """
    funding_wallet = settings.master_wallet_address
    if not funding_wallet:
        raise HTTPException(
            status_code=500,
            detail="Global pool funding wallet not configured",
        )

    try:
        balance_lamports = await get_balance(funding_wallet)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to read on-chain balance: {exc}")
    balance_sol = balance_lamports / 1e9

    try:
        sol_price = await get_sol_price()
    except Exception:
        sol_price = 0.0
    balance_usd = balance_sol * sol_price

    buffer_sol = float(settings.global_pool_funding_buffer_sol)
    available_sol = max(0.0, balance_sol - buffer_sol)
    available_lamports = int(available_sol * 1e9)
    available_usd = available_sol * sol_price

    pool = await get_current_pool()
    if pool is None:
        return {
            "active": False,
            "pool": None,
            "pool_index": None,
            "start_at": None,
            "end_at": None,
            "pool_collected_lamports": 0,
            "pool_collected_sol": 0.0,
            "pool_collected_usd": 0.0,
            "pool_points_usd": 0.0,
            "pool_user_count": 0,
            "funding_wallet": funding_wallet,
            "funding_wallet_balance_lamports": balance_lamports,
            "funding_wallet_balance_sol": balance_sol,
            "funding_wallet_balance_usd": balance_usd,
            "funding_buffer_sol": buffer_sol,
            "available_for_settlement_lamports": available_lamports,
            "available_for_settlement_sol": available_sol,
            "available_for_settlement_usd": available_usd,
            "sol_price_usd": sol_price,
        }

    start_at = pool["start_at"]
    end_at = pool["end_at"]
    pool_index = int(pool["pool_index"])
    pool_collected_sol = await _sum_master_commissions_in_window(start_at, end_at)
    pool_collected_lamports = int(pool_collected_sol * 1e9)
    pool_collected_usd = pool_collected_sol * sol_price
    pool_points_usd = float(pool.get("total_points_usd", 0.0) or 0.0)
    pool_user_count = await get_pool_user_count(pool_index)

    return {
        "active": True,
        "pool": _jsonable(pool),
        "pool_index": pool_index,
        "start_at": _jsonable(start_at),
        "end_at": _jsonable(end_at),
        "pool_collected_lamports": pool_collected_lamports,
        "pool_collected_sol": pool_collected_sol,
        "pool_collected_usd": pool_collected_usd,
        "pool_points_usd": pool_points_usd,
        "pool_user_count": pool_user_count,
        "funding_wallet": funding_wallet,
        "funding_wallet_balance_lamports": balance_lamports,
        "funding_wallet_balance_sol": balance_sol,
        "funding_wallet_balance_usd": balance_usd,
        "funding_buffer_sol": buffer_sol,
        "available_for_settlement_lamports": available_lamports,
        "available_for_settlement_sol": available_sol,
        "available_for_settlement_usd": available_usd,
        "sol_price_usd": sol_price,
    }


@router.get("/")
async def list_global_pools(
    status: Optional[str] = Query(
        None,
        description="Filter by status: active | ready_to_finalize | finalizing | finalized",
    ),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=200),
):
    data = await list_pools(status=status, page=page, limit=limit)
    return _jsonable(data)


@router.get("/current")
async def current_global_pool(
    limit: int = Query(50, ge=1, le=500),
    page: int = Query(1, ge=1),
):
    pool = await get_current_pool()
    if not pool:
        return {"active": False, "pool": None, "standings": [], "total_users": 0}
    skip = (page - 1) * limit
    pool_index = int(pool["pool_index"])
    standings = await get_pool_standings(pool_index, limit=limit, skip=skip)
    total_users = await get_pool_user_count(pool_index)
    return {
        "active": True,
        "pool": _jsonable(pool),
        "standings": _jsonable(standings),
        "total_users": total_users,
        "page": page,
        "limit": limit,
    }


@router.get("/user/{wallet_address}/points")
async def user_global_pool_points(
    wallet_address: str,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=200),
):
    data = await get_user_pool_history(wallet_address, page=page, limit=limit)
    return {"wallet_address": wallet_address, **_jsonable(data)}


@router.get("/{pool_index}")
async def global_pool_by_index(
    pool_index: int,
    limit: int = Query(100, ge=1, le=1000),
    page: int = Query(1, ge=1),
):
    pool = await global_pools_col().find_one({"pool_index": pool_index})
    if not pool:
        raise HTTPException(status_code=404, detail="Global pool not found")
    skip = (page - 1) * limit
    standings = await get_pool_standings(pool_index, limit=limit, skip=skip)
    total_users = await get_pool_user_count(pool_index)
    return {
        "pool": _jsonable(pool),
        "standings": _jsonable(standings),
        "total_users": total_users,
        "page": page,
        "limit": limit,
    }


@router.get("/{pool_index}/user/{wallet_address}")
async def user_pool_entry(pool_index: int, wallet_address: str):
    pool = await global_pools_col().find_one({"pool_index": pool_index})
    if not pool:
        raise HTTPException(status_code=404, detail="Global pool not found")
    entry = await get_user_pool_entry(wallet_address, pool_index)
    if not entry:
        return {
            "pool": _jsonable(pool),
            "wallet_address": wallet_address,
            "in_pool": False,
            "entry": None,
        }
    return {
        "pool": _jsonable(pool),
        "wallet_address": wallet_address,
        "in_pool": True,
        "entry": _jsonable(entry),
    }


@admin_router.post("/{pool_index}/settle")
async def admin_settle_global_pool(
    pool_index: int,
    force: bool = Query(False, description="If true, end the active pool early and settle now."),
):
    """Settle (auto-pay) a global pool from the funding wallet.

    Safe to retry: idempotent per-user via the on-chain memo and DB state machine.
    """
    try:
        return _jsonable(await settle_pool(pool_index, force=force))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@admin_router.post("/process-due")
async def admin_process_due_global_pools():
    """Trigger the worker scan for all pools whose window has ended."""
    return _jsonable(await process_due_pools())
