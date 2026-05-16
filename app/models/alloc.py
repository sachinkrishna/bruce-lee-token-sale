from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class AllocResponse(BaseModel):
    id: str
    purchase_id: str
    recipient_wallet: str
    sol_amount: float
    sale_usd: float
    sale_tokens: int = 0
    alloc_type: str
    ancestor_level_tier: int
    differential_rate: float
    on_chain_tx: Optional[str] = None
    status: str
    indexed: bool = False
    created_at: datetime
