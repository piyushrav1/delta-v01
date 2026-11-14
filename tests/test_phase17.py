"""
Phase 17 Tests: Day-Trader-Skill Algorithmic Adaptations

Tests the three institutional algorithmic trading modules adapted from day-trader-skill:
1. OpeningRangeBreakout (ORB) - bar-by-bar breakout, volume confirmation, midpoint stops
2. DailySetupScanner - 5 core setups (20d breakout, bull flag, MA20 reclaim, oversold bounce/breakdown)
3. ATRExitManager & calculate_exit_bracket - ATR brackets, TP/SL monitoring, time-based exits
4. Registry & Rollout Integration - Whitelist checks, rollout safety, forbidden import guards
"""
import pytest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import pandas as pd
import numpy as np

from strategies.opening_range_breakout import OpeningRangeBreakout
from strategies.daily_setup_scanner import DailySetupScanner
from strategies.atr_exit_manager import ATRExitManager, calculate_exit_bracket
from strategies.registry import (
    get_strategy_metadata,
    get_all_strategies_metadata,
    is_strategy_enabled,
    list_strategies,
    get_strategy,
)
from config.live_rollout import get_rollout_config, RolloutStage
from analytics.signal import TradeSignal


# ──────────────────────────────────────────────
# 1. Opening Range Breakout (ORB) Tests
# ──────────────────────────────────────────────
class TestOpeningRangeBreakoutStrategy:
    def test_range_formation_and_warmup(self):
        strat = OpeningRangeBreakout(range_bars=3)
        now = datetime.now()

        # Bar 1
        sig = strat.on_candle(open_=100.0, high=102.0, low=99.0, close=101.0, volume=1000, timestamp=now, symbol="BTCUSD")
        assert sig is None

        # Bar 2
        sig = strat.on_candle(open_=101.0, high=103.0, low=100.0, close=102.0, volume=1200, timestamp=now + timedelta(minutes=5), symbol="BTCUSD")
        assert sig is None

        # Bar 3 (finalizing opening range: high=103, low=99, mid=101, avg_vol=1100)
        sig = strat.on_candle(open_=102.0, high=102.5, low=101.0, close=102.0, volume=1100, timestamp=now + timedelta(minutes=10), symbol="BTCUSD")
        assert sig is None

        state = strat._state["BTCUSD"]
        assert state["range_complete"] is True
        assert state["range_high"] == 103.0
        assert state["range_low"] == 99.0
        assert state["range_mid"] == 101.0
        assert state["avg_vol"] == 1100.0

    def test_bullish_orb_breakout_trigger(self):
        strat = OpeningRangeBreakout(range_bars=3, min_volume_x=1.0, rr_ratio=2.0)
        now = datetime.now()

        # Build 3-bar range (high=105.0, low=95.0, mid=100.0, avg_vol=1000)
        for i in range(3):
            strat.on_candle(open_=100.0, high=105.0, low=95.0, close=100.0, volume=1000, timestamp=now + timedelta(minutes=i*5), symbol="BTCUSD")

        # Bar 4: Closes strictly ABOVE range_high (105.0) at 106.5 with volume surge
        sig = strat.on_candle(open_=104.0, high=107.0, low=103.5, close=106.5, volume=1500, timestamp=now + timedelta(minutes=15), symbol="BTCUSD")

        assert sig is not None
        assert sig.direction == "BUY"
        assert sig.symbol == "BTCUSD"
        assert sig.target_entry_price == Decimal("106.5")
        # Midpoint stop: 100.0
        assert sig.stop_loss_price == Decimal("100.0")
        # Risk = 106.5 - 100.0 = 6.5. Target = 106.5 + (6.5 * 2) = 119.5
        assert sig.take_profit_price == Decimal("119.5")
        assert sig.confidence >= Decimal("0.65")
        assert sig.rationale["setup"] == "orb_bullish_breakout"

    def test_wick_above_range_does_not_trigger(self):
        strat = OpeningRangeBreakout(range_bars=3)
        now = datetime.now()

        for i in range(3):
            strat.on_candle(open_=100.0, high=105.0, low=95.0, close=100.0, volume=1000, timestamp=now + timedelta(minutes=i*5), symbol="BTCUSD")

        # High wicks to 108, but CLOSE is 104.5 (below 105.0 range high)
        sig = strat.on_candle(open_=104.0, high=108.0, low=103.0, close=104.5, volume=2000, timestamp=now + timedelta(minutes=15), symbol="BTCUSD")
        assert sig is None

    def test_low_volume_breakout_rejected(self):
        # min_volume_x = 1.5 -> requires >= 1500 vol
        strat = OpeningRangeBreakout(range_bars=3, min_volume_x=1.5)
        now = datetime.now()

        for i in range(3):
            strat.on_candle(open_=100.0, high=105.0, low=95.0, close=100.0, volume=1000, timestamp=now + timedelta(minutes=i*5), symbol="BTCUSD")

        # Closes above range at 106.0, but volume is only 1100 (< 1500)
        sig = strat.on_candle(open_=104.0, high=107.0, low=103.0, close=106.0, volume=1100, timestamp=now + timedelta(minutes=15), symbol="BTCUSD")
        assert sig is None

    def test_bearish_orb_breakdown_trigger(self):
        strat = OpeningRangeBreakout(range_bars=3, allow_short=True)
        now = datetime.now()

        for i in range(3):
            strat.on_candle(open_=100.0, high=105.0, low=95.0, close=100.0, volume=1000, timestamp=now + timedelta(minutes=i*5), symbol="ETHUSD")

        # Bar 4: Closes strictly BELOW range_low (95.0) at 93.0
        sig = strat.on_candle(open_=96.0, high=96.5, low=92.5, close=93.0, volume=1500, timestamp=now + timedelta(minutes=15), symbol="ETHUSD")

        assert sig is not None
        assert sig.direction == "SELL"
        assert sig.symbol == "ETHUSD"
        assert sig.target_entry_price == Decimal("93.0")
        # Midpoint stop: 100.0
        assert sig.stop_loss_price == Decimal("100.0")
        # Risk = 100.0 - 93.0 = 7.0. Target = 93.0 - (7.0 * 2) = 79.0
        assert sig.take_profit_price == Decimal("79.0")
        assert sig.rationale["setup"] == "orb_bearish_breakdown"

    def test_no_duplicate_triggers_in_same_window(self):
        strat = OpeningRangeBreakout(range_bars=3)
        now = datetime.now()

        for i in range(3):
            strat.on_candle(open_=100.0, high=105.0, low=95.0, close=100.0, volume=1000, timestamp=now + timedelta(minutes=i*5), symbol="BTCUSD")

        sig1 = strat.on_candle(open_=104.0, high=107.0, low=103.5, close=106.5, volume=1500, timestamp=now + timedelta(minutes=15), symbol="BTCUSD")
        assert sig1 is not None

        # Subsequent bar closing even higher should NOT emit another signal
        sig2 = strat.on_candle(open_=106.5, high=109.0, low=106.0, close=108.5, volume=1800, timestamp=now + timedelta(minutes=20), symbol="BTCUSD")
        assert sig2 is None

    def test_reset_range_allows_new_trigger(self):
        strat = OpeningRangeBreakout(range_bars=3)
        now = datetime.now()

        for i in range(3):
            strat.on_candle(open_=100.0, high=105.0, low=95.0, close=100.0, volume=1000, timestamp=now + timedelta(minutes=i*5), symbol="BTCUSD")
        sig1 = strat.on_candle(open_=104.0, high=107.0, low=103.5, close=106.5, volume=1500, timestamp=now + timedelta(minutes=15), symbol="BTCUSD")
        assert sig1 is not None

        # Reset range for symbol
        strat.reset_range("BTCUSD")
        assert strat._state["BTCUSD"]["range_complete"] is False
        assert strat._state["BTCUSD"]["triggered"] is False


# ──────────────────────────────────────────────
# 2. Daily Setup Scanner Tests
# ──────────────────────────────────────────────
class TestDailySetupScannerStrategy:
    def test_warmup_enforcement(self):
        strat = DailySetupScanner(warmup_period=25)
        now = datetime.now()
        # Feed 15 candles (less than 25)
        sig = None
        for i in range(15):
            sig = strat.on_candle(open_=100.0, high=105.0, low=95.0, close=100.0, volume=1000, timestamp=now + timedelta(days=i), symbol="BTCUSD")
        assert sig is None

    def test_setup_1_breakout_20d(self):
        strat = DailySetupScanner(breakout_lookback=20, warmup_period=22)
        now = datetime.now()

        # 21 bars ranging between 90 and 100
        for i in range(21):
            p = 90.0 + (i % 10)  # max is 99.0
            strat.on_candle(open_=p, high=p + 2.0, low=p - 2.0, close=p, volume=1000, timestamp=now + timedelta(days=i), symbol="BTCUSD")

        # Bar 22 closes at 105.0 (strictly above all 20 previous bars)
        sig = strat.on_candle(open_=98.0, high=106.0, low=97.0, close=105.0, volume=2000, timestamp=now + timedelta(days=22), symbol="BTCUSD")

        assert sig is not None
        assert sig.direction == "BUY"
        assert sig.target_entry_price == Decimal("105.0")
        assert sig.stop_loss_price < sig.target_entry_price
        assert sig.take_profit_price > sig.target_entry_price
        assert sig.rationale["setup"] == "breakout_20d"
        assert sig.confidence == Decimal("0.80")

    def test_setup_2_bull_flag(self):
        strat = DailySetupScanner(flag_pole_bars=10, flag_consolidation_bars=5, flag_min_gain_pct=10.0, flag_max_range_pct=5.0, warmup_period=15)
        now = datetime.now()

        # Pole: 10 bars rising from 100 to 120 (+20% gain > 10% min)
        for i in range(10):
            p = 100.0 + (i * 2.0)
            strat.on_candle(open_=p, high=p + 1.0, low=p - 1.0, close=p, volume=2000, timestamp=now + timedelta(days=i), symbol="BTCUSD")

        # Consolidation: 5 bars tightly fluctuating between 119 and 121 (range ~1.6% <= 5%)
        sig = None
        for i in range(10, 15):
            p = 120.0 + (0.5 if i % 2 == 0 else -0.5)
            res = strat.on_candle(open_=p, high=p + 1.0, low=p - 1.0, close=p, volume=1000, timestamp=now + timedelta(days=i), symbol="BTCUSD")
            if res:
                sig = res

        assert sig is not None
        assert sig.direction == "BUY"
        assert sig.rationale["setup"] == "bull_flag"
        assert sig.confidence == Decimal("0.78")

    def test_setup_3_reclaim_ma20(self):
        strat = DailySetupScanner(ma_period=20, warmup_period=22)
        now = datetime.now()

        # 20 bars hovering around 100
        for i in range(20):
            strat.on_candle(open_=100.0, high=102.0, low=98.0, close=100.0, volume=1000, timestamp=now + timedelta(days=i), symbol="ETHUSD")

        # Bar 21 dips below 20 MA: close 95.0
        strat.on_candle(open_=99.0, high=100.0, low=94.0, close=95.0, volume=1500, timestamp=now + timedelta(days=21), symbol="ETHUSD")

        # Bar 22 reclaims above 20 MA: close 102.0
        sig = strat.on_candle(open_=96.0, high=103.0, low=95.0, close=102.0, volume=2500, timestamp=now + timedelta(days=22), symbol="ETHUSD")

        assert sig is not None
        assert sig.direction == "BUY"
        assert "reclaim_ma20" in sig.rationale.get("all_matched_setups", [sig.rationale.get("setup")])

    def test_setup_4_oversold_bounce(self):
        strat = DailySetupScanner(rsi_period=14, warmup_period=20)
        now = datetime.now()

        # Prolonged selloff driving RSI under 30
        for i in range(20):
            p = 150.0 - (i * 3.5)  # 150 down to ~80
            strat.on_candle(open_=p + 1.0, high=p + 1.5, low=p - 1.5, close=p, volume=1000, timestamp=now + timedelta(days=i), symbol="SOLUSD")

        # Day t-1: continues selloff
        strat.on_candle(open_=80.0, high=81.0, low=74.0, close=75.0, volume=2000, timestamp=now + timedelta(days=20), symbol="SOLUSD")

        # Day t: closes UP from 75.0 to 82.0 (oversold bounce)
        sig = strat.on_candle(open_=76.0, high=83.0, low=75.0, close=82.0, volume=3000, timestamp=now + timedelta(days=21), symbol="SOLUSD")

        assert sig is not None
        assert sig.direction == "BUY"
        assert "oversold_bounce" in sig.rationale.get("all_matched_setups", [sig.rationale.get("setup")])


# ──────────────────────────────────────────────
# 3. ATR Exit Manager & Bracket Calculator Tests
# ──────────────────────────────────────────────
class TestATRExitManager:
    def test_calculate_exit_bracket_long(self):
        highs = pd.Series([102.0, 103.0, 104.0, 105.0] * 5)
        lows = pd.Series([98.0, 99.0, 100.0, 101.0] * 5)
        closes = pd.Series([100.0, 101.0, 102.0, 103.0] * 5)

        bracket = calculate_exit_bracket(
            entry_price=100.0,
            side="buy",
            high_series=highs,
            low_series=lows,
            close_series=closes,
            stop_mode="atr",
            stop_value=1.5,
            rr_ratio=2.0,
        )

        assert bracket["entry"] == Decimal("100.0")
        assert bracket["stop"] < Decimal("100.0")
        assert bracket["target"] > Decimal("100.0")
        assert bracket["target"] - bracket["entry"] == (bracket["entry"] - bracket["stop"]) * Decimal("2.0")

    def test_calculate_exit_bracket_short(self):
        highs = pd.Series([102.0, 103.0, 104.0, 105.0] * 5)
        lows = pd.Series([98.0, 99.0, 100.0, 101.0] * 5)
        closes = pd.Series([100.0, 101.0, 102.0, 103.0] * 5)

        bracket = calculate_exit_bracket(
            entry_price=100.0,
            side="sell",
            high_series=highs,
            low_series=lows,
            close_series=closes,
            stop_mode="pct",
            stop_value=2.0,  # 2% stop = 2.0
            rr_ratio=2.5,
        )

        assert bracket["entry"] == Decimal("100.0")
        assert bracket["stop"] == Decimal("102.0")
        assert bracket["target"] == Decimal("95.0")  # 100 - (2.0 * 2.5)

    def test_track_position_and_take_profit_exit(self):
        strat = ATRExitManager()
        strat.track_position(
            symbol="BTCUSD",
            side="buy",
            entry_price=100.0,
            size=2,
            stop_loss=95.0,
            take_profit=110.0,
        )
        now = datetime.now()

        # Candle 1: price inside bracket (105.0) -> No exit
        sig = strat.on_candle(open_=100.0, high=106.0, low=99.0, close=105.0, volume=1000, timestamp=now, symbol="BTCUSD")
        assert sig is None

        # Candle 2: price reaches 111.0 (>= 110.0 TP) -> Emit SELL exit signal
        sig = strat.on_candle(open_=106.0, high=112.0, low=105.0, close=111.0, volume=1500, timestamp=now + timedelta(minutes=5), symbol="BTCUSD")
        assert sig is not None
        assert sig.direction == "SELL"
        assert sig.proposed_size == 2
        assert sig.confidence == Decimal("0.95")
        assert sig.rationale["exit_reason"] == "take_profit_hit"

        # Position should now be cleared
        assert "BTCUSD" not in strat._positions

    def test_track_position_and_stop_loss_exit(self):
        strat = ATRExitManager()
        strat.track_position(
            symbol="ETHUSD",
            side="buy",
            entry_price=100.0,
            size=1,
            stop_loss=95.0,
            take_profit=110.0,
        )
        now = datetime.now()

        # Price dumps to 94.0 (<= 95.0 SL) -> Emit SELL exit signal
        sig = strat.on_candle(open_=98.0, high=98.5, low=93.5, close=94.0, volume=2000, timestamp=now, symbol="ETHUSD")
        assert sig is not None
        assert sig.direction == "SELL"
        assert sig.rationale["exit_reason"] == "stop_loss_hit"

    def test_time_based_exit(self):
        strat = ATRExitManager(max_hold_bars=3)
        strat.track_position(
            symbol="SOLUSD",
            side="buy",
            entry_price=100.0,
            size=1,
            stop_loss=90.0,
            take_profit=120.0,
        )
        now = datetime.now()

        # Bar 1 & 2: within max_hold_bars
        assert strat.on_candle(open_=100.0, high=101.0, low=99.0, close=100.0, volume=500, timestamp=now, symbol="SOLUSD") is None
        assert strat.on_candle(open_=100.0, high=101.0, low=99.0, close=100.0, volume=500, timestamp=now + timedelta(minutes=5), symbol="SOLUSD") is None

        # Bar 3: reaches max_hold_bars (3) -> Time exit signal
        sig = strat.on_candle(open_=100.0, high=101.0, low=99.0, close=100.5, volume=500, timestamp=now + timedelta(minutes=10), symbol="SOLUSD")
        assert sig is not None
        assert sig.rationale["exit_reason"] == "time_exit"


# ──────────────────────────────────────────────
# 4. Registry & Rollout Integration Tests
# ──────────────────────────────────────────────
class TestDayTraderSkillIntegration:
    def test_all_new_strategies_registered_in_metadata(self):
        all_metadata = get_all_strategies_metadata()
        strat_ids = {m["strategy_id"] for m in all_metadata}

        assert "opening_range_breakout" in strat_ids
        assert "daily_setup_scanner" in strat_ids
        assert "atr_exit_manager" in strat_ids

        for sid in ("opening_range_breakout", "daily_setup_scanner", "atr_exit_manager"):
            assert is_strategy_enabled(sid) is True
            meta = get_strategy_metadata(sid)
            assert meta["category"] != ""
            assert meta["version"] == "1.0.0"

    def test_rollout_stage_micro_lot_authorizes_new_strategies(self):
        cfg = get_rollout_config(RolloutStage.MICRO_LOT)
        for sid in ("opening_range_breakout", "daily_setup_scanner", "atr_exit_manager"):
            valid, reason = cfg.validate_order(
                strategy_id=sid,
                size=1,
                current_open_positions=0,
                current_daily_loss_usd=Decimal("0.0"),
                has_stop_loss=True,
            )
            assert valid is True, f"Failed for {sid}: {reason}"

    def test_forbidden_imports_protection(self):
        import inspect
        import strategies.opening_range_breakout as orb_mod
        import strategies.daily_setup_scanner as dss_mod
        import strategies.atr_exit_manager as aem_mod

        for mod in (orb_mod, dss_mod, aem_mod):
            src = inspect.getsource(mod)
            assert "exchange.delta_client" not in src
            assert "trading.execution_manager" not in src
