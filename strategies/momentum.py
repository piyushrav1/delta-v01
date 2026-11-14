"""
Strategy B: Momentum Burst

Momentum strategy using RSI burst combined with volume acceleration filter.

Key Features:
- RSI momentum cross/burst above 50 (bullish) or below 50 (bearish)
- Volume acceleration filter: current volume exceeds 2.0x 20-period volume SMA
- ATR-based volatility stops and profit targets
- Dynamic confidence scoring scaled by volume surge ratio
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Any
import pandas as pd
import numpy as np

from strategies.base import BaseStrategy, register_strategy
from analytics.indicators import rsi, atr
from analytics.signal import TradeSignal, buy_signal, sell_signal


@register_strategy
class Momentum(BaseStrategy):
    """
    Strategy B: Momentum Burst using RSI and Volume Acceleration.

    Parameters:
    - rsi_period: 14 (RSI lookback period)
    - rsi_center: 50.0 (neutral threshold)
    - volume_period: 20 (SMA period for volume baseline)
    - volume_multiplier: 2.0 (volume surge requirement)
    - atr_period: 14 (ATR lookback for stops)
    - atr_multiplier: 2.0 (stop distance in ATR multiples)
    - rr_ratio: 2.0 (risk/reward ratio)
    - warmup_period: 30 (minimum bars needed for RSI 14 and Volume SMA 20)
    """
    strategy_id: str = "momentum"
    strategy_version: str = "1.0.0"
    display_name: str = "Momentum Burst (Strategy B)"
    warmup_period: int = 30
    allowed_environments = {"BACKTEST", "PAPER", "TESTNET", "LIVE_AUTO"}

    def __init__(
        self,
        rsi_period: int = 14,
        rsi_center: float = 50.0,
        volume_period: int = 20,
        volume_multiplier: float = 2.0,
        atr_period: int = 14,
        atr_multiplier: float = 2.0,
        rr_ratio: float = 2.0,
        warmup_period: Optional[int] = None,
    ):
        self.rsi_period = rsi_period
        self.rsi_center = float(rsi_center)
        self.volume_period = volume_period
        self.volume_multiplier = float(volume_multiplier)
        self.atr_period = atr_period
        self.atr_multiplier = Decimal(str(atr_multiplier))
        self.rr_ratio = Decimal(str(rr_ratio))
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
        Record a new candle bar and evaluate for RSI momentum and volume burst signals.
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
        vol_series = df['volume']
        rsi_series = rsi(close_series, period=self.rsi_period)
        atr_series = atr(df['high'], df['low'], df['close'], period=self.atr_period)

        curr_close = Decimal(str(round(close_series.iloc[-1], 4)))
        curr_vol = vol_series.iloc[-1]
        avg_vol = vol_series.iloc[-self.volume_period - 1:-1].mean()
        if avg_vol <= 0:
            avg_vol = 1.0

        vol_ratio = curr_vol / avg_vol
        prev_rsi = rsi_series.iloc[-2]
        curr_rsi = rsi_series.iloc[-1]
        curr_atr = Decimal(str(round(max(atr_series.iloc[-1], 0.01), 4)))

        # Volume acceleration filter
        if vol_ratio < self.volume_multiplier:
            return None

        signal_id = f"sig_mom_{symbol}_{uuid.uuid4().hex[:8]}"
        now = datetime.now()
        expiry = now + timedelta(hours=1)

        # Scale confidence between 0.60 and 0.95
        vol_bonus = min(0.20, (vol_ratio - self.volume_multiplier) * 0.05)
        rsi_bonus = min(0.15, abs(curr_rsi - self.rsi_center) / 100.0)
        confidence = Decimal(str(round(min(0.95, 0.60 + vol_bonus + rsi_bonus), 2)))

        # Bullish Momentum Burst: RSI crosses above 50 or surges strongly above 50 with rising delta
        if (prev_rsi <= self.rsi_center and curr_rsi > self.rsi_center) or (curr_rsi > 55.0 and curr_rsi > prev_rsi):
            sl_dist = curr_atr * self.atr_multiplier
            stop_loss = max(Decimal("0.01"), curr_close - sl_dist)
            take_profit = curr_close + (sl_dist * self.rr_ratio)

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
                    "indicator": "RSI_Volume_Burst",
                    "direction": "bullish",
                    "rsi": float(curr_rsi),
                    "prev_rsi": float(prev_rsi),
                    "volume_ratio": float(vol_ratio),
                    "atr": float(curr_atr),
                }
            )

        # Bearish Momentum Burst: RSI crosses below 50 or falls strongly below 50 with falling delta
        if (prev_rsi >= self.rsi_center and curr_rsi < self.rsi_center) or (curr_rsi < 45.0 and curr_rsi < prev_rsi):
            sl_dist = curr_atr * self.atr_multiplier
            stop_loss = curr_close + sl_dist
            take_profit = max(Decimal("0.01"), curr_close - (sl_dist * self.rr_ratio))

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
                    "indicator": "RSI_Volume_Burst",
                    "direction": "bearish",
                    "rsi": float(curr_rsi),
                    "prev_rsi": float(prev_rsi),
                    "volume_ratio": float(vol_ratio),
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
