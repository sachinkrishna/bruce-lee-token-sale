import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import (
    close_db,
    connect_db,
    ensure_indexes,
    purchases_col,
    relationship_tree_col,
    system_meta_col,
    users_col,
)
from app.services.solana_rpc import (
    close_http_client,
    get_token_account_balance,
    init_http_client,
    rpc_request,
)
from app.utils.level import MAX_COMMISSION_LEVEL
from app.services.wallet_pool import ensure_wallet_pool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting XFEE Sale Backend...")

    await connect_db()
    await ensure_indexes()
    logger.info("MongoDB connected, indexes ensured")

    await init_http_client()
    logger.info("HTTP client initialized")

    from datetime import datetime, timezone

    await _migrate_levels_to_16_tier()

    existing = await users_col().find_one({"wallet_address": settings.master_wallet_address})
    if not existing:
        await users_col().insert_one({
            "wallet_address": settings.master_wallet_address,
            "referrer_wallet": "",
            "level": MAX_COMMISSION_LEVEL,
            "is_valid_referrer": True,
            "joined_at": datetime.now(timezone.utc),
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
        })
        logger.info(f"Master wallet created: {settings.master_wallet_address}")
    else:
        if existing.get("level", 1) < MAX_COMMISSION_LEVEL or not existing.get("is_valid_referrer", False):
            await users_col().update_one(
                {"wallet_address": settings.master_wallet_address},
                {"$set": {"level": MAX_COMMISSION_LEVEL, "is_valid_referrer": True}},
            )
            logger.info("Master wallet promoted to level %s", MAX_COMMISSION_LEVEL)
        logger.info(f"Master wallet exists: {settings.master_wallet_address}")

    if settings.root_child_wallet_address:
        await _ensure_root_child()

    await ensure_wallet_pool()
    logger.info("Purchase wallet pool checked")

    if settings.test_mode:
        logger.info("*** TEST MODE ENABLED — Solana calls are mocked ***")

    # Verify treasury XFEE balance
    try:
        from spl.token.instructions import get_associated_token_address
        from solders.pubkey import Pubkey

        treasury_pubkey = Pubkey.from_string(settings.treasury_wallet_address)
        mint_pubkey = Pubkey.from_string(settings.xfee_token_mint)
        treasury_ata = get_associated_token_address(treasury_pubkey, mint_pubkey)
        balance = await get_token_account_balance(str(treasury_ata))
        if balance < 10_000:
            logger.warning(f"Treasury XFEE balance low: {balance}")
        else:
            logger.info(f"Treasury XFEE balance: {balance}")
    except Exception:
        logger.warning("Could not verify treasury XFEE balance (check config)")

    # Log global stats
    try:
        pipeline = [
            {"$match": {"status": "completed"}},
            {"$group": {"_id": None, "total": {"$sum": "$xfee_amount"}}},
        ]
        tokens_sold = 0
        async for doc in purchases_col().aggregate(pipeline):
            tokens_sold = doc.get("total", 0)
        if settings.xfee_total_supply <= 0:
            logger.info(f"Global stats: {tokens_sold} XFEE sold (unlimited supply)")
        else:
            remaining = max(0, settings.xfee_total_supply - tokens_sold)
            logger.info(f"Global stats: {tokens_sold} XFEE sold, {remaining} remaining (cap-clamped)")
    except Exception:
        logger.warning("Could not load global stats")

    # Recover any pending purchases from before restart
    await _recover_pending_purchases()

    logger.info("XFEE Sale Backend ready")

    repair_task = None
    if settings.power_distribution_enabled and not settings.test_mode:
        from app.services.stake_repair import stake_repair_worker_loop

        repair_task = asyncio.create_task(stake_repair_worker_loop())
        logger.info(
            "Stake repair worker started (every %ss; min purchase age %s min; since_unix %s)",
            settings.stake_repair_interval_seconds,
            settings.stake_repair_min_age_minutes,
            settings.stake_repair_since_unix,
        )
    elif not settings.power_distribution_enabled:
        logger.warning("Stake repair worker not started: POWER distribution disabled")

    global_pool_task = None
    if settings.global_pool_enabled and not settings.test_mode:
        from app.services.global_pool import global_pool_worker_loop

        global_pool_task = asyncio.create_task(global_pool_worker_loop())
        logger.info(
            "Global pool worker started (duration=%s days; interval=%ss)",
            settings.global_pool_duration_days,
            settings.global_pool_finalize_interval_seconds,
        )

    yield

    if repair_task:
        repair_task.cancel()
        try:
            await repair_task
        except asyncio.CancelledError:
            pass
    if global_pool_task:
        global_pool_task.cancel()
        try:
            await global_pool_task
        except asyncio.CancelledError:
            pass

    logger.info("Shutting down...")
    await close_http_client()
    await close_db()
    logger.info("Shutdown complete")


async def _migrate_levels_to_16_tier():
    """Shift any users from the old 15-tier ranking to the new 16-tier ranking.

    Idempotent. A marker doc in `system_meta` prevents re-application.
    Shift in descending order so we never double-bump the same record.
    """
    from datetime import datetime, timezone

    marker_id = "level_renumber_to_16_tier"
    marker = await system_meta_col().find_one({"_id": marker_id})
    if marker and marker.get("applied"):
        return

    total_shifted = 0
    for old, new in [(15, 16), (14, 15), (13, 14), (12, 13)]:
        result = await users_col().update_many(
            {"level": old},
            {"$set": {"level": new}},
        )
        if result.modified_count:
            logger.info(
                "Level migration: shifted %s user(s) from L%s to L%s",
                result.modified_count, old, new,
            )
            total_shifted += result.modified_count

    await system_meta_col().update_one(
        {"_id": marker_id},
        {"$set": {"applied": True, "applied_at": datetime.now(timezone.utc), "total_shifted": total_shifted}},
        upsert=True,
    )
    logger.info("Level migration to 16-tier applied (total shifted: %s)", total_shifted)


async def _ensure_root_child():
    """Ensure the configured single root child exists under the master wallet."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    target_level = settings.root_child_level
    expected_min = MAX_COMMISSION_LEVEL - 1
    if target_level < expected_min:
        logger.warning(
            "ROOT_CHILD_LEVEL=%s is below MAX_COMMISSION_LEVEL-1=%s; clamping to %s. "
            "Update the env var to silence this warning.",
            target_level, expected_min, expected_min,
        )
        target_level = expected_min

    existing_child = await users_col().find_one({"wallet_address": settings.root_child_wallet_address})
    base_fields = {
        "wallet_address": settings.root_child_wallet_address,
        "referrer_wallet": settings.master_wallet_address,
        "level": target_level,
        "is_valid_referrer": True,
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
    if not existing_child:
        await users_col().insert_one(base_fields)
        logger.info("Configured root child created: %s", settings.root_child_wallet_address)
    else:
        await users_col().update_one(
            {"wallet_address": settings.root_child_wallet_address},
            {
                "$set": {
                    "referrer_wallet": settings.master_wallet_address,
                    "level": target_level,
                    "is_valid_referrer": True,
                }
            },
        )
        logger.info("Configured root child ensured: %s", settings.root_child_wallet_address)

    await relationship_tree_col().update_one(
        {"wallet_address": settings.root_child_wallet_address},
        {
            "$set": {
                "wallet_address": settings.root_child_wallet_address,
                "referrer_wallet": settings.master_wallet_address,
                "ancestors": [settings.master_wallet_address],
                "depth": 1,
            }
        },
        upsert=True,
    )


async def _recover_pending_purchases():
    """Re-launch pollers or immediately process any pending purchases surviving a restart."""
    import asyncio
    from datetime import datetime, timezone
    from app.services.purchase_flow import process_completed_purchase
    from app.services.solana_rpc import get_balance
    from app.tasks.poller import poll_purchase_wallet

    now = datetime.now(timezone.utc)
    recovered = 0

    cursor = purchases_col().find({"status": "pending"})
    async for purchase in cursor:
        pid = str(purchase["_id"])
        pubkey = purchase.get("purchase_wallet_pubkey")
        expires_at = purchase.get("expires_at")
        if expires_at and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        if not pubkey:
            continue

        try:
            balance_lamports = await get_balance(pubkey)
            balance_sol = balance_lamports / 1e9

            expected_sol = purchase.get("sol_amount_expected", 0.0)
            if balance_sol >= expected_sol * 0.95:
                logger.info(f"Startup recovery: processing {pid} ({balance_sol:.6f} SOL found, expected {expected_sol:.6f})")
                await process_completed_purchase(pid, balance_sol)
                recovered += 1
            elif expires_at and expires_at > now:
                logger.info(f"Startup recovery: re-polling {pid} (expires {expires_at.isoformat()})")
                asyncio.create_task(
                    poll_purchase_wallet(
                        purchase_id=pid,
                        pubkey=pubkey,
                        expected_sol=purchase["sol_amount_expected"],
                        expires_at=expires_at,
                    )
                )
                recovered += 1
            else:
                logger.info(f"Startup recovery: expiring {pid}")
                try:
                    from app.database import purchase_wallets_col
                    await purchase_wallets_col().update_one(
                        {"public_key": pubkey},
                        {"$set": {"remaining_balance_sol": balance_sol}},
                    )
                except Exception:
                    logger.exception(f"Failed to record remaining balance for {pubkey}")
                await purchases_col().update_one(
                    {"_id": purchase["_id"]},
                    {"$set": {"status": "expired"}},
                )
        except Exception:
            logger.exception(f"Startup recovery failed for {pid}")

    if recovered:
        logger.info(f"Startup recovery: handled {recovered} pending purchase(s)")


app = FastAPI(
    title="XFEE Token Sale API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from app.routers import admin, burns, global_pool, purchases, stats, users

app.include_router(users.router)
app.include_router(purchases.router)
app.include_router(stats.router)
app.include_router(global_pool.router)
app.include_router(burns.router)
app.include_router(admin.router)
app.include_router(admin.public_admin_router)
app.include_router(global_pool.admin_router)

if settings.test_mode:
    from app.routers import test
    app.include_router(test.router)


@app.get("/health")
async def health():
    return {"status": "ok"}
