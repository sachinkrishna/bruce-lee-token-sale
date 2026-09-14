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
        if existing.get("level", 1) != MAX_COMMISSION_LEVEL or not existing.get("is_valid_referrer", False):
            await users_col().update_one(
                {"wallet_address": settings.master_wallet_address},
                {"$set": {"level": MAX_COMMISSION_LEVEL, "is_valid_referrer": True}},
            )
            logger.info("Master wallet set to level %s", MAX_COMMISSION_LEVEL)
        logger.info(f"Master wallet exists: {settings.master_wallet_address}")

    if settings.enforce_root_child and settings.root_child_wallet_address:
        await _ensure_root_child()

    await _migrate_ladder_to_15_tier()
    await _migrate_remove_liquidity_wallet()

    try:
        from app.services.founder import ensure_founder_backfill

        await ensure_founder_backfill()
    except Exception:
        logger.exception("Founder backfill failed (non-fatal, will retry next start)")

    try:
        from app.services.node_number import ensure_node_number_backfill

        await ensure_node_number_backfill()
    except Exception:
        logger.exception("Node-number backfill failed (non-fatal, will retry next start)")

    try:
        from app.services.founder_onchain import ensure_founder_onchain_backfill

        await ensure_founder_onchain_backfill()
    except Exception:
        logger.exception(
            "Founder-onchain backfill failed (non-fatal; repair worker will still pick up eligible purchases)"
        )

    try:
        from app.services.power_entitlement import ensure_power_entitlement_backfill

        await ensure_power_entitlement_backfill()
    except Exception:
        logger.exception(
            "power_entitlement backfill failed (non-fatal; admin rerun endpoint can retry)"
        )

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

    founder_onchain_task = None
    if settings.founder_onchain_enabled and not settings.test_mode:
        from app.services.founder_onchain import founder_onchain_repair_worker_loop

        founder_onchain_task = asyncio.create_task(founder_onchain_repair_worker_loop())
        logger.info(
            "Founder-onchain repair worker started (interval=%ss; min age=%s min; since_unix=%s; pool=%s)",
            settings.founder_onchain_repair_interval_seconds,
            settings.founder_onchain_repair_min_age_minutes,
            settings.founder_onchain_repair_since_unix,
            settings.founder_onchain_pool_address or "<unset>",
        )
    elif not settings.founder_onchain_enabled:
        logger.info("Founder-onchain repair worker not started: FOUNDER_ONCHAIN_ENABLED=false")

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
    if founder_onchain_task:
        founder_onchain_task.cancel()
        try:
            await founder_onchain_task
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


async def _migrate_ladder_to_15_tier():
    """Shift the DB from the 16-tier ladder to the new 15-tier ladder.

    - Master is repositioned to the new max (L15) — the master-bootstrap block
      above has already handled this via MAX_COMMISSION_LEVEL, so this migration
      only cleans up any remaining stragglers (defensive) and demotes the
      root-child wallet based on its actual sales volume.
    - Root-child loses its reserved slot. Its DB record and downline stay
      intact; its `level` is recomputed from `total_sales_usd` so it re-enters
      the ranking system as a regular user.

    Idempotent via a marker doc in `system_meta`.
    """
    from datetime import datetime, timezone
    from app.utils.level import get_level_from_sales

    marker_id = "ladder_renumber_to_15_tier"
    marker = await system_meta_col().find_one({"_id": marker_id})
    if marker and marker.get("applied"):
        return

    # Master used to be at L16 under the 16-tier ladder; force it to the new
    # MAX (15). The master-bootstrap block only *upgrades* master, so it won't
    # step master down from 16 -> 15 on its own — we do that here.
    if settings.master_wallet_address:
        master_result = await users_col().update_one(
            {"wallet_address": settings.master_wallet_address},
            {"$set": {"level": MAX_COMMISSION_LEVEL, "is_valid_referrer": True}},
        )
        if master_result.modified_count:
            logger.info(
                "Ladder migration: master %s repositioned to new max L%s",
                settings.master_wallet_address, MAX_COMMISSION_LEVEL,
            )

    # Any *other* straggler at old L16 (shouldn't happen unless someone was
    # manually promoted there) gets recomputed from sales volume.
    stragglers = 0
    async for user in users_col().find(
        {"level": 16, "wallet_address": {"$ne": settings.master_wallet_address}}
    ):
        new_lvl = get_level_from_sales(float(user.get("total_sales_usd", 0.0) or 0.0))
        await users_col().update_one(
            {"wallet_address": user["wallet_address"]},
            {"$set": {"level": new_lvl}},
        )
        stragglers += 1
        logger.info(
            "Ladder migration: %s (was L16) recomputed to L%s from total_sales_usd=$%s",
            user["wallet_address"], new_lvl, user.get("total_sales_usd", 0.0),
        )

    # Root-child (if it exists) loses its reserved slot.
    root_child_addr = settings.root_child_wallet_address
    root_child_demoted = 0
    if root_child_addr and root_child_addr != settings.master_wallet_address:
        rc = await users_col().find_one({"wallet_address": root_child_addr})
        if rc and rc.get("level", 1) >= 14:
            new_lvl = get_level_from_sales(float(rc.get("total_sales_usd", 0.0) or 0.0))
            await users_col().update_one(
                {"wallet_address": root_child_addr},
                {"$set": {"level": new_lvl}},
            )
            root_child_demoted = 1
            logger.info(
                "Ladder migration: root-child %s demoted from L%s to L%s (total_sales_usd=$%s)",
                root_child_addr, rc.get("level"), new_lvl, rc.get("total_sales_usd", 0.0),
            )

    await system_meta_col().update_one(
        {"_id": marker_id},
        {
            "$set": {
                "applied": True,
                "applied_at": datetime.now(timezone.utc),
                "stragglers_recomputed": stragglers,
                "root_child_demoted": root_child_demoted,
            }
        },
        upsert=True,
    )
    logger.info(
        "Ladder migration to 15-tier applied (stragglers=%s, root_child_demoted=%s)",
        stragglers, root_child_demoted,
    )


# Wallets promoted alongside the liquidity-wallet removal (see
# `_migrate_remove_liquidity_wallet`). Hardcoded because this is a one-shot
# production migration for these specific wallets; the migration is
# marker-guarded so it can't re-run in another environment where these
# addresses may not exist (the update_one calls would just no-op).
_POST_LIQUIDITY_PROMOTIONS = [
    # (wallet_address, new_level, description)
    ("Ghq6fHG9H5bqPrhhXwqpnXf5DbxJV239geenCzGfNYWY", 14, "Ghq6fH takes over BRrtYf's L14 (95%) slot"),
    ("DjTmMTBnMbRqY4fdBumKkwoPsrNoaiKX5fSVeihgLv7R", 13, "DjTmMT promoted L12 -> L13 (50%)"),
]


async def _migrate_remove_liquidity_wallet():
    """Remove the liquidity/root-child wallet from the referral tree and
    promote the wallets that inherit its position.

    The ladder itself is unchanged (still 15 tiers with the same rates). What
    changes is *who occupies which slot*:

    - The wallet configured as `root_child` (BRrtYf in production) is
      reparented **out of the tree**:
        * Its `relationship_tree` doc is deleted so it no longer appears in
          `/user/{wallet}/tree`, ancestor walks, or commission distribution.
        * Its direct child (Ghq6fH in production) is reparented under master,
          and every descendant in that subtree has the liquidity wallet
          removed from its `ancestors` array and its `depth` decremented
          by one.
        * The user doc is kept for historical auditing (past commissions,
          founder flag, etc.), tagged with `removed_from_tree_at`.
    - Ghq6fH is promoted L13 -> L14 (takes over the retired liquidity slot's
      95% cumulative rate).
    - DjTmMT is promoted L12 -> L13 (fills the slot Ghq6fH vacated at 50%).
    - Any straggler still at L14 that isn't part of this promotion set gets
      recomputed from sales (defensive; BRrtYf itself would be the only such
      wallet in production).

    Idempotent via a marker doc in `system_meta`.
    """
    from datetime import datetime, timezone
    from app.utils.level import get_level_from_sales

    marker_id = "remove_liquidity_wallet_2026_09"
    marker = await system_meta_col().find_one({"_id": marker_id})
    if marker and marker.get("applied"):
        return

    now = datetime.now(timezone.utc)
    master_addr = settings.master_wallet_address

    # 1. Reparent the retired liquidity wallet out of the tree (if it exists
    #    and is not master).
    liquidity_addr = settings.root_child_wallet_address
    liquidity_reparented = 0
    subtree_updated = 0

    if liquidity_addr and liquidity_addr != master_addr:
        liquidity_tree = await relationship_tree_col().find_one(
            {"wallet_address": liquidity_addr}
        )

        if liquidity_tree is not None:
            # Descendants whose ancestor list includes the liquidity wallet.
            # These need liquidity_addr removed from `ancestors` and `depth -= 1`.
            async for doc in relationship_tree_col().find({"ancestors": liquidity_addr}):
                new_ancestors = [a for a in doc.get("ancestors", []) if a != liquidity_addr]
                new_depth = max(0, int(doc.get("depth", 0) or 0) - 1)
                update_fields = {"ancestors": new_ancestors, "depth": new_depth}
                # If this doc's *direct* parent was the liquidity wallet, that
                # direct child (Ghq6fH in production) becomes a direct child of
                # master. Every other descendant keeps its immediate referrer.
                if doc.get("referrer_wallet") == liquidity_addr:
                    update_fields["referrer_wallet"] = master_addr
                    await users_col().update_one(
                        {"wallet_address": doc["wallet_address"]},
                        {"$set": {"referrer_wallet": master_addr}},
                    )
                await relationship_tree_col().update_one(
                    {"_id": doc["_id"]},
                    {"$set": update_fields},
                )
                subtree_updated += 1

            # 2. Delete the liquidity wallet's tree entry so it disappears from
            #    tree walks and no longer receives commission allocs.
            await relationship_tree_col().delete_one({"wallet_address": liquidity_addr})
            liquidity_reparented = 1

            # 3. Mark the user doc for auditing; reset in-tree counters and
            #    demote the level (was L14, no longer occupies that slot).
            liq_user = await users_col().find_one({"wallet_address": liquidity_addr})
            new_liq_lvl = get_level_from_sales(
                float((liq_user or {}).get("total_sales_usd", 0.0) or 0.0)
            )
            await users_col().update_one(
                {"wallet_address": liquidity_addr},
                {
                    "$set": {
                        "removed_from_tree_at": now,
                        "direct_referral_count": 0,
                        "network_size": 0,
                        "level": new_liq_lvl,
                    }
                },
            )

            logger.info(
                "Liquidity-wallet removal: %s reparented out "
                "(subtree_updated=%s, level %s -> L%s)",
                liquidity_addr, subtree_updated,
                (liq_user or {}).get("level"), new_liq_lvl,
            )

    # 4. Refresh master's direct_referral_count and network_size after the
    #    reparent (a new direct child appeared under master).
    if master_addr:
        directs = await users_col().count_documents({"referrer_wallet": master_addr})
        network = await relationship_tree_col().count_documents({"ancestors": master_addr})
        await users_col().update_one(
            {"wallet_address": master_addr},
            {"$set": {"direct_referral_count": directs, "network_size": network}},
        )

    # 5. Promote the successor wallets. Only promotes (never demotes).
    promotions = []
    for addr, target_lvl, desc in _POST_LIQUIDITY_PROMOTIONS:
        user = await users_col().find_one({"wallet_address": addr})
        if not user:
            logger.info("Promotion skipped, wallet not present: %s (%s)", addr, desc)
            continue
        current_lvl = int(user.get("level", 1) or 1)
        if current_lvl >= target_lvl:
            logger.info(
                "Promotion no-op: %s already at L%s (target L%s) — %s",
                addr, current_lvl, target_lvl, desc,
            )
            continue
        await users_col().update_one(
            {"wallet_address": addr},
            {"$set": {"level": target_lvl}},
        )
        promotions.append({"wallet": addr, "from": current_lvl, "to": target_lvl})
        logger.info("Promoted %s: L%s -> L%s (%s)", addr, current_lvl, target_lvl, desc)

    # 6. Defensive: any straggler still at L14 that isn't master and isn't
    #    part of our promotion set gets recomputed from sales.
    promotion_addrs = {a for a, _, _ in _POST_LIQUIDITY_PROMOTIONS}
    exclude = {master_addr} | promotion_addrs
    stragglers_l14 = 0
    async for user in users_col().find(
        {"level": 14, "wallet_address": {"$nin": list(exclude)}}
    ):
        new_lvl = get_level_from_sales(float(user.get("total_sales_usd", 0.0) or 0.0))
        await users_col().update_one(
            {"wallet_address": user["wallet_address"]},
            {"$set": {"level": new_lvl}},
        )
        stragglers_l14 += 1
        logger.info(
            "Liquidity-wallet removal: straggler %s (L14) recomputed to L%s "
            "from total_sales_usd=$%s",
            user["wallet_address"], new_lvl, user.get("total_sales_usd", 0.0),
        )

    await system_meta_col().update_one(
        {"_id": marker_id},
        {
            "$set": {
                "applied": True,
                "applied_at": now,
                "liquidity_reparented": liquidity_reparented,
                "subtree_updated": subtree_updated,
                "promotions": promotions,
                "stragglers_l14": stragglers_l14,
            }
        },
        upsert=True,
    )
    logger.info(
        "Liquidity-wallet removal migration applied "
        "(reparented=%s, subtree_updated=%s, promotions=%s, stragglers_l14=%s)",
        liquidity_reparented, subtree_updated, len(promotions), stragglers_l14,
    )


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

from app.routers import admin, burns, founder, global_pool, nodes, purchases, stats, users

app.include_router(users.router)
app.include_router(purchases.router)
app.include_router(stats.router)
app.include_router(global_pool.router)
app.include_router(burns.router)
app.include_router(admin.router)
app.include_router(admin.public_admin_router)
app.include_router(global_pool.admin_router)
app.include_router(founder.router)
app.include_router(nodes.router)

if settings.test_mode:
    from app.routers import test
    app.include_router(test.router)


@app.get("/health")
async def health():
    return {"status": "ok"}
