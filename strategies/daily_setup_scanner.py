"""
Daily Setup Scanner Strategy
Adapted from day-trader-skill (trader-scanner) for Crypto Perpetuals & Futures.

Features the 5 Core Algorithmic Setups from day-trader-skill:
1. breakout_20d: Last close is strictly above every close of the preceding 20 bars.
2. bull_flag: Trend surge (>10% gain over bars [-15:-5]) followed by tight consolidation
   (last 5 closes within a 5% range).
3. reclaim_ma20: Price closed below the 20-period SMA on bar t-1, and reclaimed/closed
   above the 20-period SMA on bar t.
4. oversold_bounce: RSI 14 (Wilder) was under 30 on bar t-1, and bar t closed positive
   (close[t] > close[t-1]).
5. oversold: RSI 14 (Wilder) under 30 (bearish continuation / capitulation signal).

Risk Management:
- Dynamic ATR(14) stop-loss & take-profit brackets.
- Bullish setups emit BUY signals; oversold momentum emits SELL signals.
- Confidence dynamically scored based on setup strength.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Any, Union, Tuple
import pandas as pd
import numpy as np

from strategies.base import BaseStrategy, register_strategy
from analytics.indicators import rsi, atr
from analytics.signal import TradeSignal, buy_signal, sell_signal


@register_strategy
class DailySetupScanner(BaseStrategy):
    """
    Daily Multi-Setup Scanner Strategy.
    """
    strategy_id: str = "daily_setup_scanner"
    strategy_version: str = "1.0.0"
    display_name: str = "Daily Setup Scanner"
    warmup_period: int = 25
    allowed_environments = {"BACKTEST", "PAPER", "TESTNET", "LIVE_AUTO"}

    def __init__(
        self,
        breakout_lookback: int = 20,
        flag_pole_bars: int = 10,
        flag_consolidation_bars: int = 5,
        flag_min_gain_pct: float = 10.0,
        flag_max_range_pct: float = 5.0,
        ma_period: int = 20,
        rsi_period: int = 14,
        rsi_oversold_threshold: float = 30.0,
        atr_period: int = 14,
        atr_sl_mult: float = 1.5,
        rr_ratio: float = 2.0,
        warmup_period: Optional[int] = None,
    ):
        self.breakout_lookback = breakout_lookback
        self.flag_pole_bars = flag_pole_bars
        self.flag_consolidation_bars = flag_consolidation_bars
        self.flag_min_gain_pct = flag_min_gain_pct
        self.flag_max_range_pct = flag_max_range_pct
        self.ma_period = ma_period
        self.rsi_period = rsi_period
        self.rsi_oversold_threshold = rsi_oversold_threshold
        self.atr_period = atr_period
        self.atr_sl_mult = Decimal(str(atr_sl_mult))
        self.rr_ratio = Decimal(str(rr_ratio))

        min_warmup = max(breakout_lookback + 2, flag_pole_bars + flag_consolidation_bars + 2, ma_period + 2, rsi_period + 2)
        self.warmup_period = warmup_period if warmup_period is not None else min_warmup

        self._candle_history: Dict[str, pd.DataFrame] = {}

    def add_candle_data(self, symbol: str, df: pd.DataFrame):
        """Preload or append historical candle DataFrame."""
        required = {'open', 'high', 'low', 'close', 'volume'}
        if not required.issubset(set(df.columns)):
            raise ValueError(f"DataFrame must contain columns {required}")
        self._candle_history[symbol] = df.copy().reset_index(drop=True)

    def detect_setups(self, df: pd.DataFrame) -> List[Dict[str, Any]]:
        """
        Pure function to evaluate the 5 day-trader-skill setups on a DataFrame.
        Returns a list of detected setup dicts.
        """
        if len(df) < self.warmup_period:
            return []

        closes = df['close'].values
        highs = df['high'].values
        lows = df['low'].values
        n = len(closes)
        curr_close = closes[-1]
        prev_close = closes[-2]

        setups = []

        # 1. Breakout 20D: last close is strictly higher than every close of the 20 before it
        if n > self.breakout_lookback:
            prior_20 = closes[-(self.breakout_lookback + 1):-1]
            if curr_close > np.max(prior_20):
                setups.append({
                    "setup": "breakout_20d",
                    "direction": "BUY",
                    "base_confidence": Decimal("0.80"),
                    "rationale": {
                        "prior_20_max": float(np.max(prior_20)),
                        "curr_close": float(curr_close),
                        "description": f"Close {curr_close:.2f} broke above 20-bar high {np.max(prior_20):.2f}",
                    }
                })

        # 2. Bull Flag: up > 10% over the 10 sessions before the last 5, then last 5 inside 5% range
        flag_total = self.flag_pole_bars + self.flag_consolidation_bars
        if n >= flag_total:
            # Pole: from start of pole to end of pole
            pole_start = closes[-flag_total]
            pole_end = closes[-self.flag_consolidation_bars]
            pole_gain_pct = ((pole_end - pole_start) / max(pole_start, 1e-6)) * 100.0

            # Consolidation: last 5 closes
            cons_closes = closes[-self.flag_consolidation_bars:]
            cons_min = np.min(cons_closes)
            cons_max = np.max(cons_closes)
            cons_range_pct = ((cons_max - cons_min) / max(cons_min, 1e-6)) * 100.0

            if pole_gain_pct >= self.flag_min_gain_pct and cons_range_pct <= self.flag_max_range_pct:
                setups.append({
                    "setup": "bull_flag",
                    "direction": "BUY",
                    "base_confidence": Decimal("0.78"),
                    "rationale": {
                        "pole_gain_pct": round(float(pole_gain_pct), 2),
                        "consolidation_range_pct": round(float(cons_range_pct), 2),
                        "description": f"Bull flag: +{pole_gain_pct:.1f}% pole, {cons_range_pct:.1f}% tight consolidation",
                    }
                })

        # 3. Reclaim MA20: closed under MA20 on t-1, closed above MA20 on t
        if n >= self.ma_period + 1:
            ma20 = df['close'].rolling(window=self.ma_period).mean().values
            curr_ma = ma20[-1]
            prev_ma = ma20[-2]
            if prev_close < prev_ma and curr_close > curr_ma:
                setups.append({
                    "setup": "reclaim_ma20",
                    "direction": "BUY",
                    "base_confidence": Decimal("0.72"),
                    "rationale": {
                        "prev_close": float(prev_close),
                        "prev_ma20": float(prev_ma),
                        "curr_close": float(curr_close),
                        "curr_ma20": float(curr_ma),
                        "description": f"Reclaimed 20 MA: {prev_close:.2f} < {prev_ma:.2f} -> {curr_close:.2f} > {curr_ma:.2f}",
                    }
                })

        # RSI 14 for setups 4 & 5
        rsi_series = rsi(df['close'], period=self.rsi_period)
        curr_rsi = rsi_series.iloc[-1]
        prev_rsi = rsi_series.iloc[-2]

        if not np.isnan(curr_rsi) and not np.isnan(prev_rsi):
            # 4. Oversold Bounce: RSI was < 30 on t-1, today closed up (close[t] > close[t-1])
            if prev_rsi < self.rsi_oversold_threshold and curr_close > prev_close:
                setups.append({
                    "setup": "oversold_bounce",
                    "direction": "BUY",
                    "base_confidence": Decimal("0.68"),
                    "rationale": {
                        "prev_rsi": round(float(prev_rsi), 2),
                        "curr_rsi": round(float(curr_rsi), 2),
                        "curr_close": float(curr_close),
                        "prev_close": float(prev_close),
                        "description": f"Oversold bounce: RSI rebounded from {prev_rsi:.1f} with up candle",
                    }
                })

            # 5. Oversold Breakdown (or Bearish Momentum): RSI under 30 and still breaking down
            elif curr_rsi < self.rsi_oversold_threshold and curr_close <= prev_close:
                setups.append({
                    "setup": "oversold",
                    "direction": "SELL",
                    "base_confidence": Decimal("0.65"),
                    "rationale": {
                        "curr_rsi": round(float(curr_rsi), 2),
                        "curr_close": float(curr_close),
                        "description": f"Bearish momentum: RSI {curr_rsi:.1f} < 30 continuing breakdown",
                    }
                })

        return setups

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

        setups = self.detect_setups(df)
        if not setups:
            return None

        # Pick the strongest setup by base_confidence
        strongest = max(setups, key=lambda s: s["base_confidence"])

        # Compute ATR(14) for stop/target brackets
        atr_series = atr(df['high'], df['low'], df['close'], period=self.atr_period)
        curr_atr = Decimal(str(round(max(atr_series.iloc[-1], 0.01), 4)))
        entry_price = Decimal(str(round(close, 4)))
        sl_dist = curr_atr * self.atr_sl_mult
        tp_dist = sl_dist * self.rr_ratio

        signal_id = f"sig_daily_{strongest['setup']}_{symbol}_{uuid.uuid4().hex[:8]}"
        now = datetime.now()
        expiry = now + timedelta(hours=6)

        if strongest["direction"] == "BUY":
            stop_loss = max(Decimal("0.01"), entry_price - sl_dist)
            take_profit = entry_price + tp_dist
            return buy_signal(
                signal_id=signal_id,
                strategy_id=self.strategy_id,
                strategy_version=self.strategy_version,
                symbol=symbol,
                entry_price=entry_price,
                stop_loss=round(stop_loss, 4),
                take_profit=round(take_profit, 4),
                proposed_size=1,
                confidence=strongest["base_confidence"],
                expiry_time=expiry,
                rationale={
                    "setup": strongest["setup"],
                    "atr": float(curr_atr),
                    "all_matched_setups": [s["setup"] for s in setups],
                    **strongest["rationale"],
                }
            )
        else:
            stop_loss = entry_price + sl_dist
            take_profit = max(Decimal("0.01"), entry_price - tp_dist)
            return sell_signal(
                signal_id=signal_id,
                strategy_id=self.strategy_id,
                strategy_version=self.strategy_version,
                symbol=symbol,
                entry_price=entry_price,
                stop_loss=round(stop_loss, 4),
                take_profit=round(take_profit, 4),
                proposed_size=1,
                confidence=strongest["base_confidence"],
                expiry_time=expiry,
                rationale={
                    "setup": strongest["setup"],
                    "atr": float(curr_atr),
                    "all_matched_setups": [s["setup"] for s in setups],
                    **strongest["rationale"],
                }
            )

    def on_quote(self, quote: Any) -> Optional[TradeSignal]:
        return None

    def generate_signals(self, quotes: Dict[str, Any]) -> List[TradeSignal]:
        signals: List[TradeSignal] = []
        for symbol in quotes.keys():
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

