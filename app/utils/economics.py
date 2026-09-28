POWER_STAKE_MULTIPLIER = 20

# Package power entitlements (exact-match lookup, POWER units).
#
# Each value is the TOTAL POWER a buyer receives for that exact USD purchase
# amount (base 20x + tier bonus, folded into one number). Only the exact USD
# amounts in this table trigger the tiered rate; any other amount receives
# base 20x POWER only. The frontend enforces the set of allowed purchase
# sizes; the API also rejects any purchase below `MIN_PACKAGE_USD`.
#
# Per-dollar POWER rates: 26, 28, 30, 32, 34, 36, 38, 40 across the ladder.
PACKAGE_POWER_TABLE: dict[int, int] = {
    100:      2_600,
    250:      7_000,
    500:     15_000,
    1_000:   32_000,
    2_500:   85_000,
    5_000:  180_000,
    10_000: 380_000,
    25_000: 1_000_000,
}

# Minimum accepted purchase (USD). Rejected at API level.
MIN_PACKAGE_USD = 100

# Legacy alias: derived from PACKAGE_POWER_TABLE for callers that expect the
# "founder tier bonus" (= package total - base 20x). Kept for backward
# compatibility with /stats/power, admin scripts, and existing tests. New
# code should read PACKAGE_POWER_TABLE directly.
FOUNDER_TIER_BONUS_TABLE: dict[int, int] = {
    usd: total - usd * POWER_STAKE_MULTIPLIER
    for usd, total in PACKAGE_POWER_TABLE.items()
}


def package_power(purchase_usd: int | float) -> int:
    """Total POWER for a package amount (exact-match lookup).

    Returns 0 for any USD not in the table (callers should fall back to
    base 20x for non-tier amounts).
    """
    try:
        return PACKAGE_POWER_TABLE.get(int(purchase_usd), 0)
    except (TypeError, ValueError):
        return 0


def calculate_power_amount(purchase_amount_usd: float, bonus_multiplier: float = 1.0) -> int:
    """Legacy base POWER computation (base x multiplier). Kept for the
    existing stake-repair / delayed-stake pathway, which is orthogonal to
    the tiered `power_entitlement` shadow ledger."""
    return int(float(purchase_amount_usd) * POWER_STAKE_MULTIPLIER * float(bonus_multiplier))


def is_power_bonus_eligible(purchase: dict) -> bool:
    return bool(purchase.get("power_distribution_bonus_eligible"))


def calculate_purchase_power_amount(purchase: dict, bonus_multiplier: float = 1.0) -> int:
    applied_multiplier = bonus_multiplier if is_power_bonus_eligible(purchase) else 1.0
    return calculate_power_amount(purchase.get("xfee_amount", 0), applied_multiplier)


def founder_tier_bonus(purchase_usd: int | float) -> int:
    """Legacy alias: bonus-only lookup (= package total - base 20x).

    Returns 0 for any USD not in the tiered table. Kept for backward
    compatibility; use `package_power` for new code.
    """
    try:
        return FOUNDER_TIER_BONUS_TABLE.get(int(purchase_usd), 0)
    except (TypeError, ValueError):
        return 0


def calculate_power_entitlement(purchase_usd: int | float, *, founder_eligible: bool = True) -> int:
    """Total POWER a completed purchase is entitled to.

    - Tier amount (in `PACKAGE_POWER_TABLE`): returns the package total.
      The tier bonus is no longer gated on `founder_eligible` — tier
      amounts always get the full package power. The `founder_eligible`
      parameter is retained in the signature for backward compatibility
      with the existing shadow-ledger callers but is ignored.
    - Non-tier amount: returns base 20x POWER only.

    This is a *shadow ledger* value — it records what a wallet is owed
    under the tiered scheme, independent of what has actually been staked
    on-chain. Reconciling on-chain state to the entitlement is a separate
    process handled elsewhere.
    """
    try:
        usd = int(purchase_usd)
    except (TypeError, ValueError):
        return 0
    if usd <= 0:
        return 0
    package_total = PACKAGE_POWER_TABLE.get(usd)
    if package_total is not None:
        return package_total
    return usd * POWER_STAKE_MULTIPLIER
