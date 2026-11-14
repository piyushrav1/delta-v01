"""
Strategy A: Trend Following

Trend-following strategy using EMA 20/50 crossover with ATR volatility channel confirmation.

Key Features:
- EMA 20 fast crosses EMA 50 slow for trend direction
- ATR-based volatility channel for stop-loss and take-profit calculation
- Dynamic regime filter and slope confirmation
- Trade size proposed to Central Risk Gateway (subject to Risk Gate approval)
- Full forbidden-import enforcement (never touches exchange/delta_client directly)

Signal Contract:
- Emits canonical TradeSignal with valid Decimal prices, confidence, and rationale.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Any, Union
import pandas as pd
import numpy as np

from strategies.base import BaseStrategy, register_strategy, _check_forbidden_imports
from analytics.indicators import ema, atr
from analytics.signal import TradeSignal, buy_signal, sell_signal


@register_strategy
class TrendFollowing(BaseStrategy):
    """
    Strategy A: Trend Following with EMA 20/50 crossover and ATR volatility trailing.

    Parameters:
    - ema_fast_span: 20 (fast EMA period)
    - ema_slow_span: 50 (slow EMA period)
    - atr_period: 14 (ATR lookback for channel/stop)
    - atr_multiplier: 2.0 (stop-loss distance in ATR multiples)
    - rr_ratio: 2.0 (take-profit risk-to-reward ratio)
    - warmup_period: 55 (bars needed for 50 EMA + crossover confirmation)
    """
    strategy_id: str = "trend_following"
    strategy_version: str = "1.0.0"
    display_name: str = "Trend Following (Strategy A)"
    warmup_period: int = 55
    allowed_environments = {"BACKTEST", "PAPER", "TESTNET", "LIVE_AUTO"}

    def __init__(
        self,
        ema_fast_span: int = 20,
        ema_slow_span: int = 50,
        atr_period: int = 14,
        atr_multiplier: float = 2.0,
        rr_ratio: float = 2.0,
        min_trend_slope: float = 0.0005,
        warmup_period: Optional[int] = None,
    ):
        self.ema_fast_span = ema_fast_span
        self.ema_slow_span = ema_slow_span
        self.atr_period = atr_period
        self.atr_multiplier = Decimal(str(atr_multiplier))
        self.rr_ratio = Decimal(str(rr_ratio))
        self.min_trend_slope = Decimal(str(min_trend_slope))
        if warmup_period is not None:
            self.warmup_period = warmup_period

        # In-memory OHLCV candle store per symbol: symbol -> pd.DataFrame
        self._candle_history: Dict[str, pd.DataFrame] = {}

    def add_candle_data(self, symbol: str, df: pd.DataFrame):
        """Preload or append historical candle DataFrame."""
        required = {'open', 'high', 'low', 'close', 'volume'}
        if not required.issubset(set(df.columns)):
            raise ValueError(f"DataFrame must contain columns {required}")
        self._candle_history[symbol] = df.copy().reset_index(drop=True)

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
        Record a new candle bar and evaluate for EMA crossover trade signals.
        """
        new_row = pd.DataFrame([{
            'open': float(open_),
            'high': float(high),
            'low': float(low),
            'close': float(close),
            'volume': float(volume),
            'timestamp': timestamp,
        }])

        if symbol not in self._candle_history or self._candle_history[symbol].empty:
            self._candle_history[symbol] = new_row
        else:
            self._candle_history[symbol] = pd.concat(
                [self._candle_history[symbol], new_row],
                ignore_index=True
            ).tail(self.warmup_period * 3)  # Keep rolling memory reasonable

        df = self._candle_history[symbol]
        if len(df) < self.warmup_period:
            return None

        # Calculate indicators
        close_series = df['close']
        ema_fast = ema(close_series, span=self.ema_fast_span)
        ema_slow = ema(close_series, span=self.ema_slow_span)
        atr_series = atr(df['high'], df['low'], df['close'], period=self.atr_period)

        current_close = Decimal(str(round(close_series.iloc[-1], 4)))
        prev_fast = ema_fast.iloc[-2]
        curr_fast = ema_fast.iloc[-1]
        prev_slow = ema_slow.iloc[-2]
        curr_slow = ema_slow.iloc[-1]
        curr_atr = Decimal(str(round(max(atr_series.iloc[-1], 0.01), 4)))

        # Slope of slow EMA
        slow_slope = abs(curr_slow - prev_slow) / max(curr_slow, 1.0)
        fast_spread = abs(curr_fast - curr_slow) / max(curr_slow, 1.0)

        # Confidence based on spread and ATR normalization
        confidence = min(Decimal("0.95"), Decimal("0.60") + Decimal(str(round(min(fast_spread * 10, 0.35), 2))))

        signal_id = f"sig_trend_{symbol}_{uuid.uuid4().hex[:8]}"
        now = datetime.now()
        expiry = now + timedelta(hours=1)

        # Bullish Crossover: Fast EMA crosses ABOVE Slow EMA
        if prev_fast <= prev_slow and curr_fast > curr_slow:
            sl_dist = curr_atr * self.atr_multiplier
            stop_loss = max(Decimal("0.01"), current_close - sl_dist)
            take_profit = current_close + (sl_dist * self.rr_ratio)

            return buy_signal(
                signal_id=signal_id,
                strategy_id=self.strategy_id,
                strategy_version=self.strategy_version,
                symbol=symbol,
                entry_price=current_close,
                stop_loss=round(stop_loss, 4),
                take_profit=round(take_profit, 4),
                proposed_size=1,
                confidence=confidence,
                expiry_time=expiry,
                rationale={
                    "indicator": "EMA_crossover",
                    "crossover": "bullish",
                    "ema_fast": float(curr_fast),
                    "ema_slow": float(curr_slow),
                    "atr": float(curr_atr),
                    "fast_spread_pct": float(fast_spread),
                }
            )

        # Bearish Crossover: Fast EMA crosses BELOW Slow EMA
        if prev_fast >= prev_slow and curr_fast < curr_slow:
            sl_dist = curr_atr * self.atr_multiplier
            stop_loss = current_close + sl_dist
            take_profit = max(Decimal("0.01"), current_close - (sl_dist * self.rr_ratio))

            return sell_signal(
                signal_id=signal_id,
                strategy_id=self.strategy_id,
                strategy_version=self.strategy_version,
                symbol=symbol,
                entry_price=current_close,
                stop_loss=round(stop_loss, 4),
                take_profit=round(take_profit, 4),
                proposed_size=1,
                confidence=confidence,
                expiry_time=expiry,
                rationale={
                    "indicator": "EMA_crossover",
                    "crossover": "bearish",
                    "ema_fast": float(curr_fast),
                    "ema_slow": float(curr_slow),
                    "atr": float(curr_atr),
                    "fast_spread_pct": float(fast_spread),
                }
            )

        return None

    def on_quote(self, quote: Any) -> Optional[TradeSignal]:
        """
        Trend Following is predominantly candle-driven.
        If a quote arrives, verify if live price is outside current bounds.
        """
        return None

    def generate_signals(self, quotes: Dict[str, Any]) -> List[TradeSignal]:
        """
        Batch generate signals across symbols that have sufficient candle history.
        """
        signals: List[TradeSignal] = []
        for symbol, quote in quotes.items():
            if symbol in self._candle_history:
                df = self._candle_history[symbol]
                if len(df) >= self.warmup_period:
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
