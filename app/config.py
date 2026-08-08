from pydantic_settings import BaseSettings

# Burn collection name — fixed in code (not env-overridable).
# The two wallets scanned by /api/v1/burn/* are configurable via env below.
MONGO_BURN_COLLECTION = "burns"

# Default on-chain program ID for the POWER staking program. Override via
# STAKING_PROGRAM_ID env var when deploying against a different program.
DEFAULT_STAKING_PROGRAM_ID = "EX7YLYMv9pjarwgFF8JN5kwSohuhgVVmTDfD31ETekBC"


class Settings(BaseSettings):
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db_name: str = "xfee_sol_sale"

    master_wallet_address: str = ""
    master_wallet_private_key: str = ""
    root_child_wallet_address: str = ""
    root_child_level: int = 14
    root_child_max_direct_referrals: int = 1
    # Enforce that master can only refer the root child and the root child has at
    # most `root_child_max_direct_referrals` direct referrals. When False (the
    # current default) the master accepts direct referrals from anyone and the
    # root-child wallet is treated as a normal user.
    enforce_root_child: bool = False

    treasury_wallet_address: str = ""
    treasury_wallet_private_key: str = ""

    xfee_token_mint: str = ""
    pool_address: str = ""
    # Base58 private key of the staking pool's authority. When unset, the
    # backend falls back to MASTER_WALLET_PRIVATE_KEY for backward compatibility
    # with single-keypair deployments. Use this to keep the on-chain pool
    # authority independent of the master / commission / global-pool wallet.
    pool_authority_private_key: str = ""
    # On-chain program ID (base58) for the POWER staking program. Leave unset
    # to use DEFAULT_STAKING_PROGRAM_ID above. Override when this deployment
    # targets a different staking program on Solana.
    staking_program_id: str = ""

    # Wallets scanned by /api/v1/burn/* for the burn dashboard. Optional —
    # leave empty if this deployment doesn't publish burn stats.
    burn_token_buy_wallet: str = ""
    burn_fee_wallet: str = ""

    quicknode_rpc_url: str = ""
    sol_price_api_url: str = ""

    xfee_total_supply: int = 400_000

    purchase_wallet_expiry_minutes: int = 15
    purchase_min_usd: float = 6.00
    gas_buffer_usd: float = 5.00
    leave_in_purchase_wallet_usd: float = 4.50

    admin_api_key: str = ""

    # Optional: a separate Solana wallet (base58 pubkey) authorized to sign
    # set-user-level requests. If empty, master wallet is used. The X-Admin-Key
    # header path is unaffected by this setting.
    set_user_level_signer_wallet: str = ""

    test_mode: bool = False
    test_sol_price: float = 150.0
    power_distribution_enabled: bool = True
    power_delayed_stake_bonus_multiplier: float = 1.0

    stake_repair_interval_seconds: int = 120
    stake_repair_min_age_minutes: int = 5
    stake_repair_batch_size: int = 100
    # Only repair purchases with confirmed_at >= this instant (UTC); aligns with staking audit default.
    stake_repair_since_unix: int = 1778346247

    global_pool_enabled: bool = True
    global_pool_duration_days: int = 15
    global_pool_finalize_interval_seconds: int = 300
    global_pool_funding_wallet_private_key: str = ""
    global_pool_funding_buffer_sol: float = 0.05
    global_pool_settlement_concurrency: int = 3
    global_pool_confirm_retries: int = 30

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()


def staking_signer_private_key() -> str:
    """Resolve the base58 private key that signs on-chain stake calls.

    Prefers POOL_AUTHORITY_PRIVATE_KEY (dedicated staking pool authority).
    Falls back to MASTER_WALLET_PRIVATE_KEY so legacy single-keypair
    deployments keep working without any env change.
    """
    return settings.pool_authority_private_key or settings.master_wallet_private_key
