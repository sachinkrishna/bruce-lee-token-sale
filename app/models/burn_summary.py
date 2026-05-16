from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class BurnWalletSummary(BaseModel):
    wallet: str
    burn: float = Field(ge=0, description="Sum of ui_amount for this wallet")
    type: Literal["token_buy", "fee"]


class BurnSummaryResponse(BaseModel):
    summaries: list[BurnWalletSummary]


class BurnRecordItem(BaseModel):
    """Single document from the burns collection."""

    id: str
    wallet: str
    ui_amount: float
    timestamp: datetime
    type: Optional[Literal["token_buy", "fee"]] = None


class BurnRecentResponse(BaseModel):
    items: list[BurnRecordItem]
