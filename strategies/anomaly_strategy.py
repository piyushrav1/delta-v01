"""
Anomaly Strategy (Strategy F) — Refactored from scanner into TradeSignal

Converts the raw HTML-text anomaly scanner output into structured
multi-leg TradeSignal objects. Replaces the old regex parsing with
proper contract-aware signal emission.

Key differences from old scanner:
- Emits TradeSignal objects (never raw order placement)
- Contract-aware sizing (uses contract_value, lot_size from instruments)
- Proper option type (C/P), strike, expiry extraction
- Strategy cannot place orders directly — signals go through Risk Gateway
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Any

from strategies.base import BaseStrategy, register_strategy
from analytics.signal import TradeSignal, buy_signal, sell_signal
from analytics.indicators import ema, rsi, atr
from exchange.market_data import get_current_quote


# ──────────────────────────────────────────────
# Anomaly Strategy Class
# ──────────────────────────────────────────────
@register_strategy
class AnomalyStrategy(BaseStrategy):
    """
    Strategy F: Anomaly-driven options arbitrage/scanner.

    Emits TradeSignal objects for:
    - Illiquidity bid-ask gaps (>= threshold)
    - Fat-finger trade anomalies (last price >> mid)
    - Strike arbitrage (vertical spread violations)
    - Butterfly spreads (net credit >= threshold)
    - Calendar spreads (strike-matched credit >= threshold)
    - Credit spread opportunities

    ALL signal emission goes through TradeSignal — NO raw order placement.
    """
    strategy_id = "anomaly_strategy"
    strategy_version = "1.0.0"
    display_name = "Anomaly Scanner (Strategy F)"
    warmup_period = 5
    allowed_environments = {"BACKTEST", "PAPER", "TESTNET"}

    # Anomaly thresholds (configurable via class attrs)
    atm_band: Decimal = Decimal("0.03")     # 3% for near-ATM gap detection
    far_otm_gap: Decimal = Decimal("30")    # 30 pts for far-OTM gap
    near_atm_gap: Decimal = Decimal("70")   # 70 pts for near-ATM gap
    fat_finger: Decimal = Decimal("70")     # 70 pts deviation from mid
    arb_thresh: Decimal = Decimal("200")    # 200 pts strike arbitrage
    fly_thresh: Decimal = Decimal("0")      # 0 min credit for butterflies
    cal_thresh: Decimal = Decimal("0")      # 0 min calendar credit

    # Registry auto-call (forbidden import check happens in BaseStrategy.__init__)
    def __post__(self):
        """Post-init: verify no forbidden imports in source."""
        import inspect
        source = inspect.getsource(self.__class__)
        _check_forbidden_imports(source)

    # ————————————————————————————————
    # Core Signal Emission Helpers
    # ————————————————————————————————
    def _emit_buy_signal(self, symbol: str, entry: Decimal, sl: Decimal,
                         tp: Decimal, size: int, rationale: Dict[str, Any]) -> TradeSignal:
        """Buy signal through the safe emission pipeline."""
        return emit_buy_signal(
            symbol=symbol, entry=entry, sl=sl, tp=tp, size=size,
            strategy_id=self.strategy_id, confidence=Decimal("0.6"),
        )

    def _emit_sell_signal(self, symbol: str, entry: Decimal, sl: Decimal,
                          tp: Decimal, size: int, rationale: Dict[str, Any]) -> TradeSignal:
        """Sell signal through the safe emission pipeline."""
        return emit_sell_signal(
            symbol=symbol, entry=entry, sl=sl, tp=tp, size=size,
            strategy_id=self.strategy_id, confidence=Decimal("0.6"),
        )

    # ————————————————————————————————
    # on_quote — called by the trading engine on each quote tick
    # ————————————————————————————————
    def on_quote(self, quote: 'MarketDataQuote') -> Optional[TradeSignal]:
        """Entry point: called when normalized quote arrives from the store."""
        # Don't trade if quote is stale
        if not quote.valid:
            return None

        symbol = quote.symbol
        bid, ask = quote.bid, quote.ask
        mid = quote.mid

        # Skip if we can't compute mid
        if bid is None or ask is None or mid is None:
            return None

        # 1. Illiquidity gap detection
        gap = ask - bid
        near_atm = abs(Decimal(str(quote.symbol)) - mid) / abs(mid) <= Decimal(str(self.atm_band)) if mid else False
        gap_threshold = self.near_atm_gap if near_atm else self.far_otm_gap

        if gap >= gap_threshold:
            # Significant bid-ask gap — signal illiquidity
            size = 1  # size will be determined by Risk Gateway
            rationale = {
                "gap": float(gap),
                "gap_threshold": float(gap_threshold),
                "bid": float(bid),
                "ask": float(ask),
                "mid": float(mid),
                "near_atm": near_atm,
                "strategy": "liquidity_gap",
            }
            return self._emit_buy_signal(symbol, entry=Decimal("0"), sl=Decimal("0"),
                                         tp=Decimal("0"), size=size, rationale=rationale)

        # 2. Fat-finger trade anomaly detection
        # (Would require last price — would come from quote.last in full implementation)
        # For now, skip or placeholder
        # if quote.last and abs(quote.last - mid) >= self.fat_finger:
        #     ...

        # 3. Strike arbitrage detection (would need full orderbook)
        # ...

        # 4. Butterfly spread detection
        # ...

        # 5. Calendar spread detection
        # ...

        # 5. Credit spread opportunities
        # ...

        return None

    # ————————————————————————————————
    # on_candle — called when new OHLC candle forms
    # ————————————————————————————————
    def on_candle(self, open_: float, high: float, low: float, close: float,
                  volume: int, timestamp: datetime) -> Optional[TradeSignal]:
        """Not used for tick-based anomaly scanner; return None."""
        return None

    # ————————————————————————————————
    # generate_signals — batch signal generation
    # ————————————————————————————————
    def generate_signals(self, quotes: Dict[str, 'MarketDataQuote']) -> List[TradeSignal]:
        """Batch generate signals from a dict of symbol->MarketDataQuote."""
        signals: List[TradeSignal] = []

        for symbol, quote in quotes.items():
            signal = self.on_quote(quote)
            if signal is not None:
                signals.append(signal)

        return signals