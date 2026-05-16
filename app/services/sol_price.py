import logging
import time
from typing import Optional

from app.config import settings
from app.services.solana_rpc import get_http_client

logger = logging.getLogger(__name__)

_cached_price: Optional[float] = None
_cached_at: float = 0.0
CACHE_TTL_SECONDS = 30.0


async def get_sol_price() -> float:
    global _cached_price, _cached_at

    if settings.test_mode:
        return settings.test_sol_price

    now = time.time()
    if _cached_price is not None and (now - _cached_at) < CACHE_TTL_SECONDS:
        return _cached_price

    try:
        client = get_http_client()
        resp = await client.get(settings.sol_price_api_url)
        resp.raise_for_status()
        data = resp.json()
        price = float(data["price"])
        _cached_price = price
        _cached_at = now
        logger.info(f"SOL price fetched: ${price}")
        return price
    except Exception:
        logger.exception("Failed to fetch SOL price")
        if _cached_price is not None:
            return _cached_price
        raise
