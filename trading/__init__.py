"""
Trading Package

Core trading engine modules: signal contract, strategy selection,
conflict resolution, multi-factor ranking, and execution management.
"""
from trading.signal import TradeSignal, buy_signal, sell_signal
from trading.conflict_resolver import ConflictResolver
from trading.ranker import SignalRanker
from trading.selector import StrategySelector, SignalDeduplicator

__all__ = [
    "TradeSignal",
    "buy_signal",
    "sell_signal",
    "ConflictResolver",
    "SignalRanker",
    "StrategySelector",
    "SignalDeduplicator",
]
