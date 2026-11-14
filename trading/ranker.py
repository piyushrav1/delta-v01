"""
Multi-Factor Signal Ranking Engine

Ranks candidate TradeSignals across multiple quantitative factors:
1. Reward-to-Risk ratio (R:R)
2. Strategy statistical confidence
3. Market regime alignment
4. Order book liquidity depth
5. Estimated taker fee and execution drag
"""
from __future__ import annotations

from typing import List, Dict, Optional, Tuple, Any
from decimal import Decimal
import logging

from analytics.signal import TradeSignal
from analytics.market_regime import MarketRegime

logger = logging.getLogger(__name__)


class SignalRanker:
    """
    Ranks TradeSignals into an authoritative score in [0.0, 1.0].

    Weights:
    - Confidence: 0.35
    - Reward-to-Risk Ratio: 0.30
    - Regime Alignment: 0.20
    - Liquidity / Execution Cost: 0.15
    """

    def __init__(
        self,
        weight_confidence: float = 0.35,
        weight_rr: float = 0.30,
        weight_regime: float = 0.20,
        weight_liquidity: float = 0.15,
        default_taker_fee_pct: float = 0.0005,  # 0.05% Delta taker fee
    ):
        self.w_conf = float(weight_confidence)
        self.w_rr = float(weight_rr)
        self.w_regime = float(weight_regime)
        self.w_liq = float(weight_liquidity)
        self.taker_fee_pct = float(default_taker_fee_pct)

    def calculate_score(
        self,
        signal: TradeSignal,
        regime: Optional[MarketRegime] = None,
        quote: Optional[Any] = None,
    ) -> float:
        """
        Calculate composite quantitative rank score for a single TradeSignal.
        """
        # Factor 1: Strategy Confidence
        conf_score = float(signal.confidence)

        # Factor 2: Reward-to-Risk Ratio
        entry = float(signal.target_entry_price)
        sl = float(signal.stop_loss_price)
        tp = float(signal.take_profit_price)

        risk = abs(entry - sl) if sl > 0 else (entry * 0.02)
        reward = abs(tp - entry) if tp > 0 else (entry * 0.04)

        if risk > 0:
            rr = reward / risk
            # Normalize R:R (e.g., 2:1 -> 0.67, 3:1 -> 1.0)
            rr_score = min(1.0, max(0.0, rr / 3.0))
        else:
            rr_score = 0.5

        # Factor 3: Regime Alignment
        regime_score = 0.5  # Neutral default
        if regime is not None:
            if regime == MarketRegime.TRENDING_BULL:
                regime_score = 0.9 if signal.direction == "BUY" else 0.1
            elif regime == MarketRegime.TRENDING_BEAR:
                regime_score = 0.9 if signal.direction == "SELL" else 0.1
            elif regime in (MarketRegime.RANGING_LOW_VOL, MarketRegime.RANGING_HIGH_VOL):
                regime_score = 0.85 if "mean_reversion" in signal.strategy_id or "options" in signal.strategy_id else 0.4
            elif regime == MarketRegime.BREAKOUT_EXPANSION:
                regime_score = 0.9 if "momentum" in signal.strategy_id else 0.5

        # Factor 4: Liquidity Depth & Fee Drag
        liq_score = 0.8  # Good liquidity default
        if quote is not None:
            bid = getattr(quote, 'bid', None) or (quote.get('bid') if isinstance(quote, dict) else None)
            ask = getattr(quote, 'ask', None) or (quote.get('ask') if isinstance(quote, dict) else None)
            if bid is not None and ask is not None and float(bid) > 0 and float(ask) > float(bid):
                spread_pct = (float(ask) - float(bid)) / float(bid)
                # Lower spread = higher score
                liq_score = max(0.1, min(1.0, 1.0 - (spread_pct * 100.0)))

        # Weighted combination
        composite = (
            (self.w_conf * conf_score) +
            (self.w_rr * rr_score) +
            (self.w_regime * regime_score) +
            (self.w_liq * liq_score)
        )

        return round(max(0.0, min(1.0, composite)), 4)

    def rank(
        self,
        signals: List[TradeSignal],
        regimes: Optional[Dict[str, MarketRegime]] = None,
        quotes: Optional[Dict[str, Any]] = None,
    ) -> List[Tuple[TradeSignal, float]]:
        """
        Rank a list of signals descending by composite score.
        """
        regimes = regimes or {}
        quotes = quotes or {}

        scored = []
        for s in signals:
            score = self.calculate_score(
                signal=s,
                regime=regimes.get(s.symbol),
                quote=quotes.get(s.symbol),
            )
            scored.append((s, score))

        scored.sort(key=lambda item: item[1], reverse=True)
        return scored

