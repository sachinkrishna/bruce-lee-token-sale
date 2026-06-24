import logging
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query
from solders.pubkey import Pubkey

from app.config import settings
from app.database import purchases_col, relationship_tree_col, users_col
from app.models.user import TreeNode, UserRegisterRequest, UserResponse
from app.utils.economics import POWER_STAKE_MULTIPLIER

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
    is_root_child_referrer = (
        bool(settings.root_child_wallet_address)
        and req.referrer_wallet == settings.root_child_wallet_address
    )
    if settings.enforce_root_child and settings.root_child_wallet_address:
        if is_master and req.wallet_address != settings.root_child_wallet_address:
            raise HTTPException(
                status_code=400,
                detail="Master wallet can only refer the configured root child wallet",
            )
        if is_root_child_referrer:
            direct_count = await relationship_tree_col().count_documents(
                {"referrer_wallet": settings.root_child_wallet_address}
            )
            if direct_count >= settings.root_child_max_direct_referrals:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Configured root child wallet has reached its direct referral limit "
                        f"({settings.root_child_max_direct_referrals})"
                    ),
                )

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
async def get_user_tree(
    wallet_address: str,
    max_depth: int = Query(
        50,
        ge=1,
        le=200,
        description="Maximum recursion depth. Default 50 (well above the 15-tier rank system).",
    ),
):
    validate_solana_pubkey(wallet_address)

    user = await users_col().find_one({"wallet_address": wallet_address})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    tree, truncated = await _build_tree(
        wallet_address,
        user.get("level", 1),
        max_depth=max_depth,
    )
    if truncated:
        tree["truncated"] = True
        tree["max_depth"] = max_depth
    return tree


async def _build_tree(
    root_wallet: str,
    root_level: int,
    max_depth: int,
) -> tuple[dict, bool]:
    """Build the full subtree under `root_wallet` using batched lookups.

    Three Mongo round-trips regardless of tree size:
      1. `relationship_tree` to fetch every descendant whose `ancestors`
         contains `root_wallet`, restricted to global depths in
         `(root_depth, root_depth + max_depth]`.
      2. `relationship_tree` count probe to detect any deeper descendant
         beyond `max_depth` so we can surface a truncation flag.
      3. `users` to fetch the `level` field for every wallet in the
         result set in a single $in query.

    The nested structure is then assembled in memory from the parent map.
    """
    root_tree = await relationship_tree_col().find_one({"wallet_address": root_wallet})
    root_depth = int(root_tree.get("depth", 0) or 0) if root_tree else 0
    max_global_depth = root_depth + max_depth

    descendants: list[dict] = []
    async for doc in relationship_tree_col().find(
        {
            "ancestors": root_wallet,
            "depth": {"$gt": root_depth, "$lte": max_global_depth},
        },
        {"wallet_address": 1, "referrer_wallet": 1, "depth": 1},
    ):
        descendants.append(doc)

    deeper_count = await relationship_tree_col().count_documents(
        {"ancestors": root_wallet, "depth": {"$gt": max_global_depth}},
        limit=1,
    )
    truncated = deeper_count > 0

    wallets = {root_wallet}
    wallets.update(d["wallet_address"] for d in descendants)
    levels: dict[str, int] = {root_wallet: int(root_level or 1)}
    if wallets:
        async for u in users_col().find(
            {"wallet_address": {"$in": list(wallets)}},
            {"wallet_address": 1, "level": 1},
        ):
            levels[u["wallet_address"]] = int(u.get("level", 1) or 1)

    children_map: dict[str, list[str]] = defaultdict(list)
    for d in descendants:
        parent = d.get("referrer_wallet")
        child = d.get("wallet_address")
        if parent and child:
            children_map[parent].append(child)

    def build_node(wallet: str) -> dict:
        return {
            "wallet": wallet,
            "level": levels.get(wallet, 1),
            "children": [build_node(c) for c in children_map.get(wallet, [])],
        }

    return build_node(root_wallet), truncated


_DISTRIBUTED_STATUSES = ("staked", "already_staked")
_PENDING_STATUSES = ("pending_delayed_stake",)


@router.get("/{wallet_address}/power")
async def get_user_power(
    wallet_address: str,
    include_purchases: bool = Query(
        True,
        description="If true, includes a per-purchase breakdown alongside aggregates.",
    ),
):
    """Total POWER for a wallet (distributed + pending), including bonuses.

    Sums across the user's own confirmed purchases:
      - `total_distributed` — POWER actually on-chain (staked or already_staked),
        with any delayed-stake bonus folded in via `power_amount_staked`.
      - `total_pending_delayed_stake` — POWER captured for purchases that
        completed while distribution was disabled; not yet on-chain. Bonus
        will be applied when these are eventually staked.
      - `total_bonus_applied` — distributed POWER attributable to bonus
        multipliers (= power_amount_staked - power_base_amount for purchases
        where the bonus has actually been applied).
    """
    validate_solana_pubkey(wallet_address)

    user = await users_col().find_one({"wallet_address": wallet_address})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    pipeline = [
        {"$match": {"user_wallet": wallet_address, "status": "completed"}},
        {
            "$group": {
                "_id": "$power_distribution_status",
                "count": {"$sum": 1},
                "power_total": {
                    "$sum": {
                        "$ifNull": [
                            "$power_amount_staked",
                            {"$ifNull": ["$power_base_amount", 0]},
                        ]
                    }
                },
                "base_total": {"$sum": {"$ifNull": ["$power_base_amount", 0]}},
            }
        },
    ]

    by_status: dict[str, dict[str, int]] = {}
    async for doc in purchases_col().aggregate(pipeline):
        status_key = str(doc.get("_id") or "unknown")
        power_total = int(doc.get("power_total") or 0)
        base_total = int(doc.get("base_total") or 0)
        bonus_total = max(0, power_total - base_total) if status_key in _DISTRIBUTED_STATUSES else 0
        by_status[status_key] = {
            "count": int(doc.get("count") or 0),
            "power_total": power_total,
            "base_total": base_total,
            "bonus_total": bonus_total,
        }

    total_distributed = sum(by_status.get(k, {}).get("power_total", 0) for k in _DISTRIBUTED_STATUSES)
    distributed_purchase_count = sum(by_status.get(k, {}).get("count", 0) for k in _DISTRIBUTED_STATUSES)
    total_bonus_applied = sum(by_status.get(k, {}).get("bonus_total", 0) for k in _DISTRIBUTED_STATUSES)
    total_pending_delayed_stake = sum(by_status.get(k, {}).get("power_total", 0) for k in _PENDING_STATUSES)
    pending_purchase_count = sum(by_status.get(k, {}).get("count", 0) for k in _PENDING_STATUSES)

    response: dict = {
        "wallet_address": wallet_address,
        "total_distributed": total_distributed,
        "distributed_purchase_count": distributed_purchase_count,
        "total_pending_delayed_stake": total_pending_delayed_stake,
        "pending_purchase_count": pending_purchase_count,
        "total_bonus_applied": total_bonus_applied,
        "by_status": by_status,
        "power_stake_multiplier": POWER_STAKE_MULTIPLIER,
        "power_distribution_enabled": settings.power_distribution_enabled,
        "power_delayed_stake_bonus_multiplier": settings.power_delayed_stake_bonus_multiplier,
    }

    if include_purchases:
        purchases: list[dict] = []
        cursor = (
            purchases_col()
            .find({"user_wallet": wallet_address, "status": "completed"})
            .sort("confirmed_at", -1)
        )
        async for p in cursor:
            staked = p.get("power_amount_staked")
            base = p.get("power_base_amount") or 0
            effective = staked if staked is not None else base
            bonus_applied = bool(p.get("power_bonus_applied"))
            bonus_portion = max(0, (staked or 0) - base) if bonus_applied else 0
            purchases.append(
                {
                    "purchase_id": str(p["_id"]),
                    "xfee_amount": int(p.get("xfee_amount", 0)),
                    "power_base_amount": int(base),
                    "power_amount_staked": int(staked) if staked is not None else None,
                    "power_effective": int(effective),
                    "power_bonus_multiplier": float(p.get("power_bonus_multiplier", 1.0) or 1.0),
                    "power_bonus_applied": bonus_applied,
                    "power_bonus_amount": int(bonus_portion),
                    "power_distribution_status": p.get("power_distribution_status"),
                    "token_dispatch_tx": p.get("token_dispatch_tx"),
                    "power_staked_at": p.get("power_staked_at"),
                    "confirmed_at": p.get("confirmed_at"),
                }
            )
        response["purchases"] = purchases

    return response
