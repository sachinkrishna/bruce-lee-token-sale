"""Public burn aggregates for fixed token_buy / fee vault wallets."""

from typing import Literal, Optional

from fastapi import APIRouter, Query

from app.config import BURN_FEE_WALLET, BURN_TOKEN_BUY_WALLET
from app.database import burns_col
from app.models.burn_summary import (
    BurnRecentResponse,
    BurnRecordItem,
    BurnSummaryResponse,
    BurnWalletSummary,
)

router = APIRouter(prefix="/api/v1/burn", tags=["burn"])


def _burn_kind(wallet: str) -> Optional[Literal["token_buy", "fee"]]:
    if wallet == BURN_TOKEN_BUY_WALLET:
        return "token_buy"
    if wallet == BURN_FEE_WALLET:
        return "fee"
    return None


@router.get("/summary", response_model=BurnSummaryResponse)
async def burn_summary():
    token_buy = BURN_TOKEN_BUY_WALLET
    fee_wallet = BURN_FEE_WALLET
    wallets = [token_buy, fee_wallet]

    pipeline = [
        {"$match": {"wallet": {"$in": wallets}}},
        {"$group": {"_id": "$wallet", "burn": {"$sum": "$ui_amount"}}},
    ]

    totals: dict[str, float] = {w: 0.0 for w in wallets}
    async for doc in burns_col().aggregate(pipeline):
        w = doc.get("_id")
        if isinstance(w, str):
            totals[w] = float(doc.get("burn") or 0.0)

    summaries = [
        BurnWalletSummary(
            wallet=token_buy,
            burn=totals.get(token_buy, 0.0),
            type="token_buy",
        ),
        BurnWalletSummary(
            wallet=fee_wallet,
            burn=totals.get(fee_wallet, 0.0),
            type="fee",
        ),
    ]
    return BurnSummaryResponse(summaries=summaries)


@router.get("/recent", response_model=BurnRecentResponse)
async def burn_recent(limit: int = Query(5, ge=1, le=50, description="Max items to return (newest first)")):
    cursor = burns_col().find({}).sort("timestamp", -1).limit(limit)
    items: list[BurnRecordItem] = []
    async for doc in cursor:
        w = doc.get("wallet") or ""
        if not isinstance(w, str):
            w = str(w)
        ts = doc.get("timestamp")
        if ts is None:
            continue
        items.append(
            BurnRecordItem(
                id=str(doc.get("_id", "")),
                wallet=w,
                ui_amount=float(doc.get("ui_amount") or 0.0),
                timestamp=ts,
                type=_burn_kind(w),
            )
        )
    return BurnRecentResponse(items=items)
