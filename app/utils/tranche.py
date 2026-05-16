"""Commission tranche deduction (USD). Does not affect gross sale_usd / total_sales_usd."""

import math

TRANCHE_USD = 45.0
DEDUCTION_PER_TRANCHE_USD = 0.50
MAX_TRANCHE_DEDUCTION_USD = 2.5


def tranche_deduction_usd(sale_usd: float) -> float:
    """
    For each full $45 of gross sale, $0.50 is excluded from the commissionable SOL base only,
    capped at MAX_TRANCHE_DEDUCTION_USD total. Does not affect gross sale_usd / total_sales_usd.
    """
    if sale_usd <= 0:
        return 0.0
    uncapped = math.floor(sale_usd / TRANCHE_USD) * DEDUCTION_PER_TRANCHE_USD
    return min(uncapped, MAX_TRANCHE_DEDUCTION_USD)
