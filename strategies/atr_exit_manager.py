"""
ATR Dynamic Exit Manager Strategy
Adapted from day-trader-skill (trader-exit) for Institutional Position Management.

Features:
1. Pure Math Exit Bracket Planner:
   - Stops: atr:K (K * ATR(14) below long / above short) or pct:X.
   - Targets: 1R, 1.5R, 2R multiples of risk (R = |entry - stop|).
   - Validates that risk >= 0.05% of price to avoid spread noise.
2. Active Position Monitoring & Exit Signal Generation:
   - Emits closing TradeSignal on Take-Profit hit ("take_profit_hit").
   - Emits closing TradeSignal on Stop-Loss hit ("stop_loss_hit").
   - Emits closing TradeSignal on Time-based Expiry ("time_exit" after max_hold_bars).
3. Zero Exchange Imports:
   - Operates purely on market data and tracked positions.
   - Emits high-confidence (0.95) exit signals to Risk Gateway.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP, ROUND_FLOOR
from typing import Dict, List, Optional, Any, Union, Tuple
import pandas as pd
import numpy as np

from strategies.base import BaseStrategy, register_strategy
from analytics.indicators import atr
from analytics.signal import TradeSignal, buy_signal, sell_signal


MIN_RISK_PCT = Decimal("0.0005")  # 0.05% minimum risk distance to clear spread noise


def calculate_exit_bracket(
    entry_price: Union[float, Decimal],
    side: str,
    high_series: pd.Series,
    low_series: pd.Series,
    close_series: pd.Series,
    stop_mode: str = "atr",
    stop_value: float = 1.5,
    rr_ratio: float = 2.0,
    atr_period: int = 14,
) -> Dict[str, Decimal]:
    """
    Pure utility function calculating stop-loss and take-profit according to trader-exit contract.

    Args:
        entry_price: Entry price of the position
        side: "buy" (long) or "sell" (short)
        high_series, low_series, close_series: Historical price series for ATR
        stop_mode: "atr" (multiplier of ATR) or "pct" (percentage)
        stop_value: Value for the stop mode (e.g. 1.5 for 1.5 ATR, or 1.0 for 1%)
        rr_ratio: Reward-to-risk multiple (e.g. 2.0 for 2R)
        atr_period: Lookback for ATR (default 14)

    Returns:
        Dict with keys: entry, stop, target, risk_per_unit, reward_risk
    """
    entry = Decimal(str(round(float(entry_price), 4)))
    is_long = side.lower() in ("buy", "long")

    if stop_mode == "pct":
        risk_dist = entry * (Decimal(str(stop_value)) / Decimal("100.0"))
    else:  # "atr"
        atr_vals = atr(high_series, low_series, close_series, period=atr_period)
        latest_atr = float(atr_vals.iloc[-1]) if not atr_vals.empty and not np.isnan(atr_vals.iloc[-1]) else float(entry) * 0.01
        risk_dist = Decimal(str(round(max(latest_atr * stop_value, float(entry) * 0.001), 4)))

    # Ensure risk meets minimum 0.05% threshold
    min_dist = entry * MIN_RISK_PCT
    if risk_dist < min_dist:
        risk_dist = min_dist

    if is_long:
        stop = entry - risk_dist
        target = entry + (risk_dist * Decimal(str(rr_ratio)))
    else:
        stop = entry + risk_dist
        target = entry - (risk_dist * Decimal(str(rr_ratio)))
        target = max(Decimal("0.01"), target)

    return {
        "entry": entry,
        "stop": round(stop, 4),
        "target": round(target, 4),
        "risk_per_unit": round(risk_dist, 4),
        "reward_risk": Decimal(str(rr_ratio)),
    }


@register_strategy
class ATRExitManager(BaseStrategy):
    """
    ATR Dynamic Exit Manager Strategy.
    """
    strategy_id: str = "atr_exit_manager"
    strategy_version: str = "1.0.0"
    display_name: str = "ATR Dynamic Exit Manager"
    warmup_period: int = 15
    allowed_environments = {"BACKTEST", "PAPER", "TESTNET", "LIVE_AUTO"}

    def __init__(
        self,
        atr_period: int = 14,
        default_atr_sl_mult: float = 1.5,
        default_rr_ratio: float = 2.0,
        max_hold_bars: int = 48,  # ~4 hours on 5m candles
        warmup_period: Optional[int] = None,
    ):
        self.atr_period = atr_period
        self.default_atr_sl_mult = default_atr_sl_mult
        self.default_rr_ratio = default_rr_ratio
        self.max_hold_bars = max_hold_bars
        self.warmup_period = warmup_period if warmup_period is not None else atr_period + 1

        self._candle_history: Dict[str, pd.DataFrame] = {}
        # Tracked active positions: symbol -> position dict
        self._positions: Dict[str, Dict[str, Any]] = {}

    def track_position(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        size: int = 1,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Register or update an active position to monitor for exits.
        If stop_loss or take_profit is omitted, calculates dynamically using ATR.
        """
        entry = Decimal(str(round(entry_price, 4)))
        is_long = side.lower() in ("buy", "long")

        if stop_loss is None or take_profit is None:
            df = self._candle_history.get(symbol)
            if df is not None and len(df) >= self.warmup_period:
                bracket = calculate_exit_bracket(
                    entry_price=entry,
                    side=side,
                    high_series=df['high'],
                    low_series=df['low'],
                    close_series=df['close'],
                    stop_mode="atr",
                    stop_value=self.default_atr_sl_mult,
                    rr_ratio=self.default_rr_ratio,
                    atr_period=self.atr_period,
                )
                stop_loss = float(bracket["stop"])
                take_profit = float(bracket["target"])
            else:
                default_risk = entry * Decimal("0.015")
                stop_loss = float(entry - default_risk if is_long else entry + default_risk)
                take_profit = float(entry + (default_risk * Decimal("2.0")) if is_long else entry - (default_risk * Decimal("2.0")))

        pos = {
            "symbol": symbol,
            "side": "BUY" if is_long else "SELL",
            "entry_price": entry,
            "stop_loss": Decimal(str(round(stop_loss, 4))),
            "take_profit": Decimal(str(round(take_profit, 4))),
            "size": size,
            "bars_held": 0,
            "opened_at": datetime.now(),
        }
        self._positions[symbol] = pos
        return pos

    def close_tracked_position(self, symbol: str):
        """Remove tracked position once exited."""
        self._positions.pop(symbol, None)

    def add_candle_data(self, symbol: str, df: pd.DataFrame):
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

        pos = self._positions.get(symbol)
        if not pos:
            return None

        pos["bars_held"] += 1
        curr_price = Decimal(str(round(close, 4)))
        is_long = pos["side"] == "BUY"
        exit_reason = None
        exit_price = curr_price

        # Check Take Profit
        if is_long and curr_price >= pos["take_profit"]:
            exit_reason = "take_profit_hit"
        elif not is_long and curr_price <= pos["take_profit"]:
            exit_reason = "take_profit_hit"

        # Check Stop Loss
        elif is_long and curr_price <= pos["stop_loss"]:
            exit_reason = "stop_loss_hit"
        elif not is_long and curr_price >= pos["stop_loss"]:
            exit_reason = "stop_loss_hit"

        # Check Time Expiry
        elif pos["bars_held"] >= self.max_hold_bars:
            exit_reason = "time_exit"

        if exit_reason:
            signal_id = f"sig_exit_{symbol}_{uuid.uuid4().hex[:8]}"
            now = datetime.now()
            # To exit a long, emit SELL. To exit a short, emit BUY.
            exit_dir = "SELL" if is_long else "BUY"

            # Auto remove from tracking upon exit trigger
            self._positions.pop(symbol, None)

            if exit_dir == "SELL":
                return sell_signal(
                    signal_id=signal_id,
                    strategy_id=self.strategy_id,
                    strategy_version=self.strategy_version,
                    symbol=symbol,
                    entry_price=exit_price,
                    stop_loss=exit_price * Decimal("1.01"),  # protective
                    take_profit=exit_price * Decimal("0.99"),
                    proposed_size=pos["size"],
                    confidence=Decimal("0.95"),
                    rationale={
                        "exit_reason": exit_reason,
                        "position_side": pos["side"],
                        "entry_price": float(pos["entry_price"]),
                        "stop_loss": float(pos["stop_loss"]),
                        "take_profit": float(pos["take_profit"]),
                        "bars_held": pos["bars_held"],
                        "pnl_pct": round(float((curr_price - pos["entry_price"]) / pos["entry_price"] * 100), 2),
                    }
                )
            else:
                return buy_signal(
                    signal_id=signal_id,
                    strategy_id=self.strategy_id,
                    strategy_version=self.strategy_version,
                    symbol=symbol,
                    entry_price=exit_price,
                    stop_loss=exit_price * Decimal("0.99"),  # protective
                    take_profit=exit_price * Decimal("1.01"),
                    proposed_size=pos["size"],
                    confidence=Decimal("0.95"),
                    rationale={
                        "exit_reason": exit_reason,
                        "position_side": pos["side"],
                        "entry_price": float(pos["entry_price"]),
                        "stop_loss": float(pos["stop_loss"]),
                        "take_profit": float(pos["take_profit"]),
                        "bars_held": pos["bars_held"],
                        "pnl_pct": round(float((pos["entry_price"] - curr_price) / pos["entry_price"] * 100), 2),
                    }
                )

        return None

    def on_quote(self, quote: Any) -> Optional[TradeSignal]:
        return None

    def generate_signals(self, quotes: Dict[str, Any]) -> List[TradeSignal]:
        signals: List[TradeSignal] = []
        for symbol in list(self._positions.keys()):
            if symbol in self._candle_history:
                df = self._candle_history[symbol]
                if not df.empty:
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
