from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.config import MONGO_BURN_COLLECTION, settings

client: AsyncIOMotorClient = None  # type: ignore[assignment]
db: AsyncIOMotorDatabase = None  # type: ignore[assignment]


async def connect_db() -> None:
    global client, db
    client = AsyncIOMotorClient(settings.mongo_uri)
    db = client[settings.mongo_db_name]


async def close_db() -> None:
    global client
    if client:
        client.close()


async def ensure_indexes() -> None:
    await db.users.create_index("wallet_address", unique=True)
    await db.relationship_tree.create_index("wallet_address", unique=True)
    await db.relationship_tree.create_index("referrer_wallet")
    await db.relationship_tree.create_index("ancestors")
    await db.purchase_wallets.create_index("status")
    await db.purchases.create_index("status")
    await db.purchases.create_index("user_wallet")
    await db.allocs.create_index("recipient_wallet")
    await db.allocs.create_index("purchase_id")
    await db.allocs.create_index([("recipient_wallet", 1), ("indexed", 1)])
    await db.allocs.create_index(
        [("purchase_id", 1), ("recipient_wallet", 1), ("alloc_type", 1)],
        unique=True,
    )
    await db.transactions.create_index("purchase_id")
    await db.global_pools.create_index("pool_index", unique=True)
    await db.global_pools.create_index([("status", 1), ("end_at", 1)])
    await db.global_pools.create_index([("start_at", 1), ("end_at", 1)])
    await db.pool_points.create_index([("pool_id", 1), ("wallet_address", 1)], unique=True)
    await db.pool_points.create_index([("pool_index", 1), ("points_usd", -1)])
    await db.pool_points.create_index("wallet_address")
    await db[MONGO_BURN_COLLECTION].create_index("wallet")
    await db[MONGO_BURN_COLLECTION].create_index([("timestamp", -1)])


def users_col():
    return db.users


def purchase_wallets_col():
    return db.purchase_wallets


def purchases_col():
    return db.purchases


def allocs_col():
    return db.allocs


def relationship_tree_col():
    return db.relationship_tree


def transactions_col():
    return db.transactions


def global_pools_col():
    return db.global_pools


def pool_points_col():
    return db.pool_points


def burns_col():
    return db[MONGO_BURN_COLLECTION]


def system_meta_col():
    return db.system_meta
