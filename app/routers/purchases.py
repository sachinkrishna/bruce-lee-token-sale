import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from fastapi import APIRouter, HTTPException, Query
from solders.pubkey import Pubkey

from app.config import settings
from app.database import allocs_col, purchase_wallets_col, purchases_col, users_col
from app.services.solana_rpc import get_balance
from app.models.alloc import AllocResponse
from app.models.purchase import (
    PurchaseInitiateRequest,
    PurchaseInitiateResponse,
    PurchaseResponse,
)
from app.services.sol_price import get_sol_price
from app.services.wallet_pool import ensure_wallet_pool, lock_wallet
from app.tasks.poller import poll_purchase_wallet

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["purchases"])


def validate_solana_pubkey(address: str) -> None:
    try:
        Pubkey.from_string(address)
    except Exception:
        raise HTTPException(status_code=400, detail=f"Invalid Solana address: {address}")


@router.get("/purchase/estimate")
async def estimate_purchase(xfee_amount: int = Query(..., ge=1)):
    """Get estimated SOL needed for a purchase without creating one."""
    sol_price = await get_sol_price()
    # 1 XFEE = 1 USD (or 10 POWER)
    token_cost_usd = float(xfee_amount)

    if token_cost_usd < 26.0:
        gas_buffer = 0.20
    else:
        gas_buffer = round(random.uniform(2.0, 4.0), 2)
        
    usd_total = token_cost_usd + gas_buffer
    sol_needed = round(usd_total / sol_price, 6)

    return {
        "xfee_amount": xfee_amount,
        "token_cost_usd": token_cost_usd,
        "gas_buffer_usd": gas_buffer,
        "total_usd": usd_total,
        "sol_price_usd": round(sol_price, 2),
        "sol_needed": sol_needed,
    }


@router.post("/purchase/initiate", response_model=PurchaseInitiateResponse)
async def initiate_purchase(req: PurchaseInitiateRequest):
    validate_solana_pubkey(req.wallet_address)

    if req.xfee_amount <= 0:
        raise HTTPException(status_code=400, detail="xfee_amount must be positive")

    user = await users_col().find_one({"wallet_address": req.wallet_address})
    if not user:
        raise HTTPException(status_code=404, detail="User not registered")

    # One active pending purchase per wallet
    active = await purchases_col().find_one(
        {"user_wallet": req.wallet_address, "status": "pending"}
    )
    if active:
        raise HTTPException(
            status_code=409,
            detail="You already have an active pending purchase. Wait for 15 mintues and try again.",
        )

    sol_price = await get_sol_price()
    # 1 XFEE = 1 USD (or 10 POWER)
    token_cost_usd = float(req.xfee_amount)
    purchase_value_sol = token_cost_usd / sol_price

    if token_cost_usd < 26.0:
        gas_buffer_usd = 0.20
    else:
        gas_buffer_usd = 2.0
        try:
            buyer_balance_lamports = await get_balance(req.wallet_address)
            buyer_balance_sol = buyer_balance_lamports / 1e9
            if buyer_balance_sol >= purchase_value_sol + (4.0 / sol_price):
                gas_buffer_usd = round(random.uniform(2.0, 4.0), 2)
        except Exception:
            logger.warning(f"Could not read balance for {req.wallet_address}, using minimum gas buffer")

    sol_needed = (token_cost_usd + gas_buffer_usd) / sol_price

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=settings.purchase_wallet_expiry_minutes)

    purchase_doc = {
        "user_wallet": req.wallet_address,
        "purchase_wallet_pubkey": "",
        "xfee_amount": req.xfee_amount,
        "sol_amount_expected": round(sol_needed, 6),
        "purchase_value_sol": round(purchase_value_sol, 6),
        "gas_buffer_usd": gas_buffer_usd,
        "sol_amount_received": 0.0,
        "sol_price_at_confirmation": 0.0,
        "status": "pending",
        "created_at": now,
        "expires_at": expires_at,
        "confirmed_at": None,
        "token_dispatch_tx": None,
        "commission_distributed": False,
    }
    result = await purchases_col().insert_one(purchase_doc)
    purchase_id = result.inserted_id

    wallet = await lock_wallet(purchase_id)
    if not wallet:
        await purchases_col().delete_one({"_id": purchase_id})
        await ensure_wallet_pool()
        raise HTTPException(status_code=503, detail="No purchase wallets available, try again shortly")

    await purchases_col().update_one(
        {"_id": purchase_id},
        {"$set": {"purchase_wallet_pubkey": wallet["public_key"]}},
    )

    asyncio.create_task(
        poll_purchase_wallet(
            purchase_id=str(purchase_id),
            pubkey=wallet["public_key"],
            expected_sol=sol_needed,
            expires_at=expires_at,
        )
    )

    logger.info(
        f"Purchase initiated: {req.xfee_amount} XFEE for {req.wallet_address}, "
        f"wallet={wallet['public_key']}, sol_needed={sol_needed:.6f}"
    )

    return PurchaseInitiateResponse(
        purchase_id=str(purchase_id),
        purchase_wallet=wallet["public_key"],
        sol_expected=round(sol_needed, 6),
        expires_at=expires_at,
    )


@router.get("/purchase/{purchase_id}", response_model=PurchaseResponse)
async def get_purchase(purchase_id: str):
    try:
        pid = ObjectId(purchase_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid purchase ID")

    purchase = await purchases_col().find_one({"_id": pid})
    if not purchase:
        raise HTTPException(status_code=404, detail="Purchase not found")

    return PurchaseResponse(
        id=str(purchase["_id"]),
        user_wallet=purchase["user_wallet"],
        purchase_wallet_pubkey=purchase["purchase_wallet_pubkey"],
        xfee_amount=purchase["xfee_amount"],
        sol_amount_expected=purchase["sol_amount_expected"],
        sol_amount_received=purchase["sol_amount_received"],
        sol_price_at_confirmation=purchase["sol_price_at_confirmation"],
        status=purchase["status"],
        created_at=purchase["created_at"],
        expires_at=purchase["expires_at"],
        confirmed_at=purchase.get("confirmed_at"),
        token_dispatch_tx=purchase.get("token_dispatch_tx"),
        commission_distributed=purchase.get("commission_distributed", False),
    )


@router.get("/user/{wallet_address}/purchases")
async def get_user_purchases(
    wallet_address: str,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
):
    validate_solana_pubkey(wallet_address)

    skip = (page - 1) * limit
    cursor = (
        purchases_col()
        .find({"user_wallet": wallet_address})
        .sort("created_at", -1)
        .skip(skip)
        .limit(limit)
    )

    results = []
    async for p in cursor:
        results.append(
            PurchaseResponse(
                id=str(p["_id"]),
                user_wallet=p["user_wallet"],
                purchase_wallet_pubkey=p["purchase_wallet_pubkey"],
                xfee_amount=p["xfee_amount"],
                sol_amount_expected=p["sol_amount_expected"],
                sol_amount_received=p["sol_amount_received"],
                sol_price_at_confirmation=p["sol_price_at_confirmation"],
                status=p["status"],
                created_at=p["created_at"],
                expires_at=p["expires_at"],
                confirmed_at=p.get("confirmed_at"),
                token_dispatch_tx=p.get("token_dispatch_tx"),
                commission_distributed=p.get("commission_distributed", False),
            )
        )

    total = await purchases_col().count_documents({"user_wallet": wallet_address})

    return {"items": results, "total": total, "page": page, "limit": limit}


@router.get("/purchase-wallets/used")
async def get_used_purchase_wallets(
    recent_completed_hours: int = Query(
        24,
        ge=1,
        le=720,
        description=(
            "Rolling window (hours): sum remaining_balance_sol for purchase wallets in 'used' status "
            "whose purchase has status completed and confirmed_at in this window."
        ),
    ),
):
    """
    - s: total remaining SOL across all used purchase wallets (unchanged).
    - remaining_sol_recent_completed_purchases: sum of remaining_balance_sol on those used wallets
      whose purchase completed (confirmed_at) within the last `recent_completed_hours` hours.
    """
    pipeline_total = [
        {"$match": {"status": "used"}},
        {"$group": {"_id": 1, "s": {"$sum": {"$ifNull": ["$remaining_balance_sol", 0]}}}},
    ]

    total_s = 0.0
    async for doc in purchase_wallets_col().aggregate(pipeline_total):
        total_s = float(doc.get("s") or 0)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=recent_completed_hours)
    pipeline_recent = [
        {
            "$match": {
                "status": "completed",
                "confirmed_at": {"$ne": None, "$gte": cutoff},
                "purchase_wallet_pubkey": {"$exists": True, "$nin": [None, ""]},
            }
        },
        {
            "$lookup": {
                "from": "purchase_wallets",
                "let": {"pw": "$purchase_wallet_pubkey"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$public_key", "$$pw"]},
                                    {"$eq": ["$status", "used"]},
                                ],
                            },
                        },
                    },
                ],
                "as": "wallet",
            }
        },
        {"$unwind": {"path": "$wallet", "preserveNullAndEmptyArrays": False}},
        {
            "$group": {
                "_id": None,
                "remaining_sol_sum": {"$sum": {"$ifNull": ["$wallet.remaining_balance_sol", 0]}},
                "purchase_count": {"$sum": 1},
            }
        },
    ]

    recent_sum = 0.0
    recent_purchase_count = 0
    async for doc in purchases_col().aggregate(pipeline_recent):
        recent_sum = float(doc.get("remaining_sol_sum") or 0)
        recent_purchase_count = int(doc.get("purchase_count") or 0)

    return {
        "_id": 1,
        "s": total_s,
        "remaining_sol_recent_completed_purchases": recent_sum,
        "recent_completed_purchases_count": recent_purchase_count,
        "recent_window_hours": recent_completed_hours,
        "recent_cutoff_iso_utc": cutoff.isoformat(),
    }


@router.get("/user/{wallet_address}/allocs")
async def get_user_allocs(
    wallet_address: str,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
):
    validate_solana_pubkey(wallet_address)

    skip = (page - 1) * limit
    cursor = (
        allocs_col()
        .find({"recipient_wallet": wallet_address})
        .sort("created_at", -1)
        .skip(skip)
        .limit(limit)
    )

    results = []
    async for a in cursor:
        results.append(
            AllocResponse(
                id=str(a["_id"]),
                purchase_id=str(a["purchase_id"]),
                recipient_wallet=a["recipient_wallet"],
                sol_amount=a["sol_amount"],
                sale_usd=a.get("sale_usd", 0.0),
                sale_tokens=a.get("sale_tokens", 0),
                alloc_type=a["alloc_type"],
                ancestor_level_tier=a.get("ancestor_level_tier", 0),
                differential_rate=a.get("differential_rate", 0.0),
                on_chain_tx=a.get("on_chain_tx"),
                status=a["status"],
                indexed=a.get("indexed", False),
                created_at=a["created_at"],
            )
        )

    total = await allocs_col().count_documents({"recipient_wallet": wallet_address})

    return {"items": results, "total": total, "page": page, "limit": limit}
