"""Oracle client for the SPL payment token (XFEE) price in USD.

The oracle URL and the JSON path to the price field are configurable so a
new deployment can point at a different data source without code changes.
"""
import logging
import time
from typing import Optional

from app.config import settings
from app.services.solana_rpc import get_http_client

logger = logging.getLogger(__name__)

_cached_price: Optional[float] = None
_cached_at: float = 0.0


def _dig(obj, dotted_path: str):
    """Walk a dotted JSON path (e.g. 'data.price') into a nested object."""
    cur = obj
    for part in dotted_path.split("."):
        if part == "":
            continue
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
            continue
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


async def get_xfee_price() -> float:
    """Return the current XFEE/USD price. Uses a short in-process cache."""
    global _cached_price, _cached_at

    if settings.test_mode:
        # Deterministic price for tests; overridable via env if needed later.
        return 1.0

    if not settings.xfee_price_oracle_url:
        raise RuntimeError(
            "XFEE_PRICE_ORACLE_URL is not configured; cannot price XFEE in USD."
        )

    now = time.time()
    ttl = float(settings.xfee_price_cache_ttl_seconds or 30)
    if _cached_price is not None and (now - _cached_at) < ttl:
        return _cached_price

    try:
        client = get_http_client()
        resp = await client.get(settings.xfee_price_oracle_url)
        resp.raise_for_status()
        data = resp.json()
        raw = _dig(data, settings.xfee_price_oracle_json_path)
        if raw is None:
            raise RuntimeError(
                f"XFEE price oracle response missing path "
                f"'{settings.xfee_price_oracle_json_path}': {data!r}"
            )
        price = float(raw)
        if price <= 0:
            raise RuntimeError(f"XFEE price oracle returned non-positive: {price}")
        _cached_price = price
        _cached_at = now
        logger.info("XFEE price fetched: $%s", price)
        return price
    except Exception:
        logger.exception("Failed to fetch XFEE price")
        if _cached_price is not None:
            logger.warning("Falling back to stale XFEE price: $%s", _cached_price)
            return _cached_price
        raise
