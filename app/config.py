from pydantic_settings import BaseSettings

# Burn API — fixed in code (not env-overridable)
MONGO_BURN_COLLECTION = "burns"
BURN_TOKEN_BUY_WALLET = "AUswMSZNVDpTjv38kF7nWoLWjg66n2Fg6pD9LwfuFrAv"
BURN_FEE_WALLET = "4yji9nRqyjGwg8HkwsGRUM7tuxzjxX6Yia6sbjG3pfuu"


class Settings(BaseSettings):
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db_name: str = "xfee_sale"

    master_wallet_address: str = ""
    master_wallet_private_key: str = ""

    treasury_wallet_address: str = ""
    treasury_wallet_private_key: str = ""

    xfee_token_mint: str = ""
    pool_address: str = ""

    quicknode_rpc_url: str = ""
    sol_price_api_url: str = ""

    xfee_price_usd: float = 2.00
    xfee_total_supply: int = 400_000

    purchase_wallet_expiry_minutes: int = 15
    purchase_min_usd: float = 6.00
    gas_buffer_usd: float = 5.00
    leave_in_purchase_wallet_usd: float = 4.50

    admin_api_key: str = ""

    test_mode: bool = False
    test_sol_price: float = 150.0
    power_distribution_enabled: bool = True
    power_delayed_stake_bonus_multiplier: float = 1.0

    stake_repair_interval_seconds: int = 120
    stake_repair_min_age_minutes: int = 5
    stake_repair_batch_size: int = 100
    # Only repair purchases with confirmed_at >= this instant (UTC); aligns with staking audit default.
    stake_repair_since_unix: int = 1778346247

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
