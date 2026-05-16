POWER_STAKE_MULTIPLIER = 20


def calculate_power_amount(purchase_amount_usd: float, bonus_multiplier: float = 1.0) -> int:
    return int(float(purchase_amount_usd) * POWER_STAKE_MULTIPLIER * float(bonus_multiplier))


def is_power_bonus_eligible(purchase: dict) -> bool:
    return bool(purchase.get("power_distribution_bonus_eligible"))


def calculate_purchase_power_amount(purchase: dict, bonus_multiplier: float = 1.0) -> int:
    applied_multiplier = bonus_multiplier if is_power_bonus_eligible(purchase) else 1.0
    return calculate_power_amount(purchase.get("xfee_amount", 0), applied_multiplier)
