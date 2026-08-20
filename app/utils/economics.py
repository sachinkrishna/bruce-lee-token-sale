POWER_STAKE_MULTIPLIER = 20

# Founder tier bonuses (exact-match lookup, POWER units).
#
# Bonuses are added on top of the base 20× stake for purchases marked
# `founder_eligible=true`. Only these exact USD amounts get a bonus — the
# frontend enforces the set of allowed purchase sizes. Anything not in this
# table (or any post-cap purchase) has a zero tier bonus and receives base
# POWER only.
FOUNDER_TIER_BONUS_TABLE: dict[int, int] = {
    50:     0,
    100:    400,
    240:    2_000,
    500:    6_000,
    1_000:  16_000,
    2_500:  50_000,
    5_000:  120_000,
}


def calculate_power_amount(purchase_amount_usd: float, bonus_multiplier: float = 1.0) -> int:
    """Legacy base POWER computation (base × multiplier). Kept for the
    existing stake-repair / delayed-stake pathway, which is orthogonal to
    the new `power_entitlement` shadow ledger."""
    return int(float(purchase_amount_usd) * POWER_STAKE_MULTIPLIER * float(bonus_multiplier))


def is_power_bonus_eligible(purchase: dict) -> bool:
    return bool(purchase.get("power_distribution_bonus_eligible"))


def calculate_purchase_power_amount(purchase: dict, bonus_multiplier: float = 1.0) -> int:
    applied_multiplier = bonus_multiplier if is_power_bonus_eligible(purchase) else 1.0
    return calculate_power_amount(purchase.get("xfee_amount", 0), applied_multiplier)


def founder_tier_bonus(purchase_usd: int | float) -> int:
    """Exact-match tier bonus lookup. Returns 0 for any USD not in the table.

    Only ever adds to a founder-eligible purchase — the caller is responsible
    for the `founder_eligible` gate.
    """
    try:
        return FOUNDER_TIER_BONUS_TABLE.get(int(purchase_usd), 0)
    except (TypeError, ValueError):
        return 0


def calculate_power_entitlement(purchase_usd: int | float, *, founder_eligible: bool) -> int:
    """Total POWER a completed purchase is entitled to under the tiered scheme.

    Formula:
        base = xfee_amount × POWER_STAKE_MULTIPLIER
        tier = FOUNDER_TIER_BONUS_TABLE[xfee_amount]  # exact match only
        entitlement = base + (tier if founder_eligible else 0)

    This is a *shadow ledger* value — it records what a wallet is owed under
    the new tiered rules, independent of what has actually been staked
    on-chain. Reconciling on-chain state to the entitlement is a separate
    process handled elsewhere.
    """
    try:
        usd = int(purchase_usd)
    except (TypeError, ValueError):
        return 0
    if usd <= 0:
        return 0
    base = usd * POWER_STAKE_MULTIPLIER
    if founder_eligible:
        base += founder_tier_bonus(usd)
    return base
