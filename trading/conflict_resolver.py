"""
Cross-Strategy Conflict Detection & Regime Arbitration

Detects conflicting signals across strategies (e.g., Trend Following LONG
vs. Mean Reversion SHORT on the same symbol) and arbitrates using
higher-timeframe market regime and statistical confidence.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple, Set
from decimal import Decimal

from analytics.signal import TradeSignal
from analytics.market_regime import MarketRegime

logger = logging.getLogger(__name__)


class ConflictResolver:
    """
    Arbitrates directional collisions between competing strategies.

    Rules:
    1. If a symbol has only one directional bias (all BUY or all SELL), no conflict.
    2. If a symbol has both BUY and SELL signals:
       - If higher-timeframe regime is TRENDING_BULL, select BUY, discard SELL.
       - If higher-timeframe regime is TRENDING_BEAR, select SELL, discard BUY.
       - If regime is RANGING (LOW/HIGH VOL), favor Mean Reversion strategy.
       - If regime is BREAKOUT, favor Momentum strategy.
       - If regime is UNKNOWN/AMBIGUOUS:
         - Compare confidence scores. If delta >= min_confidence_edge (default 0.20),
           select higher confidence.
         - Otherwise suppress BOTH to prevent whipsaw losses.
    """

    def __init__(self, min_confidence_edge: float = 0.20):
        self.min_confidence_edge = Decimal(str(min_confidence_edge))

    def detect_conflicts(self, signals: List[TradeSignal]) -> Dict[str, List[TradeSignal]]:
        """Group signals by symbol that exhibit opposing BUY and SELL directions."""
        by_symbol: Dict[str, List[TradeSignal]] = {}
        for s in signals:
            by_symbol.setdefault(s.symbol, []).append(s)

        conflicts: Dict[str, List[TradeSignal]] = {}
        for sym, sigs in by_symbol.items():
            directions = {s.direction for s in sigs}
            if "BUY" in directions and "SELL" in directions:
                conflicts[sym] = sigs

        return conflicts

    def resolve(
        self,
        signals: List[TradeSignal],
        regimes: Optional[Dict[str, MarketRegime]] = None,
    ) -> List[TradeSignal]:
        """
        Filter incoming signals, resolving any directional conflicts.

        Args:
            signals: List of incoming TradeSignal objects.
            regimes: Optional dict of symbol -> MarketRegime.

        Returns:
            Resolved list of non-conflicting TradeSignal objects.
        """
        if not signals:
            return []

        regimes = regimes or {}
        by_symbol: Dict[str, List[TradeSignal]] = {}
        for s in signals:
            by_symbol.setdefault(s.symbol, []).append(s)

        resolved_signals: List[TradeSignal] = []

        for symbol, sigs in by_symbol.items():
            directions = {s.direction for s in sigs}
            if len(directions) <= 1:
                # No conflict: all BUY or all SELL
                resolved_signals.extend(sigs)
                continue

            # Directional conflict exists
            regime = regimes.get(symbol)
            buys = [s for s in sigs if s.direction == "BUY"]
            sells = [s for s in sigs if s.direction == "SELL"]

            best_buy = max(buys, key=lambda s: s.confidence)
            best_sell = max(sells, key=lambda s: s.confidence)

            # Arbitration via Market Regime
            if regime == MarketRegime.TRENDING_BULL:
                logger.info(f"Conflict on {symbol}: Bull trend regime arbitrates in favor of BUY")
                resolved_signals.append(best_buy)
                continue
            elif regime == MarketRegime.TRENDING_BEAR:
                logger.info(f"Conflict on {symbol}: Bear trend regime arbitrates in favor of SELL")
                resolved_signals.append(best_sell)
                continue
            elif regime in (MarketRegime.RANGING_LOW_VOL, MarketRegime.RANGING_HIGH_VOL):
                # Check if one is mean reversion
                mr_signals = [s for s in sigs if "mean_reversion" in s.strategy_id]
                if mr_signals:
                    logger.info(f"Conflict on {symbol}: Ranging regime favors Mean Reversion {mr_signals[0].direction}")
                    resolved_signals.append(mr_signals[0])
                    continue
            elif regime == MarketRegime.BREAKOUT_EXPANSION:
                mom_signals = [s for s in sigs if "momentum" in s.strategy_id]
                if mom_signals:
                    logger.info(f"Conflict on {symbol}: Breakout regime favors Momentum {mom_signals[0].direction}")
                    resolved_signals.append(mom_signals[0])
                    continue

            # Fallback to Confidence Edge
            conf_diff = best_buy.confidence - best_sell.confidence
            if conf_diff >= self.min_confidence_edge:
                logger.info(f"Conflict on {symbol}: Confidence edge favors BUY ({best_buy.confidence} vs {best_sell.confidence})")
                resolved_signals.append(best_buy)
            elif -conf_diff >= self.min_confidence_edge:
                logger.info(f"Conflict on {symbol}: Confidence edge favors SELL ({best_sell.confidence} vs {best_buy.confidence})")
                resolved_signals.append(best_sell)
            else:
                logger.warning(
                    f"Conflict on {symbol} unresolved: Confidence difference {abs(conf_diff)} < {self.min_confidence_edge}. "
                    "Suppressing both signals to avoid whipsaw."
                )
                # Suppress both

        return resolved_signals

