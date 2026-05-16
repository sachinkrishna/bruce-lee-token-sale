import logging

from app.config import settings
from app.database import allocs_col, purchases_col, relationship_tree_col, users_col
from app.utils.level import get_level_from_sales

logger = logging.getLogger(__name__)


async def run_indexer_for_wallet(wallet_address: str) -> None:
    """
    Incrementally index unprocessed allocs for a wallet.
    Adds their sale_usd and commission amounts to the user's running totals,
    recomputes level, then marks the allocs as indexed.
    """
    unindexed = []
    async for alloc in allocs_col().find(
        {"recipient_wallet": wallet_address, "alloc_type": "commission", "indexed": False}
    ):
        unindexed.append(alloc)

    if not unindexed:
        logger.debug(f"No unindexed allocs for {wallet_address}")
        return

    inc_sales_usd = 0.0
    inc_commission_sol = 0.0
    inc_tokens_sold = 0
    alloc_ids = []

    for alloc in unindexed:
        alloc_ids.append(alloc["_id"])
        inc_sales_usd += alloc.get("sale_usd", 0.0)
        inc_tokens_sold += alloc.get("sale_tokens", 0)
        if alloc.get("status") == "sent":
            inc_commission_sol += alloc.get("sol_amount", 0.0)

    # Atomically increment running totals
    await users_col().update_one(
        {"wallet_address": wallet_address},
        {
            "$inc": {
                "total_sales_usd": inc_sales_usd,
                "total_commission_sol": inc_commission_sol,
                "total_tokens_sold": inc_tokens_sold,
            }
        },
    )

    # Mark all processed allocs as indexed
    await allocs_col().update_many(
        {"_id": {"$in": alloc_ids}},
        {"$set": {"indexed": True}},
    )

    # Recompute counts and level
    user = await users_col().find_one({"wallet_address": wallet_address})
    if user:
        computed_level = get_level_from_sales(user.get("total_sales_usd", 0.0))
        current_level = user.get("level", 1)
        new_level = max(current_level, computed_level)

        direct_referral_count = await users_col().count_documents({"referrer_wallet": wallet_address})
        network_size = await relationship_tree_col().count_documents({"ancestors": wallet_address})

        update_fields = {
            "direct_referral_count": direct_referral_count,
            "network_size": network_size,
        }
        if new_level != current_level:
            update_fields["level"] = new_level

        await users_col().update_one(
            {"wallet_address": wallet_address},
            {"$set": update_fields},
        )

    logger.info(
        f"Indexed {wallet_address}: +${inc_sales_usd:.2f} sales, "
        f"+{inc_commission_sol:.6f} SOL commission ({len(alloc_ids)} allocs)"
    )


async def run_indexer_batch(wallet_addresses: list) -> None:
    """
    Index unprocessed allocs for multiple wallets efficiently.
    Uses a single query to fetch all unindexed allocs, groups by wallet,
    then batch-updates users and marks allocs as indexed.
    """
    if not wallet_addresses:
        return

    unique_wallets = list(set(wallet_addresses))

    # Single query: fetch all unindexed allocs for all ancestors
    allocs_by_wallet = {}
    all_alloc_ids = []
    async for alloc in allocs_col().find(
        {"recipient_wallet": {"$in": unique_wallets}, "alloc_type": "commission", "indexed": False}
    ):
        wallet = alloc["recipient_wallet"]
        if wallet not in allocs_by_wallet:
            allocs_by_wallet[wallet] = {"inc_sales": 0.0, "inc_commission": 0.0, "inc_tokens": 0}
        allocs_by_wallet[wallet]["inc_sales"] += alloc.get("sale_usd", 0.0)
        allocs_by_wallet[wallet]["inc_tokens"] += alloc.get("sale_tokens", 0)
        if alloc.get("status") == "sent":
            allocs_by_wallet[wallet]["inc_commission"] += alloc.get("sol_amount", 0.0)
        all_alloc_ids.append(alloc["_id"])

    if not all_alloc_ids:
        return

    # Mark all allocs as indexed in one operation
    await allocs_col().update_many(
        {"_id": {"$in": all_alloc_ids}},
        {"$set": {"indexed": True}},
    )

    # Update each wallet's running totals
    for wallet, increments in allocs_by_wallet.items():
        await users_col().update_one(
            {"wallet_address": wallet},
            {"$inc": {
                "total_sales_usd": increments["inc_sales"],
                "total_commission_sol": increments["inc_commission"],
                "total_tokens_sold": increments["inc_tokens"],
            }},
        )

    # Batch-fetch all users to recompute levels
    users_map = {}
    async for user in users_col().find({"wallet_address": {"$in": unique_wallets}}):
        users_map[user["wallet_address"]] = user

    for wallet in unique_wallets:
        user = users_map.get(wallet)
        if not user:
            continue
        computed_level = get_level_from_sales(user.get("total_sales_usd", 0.0))
        current_level = user.get("level", 1)
        new_level = max(current_level, computed_level)

        direct_referral_count = await users_col().count_documents({"referrer_wallet": wallet})
        network_size = await relationship_tree_col().count_documents({"ancestors": wallet})

        update_fields = {
            "direct_referral_count": direct_referral_count,
            "network_size": network_size,
        }
        if new_level != current_level:
            update_fields["level"] = new_level

        await users_col().update_one(
            {"wallet_address": wallet},
            {"$set": update_fields},
        )

    logger.info(f"Batch indexed {len(allocs_by_wallet)} wallets ({len(all_alloc_ids)} allocs)")


async def run_self_purchase_index(wallet_address: str) -> None:
    """Recompute self_purchase and self_purchase_tokens from the user's own purchases."""
    self_purchase = 0.0
    self_purchase_tokens = 0
    async for p in purchases_col().find({"user_wallet": wallet_address, "status": "completed"}):
        self_purchase_tokens += p.get("xfee_amount", 0)
        self_purchase += float(p.get("xfee_amount", 0))

    await users_col().update_one(
        {"wallet_address": wallet_address},
        {"$set": {"self_purchase": self_purchase, "self_purchase_tokens": self_purchase_tokens}},
    )


async def reindex_full(wallet_address: str) -> None:
    """Full reindex for admin endpoint — resets totals from all allocs."""
    total_sales_usd = 0.0
    total_commission_sol = 0.0
    total_tokens_sold = 0

    async for alloc in allocs_col().find(
        {"recipient_wallet": wallet_address, "alloc_type": "commission"}
    ):
        total_sales_usd += alloc.get("sale_usd", 0.0)
        total_tokens_sold += alloc.get("sale_tokens", 0)
        if alloc.get("status") == "sent":
            total_commission_sol += alloc.get("sol_amount", 0.0)

    # Mark all allocs as indexed
    await allocs_col().update_many(
        {"recipient_wallet": wallet_address, "alloc_type": "commission"},
        {"$set": {"indexed": True}},
    )

    user = await users_col().find_one({"wallet_address": wallet_address})
    computed_level = get_level_from_sales(total_sales_usd)
    current_level = user.get("level", 1) if user else 1
    level = max(current_level, computed_level)

    direct_referral_count = await users_col().count_documents({"referrer_wallet": wallet_address})
    network_size = await relationship_tree_col().count_documents({"ancestors": wallet_address})

    await run_self_purchase_index(wallet_address)

    await users_col().update_one(
        {"wallet_address": wallet_address},
        {
            "$set": {
                "total_sales_usd": total_sales_usd,
                "total_commission_sol": total_commission_sol,
                "total_tokens_sold": total_tokens_sold,
                "level": level,
                "direct_referral_count": direct_referral_count,
                "network_size": network_size,
            }
        },
    )

    await reindex_level_stats(wallet_address)
    await reindex_dir_indir(wallet_address)

    logger.info(
        f"Full reindex {wallet_address}: sales=${total_sales_usd:.2f}, "
        f"commission={total_commission_sol:.6f} SOL, L{level}, "
        f"referrals={direct_referral_count}, network={network_size}"
    )


async def run_level_index_for_wallet(wallet_address: str) -> None:
    """Incrementally index allocs by ancestor_level_tier into level_sales and level_commission."""
    unindexed = []
    async for alloc in allocs_col().find(
        {"recipient_wallet": wallet_address, "alloc_type": "commission", "level_indexed": {"$ne": True}}
    ):
        unindexed.append(alloc)

    if not unindexed:
        return

    inc_by_level = {}
    alloc_ids = []

    for alloc in unindexed:
        alloc_ids.append(alloc["_id"])
        lvl = str(alloc.get("ancestor_level_tier", 1))
        if lvl not in inc_by_level:
            inc_by_level[lvl] = {"sales": 0.0, "commission": 0.0}
        inc_by_level[lvl]["sales"] += alloc.get("sale_sol", 0.0)
        if alloc.get("status") == "sent":
            inc_by_level[lvl]["commission"] += alloc.get("sol_amount", 0.0)

    inc_update = {}
    for lvl, vals in inc_by_level.items():
        inc_update[f"level_sales.{lvl}"] = vals["sales"]
        inc_update[f"level_commission.{lvl}"] = vals["commission"]

    if inc_update:
        await users_col().update_one(
            {"wallet_address": wallet_address},
            {"$inc": inc_update},
        )

    await allocs_col().update_many(
        {"_id": {"$in": alloc_ids}},
        {"$set": {"level_indexed": True}},
    )

    logger.info(f"Level-indexed {wallet_address}: {len(alloc_ids)} allocs across levels {list(inc_by_level.keys())}")


async def run_level_index_batch(wallet_addresses: list) -> None:
    """Batch level-index allocs for multiple wallets."""
    if not wallet_addresses:
        return

    unique_wallets = list(set(wallet_addresses))

    by_wallet = {}
    all_alloc_ids = []
    async for alloc in allocs_col().find(
        {"recipient_wallet": {"$in": unique_wallets}, "alloc_type": "commission", "level_indexed": {"$ne": True}}
    ):
        wallet = alloc["recipient_wallet"]
        if wallet not in by_wallet:
            by_wallet[wallet] = {}
        lvl = str(alloc.get("ancestor_level_tier", 1))
        if lvl not in by_wallet[wallet]:
            by_wallet[wallet][lvl] = {"sales": 0.0, "commission": 0.0}
        by_wallet[wallet][lvl]["sales"] += alloc.get("sale_sol", 0.0)
        if alloc.get("status") == "sent":
            by_wallet[wallet][lvl]["commission"] += alloc.get("sol_amount", 0.0)
        all_alloc_ids.append(alloc["_id"])

    if not all_alloc_ids:
        return

    await allocs_col().update_many(
        {"_id": {"$in": all_alloc_ids}},
        {"$set": {"level_indexed": True}},
    )

    for wallet, levels in by_wallet.items():
        inc_update = {}
        for lvl, vals in levels.items():
            inc_update[f"level_sales.{lvl}"] = vals["sales"]
            inc_update[f"level_commission.{lvl}"] = vals["commission"]
        if inc_update:
            await users_col().update_one(
                {"wallet_address": wallet},
                {"$inc": inc_update},
            )

    logger.info(f"Batch level-indexed {len(by_wallet)} wallets ({len(all_alloc_ids)} allocs)")


async def reindex_level_stats(wallet_address: str) -> None:
    """Full recompute of level_sales and level_commission from all allocs."""
    level_sales = {}
    level_commission = {}

    async for alloc in allocs_col().find(
        {"recipient_wallet": wallet_address, "alloc_type": "commission"}
    ):
        lvl = str(alloc.get("ancestor_level_tier", 1))
        level_sales[lvl] = level_sales.get(lvl, 0.0) + alloc.get("sale_sol", 0.0)
        if alloc.get("status") == "sent":
            level_commission[lvl] = level_commission.get(lvl, 0.0) + alloc.get("sol_amount", 0.0)

    await allocs_col().update_many(
        {"recipient_wallet": wallet_address, "alloc_type": "commission"},
        {"$set": {"level_indexed": True}},
    )

    await users_col().update_one(
        {"wallet_address": wallet_address},
        {"$set": {"level_sales": level_sales, "level_commission": level_commission}},
    )


async def run_dir_indir_index_for_wallet(wallet_address: str) -> None:
    """Incrementally index allocs into direct/indirect sales and commission."""
    unindexed = []
    async for alloc in allocs_col().find(
        {"recipient_wallet": wallet_address, "alloc_type": "commission", "dir_indir_indexed": {"$ne": True}}
    ):
        unindexed.append(alloc)

    if not unindexed:
        return

    inc_direct_sales = 0.0
    inc_indirect_sales = 0.0
    inc_direct_commission = 0.0
    inc_indirect_commission = 0.0
    alloc_ids = []

    for alloc in unindexed:
        alloc_ids.append(alloc["_id"])
        sale_sol = alloc.get("sale_sol", 0.0)
        commission = alloc.get("sol_amount", 0.0) if alloc.get("status") == "sent" else 0.0
        if alloc.get("is_direct_sale"):
            inc_direct_sales += sale_sol
            inc_direct_commission += commission
        else:
            inc_indirect_sales += sale_sol
            inc_indirect_commission += commission

    await users_col().update_one(
        {"wallet_address": wallet_address},
        {"$inc": {
            "direct_sales_sol": inc_direct_sales,
            "indirect_sales_sol": inc_indirect_sales,
            "direct_commission_sol": inc_direct_commission,
            "indirect_commission_sol": inc_indirect_commission,
        }},
    )

    await allocs_col().update_many(
        {"_id": {"$in": alloc_ids}},
        {"$set": {"dir_indir_indexed": True}},
    )

    logger.info(
        f"Dir/indir indexed {wallet_address}: "
        f"+{inc_direct_sales:.6f}/{inc_indirect_sales:.6f} sales, "
        f"+{inc_direct_commission:.6f}/{inc_indirect_commission:.6f} commission "
        f"({len(alloc_ids)} allocs)"
    )


async def run_dir_indir_index_batch(wallet_addresses: list) -> None:
    """Batch dir/indir index allocs for multiple wallets."""
    if not wallet_addresses:
        return

    unique_wallets = list(set(wallet_addresses))

    by_wallet = {}
    all_alloc_ids = []
    async for alloc in allocs_col().find(
        {"recipient_wallet": {"$in": unique_wallets}, "alloc_type": "commission", "dir_indir_indexed": {"$ne": True}}
    ):
        wallet = alloc["recipient_wallet"]
        if wallet not in by_wallet:
            by_wallet[wallet] = {"direct_sales": 0.0, "indirect_sales": 0.0, "direct_commission": 0.0, "indirect_commission": 0.0}
        sale_sol = alloc.get("sale_sol", 0.0)
        commission = alloc.get("sol_amount", 0.0) if alloc.get("status") == "sent" else 0.0
        if alloc.get("is_direct_sale"):
            by_wallet[wallet]["direct_sales"] += sale_sol
            by_wallet[wallet]["direct_commission"] += commission
        else:
            by_wallet[wallet]["indirect_sales"] += sale_sol
            by_wallet[wallet]["indirect_commission"] += commission
        all_alloc_ids.append(alloc["_id"])

    if not all_alloc_ids:
        return

    await allocs_col().update_many(
        {"_id": {"$in": all_alloc_ids}},
        {"$set": {"dir_indir_indexed": True}},
    )

    for wallet, vals in by_wallet.items():
        await users_col().update_one(
            {"wallet_address": wallet},
            {"$inc": {
                "direct_sales_sol": vals["direct_sales"],
                "indirect_sales_sol": vals["indirect_sales"],
                "direct_commission_sol": vals["direct_commission"],
                "indirect_commission_sol": vals["indirect_commission"],
            }},
        )

    logger.info(f"Batch dir/indir indexed {len(by_wallet)} wallets ({len(all_alloc_ids)} allocs)")


async def reindex_dir_indir(wallet_address: str) -> None:
    """Full recompute of direct/indirect sales and commission from all allocs."""
    direct_sales_sol = 0.0
    indirect_sales_sol = 0.0
    direct_commission_sol = 0.0
    indirect_commission_sol = 0.0

    async for alloc in allocs_col().find(
        {"recipient_wallet": wallet_address, "alloc_type": "commission"}
    ):
        sale_sol = alloc.get("sale_sol", 0.0)
        commission = alloc.get("sol_amount", 0.0) if alloc.get("status") == "sent" else 0.0
        if alloc.get("is_direct_sale"):
            direct_sales_sol += sale_sol
            direct_commission_sol += commission
        else:
            indirect_sales_sol += sale_sol
            indirect_commission_sol += commission

    await allocs_col().update_many(
        {"recipient_wallet": wallet_address, "alloc_type": "commission"},
        {"$set": {"dir_indir_indexed": True}},
    )

    await users_col().update_one(
        {"wallet_address": wallet_address},
        {"$set": {
            "direct_sales_sol": direct_sales_sol,
            "indirect_sales_sol": indirect_sales_sol,
            "direct_commission_sol": direct_commission_sol,
            "indirect_commission_sol": indirect_commission_sol,
        }},
    )
