"""Background repair: ensure completed purchases have on-chain POWER stake."""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

from solders.pubkey import Pubkey

from app.config import settings
from app.database import purchases_col, transactions_col
from app.services.staking_sdk import check_purchase_id, stake_with_purchase_id
from app.utils.economics import calculate_power_amount, is_power_bonus_eligible

logger = logging.getLogger(__name__)


async def stake_purchase_from_doc(
    purchase: dict,
    *,
    amount_override: Optional[int] = None,
    skip_if_staked: bool = True,
    pool_address: Optional[str] = None,
    rpc_url: Optional[str] = None,
) -> dict:
    """Same on-chain + DB updates as admin POST /staking/stake-purchase."""
    pid = purchase["_id"]
    purchase_id = str(pid)

    if not settings.power_distribution_enabled:
        return {
            "success": True,
            "skipped": True,
            "reason": "power_distribution_disabled",
            "purchase_id": purchase_id,
        }

    if not settings.master_wallet_private_key:
        return {"success": False, "error": "MASTER_WALLET_PRIVATE_KEY not configured", "purchase_id": purchase_id}

    pool = pool_address or settings.pool_address
    rpc = rpc_url or settings.quicknode_rpc_url
    if not pool or not rpc:
        return {"success": False, "error": "pool_address or rpc_url not configured", "purchase_id": purchase_id}

    try:
        Pubkey.from_string(pool)
        Pubkey.from_string(purchase["user_wallet"])
    except Exception as e:
        return {"success": False, "error": f"Invalid pool or user_wallet: {e}", "purchase_id": purchase_id}

    purchase_id_on_chain = str(pid)
    if skip_if_staked:
        already = await asyncio.to_thread(
            check_purchase_id,
            pool_address=pool,
            purchase_id=purchase_id_on_chain,
            rpc_url=rpc,
        )
        if already.get("staked"):
            return {
                "success": True,
                "skipped": True,
                "reason": "already_staked_on_chain",
                "check": already,
                "purchase_id": purchase_id,
            }

    xfee_amount = int(purchase.get("xfee_amount", 0))
    if xfee_amount <= 0:
        return {"success": False, "error": "invalid xfee_amount", "purchase_id": purchase_id}

    base_power_amount = calculate_power_amount(xfee_amount)
    bonus_multiplier = (
        settings.power_delayed_stake_bonus_multiplier
        if amount_override is None and is_power_bonus_eligible(purchase)
        else 1.0
    )
    power_amount = (
        int(amount_override)
        if amount_override is not None
        else calculate_power_amount(xfee_amount, bonus_multiplier)
    )
    bonus_applied = amount_override is None and bonus_multiplier != 1.0

    result = await asyncio.to_thread(
        stake_with_purchase_id,
        settings.master_wallet_private_key,
        pool,
        purchase["user_wallet"],
        power_amount,
        purchase_id_on_chain,
        rpc,
    )

    out: dict = {
        **result,
        "purchase_id": purchase_id,
        "power_base_amount": base_power_amount,
        "power_amount": power_amount,
        "power_bonus_multiplier": bonus_multiplier,
        "power_bonus_applied": bonus_applied,
    }

    if result.get("success"):
        logger.info(
            "Stake purchase %s: signature=%s amount=%s",
            purchase_id,
            result.get("signature"),
            power_amount,
        )

    if result.get("success") and result.get("signature"):
        await purchases_col().update_one(
            {"_id": pid},
            {
                "$set": {
                    "token_dispatch_tx": result["signature"],
                    "power_distribution_status": "staked",
                    "power_base_amount": base_power_amount,
                    "power_amount_staked": power_amount,
                    "power_bonus_multiplier": bonus_multiplier,
                    "power_bonus_applied": bonus_applied,
                    "power_staked_at": datetime.now(timezone.utc),
                }
            },
        )
        existing_tx = await transactions_col().find_one(
            {"purchase_id": pid, "tx_type": "power_stake"}
        )
        if not existing_tx:
            await transactions_col().insert_one(
                {
                    "purchase_id": pid,
                    "tx_type": "power_stake",
                    "from_wallet": settings.master_wallet_address,
                    "to_wallet": purchase["user_wallet"],
                    "amount_sol": 0.0,
                    "tx_signature": result["signature"],
                    "created_at": datetime.now(timezone.utc),
                }
            )
        out["purchase_updated"] = True
    elif result.get("already_staked"):
        await purchases_col().update_one(
            {"_id": pid},
            {
                "$set": {
                    "token_dispatch_tx": "already_staked",
                    "power_distribution_status": "already_staked",
                    "power_base_amount": base_power_amount,
                    "power_bonus_multiplier": bonus_multiplier,
                }
            },
        )
        out["purchase_updated"] = True

    return out


async def run_stake_repair_scan() -> dict:
    """
    Completed purchases with confirmed_at:
    - on/after stake_repair_since_unix (UTC), and
    - at least stake_repair_min_age_minutes in the past.
    For each: check on-chain stake; submit stake if missing (same as manual admin stake).
    """
    if settings.test_mode:
        return {"ran": False, "reason": "test_mode"}

    if not settings.power_distribution_enabled:
        return {"ran": False, "reason": "power_distribution_disabled"}

    if not settings.master_wallet_private_key or not settings.pool_address or not settings.quicknode_rpc_url:
        logger.warning("Stake repair skipped: missing master key, pool, or RPC")
        return {"ran": False, "reason": "not_configured"}

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=settings.stake_repair_min_age_minutes)
    since = datetime.fromtimestamp(settings.stake_repair_since_unix, tz=timezone.utc)
    query: dict = {
        "status": "completed",
        "confirmed_at": {"$ne": None, "$gte": since, "$lte": cutoff},
    }

    stats: Dict[str, Any] = {
        "ran": True,
        "checked": 0,
        "staked": 0,
        "skipped_already_staked": 0,
        "failed": 0,
        "errors": [],
    }

    cursor = (
        purchases_col()
        .find(query)
        .sort("confirmed_at", 1)
        .limit(settings.stake_repair_batch_size)
    )

    async for purchase in cursor:
        stats["checked"] += 1
        try:
            out = await stake_purchase_from_doc(purchase, skip_if_staked=True)
            if out.get("skipped") and out.get("reason") == "already_staked_on_chain":
                stats["skipped_already_staked"] += 1
            elif out.get("success") and out.get("signature"):
                stats["staked"] += 1
            elif out.get("already_staked"):
                stats["skipped_already_staked"] += 1
            elif out.get("success"):
                stats["staked"] += 1
            else:
                stats["failed"] += 1
                stats["errors"].append(
                    {"purchase_id": out.get("purchase_id"), "error": out.get("error", str(out))}
                )
        except Exception as e:
            stats["failed"] += 1
            stats["errors"].append({"purchase_id": str(purchase.get("_id")), "error": str(e)})
            logger.exception("Stake repair error for purchase %s", purchase.get("_id"))

    if stats["checked"]:
        logger.info(
            "Stake repair: checked=%s staked=%s skipped_already_staked=%s failed=%s",
            stats["checked"],
            stats["staked"],
            stats["skipped_already_staked"],
            stats["failed"],
        )

    return stats


async def stake_repair_worker_loop() -> None:
    """Periodic scan until cancelled (see main lifespan)."""
    while True:
        try:
            await run_stake_repair_scan()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Stake repair scan failed")
        await asyncio.sleep(settings.stake_repair_interval_seconds)
