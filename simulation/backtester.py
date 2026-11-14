"""
Event-Driven Backtesting Engine

Simulates strategy execution on historical OHLCV data bar-by-bar with:
1. Zero look-ahead bias
2. Realistic Delta Exchange fee schedule (Taker 0.05%, Maker 0.02%)
3. Slippage simulation model
4. Native Stop-Loss and Take-Profit bracket order execution against bar extremes
5. Dynamic equity and drawdown tracking
"""
from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Any, Tuple
import pandas as pd
import numpy as np

from strategies.base import BaseStrategy
from analytics.signal import TradeSignal
from simulation.data_loader import DataLoader
from simulation.performance import PerformanceAnalytics, BacktestReport

logger = logging.getLogger(__name__)


class EventDrivenBacktester:
    """
    Event-driven bar-by-bar backtest simulator for Delta Exchange futures.
    """

    def __init__(
        self,
        initial_equity: float = 10000.0,
        taker_fee_pct: float = 0.0005,    # 0.05% Delta taker fee
        maker_fee_pct: float = 0.0002,    # 0.02% Delta maker fee
        slippage_pct: float = 0.0005,     # 0.05% execution slippage
        contract_multiplier: float = 0.001, # Default 1 mBTC
        default_leverage: float = 10.0,
    ):
        self.initial_equity = float(initial_equity)
        self.taker_fee_pct = float(taker_fee_pct)
        self.maker_fee_pct = float(maker_fee_pct)
        self.slippage_pct = float(slippage_pct)
        self.contract_multiplier = float(contract_multiplier)
        self.default_leverage = float(default_leverage)

    def run(
        self,
        strategy: BaseStrategy,
        data: pd.DataFrame,
        symbol: str = "BTCUSD",
    ) -> BacktestReport:
        """
        Execute backtest of strategy over normalized OHLCV data.
        """
        df = DataLoader.normalize_dataframe(data)
        if len(df) == 0:
            raise ValueError("Cannot backtest on empty DataFrame")

        # Initialize simulation account state
        cash = self.initial_equity
        active_position: Optional[Dict[str, Any]] = None

        equity_curve: List[Dict[str, Any]] = []
        trades: List[Dict[str, Any]] = []

        for idx, row in df.iterrows():
            ts = row['timestamp']
            open_p = float(row['open'])
            high_p = float(row['high'])
            low_p = float(row['low'])
            close_p = float(row['close'])
            volume = int(row['volume'])

            # ──────────────────────────────────────────────
            # 1. Process active position against High/Low bounds (SL/TP check)
            # ──────────────────────────────────────────────
            if active_position is not None:
                side = active_position['side']
                entry_p = active_position['entry_price']
                size = active_position['size']
                sl_p = active_position['stop_loss']
                tp_p = active_position['take_profit']
                mult = self.contract_multiplier

                exit_triggered = False
                exit_price = 0.0
                exit_reason = ""
                fee_rate = self.taker_fee_pct

                if side == "BUY":
                    # Check stop-loss first (conservative assumption)
                    if sl_p is not None and low_p <= sl_p:
                        exit_price = min(open_p, sl_p) * (1.0 - self.slippage_pct)
                        exit_reason = "stop_loss"
                        exit_triggered = True
                        fee_rate = self.taker_fee_pct
                    elif tp_p is not None and high_p >= tp_p:
                        exit_price = tp_p  # Limit order fill at target
                        exit_reason = "take_profit"
                        exit_triggered = True
                        fee_rate = self.maker_fee_pct

                elif side == "SELL":
                    if sl_p is not None and high_p >= sl_p:
                        exit_price = max(open_p, sl_p) * (1.0 + self.slippage_pct)
                        exit_reason = "stop_loss"
                        exit_triggered = True
                        fee_rate = self.taker_fee_pct
                    elif tp_p is not None and low_p <= tp_p:
                        exit_price = tp_p
                        exit_reason = "take_profit"
                        exit_triggered = True
                        fee_rate = self.maker_fee_pct

                if exit_triggered:
                    # Calculate gross and net P&L
                    if side == "BUY":
                        gross_pnl = (exit_price - entry_p) * size * mult
                    else:
                        gross_pnl = (entry_p - exit_price) * size * mult

                    exit_fee = exit_price * size * mult * fee_rate
                    net_pnl = gross_pnl - exit_fee
                    cash += net_pnl

                    trades.append({
                        "trade_id": f"trade_{len(trades)+1}",
                        "symbol": symbol,
                        "side": side,
                        "entry_time": active_position['entry_time'],
                        "entry_price": entry_p,
                        "exit_time": ts,
                        "exit_price": round(exit_price, 2),
                        "size": size,
                        "pnl": round(net_pnl, 2),
                        "fee": round(exit_fee, 2),
                        "reason": exit_reason,
                    })
                    active_position = None

            # ──────────────────────────────────────────────
            # 2. Feed candle to strategy
            # ──────────────────────────────────────────────
            signal: Optional[TradeSignal] = strategy.on_candle(
                open_=open_p,
                high=high_p,
                low=low_p,
                close=close_p,
                volume=volume,
                timestamp=ts,
                symbol=symbol,
            )

            # ──────────────────────────────────────────────
            # 3. Simulate Entry Execution on New Signal
            # ──────────────────────────────────────────────
            if signal is not None and active_position is None:
                sig_dir = signal.direction.upper()
                proposed_size = signal.proposed_size or 1

                # Calculate execution entry price with slippage
                if sig_dir == "BUY":
                    entry_exec = close_p * (1.0 + self.slippage_pct)
                else:
                    entry_exec = close_p * (1.0 - self.slippage_pct)

                entry_fee = entry_exec * proposed_size * self.contract_multiplier * self.taker_fee_pct
                cash -= entry_fee

                active_position = {
                    "side": sig_dir,
                    "entry_time": ts,
                    "entry_price": entry_exec,
                    "size": proposed_size,
                    "stop_loss": float(signal.stop_loss_price) if signal.stop_loss_price else None,
                    "take_profit": float(signal.take_profit_price) if signal.take_profit_price else None,
                    "entry_fee": entry_fee,
                }

            # ──────────────────────────────────────────────
            # 4. Mark to market for equity curve
            # ──────────────────────────────────────────────
            unrealized = 0.0
            if active_position is not None:
                mult = self.contract_multiplier
                sz = active_position['size']
                ep = active_position['entry_price']
                if active_position['side'] == "BUY":
                    unrealized = (close_p - ep) * sz * mult
                else:
                    unrealized = (ep - close_p) * sz * mult

            total_equity = cash + unrealized
            equity_curve.append({
                "timestamp": ts,
                "cash": round(cash, 2),
                "unrealized_pnl": round(unrealized, 2),
                "equity": round(total_equity, 2),
                "price": close_p,
            })

        # Close any lingering position at end of simulation
        if active_position is not None:
            last_close = float(df.iloc[-1]['close'])
            mult = self.contract_multiplier
            sz = active_position['size']
            ep = active_position['entry_price']
            side = active_position['side']

            if side == "BUY":
                gross_pnl = (last_close - ep) * sz * mult
            else:
                gross_pnl = (ep - last_close) * sz * mult

            exit_fee = last_close * sz * mult * self.taker_fee_pct
            net_pnl = gross_pnl - exit_fee
            cash += net_pnl

            trades.append({
                "trade_id": f"trade_{len(trades)+1}",
                "symbol": symbol,
                "side": side,
                "entry_time": active_position['entry_time'],
                "entry_price": ep,
                "exit_time": df.iloc[-1]['timestamp'],
                "exit_price": round(last_close, 2),
                "size": sz,
                "pnl": round(net_pnl, 2),
                "fee": round(exit_fee, 2),
                "reason": "end_of_backtest",
            })
            active_position = None
            equity_curve[-1]['equity'] = round(cash, 2)
            equity_curve[-1]['unrealized_pnl'] = 0.0

        # Calculate performance analytics
        return PerformanceAnalytics.calculate_metrics(
            initial_equity=self.initial_equity,
            equity_records=equity_curve,
            trades=trades,
        )
