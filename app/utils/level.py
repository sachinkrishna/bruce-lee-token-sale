LEVEL_THRESHOLDS = [
    (16, 10_000_000_000.0, 1.00),
    (15, 9_000_000_000.0, 0.95),
    (14, 5_000_000_000.0, 0.75),
    (13, 3_000_000_000.0, 0.60),
    (12, 2_000_000_000.0, 0.50),
    (11, 1_000_000_000.0, 0.45),
    (10, 2_500_000.0, 0.40),
    (9, 1_000_000.0, 0.36),
    (8, 250_000.0, 0.34),
    (7, 100_000.0, 0.32),
    (6, 50_000.0, 0.30),
    (5, 25_000.0, 0.28),
    (4, 10_000.0, 0.26),
    (3, 2_500.0, 0.24),
    (2, 500.0, 0.22),
    (1, 0.0, 0.20),
]
RATE_BY_LEVEL = {lvl: rate for lvl, _, rate in LEVEL_THRESHOLDS}
MAX_COMMISSION_LEVEL = max(RATE_BY_LEVEL)


def get_level_from_sales(total_sales_usd: float) -> int:
    for level, threshold, _ in LEVEL_THRESHOLDS:
        if total_sales_usd >= threshold:
            return level
    return 1


def get_rate_for_level(level: int) -> float:
    return RATE_BY_LEVEL.get(level, 0.0)
