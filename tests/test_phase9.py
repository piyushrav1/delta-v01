"""
Phase 9 Tests: Central Risk Management Engine (FAIL-CLOSED)

Tests:
1. Contract-Aware Position Sizer (Task 9.2: Risk formulas, margin limits, lot rounding)
2. Real-Time P&L & Drawdown Tracker (Task 9.1: Realized fills, mark updates, high-water marks)
3. Hardware & Market Circuit Breakers (Task 9.4: Heartbeats, rejections, auto-trips, resets)
4. Central Risk Gateway 11-Step Verification Pipeline (Task 9.3: Fail-closed checks, approvals)
"""
import pytest
import time
from datetime import datetime, timedelta
from decimal import Decimal

from config.environments import TradingEnvironment, EnvironmentConfig
from config.settings import RiskSettings
from analytics.signal import TradeSignal, buy_signal, sell_signal
from risk.position_sizing import PositionSizer, SizingResult
from risk.pnl_tracker import PnLTracker, AccountPnLState
from risk.circuit_breaker import CircuitBreakerManager
from risk.risk_manager import CentralRiskManager, RiskDecisionResult


# ──────────────────────────────────────────────
# 1. Position Sizing Tests
# ──────────────────────────────────────────────
class TestPositionSizing:
    def test_standard_futures_sizing(self):
        sizer = PositionSizer(default_risk_pct=0.01, default_leverage=10.0)
        # Equity: $10,000 -> 1% risk = $100 budget
        # Entry: $60,000, SL: $59,000 -> $1,000 dist
        # Contract multiplier = 0.001 (1 mBTC) -> Unit loss = $1.00 + small fee
        # Sizing risk raw = $100 / $1.06 ~ 94 lots
        result = sizer.calculate_size(
            account_equity=Decimal("10000"),
            available_margin=Decimal("5000"),
            entry_price=Decimal("60000"),
            stop_loss_price=Decimal("59000"),
            contract_multiplier=Decimal("0.001"),
            lot_size=1,
        )

        assert result.is_valid is True
        assert 80 <= result.size <= 100
        assert result.allocated_risk_usd <= Decimal("105")  # Fits within $100 risk + tolerance
        assert result.margin_required_usd <= Decimal("2500")

    def test_insufficient_equity_or_margin(self):
        sizer = PositionSizer()
        # Zero equity
        res_eq = sizer.calculate_size(
            account_equity=Decimal("0"),
            available_margin=Decimal("1000"),
            entry_price=Decimal("100"),
            stop_loss_price=Decimal("95"),
        )
        assert res_eq.is_valid is False
        assert res_eq.size == 0

        # Zero margin
        res_mg = sizer.calculate_size(
            account_equity=Decimal("10000"),
            available_margin=Decimal("0"),
            entry_price=Decimal("100"),
            stop_loss_price=Decimal("95"),
        )
        assert res_mg.is_valid is False
        assert res_mg.size == 0

    def test_lot_size_step_rounding(self):
        sizer = PositionSizer(default_risk_pct=0.05)
        # If formula yields e.g. 24 lots, but lot_size=5 -> rounds down to 20
        res = sizer.calculate_size(
            account_equity=Decimal("5000"),
            available_margin=Decimal("3000"),
            entry_price=Decimal("100"),
            stop_loss_price=Decimal("95"),
            lot_size=5,
        )
        assert res.is_valid is True
        assert res.size % 5 == 0

    def test_proposed_size_ceiling(self):
        sizer = PositionSizer()
        res = sizer.calculate_size(
            account_equity=Decimal("50000"),
            available_margin=Decimal("20000"),
            entry_price=Decimal("100"),
            stop_loss_price=Decimal("98"),
            proposed_size=2,  # Strategy requests only 2 lots
        )
        assert res.is_valid is True
        assert res.size == 2


# ──────────────────────────────────────────────
# 2. P&L & Drawdown Tracking Tests
# ──────────────────────────────────────────────
class TestPnLTracker:
    def test_realized_fill_pnl_long(self):
        tracker = PnLTracker(default_start_equity=10000.0)
        # Buy at 50,000, Sell at 52,000 with 1 lot, mult=0.001 (1 mBTC)
        # PnL = (52000 - 50000) * 1 * 0.001 - fee(0.5) = $2.0 - 0.5 = $1.5
        pnl = tracker.record_fill(
            account_id="main",
            side="SELL",
            fill_price=Decimal("52000"),
            fill_size=1,
            entry_price=Decimal("50000"),
            contract_multiplier=Decimal("0.001"),
            fee_paid=Decimal("0.5"),
        )
        assert pnl == Decimal("1.5")
        summary = tracker.get_pnl_summary("main")
        assert summary["realized_pnl"] == 1.5
        assert summary["current_equity"] == 10001.5

    def test_unrealized_pnl_and_drawdown(self):
        tracker = PnLTracker(default_start_equity=10000.0)
        positions = [{
            "symbol": "BTCUSD",
            "size": 10,
            "side": "BUY",
            "entry_price": 50000.0,
            "contract_multiplier": 0.01,
        }]

        # Mark price drops to 48,000 (-2000 * 10 * 0.01 = -$200 loss)
        quotes = {"BTCUSD": {"mark": 48000.0}}
        unrealized = tracker.update_positions_mark("main", positions, quotes)
        assert unrealized == Decimal("-200.0")

        summary = tracker.get_pnl_summary("main")
        assert summary["unrealized_pnl"] == -200.0
        assert summary["current_equity"] == 9800.0
        assert summary["peak_drawdown_usd"] == 200.0
        assert summary["drawdown_pct"] == 0.02  # 2% drawdown

    def test_daily_loss_breach_detection(self):
        tracker = PnLTracker(default_start_equity=10000.0)
        # Breached when loss >= $500 or >= 2% ($200)
        positions = [{
            "symbol": "BTCUSD",
            "size": 1,
            "side": "BUY",
            "entry_price": 50000.0,
            "contract_multiplier": 1.0,
        }]
        quotes_ok = {"BTCUSD": {"mark": 49900.0}}  # -$100 loss (1% < 2%, $100 < $500)
        tracker.update_positions_mark("main", positions, quotes_ok)
        breached_ok, _ = tracker.is_daily_loss_breached("main", max_loss_usd=500.0, max_loss_pct=0.02)
        assert breached_ok is False

        quotes_bad = {"BTCUSD": {"mark": 49400.0}}  # -$600 loss (> $500 cap)
        tracker.update_positions_mark("main", positions, quotes_bad)
        breached_bad, msg = tracker.is_daily_loss_breached("main", max_loss_usd=500.0, max_loss_pct=0.02)
        assert breached_bad is True
        assert "Daily loss cap reached" in msg


# ──────────────────────────────────────────────
# 3. Circuit Breaker Tests
# ──────────────────────────────────────────────
class TestCircuitBreakers:
    def test_initial_state_not_tripped(self):
        cb = CircuitBreakerManager()
        assert cb.is_tripped() is False

    def test_heartbeat_timeout(self):
        cb = CircuitBreakerManager(heartbeat_timeout_sec=0.1)
        cb.update_heartbeat()
        assert cb.check_heartbeat() is False

        time.sleep(0.15)
        assert cb.check_heartbeat() is True
        assert cb.is_tripped() is True
        assert cb.is_tripped(CircuitBreakerManager.BREAKER_HEARTBEAT_TIMEOUT) is True

        # Heartbeat recovery
        cb.update_heartbeat()
        assert cb.is_tripped() is False

    def test_consecutive_rejections_trip(self):
        cb = CircuitBreakerManager(rejection_limit=3)
        assert cb.record_rejection("acct1", "Error 1") is False
        assert cb.record_rejection("acct1", "Error 2") is False
        # 3rd rejection trips
        assert cb.record_rejection("acct1", "Error 3") is True
        assert cb.is_tripped() is True

        # Successful order on another account resets
        cb.record_order_success("acct1")
        cb.resolve(CircuitBreakerManager.BREAKER_CONSECUTIVE_REJECTIONS)
        assert cb.is_tripped() is False


# ──────────────────────────────────────────────
# 4. Central Risk Gateway 11-Step Verification
# ──────────────────────────────────────────────
class TestCentralRiskManagerPipeline:
    @pytest.fixture
    def setup_risk_gateway(self):
        env = EnvironmentConfig(TradingEnvironment.TESTNET)
        settings = RiskSettings(
            max_daily_loss_usd=500.0,
            max_daily_loss_pct=0.02,
            max_risk_per_trade_pct=0.01,
            max_open_positions=5,
            max_leverage=10.0,
            max_slippage_pct=0.005,
            quote_freshness_max_ms=3000.0,
        )
        pnl = PnLTracker(default_start_equity=10000.0)
        cb = CircuitBreakerManager()
        manager = CentralRiskManager(
            env_config=env,
            risk_settings=settings,
            pnl_tracker=pnl,
            circuit_breaker=cb,
        )
        return manager

    def _valid_signal(self):
        now = datetime.now()
        return buy_signal(
            signal_id="sig_test_1",
            strategy_id="trend_following",
            strategy_version="1.0.0",
            symbol="BTCUSD",
            entry_price=Decimal("60000"),
            stop_loss=Decimal("59000"),
            take_profit=Decimal("62000"),
            proposed_size=1,
            confidence=Decimal("0.85"),
        )

    def _valid_quote(self):
        return {
            "symbol": "BTCUSD",
            "bid": 59990.0,
            "ask": 60010.0,
            "mid": 60000.0,
            "timestamp": datetime.now(),
        }

    def test_happy_path_approval(self, setup_risk_gateway):
        manager = setup_risk_gateway
        sig = self._valid_signal()
        quote = self._valid_quote()

        decision = manager.evaluate_signal(
            signal=sig,
            account_id="main",
            account_equity=Decimal("10000"),
            available_margin=Decimal("5000"),
            current_quote=quote,
            instrument_specs={"contract_value": 0.001, "lot_size": 1},
        )

        assert decision.approved is True
        assert decision.calculated_size > 0
        assert decision.allocated_risk_usd > 0
        assert decision.rejection_reason is None
        assert decision.checks_performed["1_environment"]["passed"] is True
        assert decision.checks_performed["11_slippage"]["passed"] is True

    def test_step1_reject_disallowed_environment(self, setup_risk_gateway):
        manager = setup_risk_gateway
        manager.env_config = EnvironmentConfig(TradingEnvironment.BACKTEST)
        sig = self._valid_signal()

        decision = manager.evaluate_signal(
            signal=sig, account_id="main", account_equity=Decimal("10000"),
            available_margin=Decimal("5000"), current_quote=self._valid_quote(),
        )
        assert decision.approved is False
        assert "not permitted in environment" in decision.rejection_reason

    def test_step2_reject_active_circuit_breaker(self, setup_risk_gateway):
        manager = setup_risk_gateway
        manager.circuit_breaker.trip("manual_halt", 1, 1, "Emergency halt")
        sig = self._valid_signal()

        decision = manager.evaluate_signal(
            signal=sig, account_id="main", account_equity=Decimal("10000"),
            available_margin=Decimal("5000"), current_quote=self._valid_quote(),
        )
        assert decision.approved is False
        assert "Trading halted: active circuit breaker" in decision.rejection_reason

    def test_step3_reject_stale_quote(self, setup_risk_gateway):
        manager = setup_risk_gateway
        sig = self._valid_signal()
        stale_quote = {
            "symbol": "BTCUSD",
            "bid": 59990.0, "ask": 60010.0,
            "timestamp": datetime.now() - timedelta(seconds=5),  # 5000ms > 3000ms
        }

        decision = manager.evaluate_signal(
            signal=sig, account_id="main", account_equity=Decimal("10000"),
            available_margin=Decimal("5000"), current_quote=stale_quote,
        )
        assert decision.approved is False
        assert "Market quote stale" in decision.rejection_reason

    def test_step5_reject_max_open_positions(self, setup_risk_gateway):
        manager = setup_risk_gateway
        sig = self._valid_signal()
        five_positions = [{"symbol": f"SYM_{i}", "size": 1} for i in range(5)]

        decision = manager.evaluate_signal(
            signal=sig, account_id="main", account_equity=Decimal("10000"),
            available_margin=Decimal("5000"), current_quote=self._valid_quote(),
            open_positions=five_positions,
        )
        assert decision.approved is False
        assert "Max open positions reached" in decision.rejection_reason

    def test_step6_reject_duplicate_position_or_order(self, setup_risk_gateway):
        manager = setup_risk_gateway
        sig = self._valid_signal()
        positions = [{"symbol": "BTCUSD", "size": 1}]

        decision = manager.evaluate_signal(
            signal=sig, account_id="main", account_equity=Decimal("10000"),
            available_margin=Decimal("5000"), current_quote=self._valid_quote(),
            open_positions=positions,
        )
        assert decision.approved is False
        assert "Active position already open" in decision.rejection_reason

    def test_step7_reject_invalid_stop_loss(self, setup_risk_gateway):
        manager = setup_risk_gateway
        # BUY signal with stop-loss ABOVE entry (invalid!)
        invalid_sl_sig = buy_signal(
            signal_id="sig_inv_sl",
            strategy_id="strat",
            strategy_version="1.0",
            symbol="BTCUSD",
            entry_price=Decimal("60000"),
            stop_loss=Decimal("61000"),
            take_profit=Decimal("65000"),
            proposed_size=1,
        )

        decision = manager.evaluate_signal(
            signal=invalid_sl_sig, account_id="main", account_equity=Decimal("10000"),
            available_margin=Decimal("5000"), current_quote=self._valid_quote(),
        )
        assert decision.approved is False
        assert "Invalid BUY stop-loss" in decision.rejection_reason

    def test_step10_reject_insufficient_margin(self, setup_risk_gateway):
        manager = setup_risk_gateway
        sig = self._valid_signal()

        decision = manager.evaluate_signal(
            signal=sig, account_id="main", account_equity=Decimal("10000"),
            available_margin=Decimal("1.0"),  # Only $1 free margin!
            current_quote=self._valid_quote(),
            instrument_specs={"contract_value": 0.001, "lot_size": 1},
        )
        assert decision.approved is False
        assert ("Margin required" in decision.rejection_reason or "Position sizing rejected" in decision.rejection_reason)

    def test_fail_closed_on_unexpected_exception(self, setup_risk_gateway, monkeypatch):
        manager = setup_risk_gateway
        sig = self._valid_signal()
        quote = self._valid_quote()

        # Simulate unexpected internal failure during sizing
        def crash_sizer(*args, **kwargs):
            raise RuntimeError("Database connection suddenly dropped")

        monkeypatch.setattr(manager.sizer, "calculate_size", crash_sizer)

        decision = manager.evaluate_signal(
            signal=sig, account_id="main", account_equity=Decimal("10000"),
            available_margin=Decimal("5000"), current_quote=quote,
        )
        assert decision.approved is False
        assert "Fail-closed on system error" in decision.rejection_reason
