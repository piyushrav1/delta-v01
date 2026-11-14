"""
Strategy C: Mean Reversion

Statistical mean reversion strategy using Bollinger Bands 2.5σ overshoot
and RSI extremity/divergence.

Key Features:
- Bollinger Band 2.5σ extreme price deviation detection
- Mean-reversion target: Middle Band (20 SMA)
- ATR-based protective stop beyond the outer band
- RSI oversold/overbought filter
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Any
import pandas as pd
import numpy as np

from strategies.base import BaseStrategy, register_strategy
from analytics.indicators import bollinger_bands, rsi, atr
from analytics.signal import TradeSignal, buy_signal, sell_signal


@register_strategy
class MeanReversion(BaseStrategy):
    """
    Strategy C: Mean Reversion with Bollinger Bands and RSI Extremes.

    Parameters:
    - bb_window: 20 (Bollinger Band lookback period)
    - bb_num_std: 2.5 (standard deviation multiplier for extreme bands)
    - rsi_period: 14 (RSI lookback period)
    - rsi_oversold: 32.0 (oversold threshold for long entry)
    - rsi_overbought: 68.0 (overbought threshold for short entry)
    - atr_period: 14 (ATR lookback for stops)
    - atr_multiplier: 1.5 (protective stop beyond the extreme band)
    - warmup_period: 30 (minimum bars needed)
    """
    strategy_id: str = "mean_reversion"
    strategy_version: str = "1.0.0"
    display_name: str = "Mean Reversion (Strategy C)"
    warmup_period: int = 30
    allowed_environments = {"BACKTEST", "PAPER", "TESTNET", "LIVE_AUTO"}

    def __init__(
        self,
        bb_window: int = 20,
        bb_num_std: float = 2.5,
        rsi_period: int = 14,
        rsi_oversold: float = 32.0,
        rsi_overbought: float = 68.0,
        atr_period: int = 14,
        atr_multiplier: float = 1.5,
        warmup_period: Optional[int] = None,
    ):
        self.bb_window = bb_window
        self.bb_num_std = float(bb_num_std)
        self.rsi_period = rsi_period
        self.rsi_oversold = float(rsi_oversold)
        self.rsi_overbought = float(rsi_overbought)
        self.atr_period = atr_period
        self.atr_multiplier = Decimal(str(atr_multiplier))
        if warmup_period is not None:
            self.warmup_period = warmup_period

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
        Record candle and evaluate for Bollinger Band 2.5σ overshoot and RSI reversal.
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
            ).tail(self.warmup_period * 3)

        df = self._candle_history[symbol]
        if len(df) < self.warmup_period:
            return None

        # Indicators
        close_series = df['close']
        mid_bb, upper_bb, lower_bb = bollinger_bands(close_series, window=self.bb_window, num_std=self.bb_num_std)
        rsi_series = rsi(close_series, period=self.rsi_period)
        atr_series = atr(df['high'], df['low'], df['close'], period=self.atr_period)

        curr_close = Decimal(str(round(close_series.iloc[-1], 4)))
        curr_mid = Decimal(str(round(mid_bb.iloc[-1], 4)))
        curr_upper = Decimal(str(round(upper_bb.iloc[-1], 4)))
        curr_lower = Decimal(str(round(lower_bb.iloc[-1], 4)))
        curr_rsi = rsi_series.iloc[-1]
        curr_atr = Decimal(str(round(max(atr_series.iloc[-1], 0.01), 4)))

        signal_id = f"sig_mr_{symbol}_{uuid.uuid4().hex[:8]}"
        now = datetime.now()
        expiry = now + timedelta(hours=1)

        # 1. Long at Oversold: Price breaches or touches Lower Band + RSI oversold
        if curr_close <= curr_lower and curr_rsi <= self.rsi_oversold:
            stop_loss = max(Decimal("0.01"), curr_close - (curr_atr * self.atr_multiplier))
            take_profit = curr_mid  # Target the mean (middle band)

            # Ensure positive reward
            if take_profit > curr_close:
                # Confidence proportional to depth of RSI oversold
                confidence = Decimal(str(round(min(0.95, 0.65 + (self.rsi_oversold - curr_rsi) * 0.01), 2)))

                return buy_signal(
                    signal_id=signal_id,
                    strategy_id=self.strategy_id,
                    strategy_version=self.strategy_version,
                    symbol=symbol,
                    entry_price=curr_close,
                    stop_loss=round(stop_loss, 4),
                    take_profit=round(take_profit, 4),
                    proposed_size=1,
                    confidence=confidence,
                    expiry_time=expiry,
                    rationale={
                        "indicator": "Bollinger_Overshoot",
                        "direction": "long_oversold",
                        "lower_band": float(curr_lower),
                        "mid_band": float(curr_mid),
                        "rsi": float(curr_rsi),
                        "atr": float(curr_atr),
                    }
                )

        # 2. Short at Overbought: Price breaches or touches Upper Band + RSI overbought
        if curr_close >= curr_upper and curr_rsi >= self.rsi_overbought:
            stop_loss = curr_close + (curr_atr * self.atr_multiplier)
            take_profit = curr_mid  # Target the mean (middle band)

            if take_profit < curr_close:
                confidence = Decimal(str(round(min(0.95, 0.65 + (curr_rsi - self.rsi_overbought) * 0.01), 2)))

                return sell_signal(
                    signal_id=signal_id,
                    strategy_id=self.strategy_id,
                    strategy_version=self.strategy_version,
                    symbol=symbol,
                    entry_price=curr_close,
                    stop_loss=round(stop_loss, 4),
                    take_profit=round(take_profit, 4),
                    proposed_size=1,
                    confidence=confidence,
                    expiry_time=expiry,
                    rationale={
                        "indicator": "Bollinger_Overshoot",
                        "direction": "short_overbought",
                        "upper_band": float(curr_upper),
                        "mid_band": float(curr_mid),
                        "rsi": float(curr_rsi),
                        "atr": float(curr_atr),
                    }
                )

        return None

    def on_quote(self, quote: Any) -> Optional[TradeSignal]:
        return None

    def generate_signals(self, quotes: Dict[str, Any]) -> List[TradeSignal]:
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
