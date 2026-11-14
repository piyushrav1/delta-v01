"""
Trading Package

Core trading engine modules:
- Canonical signal contract & selection pipeline
- Order execution gateway & state machine
- Protective bracket order manager
- Exchange state reconciliation engine
"""
from trading.signal import TradeSignal, buy_signal, sell_signal
from trading.conflict_resolver import ConflictResolver
from trading.ranker import SignalRanker
from trading.selector import StrategySelector, SignalDeduplicator
from trading.order_state import (
    ManagedOrder,
    StateTransitionEvent,
    InvalidStateTransitionError,
    VALID_TRANSITIONS,
)
from trading.bracket_manager import (
    BracketManager,
    ProtectionStatus,
    ProtectionVerificationResult,
)
from trading.execution_manager import ExecutionManager
from trading.reconciliation import (
    StateReconciler,
    ReconciliationReport,
)

__all__ = [
    "TradeSignal",
    "buy_signal",
    "sell_signal",
    "ConflictResolver",
    "SignalRanker",
    "StrategySelector",
    "SignalDeduplicator",
    "ManagedOrder",
    "StateTransitionEvent",
    "InvalidStateTransitionError",
    "VALID_TRANSITIONS",
    "BracketManager",
    "ProtectionStatus",
    "ProtectionVerificationResult",
    "ExecutionManager",
    "StateReconciler",
    "ReconciliationReport",
]
