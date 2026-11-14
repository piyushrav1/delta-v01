"""
Canonical TradeSignal Data Contract

Re-exports canonical TradeSignal definitions from analytics.signal for the
trading module hierarchy.
"""
from analytics.signal import (
    TradeSignal,
    buy_signal,
    sell_signal,
)

__all__ = [
    "TradeSignal",
    "buy_signal",
    "sell_signal",
]
