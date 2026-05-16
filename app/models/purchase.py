from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class PurchaseInitiateRequest(BaseModel):
    wallet_address: str
    xfee_amount: int


class PurchaseInitiateResponse(BaseModel):
    purchase_id: str
    purchase_wallet: str
    sol_expected: float
    expires_at: datetime


class PurchaseResponse(BaseModel):
    id: str
    user_wallet: str
    purchase_wallet_pubkey: str
    xfee_amount: int
    sol_amount_expected: float
    sol_amount_received: float
    sol_price_at_confirmation: float
    status: str
    created_at: datetime
    expires_at: datetime
    confirmed_at: Optional[datetime] = None
    token_dispatch_tx: Optional[str] = None
    commission_distributed: bool = False
