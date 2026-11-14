"""
Strategy Selection & Portfolio Coordination

Orchestrates the signal processing pipeline before submission to Central Risk Gateway:
1. Signal Freshness Filter (Age <= 5000ms)
2. Signal Deduplication (Prevents duplicate emissions within window)
3. Active Orders & Positions Filter (Blocks duplicate exposure on symbols already positioned)
4. Cross-Strategy Conflict Resolution (Arbitrates BUY vs SELL on same symbol via Market Regime)
5. Multi-Factor Signal Ranking (Scores by R:R, Confidence, Regime, and Liquidity)
"""
from __future__ import annotations

import time
import hashlib
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Callable, Any
from decimal import Decimal
import logging

from analytics.signal import TradeSignal
from analytics.market_regime import MarketRegime
from trading.conflict_resolver import ConflictResolver
from trading.ranker import SignalRanker

logger = logging.getLogger(__name__)


class SignalDeduplicator:
    """
    Filters stale signals (> 5000ms) and eliminates duplicates.
    Can also check against active symbols in DB.
    """

    def __init__(self, max_age_ms: float = 5000.0, deduplication_window_sec: float = 60.0):
        self.max_age_ms = float(max_age_ms)
        self.dedup_window_sec = float(deduplication_window_sec)
        # Map: signal_hash -> (last_seen_timestamp)
        self._seen_signals: Dict[str, float] = {}

    def _hash_signal(self, sig: TradeSignal) -> str:
        """Create deterministic content hash of signal core parameters."""
        raw = f"{sig.strategy_id}:{sig.symbol}:{sig.direction}:{round(float(sig.target_entry_price), 2)}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def is_fresh(self, sig: TradeSignal) -> bool:
        """Check if signal age <= max_age_ms."""
        now = datetime.now()
        age_ms = (now - sig.timestamp).total_seconds() * 1000.0
        return 0.0 <= age_ms <= self.max_age_ms

    def is_duplicate(self, sig: TradeSignal) -> bool:
        """Check if identical signal was seen within deduplication window."""
        h = self._hash_signal(sig)
        now_ts = time.time()
        last_seen = self._seen_signals.get(h)
        if last_seen is not None and (now_ts - last_seen) < self.dedup_window_sec:
            return True
        return False

    def record(self, sig: TradeSignal):
        """Record signal emission timestamp."""
        h = self._hash_signal(sig)
        self._seen_signals[h] = time.time()

    def purge_expired(self):
        """Clean up records older than dedup window."""
        now_ts = time.time()
        self._seen_signals = {
            h: ts for h, ts in self._seen_signals.items()
            if (now_ts - ts) < self.dedup_window_sec
        }


class StrategySelector:
    """
    Authoritative Strategy Selection and Portfolio Coordination Gateway.
    """

    def __init__(
        self,
        deduplicator: Optional[SignalDeduplicator] = None,
        conflict_resolver: Optional[ConflictResolver] = None,
        ranker: Optional[SignalRanker] = None,
        active_symbols_provider: Optional[Callable[[], Set[str]]] = None,
        max_selected_signals: int = 5,
    ):
        self.dedup = deduplicator or SignalDeduplicator()
        self.conflict_resolver = conflict_resolver or ConflictResolver()
        self.ranker = ranker or SignalRanker()
        self.active_symbols_provider = active_symbols_provider
        self.max_selected_signals = max_selected_signals

    def select(
        self,
        signals: List[TradeSignal],
        regimes: Optional[Dict[str, MarketRegime]] = None,
        quotes: Optional[Dict[str, Any]] = None,
        active_symbols: Optional[Set[str]] = None,
    ) -> List[TradeSignal]:
        """
        Execute full strategy selection pipeline.

        Pipeline Stages:
        1. Freshness & deduplication filter
        2. Active position/order filter
        3. Cross-strategy conflict resolution (Regime-aware)
        4. Multi-factor quantitative ranking
        5. Top-N portfolio selection
        """
        if not signals:
            return []

        regimes = regimes or {}
        quotes = quotes or {}

        # Resolve active symbols from parameter or provider
        current_active: Set[str] = set()
        if active_symbols is not None:
            current_active = active_symbols
        elif self.active_symbols_provider is not None:
            try:
                current_active = self.active_symbols_provider()
            except Exception as e:
                logger.error(f"Error querying active symbols provider: {e}")

        # Stage 1: Freshness & Deduplication
        fresh_signals: List[TradeSignal] = []
        for s in signals:
            if not self.dedup.is_fresh(s):
                logger.debug(f"Discarding stale signal {s.signal_id} on {s.symbol}")
                continue
            if self.dedup.is_duplicate(s):
                logger.debug(f"Discarding duplicate signal {s.signal_id} on {s.symbol}")
                continue
            fresh_signals.append(s)

        # Stage 2: Active Position Filter
        unpositioned_signals: List[TradeSignal] = []
        for s in fresh_signals:
            if s.symbol in current_active:
                logger.info(f"Discarding signal on {s.symbol}: active position/order already exists")
                continue
            unpositioned_signals.append(s)

        if not unpositioned_signals:
            return []

        # Stage 3: Cross-Strategy Conflict Resolution (Directional Arbitration)
        resolved_signals = self.conflict_resolver.resolve(unpositioned_signals, regimes=regimes)

        # Record kept signals in deduplicator
        for s in resolved_signals:
            self.dedup.record(s)

        # Stage 4: Multi-Factor Ranking
        ranked = self.ranker.rank(resolved_signals, regimes=regimes, quotes=quotes)

        # Stage 5: Top-N Portfolio Selection
        selected = [sig for sig, score in ranked[:self.max_selected_signals]]
        return selected

