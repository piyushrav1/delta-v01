"""
Exchange Module — Delta Exchange India v2 API Client

Modular monolith layer isolating all network I/O, rate-limiting,
and market-data normalization. Higher-level modules (strategies,
risk, execution) never import from this directory directly — they
only interact through TradeSignal dataclasses and the Risk Gateway.
"""
from .delta_client import DeltaRestClient
from .websocket_manager import MarketDataQuote, store, WSConnector, start_ws, get_market_quote
from .market_data import normalize_tick, validate_spread, validate_liquidity, is_fresh, \
    refresh_quote, get_current_quote

__all__ = [
    "DeltaRestClient",
    "MarketDataQuote",
    "store",
    "WSConnector",
    "start_ws",
    "get_market_quote",
    "normalize_tick",
    "validate_spread",
    "validate_liquidity",
    "is_fresh",
    "get_current_quote",
]