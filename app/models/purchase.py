from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class PurchaseInitiateRequest(BaseModel):
    wallet_address: str
    xfee_amount: int


class PurchaseInitiateResponse(BaseModel):
    purchase_id: str
    purchase_wallet: str
    payment_mode: str
    # SOL mode: total SOL to send. XFEE mode: SOL gas buffer to include on top.
    sol_expected: float
    expires_at: datetime
    # XFEE-mode only fields (None when payment_mode == "SOL")
    xfee_expected_raw: Optional[int] = None
    xfee_expected_ui: Optional[float] = None
    xfee_mint: Optional[str] = None
    xfee_decimals: Optional[int] = None
    xfee_price_at_initiate: Optional[float] = None
    # The Associated Token Account address on the ephemeral wallet that will
    # receive the XFEE. Wallets that don't auto-derive ATAs may send here
    # directly; most modern wallets derive from `purchase_wallet` + mint.
    xfee_destination_ata: Optional[str] = None


class PurchaseResponse(BaseModel):
    id: str
    user_wallet: str
    purchase_wallet_pubkey: str
    xfee_amount: int
    payment_mode: str
    sol_amount_expected: float
    sol_amount_received: float
    sol_price_at_confirmation: float
    xfee_amount_expected_raw: Optional[int] = None
    xfee_amount_received_raw: Optional[int] = None
    xfee_price_at_confirmation: Optional[float] = None
    status: str
    created_at: datetime
    expires_at: datetime
    confirmed_at: Optional[datetime] = None
    token_dispatch_tx: Optional[str] = None
    commission_distributed: bool = False
    founder_eligible: bool = False
    founder_eligible_at: Optional[datetime] = None
