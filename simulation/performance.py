"""
Performance & Quantitative Metrics Analytics

Calculates institutional-grade backtest performance analytics:
- Cumulative & Annualized Return (CAGR)
- Sharpe & Sortino Ratios (Annualized)
- Maximum Drawdown & Drawdown Duration
- Win Rate, Profit Factor, Payoff Ratio, and Expectancy
- Complete Equity Curve Time Series
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Any
import numpy as np
import pandas as pd


@dataclass
class BacktestReport:
    """Comprehensive performance metrics summary for a backtest."""
    start_date: datetime
    end_date: datetime
    initial_equity: float
    final_equity: float
    total_return_usd: float
    total_return_pct: float
    cagr: float
    annualized_volatility: float
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown_usd: float
    max_drawdown_pct: float
    max_drawdown_duration_bars: int
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    profit_factor: float
    avg_trade_pnl: float
    avg_win: float
    avg_loss: float
    payoff_ratio: float
    expectancy: float
    equity_curve: List[Dict[str, Any]] = field(default_factory=list)
    trades: List[Dict[str, Any]] = field(default_factory=list)

    def summary(self) -> str:
        """Formatted textual report."""
        return (
            f"=== BACKTEST PERFORMANCE REPORT ===\n"
            f"Period:              {self.start_date.strftime('%Y-%m-%d')} to {self.end_date.strftime('%Y-%m-%d')}\n"
            f"Initial Capital:     ${self.initial_equity:,.2f}\n"
            f"Final Capital:       ${self.final_equity:,.2f}\n"
            f"Total Return:        ${self.total_return_usd:,.2f} ({self.total_return_pct * 100:.2f}%)\n"
            f"CAGR:                {self.cagr * 100:.2f}%\n"
            f"Sharpe Ratio:        {self.sharpe_ratio:.2f}\n"
            f"Sortino Ratio:       {self.sortino_ratio:.2f}\n"
            f"Max Drawdown:        ${self.max_drawdown_usd:,.2f} ({self.max_drawdown_pct * 100:.2f}%)\n"
            f"Max DD Duration:     {self.max_drawdown_duration_bars} bars\n"
            f"Total Trades:        {self.total_trades} (Win: {self.winning_trades}, Loss: {self.losing_trades})\n"
            f"Win Rate:            {self.win_rate * 100:.2f}%\n"
            f"Profit Factor:       {self.profit_factor:.2f}\n"
            f"Payoff Ratio:        {self.payoff_ratio:.2f}\n"
            f"Expectancy:          ${self.expectancy:.2f}\n"
            f"==================================="
        )


class PerformanceAnalytics:
    """
    Computes statistical and financial performance metrics from equity curve and trades.
    """

    @staticmethod
    def calculate_metrics(
        initial_equity: float,
        equity_records: List[Dict[str, Any]],
        trades: List[Dict[str, Any]],
        risk_free_rate: float = 0.0,
        bars_per_year: int = 365 * 24 * 4,  # e.g., 15-minute bars per year
    ) -> BacktestReport:
        """
        Compute full metrics suite from recorded backtest data.
        """
        if not equity_records:
            now = datetime.now()
            return BacktestReport(
                start_date=now, end_date=now, initial_equity=initial_equity,
                final_equity=initial_equity, total_return_usd=0.0, total_return_pct=0.0,
                cagr=0.0, annualized_volatility=0.0, sharpe_ratio=0.0, sortino_ratio=0.0,
                max_drawdown_usd=0.0, max_drawdown_pct=0.0, max_drawdown_duration_bars=0,
                total_trades=0, winning_trades=0, losing_trades=0, win_rate=0.0,
                profit_factor=0.0, avg_trade_pnl=0.0, avg_win=0.0, avg_loss=0.0,
                payoff_ratio=0.0, expectancy=0.0, equity_curve=[], trades=[]
            )

        start_date = equity_records[0]['timestamp']
        end_date = equity_records[-1]['timestamp']
        final_equity = equity_records[-1]['equity']
        total_return_usd = final_equity - initial_equity
        total_return_pct = total_return_usd / initial_equity if initial_equity > 0 else 0.0

        # Duration in years
        days = max(1, (end_date - start_date).days)
        years = days / 365.25
        cagr = ((final_equity / initial_equity) ** (1.0 / years) - 1.0) if years > 0 and final_equity > 0 and initial_equity > 0 else 0.0

        # Equity array and returns
        equities = np.array([r['equity'] for r in equity_records])
        if len(equities) > 1:
            bar_returns = np.diff(equities) / equities[:-1]
        else:
            bar_returns = np.array([0.0])

        # Annualized Volatility & Sharpe
        ann_vol = float(np.std(bar_returns) * math.sqrt(bars_per_year)) if len(bar_returns) > 1 else 0.0
        excess_returns = bar_returns - (risk_free_rate / bars_per_year)
        mean_excess = float(np.mean(excess_returns)) if len(excess_returns) > 0 else 0.0

        if ann_vol > 0:
            sharpe_ratio = float((mean_excess * math.sqrt(bars_per_year)) / (np.std(bar_returns) if np.std(bar_returns) > 0 else 1.0))
        else:
            sharpe_ratio = 0.0

        # Sortino Ratio (downside deviation only)
        negative_returns = bar_returns[bar_returns < 0]
        if len(negative_returns) > 0:
            downside_std = float(np.std(negative_returns) * math.sqrt(bars_per_year))
            sortino_ratio = float((mean_excess * math.sqrt(bars_per_year)) / (downside_std if downside_std > 0 else 1.0))
        else:
            sortino_ratio = 9.99 if total_return_usd > 0 else 0.0

        # Max Drawdown & Duration
        running_peak = np.maximum.accumulate(equities)
        drawdowns_usd = running_peak - equities
        drawdowns_pct = np.where(running_peak > 0, drawdowns_usd / running_peak, 0.0)

        max_dd_usd = float(np.max(drawdowns_usd))
        max_dd_pct = float(np.max(drawdowns_pct))

        # Max Drawdown Duration
        max_dd_duration = 0
        current_dd_duration = 0
        for dd in drawdowns_usd:
            if dd > 0:
                current_dd_duration += 1
                if current_dd_duration > max_dd_duration:
                    max_dd_duration = current_dd_duration
            else:
                current_dd_duration = 0

        # Trade metrics
        total_trades = len(trades)
        wins = [t['pnl'] for t in trades if t['pnl'] > 0]
        losses = [t['pnl'] for t in trades if t['pnl'] < 0]
        winning_trades = len(wins)
        losing_trades = len(losses)
        win_rate = winning_trades / total_trades if total_trades > 0 else 0.0

        sum_wins = sum(wins)
        sum_losses = abs(sum(losses))
        profit_factor = (sum_wins / sum_losses) if sum_losses > 0 else (99.0 if sum_wins > 0 else 0.0)

        avg_win = (sum_wins / winning_trades) if winning_trades > 0 else 0.0
        avg_loss = (sum_losses / losing_trades) if losing_trades > 0 else 0.0
        payoff_ratio = (avg_win / avg_loss) if avg_loss > 0 else 0.0
        avg_trade_pnl = (sum(t['pnl'] for t in trades) / total_trades) if total_trades > 0 else 0.0
        loss_rate = 1.0 - win_rate
        expectancy = (win_rate * avg_win) - (loss_rate * avg_loss)

        return BacktestReport(
            start_date=start_date,
            end_date=end_date,
            initial_equity=initial_equity,
            final_equity=final_equity,
            total_return_usd=round(total_return_usd, 2),
            total_return_pct=round(total_return_pct, 4),
            cagr=round(cagr, 4),
            annualized_volatility=round(ann_vol, 4),
            sharpe_ratio=round(sharpe_ratio, 2),
            sortino_ratio=round(sortino_ratio, 2),
            max_drawdown_usd=round(max_dd_usd, 2),
            max_drawdown_pct=round(max_dd_pct, 4),
            max_drawdown_duration_bars=max_dd_duration,
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            win_rate=round(win_rate, 4),
            profit_factor=round(profit_factor, 2),
            avg_trade_pnl=round(avg_trade_pnl, 2),
            avg_win=round(avg_win, 2),
            avg_loss=round(avg_loss, 2),
            payoff_ratio=round(payoff_ratio, 2),
            expectancy=round(expectancy, 2),
            equity_curve=equity_records,
            trades=trades,
        )
