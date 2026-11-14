"""
Phase 7 Tests: Multi-Strategy Development Engine

Tests real execution and signal generation across all concrete strategies:
1. TrendFollowing (Strategy A: EMA 20/50 crossovers + ATR volatility channels)
2. Momentum (Strategy B: RSI momentum burst + volume acceleration filter)
3. MeanReversion (Strategy C: Bollinger Bands 2.5σ overshoot + RSI mean reversion)
4. OptionsSpreads (Strategy E: Bull call and bear put vertical debit spreads with R:R >= 1.5)
5. AnomalyStrategy (Strategy F: Illiquidity gaps, fat-fingers, strike arbitrage, butterflies)
"""
import pytest
from datetime import datetime, timedelta
from decimal import Decimal
import pandas as pd
import numpy as np

from strategies.trend_following import TrendFollowing
from strategies.momentum import Momentum
from strategies.mean_reversion import MeanReversion
from strategies.options_spreads import OptionsSpreads, parse_option_symbol
from strategies.anomaly_strategy import AnomalyStrategy, parse_option_metadata
from analytics.signal import TradeSignal


# ──────────────────────────────────────────────
# 1. Trend Following Strategy Tests
# ──────────────────────────────────────────────
class TestTrendFollowingStrategy:
    def test_warmup_period_enforcement(self):
        strat = TrendFollowing(warmup_period=55)
        now = datetime.now()
        # Feed fewer than 55 candles
        sig = None
        for i in range(30):
            sig = strat.on_candle(
                open_=100.0, high=105.0, low=95.0, close=100.0,
                volume=1000, timestamp=now + timedelta(minutes=i), symbol="BTCUSD"
            )
        assert sig is None

    def test_bullish_ema_crossover_signal(self):
        strat = TrendFollowing(ema_fast_span=10, ema_slow_span=20, warmup_period=25)
        now = datetime.now()

        # Phase 1: 30 bars in a downtrend (Fast EMA will be below Slow EMA)
        for i in range(30):
            p = 100.0 - (i * 0.5)
            strat.on_candle(
                open_=p, high=p + 1.0, low=p - 1.0, close=p,
                volume=1000, timestamp=now + timedelta(minutes=i), symbol="BTCUSD"
            )

        # Phase 2: Strong sharp breakout upward to trigger bullish crossover
        sig = None
        for i in range(30, 45):
            p = 85.0 + ((i - 30) * 3.5)
            res = strat.on_candle(
                open_=p - 1.0, high=p + 2.0, low=p - 2.0, close=p,
                volume=2000, timestamp=now + timedelta(minutes=i), symbol="BTCUSD"
            )
            if res is not None:
                sig = res
                break

        assert sig is not None
        assert sig.direction == "BUY"
        assert sig.symbol == "BTCUSD"
        assert sig.target_entry_price > 0
        assert sig.stop_loss_price < sig.target_entry_price
        assert sig.take_profit_price > sig.target_entry_price
        assert sig.confidence >= Decimal("0.60")
        assert sig.rationale["crossover"] == "bullish"

    def test_bearish_ema_crossover_signal(self):
        strat = TrendFollowing(ema_fast_span=10, ema_slow_span=20, warmup_period=25)
        now = datetime.now()

        # Phase 1: 30 bars in an uptrend (Fast EMA above Slow EMA)
        for i in range(30):
            p = 100.0 + (i * 0.5)
            strat.on_candle(
                open_=p, high=p + 1.0, low=p - 1.0, close=p,
                volume=1000, timestamp=now + timedelta(minutes=i), symbol="ETHUSD"
            )

        # Phase 2: Sharp dump to trigger bearish crossover
        sig = None
        for i in range(30, 45):
            p = 115.0 - ((i - 30) * 3.5)
            res = strat.on_candle(
                open_=p + 1.0, high=p + 2.0, low=p - 2.0, close=p,
                volume=2000, timestamp=now + timedelta(minutes=i), symbol="ETHUSD"
            )
            if res is not None:
                sig = res
                break

        assert sig is not None
        assert sig.direction == "SELL"
        assert sig.symbol == "ETHUSD"
        assert sig.stop_loss_price > sig.target_entry_price
        assert sig.take_profit_price < sig.target_entry_price
        assert sig.rationale["crossover"] == "bearish"


# ──────────────────────────────────────────────
# 2. Momentum Strategy Tests
# ──────────────────────────────────────────────
class TestMomentumStrategy:
    def test_volume_acceleration_required(self):
        strat = Momentum(rsi_period=14, volume_period=20, volume_multiplier=2.0, warmup_period=25)
        now = datetime.now()

        # Feed 30 flat bars with normal volume of 100
        for i in range(30):
            strat.on_candle(
                open_=100.0, high=101.0, low=99.0, close=100.0,
                volume=100, timestamp=now + timedelta(minutes=i), symbol="BTCUSD"
            )

        # Candle with price jump but normal volume (100) -> should NOT trigger
        sig = strat.on_candle(
            open_=100.0, high=108.0, low=100.0, close=107.0,
            volume=100, timestamp=now + timedelta(minutes=31), symbol="BTCUSD"
        )
        assert sig is None

    def test_bullish_momentum_surge(self):
        strat = Momentum(rsi_period=14, volume_period=20, volume_multiplier=2.0, warmup_period=25)
        now = datetime.now()

        # Establish baseline volume of 500 and base price with slight oscillation around 100 (RSI ~45-50)
        for i in range(30):
            strat.on_candle(
                open_=100.0, high=101.0, low=99.0, close=100.0 - (0.1 if i % 2 == 0 else -0.1),
                volume=500, timestamp=now + timedelta(minutes=i), symbol="BTCUSD"
            )

        # Huge volume surge (2500 >> 2x 500) and sharp price rally (RSI crosses above 50)
        sig = strat.on_candle(
            open_=100.0, high=120.0, low=100.0, close=118.0,
            volume=2500, timestamp=now + timedelta(minutes=31), symbol="BTCUSD"
        )

        assert sig is not None
        assert sig.direction == "BUY"
        assert sig.symbol == "BTCUSD"
        assert sig.target_entry_price == Decimal("118.0")
        assert sig.stop_loss_price < Decimal("118.0")
        assert sig.take_profit_price > Decimal("118.0")
        assert sig.rationale["volume_ratio"] >= 2.0


# ──────────────────────────────────────────────
# 3. Mean Reversion Strategy Tests
# ──────────────────────────────────────────────
class TestMeanReversionStrategy:
    def test_oversold_lower_band_overshoot(self):
        strat = MeanReversion(bb_window=20, bb_num_std=2.0, rsi_period=14, rsi_oversold=35.0, warmup_period=25)
        now = datetime.now()

        # 30 bars oscillating around 100 to establish bands
        for i in range(30):
            strat.on_candle(
                open_=100.0, high=102.0, low=98.0, close=100.0 + (1.0 if i % 2 == 0 else -1.0),
                volume=1000, timestamp=now + timedelta(minutes=i), symbol="BTCUSD"
            )

        # Extreme crash far below lower Bollinger Band
        sig = strat.on_candle(
            open_=95.0, high=95.0, low=75.0, close=76.0,
            volume=3000, timestamp=now + timedelta(minutes=31), symbol="BTCUSD"
        )

        assert sig is not None
        assert sig.direction == "BUY"
        assert sig.symbol == "BTCUSD"
        assert sig.target_entry_price == Decimal("76.0")
        assert sig.take_profit_price > Decimal("76.0")  # Targets middle band (~100)
        assert sig.stop_loss_price < Decimal("76.0")
        assert sig.rationale["direction"] == "long_oversold"

    def test_overbought_upper_band_overshoot(self):
        strat = MeanReversion(bb_window=20, bb_num_std=2.0, rsi_period=14, rsi_overbought=65.0, warmup_period=25)
        now = datetime.now()

        for i in range(30):
            strat.on_candle(
                open_=100.0, high=102.0, low=98.0, close=100.0 + (1.0 if i % 2 == 0 else -1.0),
                volume=1000, timestamp=now + timedelta(minutes=i), symbol="BTCUSD"
            )

        # Extreme rocket far above upper Bollinger Band
        sig = strat.on_candle(
            open_=105.0, high=135.0, low=105.0, close=130.0,
            volume=3000, timestamp=now + timedelta(minutes=31), symbol="BTCUSD"
        )

        assert sig is not None
        assert sig.direction == "SELL"
        assert sig.target_entry_price == Decimal("130.0")
        assert sig.take_profit_price < Decimal("130.0")  # Targets middle band (~100)
        assert sig.stop_loss_price > Decimal("130.0")
        assert sig.rationale["direction"] == "short_overbought"


# ──────────────────────────────────────────────
# 4. Options Spreads Strategy Tests
# ──────────────────────────────────────────────
class TestOptionsSpreadsStrategy:
    def test_symbol_parser(self):
        meta = parse_option_symbol("C-BTC-70000-270926")
        assert meta is not None
        assert meta["opt_type"] == "C"
        assert meta["underlying"] == "BTC"
        assert meta["strike"] == 70000.0
        assert meta["expiry"] == "270926"

    def test_bull_call_spread_detection(self):
        strat = OptionsSpreads(min_rr_ratio=1.5, max_debit_pct_of_width=0.40)
        quotes = {
            # K1 = 60000, Ask = 1500 (Buy lower strike)
            "C-BTC-60000-270926": {"bid": 1400.0, "ask": 1500.0},
            # K2 = 62000, Bid = 1000 (Sell higher strike)
            # Width = 2000, Net Debit = 1500 - 1000 = 500
            # Max Profit = 2000 - 500 = 1500 -> R:R = 1500 / 500 = 3.0 (>= 1.5)
            "C-BTC-62000-270926": {"bid": 1000.0, "ask": 1100.0},
        }

        signals = strat.generate_signals(quotes)
        assert len(signals) == 1
        sig = signals[0]
        assert sig.direction == "BUY"
        assert sig.target_entry_price == Decimal("500")
        assert sig.take_profit_price == Decimal("1500")
        assert sig.rationale["spread_type"] == "bull_call_spread"
        assert sig.rationale["risk_reward_ratio"] == 3.0
        assert len(sig.legs) == 2
        assert sig.legs[0]["side"] == "BUY"
        assert sig.legs[1]["side"] == "SELL"

    def test_bear_put_spread_detection(self):
        strat = OptionsSpreads(min_rr_ratio=1.5, max_debit_pct_of_width=0.40)
        quotes = {
            # K1 = 60000, Bid = 300 (Sell lower strike)
            "P-BTC-60000-270926": {"bid": 300.0, "ask": 350.0},
            # K2 = 62000, Ask = 800 (Buy higher strike)
            # Width = 2000, Net Debit = 800 - 300 = 500
            # Max Profit = 2000 - 500 = 1500 -> R:R = 3.0
            "P-BTC-62000-270926": {"bid": 750.0, "ask": 800.0},
        }

        signals = strat.generate_signals(quotes)
        assert len(signals) == 1
        sig = signals[0]
        assert sig.direction == "BUY"
        assert sig.target_entry_price == Decimal("500")
        assert sig.rationale["spread_type"] == "bear_put_spread"
        assert len(sig.legs) == 2


# ──────────────────────────────────────────────
# 5. Anomaly Strategy Tests
# ──────────────────────────────────────────────
class TestAnomalyStrategy:
    def test_symbol_parser(self):
        meta = parse_option_metadata("P-ETH-3000-270926")
        assert meta is not None
        assert meta["opt_type"] == "P"
        assert meta["underlying"] == "ETH"
        assert meta["strike"] == 3000.0

    def test_illiquidity_gap_detection(self):
        strat = AnomalyStrategy(far_otm_gap=30.0, near_atm_gap=70.0)
        quote = {
            "symbol": "C-BTC-70000-270926",
            "bid": 50.0,
            "ask": 200.0,  # Gap = 150 > 70
            "spot": 69000.0,
        }

        sig = strat.on_quote(quote)
        assert sig is not None
        assert sig.direction == "BUY"
        assert sig.rationale["anomaly_type"] == "illiquidity_gap"
        assert sig.rationale["gap"] == 150.0

    def test_fat_finger_trade_anomaly(self):
        strat = AnomalyStrategy(fat_finger_deviation=70.0)
        quote = {
            "symbol": "C-BTC-70000-270926",
            "bid": 90.0,
            "ask": 110.0,  # Mid = 100.0
            "last": 250.0,  # Deviation = 150.0 >= 70.0 (Huge premium paid!)
            "spot": 60000.0,
        }

        sig = strat.on_quote(quote)
        assert sig is not None
        assert sig.direction == "SELL"  # Fade the fat-finger spike
        assert sig.target_entry_price == Decimal("250.0")
        assert sig.take_profit_price == Decimal("100.0")
        assert sig.rationale["anomaly_type"] == "fat_finger"

    def test_strike_arbitrage_detection(self):
        strat = AnomalyStrategy(arb_min_profit=5.0)
        quotes = {
            # K1 = 70000 Call, Bid = 120.0
            "C-BTC-70000-270926": {"bid": 120.0, "ask": 130.0},
            # K2 = 72000 Call, Ask = 100.0 (Higher strike call selling CHEAPER than lower strike bid!)
            # Guaranteed risk-free profit: Sell K1 at 120, Buy K2 at 100 -> Edge = 20 pts
            "C-BTC-72000-270926": {"bid": 90.0, "ask": 100.0},
        }

        signals = strat.detect_strike_arbitrage(quotes)
        assert len(signals) == 1
        sig = signals[0]
        assert sig.rationale["anomaly_type"] == "strike_arbitrage"
        assert sig.rationale["arbitrage_edge"] == 20.0
        assert len(sig.legs) == 2

    def test_butterfly_spread_net_credit(self):
        strat = AnomalyStrategy(butterfly_min_credit=5.0)
        quotes = {
            # K1 = 70000, Ask = 30
            "C-BTC-70000-270926": {"bid": 25.0, "ask": 30.0},
            # K2 = 72000, Bid = 50 (2x = 100)
            "C-BTC-72000-270926": {"bid": 50.0, "ask": 55.0},
            # K3 = 74000, Ask = 30
            # Wings Ask = 30 + 30 = 60, Body Bid = 2 * 50 = 100 -> Net credit = 40 >= 5
            "C-BTC-74000-270926": {"bid": 20.0, "ask": 30.0},
        }

        signals = strat.detect_butterfly_spreads(quotes)
        assert len(signals) == 1
        sig = signals[0]
        assert sig.rationale["anomaly_type"] == "butterfly_spread"
        assert sig.rationale["net_credit"] == 40.0
        assert len(sig.legs) == 3
