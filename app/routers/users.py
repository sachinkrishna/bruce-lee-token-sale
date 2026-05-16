import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from solders.pubkey import Pubkey

from app.config import settings
from app.database import relationship_tree_col, users_col
from app.models.user import TreeNode, UserRegisterRequest, UserResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/user", tags=["users"])


def validate_solana_pubkey(address: str) -> None:
    try:
        Pubkey.from_string(address)
    except Exception:
        raise HTTPException(status_code=400, detail=f"Invalid Solana address: {address}")


def _verify_signature(wallet_address: str, message: str, signature_b58: str) -> bool:
    """Verify that `message` was signed by the private key of `wallet_address`."""
    if settings.test_mode:
        return signature_b58 == "test_signature"
    try:
        import base58
        from nacl.signing import VerifyKey

        pubkey_bytes = base58.b58decode(wallet_address)
        sig_bytes = base58.b58decode(signature_b58)
        msg_bytes = message.encode("utf-8")

        verify_key = VerifyKey(pubkey_bytes)
        verify_key.verify(msg_bytes, sig_bytes)
        return True
    except Exception:
        return False


@router.post("/register")
async def register_user(req: UserRegisterRequest):
    validate_solana_pubkey(req.wallet_address)
    validate_solana_pubkey(req.referrer_wallet)

    existing = await users_col().find_one({"wallet_address": req.wallet_address})
    if existing:
        raise HTTPException(status_code=409, detail="Wallet already registered")

    is_master = req.referrer_wallet == settings.master_wallet_address
    if not is_master:
        referrer = await users_col().find_one({"wallet_address": req.referrer_wallet})
        if not referrer:
            raise HTTPException(status_code=400, detail="Referrer not found")
        if not referrer.get("is_valid_referrer", False):
            raise HTTPException(status_code=400, detail="Referrer has not completed a purchase yet")

    now = datetime.now(timezone.utc)

    user_doc = {
        "wallet_address": req.wallet_address,
        "referrer_wallet": req.referrer_wallet,
        "level": 1,
        "is_valid_referrer": False,
        "joined_at": now,
        "self_purchase": 0.0,
        "total_sales_usd": 0.0,
        "total_commission_sol": 0.0,
        "self_purchase_tokens": 0,
        "total_tokens_sold": 0,
        "level_sales": {},
        "level_commission": {},
        "direct_sales_sol": 0.0,
        "indirect_sales_sol": 0.0,
        "direct_commission_sol": 0.0,
        "indirect_commission_sol": 0.0,
        "direct_referral_count": 0,
        "network_size": 0,
    }
    await users_col().insert_one(user_doc)

    # Build relationship tree entry
    ancestors = []
    if not is_master:
        referrer_tree = await relationship_tree_col().find_one(
            {"wallet_address": req.referrer_wallet}
        )
        if referrer_tree:
            ancestors = [req.referrer_wallet] + referrer_tree.get("ancestors", [])
        else:
            ancestors = [req.referrer_wallet]
    else:
        ancestors = [req.referrer_wallet]

    tree_doc = {
        "wallet_address": req.wallet_address,
        "referrer_wallet": req.referrer_wallet,
        "ancestors": ancestors,
        "depth": len(ancestors),
    }
    await relationship_tree_col().insert_one(tree_doc)

    logger.info(f"Registered user {req.wallet_address} referred by {req.referrer_wallet}")
    return {"success": True, "wallet_address": req.wallet_address}


@router.get("/{wallet_address}", response_model=UserResponse)
async def get_user(wallet_address: str):
    validate_solana_pubkey(wallet_address)

    user = await users_col().find_one({"wallet_address": wallet_address})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return UserResponse(
        wallet_address=user["wallet_address"],
        referrer_wallet=user.get("referrer_wallet", ""),
        level=user.get("level", 1),
        self_purchase=user.get("self_purchase", 0.0),
        total_sales_usd=user.get("total_sales_usd", 0.0),
        total_commission_sol=user.get("total_commission_sol", 0.0),
        self_purchase_tokens=user.get("self_purchase_tokens", 0),
        total_tokens_sold=user.get("total_tokens_sold", 0),
        level_sales=user.get("level_sales", {}),
        level_commission=user.get("level_commission", {}),
        direct_sales_sol=user.get("direct_sales_sol", 0.0),
        indirect_sales_sol=user.get("indirect_sales_sol", 0.0),
        direct_commission_sol=user.get("direct_commission_sol", 0.0),
        indirect_commission_sol=user.get("indirect_commission_sol", 0.0),
        direct_referral_count=user.get("direct_referral_count", 0),
        network_size=user.get("network_size", 0),
        is_valid_referrer=user.get("is_valid_referrer", False),
        joined_at=user["joined_at"],
    )


@router.get("/{wallet_address}/directs", response_model=list[UserResponse])
async def get_direct_referrals(wallet_address: str):
    validate_solana_pubkey(wallet_address)

    user = await users_col().find_one({"wallet_address": wallet_address})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    directs = []
    async for u in users_col().find({"referrer_wallet": wallet_address}):
        directs.append(UserResponse(
            wallet_address=u["wallet_address"],
            referrer_wallet=u.get("referrer_wallet", ""),
            level=u.get("level", 1),
            self_purchase=u.get("self_purchase", 0.0),
            total_sales_usd=u.get("total_sales_usd", 0.0),
            total_commission_sol=u.get("total_commission_sol", 0.0),
            self_purchase_tokens=u.get("self_purchase_tokens", 0),
            total_tokens_sold=u.get("total_tokens_sold", 0),
            level_sales=u.get("level_sales", {}),
            level_commission=u.get("level_commission", {}),
            direct_sales_sol=u.get("direct_sales_sol", 0.0),
            indirect_sales_sol=u.get("indirect_sales_sol", 0.0),
            direct_commission_sol=u.get("direct_commission_sol", 0.0),
            indirect_commission_sol=u.get("indirect_commission_sol", 0.0),
            direct_referral_count=u.get("direct_referral_count", 0),
            network_size=u.get("network_size", 0),
            is_valid_referrer=u.get("is_valid_referrer", False),
            joined_at=u["joined_at"],
        ))

    return directs


@router.get("/{wallet_address}/tree")
async def get_user_tree(wallet_address: str):
    validate_solana_pubkey(wallet_address)

    user = await users_col().find_one({"wallet_address": wallet_address})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    tree = await _build_tree(wallet_address, user.get("level", 1), max_depth=5)
    return tree


async def _build_tree(wallet: str, level: int, max_depth: int, current_depth: int = 0) -> dict:
    node = {"wallet": wallet, "level": level, "children": []}

    if current_depth >= max_depth:
        return node

    children_cursor = relationship_tree_col().find({"referrer_wallet": wallet})
    async for child_tree in children_cursor:
        child_wallet = child_tree["wallet_address"]
        child_user = await users_col().find_one({"wallet_address": child_wallet})
        child_level = child_user.get("level", 1) if child_user else 1
        child_node = await _build_tree(child_wallet, child_level, max_depth, current_depth + 1)
        node["children"].append(child_node)

    return node
