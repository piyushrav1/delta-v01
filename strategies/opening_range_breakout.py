"""
Opening Range Breakout (ORB) Strategy
Adapted from day-trader-skill (trader-entry) for 24/7 Crypto Futures.

Strategy Mechanics:
- Establishes an opening range of N bars (default 3 bars, e.g. 15-min on 5m candles).
- Computes Range High, Range Low, Midpoint, and Average Range Volume.
- Bullish Trigger: First completed candle that CLOSES strictly above Range High
  with volume >= min_volume_x * average opening range volume.
- Bearish Trigger: First completed candle that CLOSES strictly below Range Low
  with volume >= min_volume_x * average opening range volume.
- Protective Stop-Loss placed at Range Midpoint (as in day-trader-skill exit logic).
- Take-Profit set to R:R multiple (default 2.0R) of the risk distance.

Strict Architecture Compliance:
- Inherits from BaseStrategy, registers with @register_strategy.
- Zero imports from exchange/ or trading/ modules.
- Emits canonical TradeSignal with Decimal precision.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Any, Union
import pandas as pd
import numpy as np

from strategies.base import BaseStrategy, register_strategy
from analytics.signal import TradeSignal, buy_signal, sell_signal


@register_strategy
class OpeningRangeBreakout(BaseStrategy):
    """
    Opening Range Breakout Strategy (ORB).
    """
    strategy_id: str = "opening_range_breakout"
    strategy_version: str = "1.0.0"
    display_name: str = "Opening Range Breakout (ORB)"
    warmup_period: int = 3
    allowed_environments = {"BACKTEST", "PAPER", "TESTNET", "LIVE_AUTO"}

    def __init__(
        self,
        range_bars: int = 3,
        min_volume_x: float = 1.0,
        rr_ratio: float = 2.0,
        stop_at_midpoint: bool = True,
        allow_short: bool = True,
        warmup_period: Optional[int] = None,
    ):
        self.range_bars = range_bars
        self.min_volume_x = float(min_volume_x)
        self.rr_ratio = Decimal(str(rr_ratio))
        self.stop_at_midpoint = stop_at_midpoint
        self.allow_short = allow_short
        if warmup_period is not None:
            self.warmup_period = warmup_period
        else:
            self.warmup_period = range_bars

        # State tracking per symbol
        # symbol -> { 'bars': pd.DataFrame, 'range_high': float, 'range_low': float,
        #             'range_mid': float, 'avg_vol': float, 'range_complete': bool, 'triggered': bool }
        self._state: Dict[str, Dict[str, Any]] = {}

    def _init_symbol_state(self, symbol: str) -> Dict[str, Any]:
        state = {
            "bars": pd.DataFrame(columns=["open", "high", "low", "close", "volume", "timestamp"]),
            "range_high": None,
            "range_low": None,
            "range_mid": None,
            "avg_vol": None,
            "range_complete": False,
            "triggered": False,
        }
        self._state[symbol] = state
        return state

    def reset_range(self, symbol: Optional[str] = None):
        """Reset the opening range state (e.g. at the start of a new trading window)."""
        if symbol:
            self._init_symbol_state(symbol)
        else:
            self._state.clear()

    def add_candle_data(self, symbol: str, df: pd.DataFrame):
        """Preload candle history."""
        required = {'open', 'high', 'low', 'close', 'volume'}
        if not required.issubset(set(df.columns)):
            raise ValueError(f"DataFrame must contain columns {required}")
        state = self._state.get(symbol) or self._init_symbol_state(symbol)
        state["bars"] = df.copy().reset_index(drop=True)
        self._recompute_range(symbol)

    def _recompute_range(self, symbol: str):
        state = self._state[symbol]
        df = state["bars"]
        if len(df) >= self.range_bars:
            range_df = df.iloc[:self.range_bars]
            state["range_high"] = float(range_df["high"].max())
            state["range_low"] = float(range_df["low"].min())
            state["range_mid"] = (state["range_high"] + state["range_low"]) / 2.0
            state["avg_vol"] = float(range_df["volume"].mean())
            state["range_complete"] = True

    def on_candle(
        self,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: int,
        timestamp: datetime,
        symbol: str = "BTCUSD",
    ) -> Optional[TradeSignal]:
        """
        Ingest a new candle and evaluate for ORB breakout triggers.
        """
        state = self._state.get(symbol)
        if state is None:
            state = self._init_symbol_state(symbol)

        new_row = pd.DataFrame([{
            'open': float(open_),
            'high': float(high),
            'low': float(low),
            'close': float(close),
            'volume': float(volume),
            'timestamp': timestamp,
        }])

        state["bars"] = pd.concat([state["bars"], new_row], ignore_index=True)
        bar_count = len(state["bars"])

        # Forming opening range
        if bar_count < self.range_bars:
            return None

        # Once reaching range_bars, finalize range if not yet done
        if not state["range_complete"]:
            self._recompute_range(symbol)
            return None  # The final range bar itself does not trigger; subsequent bars trigger

        # If already triggered for this range window, do not trigger duplicate entries
        if state["triggered"]:
            return None

        range_high = state["range_high"]
        range_low = state["range_low"]
        range_mid = state["range_mid"]
        avg_vol = state["avg_vol"] or 1.0

        curr_close = Decimal(str(round(close, 4)))
        vol_ratio = float(volume) / max(avg_vol, 1.0)
        has_vol = vol_ratio >= self.min_volume_x

        signal_id = f"sig_orb_{symbol}_{uuid.uuid4().hex[:8]}"
        now = datetime.now()
        expiry = now + timedelta(hours=2)

        # Bullish ORB: close strictly above range_high
        if close > range_high and has_vol:
            state["triggered"] = True
            entry = curr_close
            sl_price = Decimal(str(round(range_mid if self.stop_at_midpoint else range_low, 4)))
            risk = entry - sl_price
            if risk <= Decimal("0"):
                sl_price = entry * Decimal("0.99")
                risk = entry - sl_price

            tp_price = entry + (risk * self.rr_ratio)
            pct_over = ((close - range_high) / range_high) * 100.0
            confidence = min(Decimal("0.95"), max(Decimal("0.55"), Decimal("0.65") + Decimal(str(round(pct_over / 10.0, 2)))))

            return buy_signal(
                signal_id=signal_id,
                strategy_id=self.strategy_id,
                strategy_version=self.strategy_version,
                symbol=symbol,
                entry_price=entry,
                stop_loss=round(sl_price, 4),
                take_profit=round(tp_price, 4),
                proposed_size=1,
                confidence=confidence,
                expiry_time=expiry,
                rationale={
                    "setup": "orb_bullish_breakout",
                    "range_bars": self.range_bars,
                    "range_high": range_high,
                    "range_low": range_low,
                    "range_mid": range_mid,
                    "trigger_close": float(close),
                    "volume_ratio": round(vol_ratio, 2),
                    "pct_over_range": round(pct_over, 2),
                }
            )

        # Bearish ORB: close strictly below range_low (if shorting allowed)
        if self.allow_short and close < range_low and has_vol:
            state["triggered"] = True
            entry = curr_close
            sl_price = Decimal(str(round(range_mid if self.stop_at_midpoint else range_high, 4)))
            risk = sl_price - entry
            if risk <= Decimal("0"):
                sl_price = entry * Decimal("1.01")
                risk = sl_price - entry

            tp_price = entry - (risk * self.rr_ratio)
            pct_under = ((range_low - close) / range_low) * 100.0
            confidence = min(Decimal("0.95"), max(Decimal("0.55"), Decimal("0.65") + Decimal(str(round(pct_under / 10.0, 2)))))

            return sell_signal(
                signal_id=signal_id,
                strategy_id=self.strategy_id,
                strategy_version=self.strategy_version,
                symbol=symbol,
                entry_price=entry,
                stop_loss=round(sl_price, 4),
                take_profit=round(tp_price, 4),
                proposed_size=1,
                confidence=confidence,
                expiry_time=expiry,
                rationale={
                    "setup": "orb_bearish_breakdown",
                    "range_bars": self.range_bars,
                    "range_high": range_high,
                    "range_low": range_low,
                    "range_mid": range_mid,
                    "trigger_close": float(close),
                    "volume_ratio": round(vol_ratio, 2),
                    "pct_under_range": round(pct_under, 2),
                }
            )

        return None

    def on_quote(self, quote: Any) -> Optional[TradeSignal]:
        return None

    def generate_signals(self, quotes: Dict[str, Any]) -> List[TradeSignal]:
        signals: List[TradeSignal] = []
        for symbol in quotes.keys():
            if symbol in self._state:
                df = self._state[symbol]["bars"]
                if len(df) > self.range_bars:
                    last_row = df.iloc[-1]
                    sig = self.on_candle(
                        open_=last_row['open'],
                        high=last_row['high'],
                        low=last_row['low'],
                        close=last_row['close'],
                        volume=last_row['volume'],
                        timestamp=last_row.get('timestamp', datetime.now()),
                        symbol=symbol
                    )
                    if sig:
                        signals.append(sig)
        return signals

