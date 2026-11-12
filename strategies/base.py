"""
BaseStrategy ABC & Registry

Foundation for all strategy modules. Strategies MUST NEVER import from
exchange/delta_client.py or trading/execution_manager.py. They emit
TradeSignal objects through the Risk Gateway.

Registry pattern: dynamic strategy discovery via entry_points or explicit
registration. Each strategy module exposes a `Strategy` class subclassing
BaseStrategy and registers itself via `register_strategy()`.
"""
from __future__ import annotations

import abc
import sys
from typing import Dict, List, Type, Optional, Set

from analytics.signal import TradeSignal


# ──────────────────────────────────────────────
# Forbidden import check helper
# ──────────────────────────────────────────────
def _check_forbidden_imports(module_name: str):
    """Validate that a module doesn't import forbidden packages."""
    forbidden = ("exchange.delta_client", "trading.execution_manager")
    for f in forbidden:
        if f in module_name:
            raise TypeError(
                f"Strategy module '{module_name}' cannot import '{f}'. "
                "Strategies must emit TradeSignal objects only; "
                "order placement is handled through the Risk Gateway."
            )


# ──────────────────────────────────────────────
# BaseStrategy Abstract Base Class
# ──────────────────────────────────────────────
class BaseStrategy(abc.ABC):
    """
    Base class for all trading strategies.

    Contract:
    - Strategies MUST emit TradeSignal objects (never raw order placement)
    - Strategies MUST register via @register_strategy decorator
    - Strategies MUST define strategy_id and strategy_version class attrs
    - Strategies SHOULD implement: on_quote(), on_candle(), generate_signals()
    - Strategies MUST NOT import from exchange/delta_client.py
    - Strategies MUST NOT import from trading/execution_manager.py
    """

    # Class-level attributes (must be overridden by subclasses)
    strategy_id: str = ...  # unique identifier
    strategy_version: str = "1.0.0"  # semantic version

    # Warmup period: how many bars/candles needed before signals valid
    warmup_period: int = 20

    # Whether this strategy allows auto-trading in each environment
    allowed_environments: Set[str] = None  # subset of BACKTEST,PAPER,TESTNET,LIVE_MANUAL,LIVE_AUTO

    # Strategy name for UI display
    display_name: str = "Unnamed Strategy"

    # Instance-level forbidden import check
    def _check_forbidden_imports(self):
        module = sys.modules.get(self.__module__)
        if module is None:
            return
        source = inspect.getsource(self.__class__)
        _check_forbidden_imports(module)

    @abc.abstractmethod
    def on_quote(self, quote: 'MarketDataQuote') -> Optional[TradeSignal]:
        """Called when new market quote arrives. Return TradeSignal or None."""
        raise NotImplementedError

    @abc.abstractmethod
    def on_candle(self, open_: float, high: float, low: float, close: float,
                  volume: int, timestamp: datetime) -> Optional[TradeSignal]:
        """Called when new candle closes. Return TradeSignal or None."""
        raise NotImplementedError

    @abc.abstractmethod
    def generate_signals(self, quotes: Dict[str, 'MarketDataQuote']) -> List[TradeSignal]:
        """Generate signals from a dict of symbol->quote. Return list of signals."""
        raise NotImplementedError

    def warmup_check(self, bar_count: int) -> bool:
        """Verify enough data has been collected for this strategy."""
        return bar_count >= self.warmup_period


# ──────────────────────────────────────────────
# Strategy Registry
# ──────────────────────────────────────────────
_strategies: Dict[str, Type[BaseStrategy]] = {}

def register_strategy(cls: Type[BaseStrategy]) -> Type[BaseStrategy]:
    """
    Decorator to register a strategy class.

    Usage:
        @register_strategy
        class TrendFollowing(BaseStrategy):
            strategy_id = "trend_following_btc"
            ...
    """
    if cls.strategy_id in _strategies:
        raise ValueError(f"Strategy '{cls.strategy_id}' already registered")
    _strategies[cls.strategy_id] = cls
    return cls

def get_strategy(strategy_id: str) -> Type[BaseStrategy]:
    """Retrieve a registered strategy class by ID."""
    return _strategies.get(strategy_id)

def list_strategies() -> List[str]:
    """List all registered strategy IDs."""
    return list(_strategies.keys())


# ──────────────────────────────────────────────
# Convenience: signal emission helpers
# ──────────────────────────────────────────────
def emit_buy_signal(symbol: str, entry: float, sl: float, tp: float,
                    size: int, strategy_id: str, confidence: float = 0.5) -> TradeSignal:
    """Helper to buy_signal from within a strategy."""
    from analytics.signal import buy_signal
    return buy_signal(
        signal_id=f"sig_{strategy_id}_{symbol}",
        strategy_id=strategy_id,
        strategy_version="1.0.0",
        symbol=symbol,
        entry_price=entry,
        stop_loss=sl,
        take_profit=tp,
        proposed_size=size,
        confidence=confidence,
    )


def emit_sell_signal(symbol: str, entry: float, sl: float, tp: float,
                     size: int, strategy_id: str, confidence: float = 0.5) -> TradeSignal:
    """Helper to sell_signal from within a strategy."""
    from analytics.signal import sell_signal
    return sell_signal(
        signal_id=f"sig_{strategy_id}_{symbol}",
        strategy_id=strategy_id,
        strategy_version="1.0.0",
        symbol=symbol,
        entry_price=entry,
        stop_loss=sl,
        take_profit=tp,
        proposed_size=size,
        confidence=confidence,
    )