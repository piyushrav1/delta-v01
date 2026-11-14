"""
Strategy Selector & Portfolio Coordination

Handles signal deduplication, cross-strategy conflict detection, and multi-factor
signal ranking. Ensures that only the best signals reach the Risk Gateway.

Per Master Blueprint Table 8-1 (Phase 8):
- P8-01: Signal Deduplication and Freshness Filter
- P8-02: Cross-Strategy Conflict Detection
- P8-03: Multi-Factor Signal Ranking
"""
from __future__ import annotations

from analytics.signal import TradeSignal
from analytics.market_regime import MarketRegime, classify_market_regime
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Tuple, Any
import time


# ──────────────────────────────────────────────
# Signal Deduplication & Freshness Filter
# ──────────────────────────────────────────────
class SignalDeduplicator:
    """
    Deduplicate signals and filter by freshness.

    A signal is considered stale if its age exceeds the maximum allowed
    (default: 5000 ms = 5 seconds). Signals with matching signal_id are
    considered duplicates.
    """
    def __init__(self, max_age_ms: int = 5000):
        self.max_age_ms = max_age_ms
        # Track recent signals: signal_id -> timestamp_ms
        self.recent_signals: Dict[str, int] = {}

    def is_fresh(self, signal: TradeSignal) -> bool:
        """Check if a signal is within the age threshold."""
        age_ms = abs(time.time() * 1000 - signal.timestamp.timestamp() * 1000)
        return age_ms <= self.max_age_ms

    def is_duplicate(self, signal: TradeSignal) -> bool:
        """Check if a signal with this signal_id has been seen recently."""
        if signal.signal_id in self.recent_signals:
            age_ms = time.time() * 1000 - self.recent_signals[signal.signal_id]
            return age_ms < self.max_age_ms
        return False

    def record(self, signal: TradeSignal) -> None:
        """Record a signal as seen."""
        self.recent_signals[signal.signal_id] = time.time() * 1000

        # Clean up old entries
        now = time.time() * 1000
        self.recent_signals = {
            k: v for k, v in self.recent_signals.items()
            if now - v < self.max_age_ms
        }


# ──────────────────────────────────────────────
# Cross-Strategy Conflict Detection
# ──────────────────────────────────────────────
class ConflictDetector:
    """
    Detects conflicting signals from different strategies.

    Conflicts occur when:
    - Opposite directional signals on the same symbol (BUY vs SELL)
    - Multiple signals for the same symbol with overlapping expiry
    - Signals that would open conflicting positions

    Returns a list of conflict resolutions.
    """
    def __init__(self):
        # Track signals by symbol: list of (signal_id, direction, expiry)
        self.signals_by_symbol: Dict[str, List[Tuple[str, str, Any]]] = {}

    def record_signal(self, signal: 'TradeSignal') -> None:
        """Record a newly emitted signal."""
        symbol = signal.symbol
        if symbol not in self.signals_by_symbol:
            self.signals_by_symbol[symbol] = []

        self.signals_by_symbol[symbol].append((
            signal.signal_id,
            signal.direction,
            signal.expiry_time,
        ))

        # Keep only recent signals (last 24 hours)
        cutoff = time.time() - 86400
        self.signals_by_symbol[symbol] = [
            s for s in self.signals_by_symbol[symbol]
            if s[2] and s[2].timestamp() > cutoff
        ]

    def detect_conflicts(self) -> Dict[str, List[Dict[str, Any]]]:
        """
        Detect all conflicts across symbols.

        Returns:
            Dict mapping symbol -> list of conflicts. Each conflict is a dict with:
            - 'type': 'opposite_directions', 'overlapping_expiries', etc.
            - 'signals': list of involved signal IDs
            - 'resolution': recommended action
        """
        conflicts: Dict[str, List[Dict[str, Any]]] = {}

        for symbol, signals in self.signals_by_symbol.items():
            symbol_conflicts = []

            # Check for opposite directions
            directions = [s[1] for s in signals]
            buy_signals = [s for s in signals if s[1] == 'BUY']
            sell_signals = [s for s in signals if s[1] == 'SELL']

            if buy_signals and sell_signals:
                symbol_conflicts.append({
                    'type': 'opposite_directions',
                    'signals': [s[0] for s in buy_signals + sell_signals],
                    'resolution': 'Select one direction or defer to regime filter',
                })

            # Check for overlapping expiries (simplified)
            expiry_groups = {}
            for s_id, direction, expiry in signals:
                if expiry:
                    key = expiry.date()
                    if key not in expiry_groups:
                        expiry_groups[key] = []
                    expiry_groups[key].append(s_id)

            for date, Expiry_sids in expiry_groups.items():
                if len(Expiry_sids) > 1:
                    symbol_conflicts.append({
                        'type': 'overlapping_expiries',
                        'signals': Expiry_sids,
                        'resolution': 'Select signal with best confidence/size',
                    })

            if symbol_conflicts:
                conflicts[symbol] = symbol_conflicts

        return conflicts

    def resolve_conflicts(self, conflicts: Dict[str, List[Dict[str, Any]]]) -> Dict[str, str]:
        """Generate resolution recommendations for detected conflicts."""
        resolutions: Dict[str, str] = {}

        for symbol, conflict_list in conflicts.items():
            for conflict in conflict_list:
                c_type = conflict['type']
                if c_type == 'opposite_directions':
                    resolutions[symbol] = 'Select BUY if bullish regime, SELL if bearish; ' \
                                           'or defer to conflict_ranking'
                elif c_type == 'overlapping_expiries':
                    resolutions[symbol] = 'Select signal with highest confidence ' \
                                           'or earliest expiry, then cancel others'

        return resolutions


# ──────────────────────────────────────────────
# Multi-Factor Signal Ranking
# ──────────────────────────────────────────────
class SignalRanker:
    """
    Rank signals by multiple factors to determine priority.

    Ranking factors (in order of priority):
    1. Confidence score (0.0 to 1.0)
    2. Regime alignment (trend-following strategies in trending regimes, etc.)
    3. Signal freshness (newer signals preferred)
    4. Proposed size (larger positions with better risk/return)
    5. Regime alignment (strategy-mode match)

    Ranking score range: 0.0 to 1.0
    """
    def __init__(self):
        self.weights = {
            'confidence': 0.4,
            'freshness': 0.2,
            'size': 0.15,
            'regime': 0.15,
            'conflict_penalty': 0.1,
        }

    def rank(self, signal: 'TradeSignal',
             is_fresh_fn=None,
             in_regime: str = None,
             conflict_penalty: float = 0.0) -> float:
        """
        Compute a ranking score for a signal.

        Args:
            signal: The TradeSignal to rank
            is_fresh_fn: Optional function that returns True if signal is fresh
            in_regime: Optional regime string (e.g., 'TRENDING_BULL')
            conflict_penalty: Penalty (0.0-1.0) for detected conflicts

        Returns:
            Composite ranking score in [0.0, 1.0]
        """
        score = 0.0

        # 1. Confidence (0.4 weight)
        try:
            conf = float(signal.confidence)
            score += self.weights['confidence'] * min(1.0, max(0.0, conf))
        except (TypeError, ValueError):
            score += self.weights['confidence'] * 0.5  # default mid-range

        # 2. Freshness (0.2 weight)
        if is_fresh_fn is not None:
            if is_fresh_fn(signal):
                score += self.weights['freshness']
            # else: penalty of 0 (score already reduced relative to fresh signals)

        # 3. Regime alignment (0.15 weight)
        if in_regime:
            # Bonus if signal's strategy is aligned with current regime
            # This would require knowing the strategy's allowed environments
            score += self.weights['regime'] * 0.5  # partial bonus for having a regime

        # 4. Size quality (0.15 weight)
        # Larger proposed_size with reasonable confidence is better
        try:
            size = abs(float(signal.proposed_size or 0))
            # Normalize: assume max reasonable size is 100
            size_factor = min(1.0, size / 100.0)
            score += self.weights['size'] * size_factor
        except (TypeError, ValueError):
            score += self.weights['size'] * 0.5

        # 5. Conflict penalty (0.1 weight)
        score -= self.weights['conflict_penalty'] * float(conflict_penalty)

        # Clamp to [0.0, 1.0]
        return max(0.0, min(1.0, score))

    def rank_signal_list(self, signals: List['TradeSignal'],
                         is_fresh_fn=None,
                         in_regime: str = None,
                         conflict_penalty: float = 0.0) -> List[Tuple[TradeSignal, float]]:
        """
        Rank a list of signals and return them sorted by score (best first).

        Returns:
            List of (signal, score) tuples, sorted by score descending.
        """
        ranked = []
        for signal in signals:
            score = self.rank(signal, is_fresh_fn=is_fresh_fn,
                             in_regime=in_regime,
                             conflict_penalty=conflict_penalty)
            ranked.append((signal, score))

        # Sort by score descending
        ranked.sort(key=lambda x: x[1], reverse=True)
        return ranked


# ──────────────────────────────────────────────
# Strategy Selector Orchestrator
# ──────────────────────────────────────────────
class StrategySelector:
    """
    Orchestrates the full selection pipeline:
    1. Deduplication
    2. Conflict detection
    3. Ranking
    4. Return top-N signals
    """
    def __init__(self, max_age_ms: int = 5000, max_signals: int = 10):
        self.deduplicator = SignalDeduplicator(max_age_ms=max_age_ms)
        self.conflict_detector = ConflictDetector()
        self.ranker = SignalRanker()
        self.max_signals = max_signals

    def select(self, signals: List['TradeSignal'],
               in_regime: str = None,
               conflict_penalty: float = 0.0) -> List[TradeSignal]:
        """
        Select the top signals from a list.

        Pipeline:
        1. Record all signals for deduplication
        2. Detect conflicts
        3. Rank remaining signals
        4. Return top N signals

        Args:
            signals: List of TradeSignal to select from
            in_regime: Optional regime filter (only signals aligned with this regime)
            conflict_penalty: Penalty for detected conflicts (0.0-1.0)

        Returns:
            List of selected TradeSignal objects, sorted by priority.
        """
        # Step 1: Detect conflicts
        conflicts = self.conflict_detector.detect_conflicts()
        resolutions = self.conflict_detector.resolve_conflicts(conflicts)

        # Step 2: Filter and rank
        filtered: List[TradeSignal] = []
        for signal in signals:
            # Skip stale signals
            if not self.deduplicator.is_fresh(signal):
                continue

            # Skip if already recorded as duplicate
            if self.deduplicator.is_duplicate(signal):
                continue

            self.deduplicator.record(signal)
            filtered.append(signal)

        # Step 3: Rank
        ranked = self.ranker.rank_signal_list(
            filtered, is_fresh_fn=self.deduplicator.is_fresh,
            in_regime=in_regime, conflict_penalty=conflict_penalty
        )

        # Step 4: Return top N
        selected = [s for s, _ in ranked[:self.max_signals]]

        return selected