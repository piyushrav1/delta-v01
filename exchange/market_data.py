# Flavor: future annotations for type hints
from __future__ import annotations

import time
import logging
from typing import Optional, Dict, Any, Tuple
from dataclasses import dataclass, field
from decimal import Decimal

from .websocket_manager import MarketDataQuote, store, get_market_quote

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Validation Helpers
# ──────────────────────────────────────────────
def _to_float(v: Any) -> Optional[float]:
    """Safely convert to float, returns None on failure."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v: Any) -> Optional[int]:
    """Safely convert to int, returns None on failure."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ──────────────────────────────────────────────
# Validation Functions
# ──────────────────────────────────────────────
def validate_spread(bid: float, ask: float) -> bool:
    """Spread sanity: Ask > Bid > 0."""
    return ask > bid > 0


def validate_liquidity(bid_size: float, ask_size: float, min_lot: int) -> bool:
    """Minimum lot size check across top 3 levels (simplified)."""
    return bid_size >= min_lot and ask_size >= min_lot


def is_fresh(timestamp_ms: int, max_age_ms: int = 3000) -> bool:
    """Quote freshness: local time - exchange timestamp <= max_age_ms."""
    return abs(time.time() * 1000 - timestamp_ms) <= max_age_ms


# ──────────────────────────────────────────────
# Quote Normalization Factory
# ──────────────────────────────────────────────
def normalize_tick(symbol: str, raw: Dict[str, Any]) -> Optional[MarketDataQuote]:
    """
    Normalize a raw Delta tick into MarketDataQuote.

    Expected raw format (from WS ticker channel):
    {
        "symbol": "BTCUSD",
        "best_bid": ["45000.00", 1.5],
        "best_ask": ["45010.00", 2.0],
        "last": "45005.00",
        "spot": "45000.00",
        "ts": 1700000000000
    }
    """
    bid_raw = raw.get("best_bid")
    ask_raw = raw.get("best_ask")
    last_raw = raw.get("last")
    spot_raw = raw.get("spot")
    ts_raw = raw.get("ts", int(time.time() * 1000))

    bid = _to_float(bid_raw[0]) if isinstance(bid_raw, list) and len(bid_raw) > 0 else None
    ask = _to_float(ask_raw[0]) if isinstance(ask_raw, list) and len(ask_raw) > 0 else None
    bid_size = _to_float(bid_raw[1]) if isinstance(bid_raw, list) and len(bid_raw) > 1 else None
    ask_size = _to_float(ask_raw[1]) if isinstance(ask_raw, list) and len(ask_raw) > 1 else None
    last = _to_float(last_raw)
    spot = _to_float(spot_raw)
    ts_ms = _to_int(ts_raw)

    if bid is None or ask is None or ts_ms is None:
        logger.warning(f"Failed to normalize tick for {symbol}: incomplete data {raw}")
        return None

    # Spread sanity
    if not validate_spread(bid, ask):
        logger.debug(f"Spread check failed for {symbol}: bid={bid} ask={ask}")

    # Freshness gate
    fresh = is_fresh(ts_ms)

    quote = MarketDataQuote(
        symbol=symbol,
        bid=bid,
        ask=ask,
        mid=(bid + ask) / 2,
        bid_size=bid_size,
        ask_size=ask_size,
        last=last,
        spot=spot,
        timestamp_ms=ts_ms,
        age_ms=int(time.time() * 1000) - ts_ms,
    )

    # Store the normalized quote
    store.update(quote)

    return quote


# ──────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────
def refresh_quote(symbol: str, raw: Dict[str, Any]) -> Optional[MarketDataQuote]:
    """Refresh/normalize a quote from raw WS data and return it."""
    return normalize_tick(symbol, raw)


def get_current_quote(symbol: str) -> Optional[MarketDataQuote]:
    """Get the latest normalized quote for a symbol from the shared store."""
    return store.get(symbol)