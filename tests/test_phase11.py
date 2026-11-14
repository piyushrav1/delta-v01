"""
Phase 11 Tests: Historical Backtesting Engine

Tests:
1. Historical Data Ingestion & Synthetic Generation (Task 11.1)
2. Performance & Metrics Analytics (Task 11.3: Sharpe, Sortino, Drawdown, Win Rate, Expectancy)
3. Event-Driven Backtesting Engine (Task 11.2: Bar-by-bar execution, SL/TP brackets, fee/slippage models)
"""
import pytest
from datetime import datetime, timedelta
from decimal import Decimal
import pandas as pd
import numpy as np

from simulation.data_loader import DataLoader
from simulation.performance import PerformanceAnalytics, BacktestReport
from simulation.backtester import EventDrivenBacktester
from strategies.trend_following import TrendFollowing
from strategies.momentum import Momentum


# ──────────────────────────────────────────────
# 1. Historical Data Loader Tests
# ──────────────────────────────────────────────
class TestDataLoader:
    def test_normalize_dataframe_columns_and_sorting(self):
        raw_data = [
            {"Date": "2026-01-01 00:02:00", "Open": 100.0, "High": 102.0, "Low": 99.0, "Close": 101.0, "Volume": 50},
            {"Date": "2026-01-01 00:00:00", "Open": 98.0, "High": 100.0, "Low": 97.0, "Close": 99.0, "Volume": 40},
            {"Date": "2026-01-01 00:01:00", "Open": 99.0, "High": 101.0, "Low": 98.0, "Close": 100.0, "Volume": 45},
            {"Date": "2026-01-01 00:01:00", "Open": 99.0, "High": 101.0, "Low": 98.0, "Close": 100.0, "Volume": 45}, # Duplicate
        ]
        df_raw = pd.DataFrame(raw_data)
        df_norm = DataLoader.normalize_dataframe(df_raw)

        assert list(df_norm.columns) == ['timestamp', 'open', 'high', 'low', 'close', 'volume']
        assert len(df_norm) == 3  # Duplicate removed
        assert df_norm.iloc[0]['close'] == 99.0
        assert df_norm.iloc[-1]['close'] == 101.0

    def test_resample_timeframe(self):
        # 10 minutes of 1m bars
        start = datetime(2026, 1, 1, 0, 0)
        bars = []
        for i in range(10):
            bars.append({
                "timestamp": start + timedelta(minutes=i),
                "open": 100.0 + i,
                "high": 105.0 + i,
                "low": 95.0 + i,
                "close": 102.0 + i,
                "volume": 10,
            })
        df_1m = pd.DataFrame(bars)
        df_5m = DataLoader.resample_timeframe(df_1m, rule='5min')

        assert len(df_5m) == 2
        # First 5m bar open should be bar 0 open (100.0), close should be bar 4 close (106.0)
        assert df_5m.iloc[0]['open'] == 100.0
        assert df_5m.iloc[0]['close'] == 106.0
        assert df_5m.iloc[0]['volume'] == 50

    def test_generate_synthetic_ohlcv(self):
        df = DataLoader.generate_synthetic_ohlcv(
            symbol="BTCUSD",
            num_bars=100,
            interval_minutes=15,
            base_price=60000.0,
            seed=123,
        )
        assert len(df) == 100
        assert (df['high'] >= df['open']).all()
        assert (df['high'] >= df['close']).all()
        assert (df['low'] <= df['open']).all()
        assert (df['low'] <= df['close']).all()
        assert (df['volume'] > 0).all()


# ──────────────────────────────────────────────
# 2. Performance Analytics Tests
# ──────────────────────────────────────────────
class TestPerformanceAnalytics:
    def test_performance_metrics_calculation(self):
        start = datetime(2026, 1, 1, 0, 0)
        equity_records = [
            {"timestamp": start + timedelta(days=i), "equity": 10000.0 + (i * 200.0)}
            for i in range(10)
        ]
        trades = [
            {"trade_id": "t1", "pnl": 500.0},
            {"trade_id": "t2", "pnl": -200.0},
            {"trade_id": "t3", "pnl": 700.0},
            {"trade_id": "t4", "pnl": -100.0},
        ]

        report = PerformanceAnalytics.calculate_metrics(
            initial_equity=10000.0,
            equity_records=equity_records,
            trades=trades,
        )

        assert report.initial_equity == 10000.0
        assert report.final_equity == 11800.0
        assert report.total_return_usd == 1800.0
        assert report.total_trades == 4
        assert report.winning_trades == 2
        assert report.losing_trades == 2
        assert report.win_rate == 0.50
        # Profit factor: (500 + 700) / (200 + 100) = 1200 / 300 = 4.0
        assert report.profit_factor == 4.0
        assert report.avg_win == 600.0
        assert report.avg_loss == 150.0
        assert report.payoff_ratio == 4.0
        # Expectancy: (0.5 * 600) - (0.5 * 150) = 300 - 75 = 225.0
        assert report.expectancy == 225.0
        assert report.summary() is not None
        assert "BACKTEST PERFORMANCE REPORT" in report.summary()


# ──────────────────────────────────────────────
# 3. Event-Driven Backtester Tests
# ──────────────────────────────────────────────
class TestEventDrivenBacktester:
    def test_backtest_execution_with_trend_following(self):
        # Generate 150 bars of synthetic trending data
        data = DataLoader.generate_synthetic_ohlcv(
            symbol="BTCUSD",
            num_bars=150,
            interval_minutes=15,
            base_price=50000.0,
            trend_drift=0.002,  # Strong upward trend
            volatility=0.005,
            seed=42,
        )

        strategy = TrendFollowing(ema_fast_span=10, ema_slow_span=20, warmup_period=25)
        backtester = EventDrivenBacktester(
            initial_equity=10000.0,
            taker_fee_pct=0.0005,
            maker_fee_pct=0.0002,
            contract_multiplier=0.01,
        )

        report = backtester.run(strategy=strategy, data=data, symbol="BTCUSD")

        assert isinstance(report, BacktestReport)
        assert len(report.equity_curve) == 150
        assert report.initial_equity == 10000.0
        assert report.final_equity > 0
        # Should have taken at least 1 trade during the 150-bar trend
        assert report.total_trades >= 1
        assert len(report.trades) >= 1

    def test_backtest_with_momentum_strategy(self):
        # Generate data with high volatility to trigger momentum bursts
        data = DataLoader.generate_synthetic_ohlcv(
            symbol="ETHUSD",
            num_bars=120,
            interval_minutes=15,
            base_price=3000.0,
            trend_drift=0.001,
            volatility=0.015,  # High volatility
            seed=99,
        )

        strategy = Momentum(rsi_period=14, volume_period=20, volume_multiplier=1.5, warmup_period=25)
        backtester = EventDrivenBacktester(
            initial_equity=10000.0,
            contract_multiplier=0.1,
        )

        report = backtester.run(strategy=strategy, data=data, symbol="ETHUSD")

        assert isinstance(report, BacktestReport)
        assert len(report.equity_curve) == 120
        assert report.total_trades >= 0
