from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class UserRegisterRequest(BaseModel):
    wallet_address: str
    referrer_wallet: str


class SetUserLevelRequest(BaseModel):
    wallet_address: str
    level: int
    signature: Optional[str] = None  # base58 or base64 encoded signature of "set-user-level:{wallet_address}:{level}" signed by master wallet


class UserResponse(BaseModel):
    wallet_address: str
    referrer_wallet: str = ""
    level: int = 1
    self_purchase: float = 0.0
    total_sales_usd: float = 0.0
    total_commission_sol: float = 0.0
    self_purchase_tokens: int = 0
    total_tokens_sold: int = 0
    level_sales: dict = Field(default_factory=dict)
    level_commission: dict = Field(default_factory=dict)
    direct_sales_sol: float = 0.0
    indirect_sales_sol: float = 0.0
    direct_commission_sol: float = 0.0
    indirect_commission_sol: float = 0.0
    direct_referral_count: int = 0
    network_size: int = 0
    is_valid_referrer: bool = False
    joined_at: datetime


class TreeNode(BaseModel):
    wallet: str
    level: int
    children: list["TreeNode"] = Field(default_factory=list)
