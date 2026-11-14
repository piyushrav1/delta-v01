"""
Phase 6 Tests: Strategy Framework, Canonical Signals, Strategies & Selector
"""
import pytest
from decimal import Decimal
from datetime import datetime, timezone, timedelta

from analytics.signal import TradeSignal, buy_signal, sell_signal
from strategies.base import (
    BaseStrategy, register_strategy, list_strategies, get_strategy,
    _strategies, _check_forbidden_imports
)
from strategies.trend_following import TrendFollowing
from strategies.momentum import Momentum
from strategies.mean_reversion import MeanReversion
from strategies.options_spreads import OptionsSpreads
from strategies.anomaly_strategy import AnomalyStrategy
from strategies.strategy_selector import (
    StrategySelector, SignalDeduplicator, ConflictDetector, SignalRanker
)


class TestCanonicalTradeSignal:
    def test_signal_creation_and_immutability(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        sig = TradeSignal(
            signal_id="sig_001",
            strategy_id="test_strat",
            strategy_version="1.0.0",
            symbol="BTCUSD",
            direction="BUY",
            order_type="limit_order",
            target_entry_price=Decimal("50000"),
            stop_loss_price=Decimal("49000"),
            take_profit_price=Decimal("52000"),
            proposed_size=1,
            max_slippage_pct=Decimal("0.005"),
            expected_holding_period_sec=3600,
            timestamp=now,
            expiry_time=now + timedelta(hours=1),
            confidence=Decimal("0.85"),
            rationale={"indicator": "test"},
        )
        assert sig.signal_id == "sig_001"
        assert sig.direction == "BUY"
        # Immutability check (frozen dataclass)
        with pytest.raises(Exception):
            sig.direction = "SELL"

    def test_buy_signal_constructor(self):
        sig = buy_signal(
            signal_id="buy_1",
            strategy_id="trend_following",
            strategy_version="1.0.0",
            symbol="BTCUSD",
            entry_price=Decimal("50000"),
            stop_loss=Decimal("49000"),
            take_profit=Decimal("52000"),
            proposed_size=2,
            confidence=Decimal("0.9"),
        )
        assert sig.direction == "BUY"
        assert sig.target_entry_price == Decimal("50000")
        assert sig.proposed_size == 2

    def test_sell_signal_constructor(self):
        sig = sell_signal(
            signal_id="sell_1",
            strategy_id="trend_following",
            strategy_version="1.0.0",
            symbol="ETHUSD",
            entry_price=Decimal("3000"),
            stop_loss=Decimal("3100"),
            take_profit=Decimal("2800"),
            proposed_size=5,
            confidence=Decimal("0.75"),
        )
        assert sig.direction == "SELL"
        assert sig.target_entry_price == Decimal("3000")
        assert sig.proposed_size == 5


class TestStrategyRegistryAndForbiddenImports:
    def test_registry_contains_expected_strategies(self):
        strats = list_strategies()
        assert "trend_following" in strats
        assert "momentum" in strats
        assert "mean_reversion" in strats
        assert "options_spreads" in strats
        assert "anomaly_strategy" in strats

    def test_get_strategy_by_id(self):
        cls = get_strategy("trend_following")
        assert cls == TrendFollowing

    def test_forbidden_import_guard(self):
        with pytest.raises(TypeError, match="cannot import"):
            _check_forbidden_imports("module_with_exchange.delta_client")

        with pytest.raises(TypeError, match="cannot import"):
            _check_forbidden_imports("module_with_trading.execution_manager")


class TestStrategyInstances:
    def test_trend_following_initialization(self):
        strat = TrendFollowing()
        assert strat.strategy_id == "trend_following"
        assert strat.warmup_period == 55
        assert strat.generate_signals({}) == []

    def test_momentum_initialization(self):
        strat = Momentum()
        assert strat.strategy_id == "momentum"
        assert strat.warmup_period == 30
        assert strat.generate_signals({}) == []

    def test_mean_reversion_initialization(self):
        strat = MeanReversion()
        assert strat.strategy_id == "mean_reversion"
        assert strat.warmup_period == 30
        assert strat.generate_signals({}) == []

    def test_options_spreads_initialization(self):
        strat = OptionsSpreads()
        assert strat.strategy_id == "options_spreads"
        assert strat.warmup_period == 10
        assert strat.generate_signals({}) == []

    def test_anomaly_strategy_initialization(self):
        strat = AnomalyStrategy()
        assert strat.strategy_id == "anomaly_strategy"
        assert strat.generate_signals({}) == []


class TestStrategySelector:
    def test_deduplicator(self):
        dedup = SignalDeduplicator(max_age_ms=5000)
        sig1 = buy_signal("s1", "strat1", "1.0", "BTCUSD", Decimal("50000"), Decimal("49000"), Decimal("52000"), 1)
        assert dedup.is_fresh(sig1) is True
        assert dedup.is_duplicate(sig1) is False
        dedup.record(sig1)
        assert dedup.is_duplicate(sig1) is True

    def test_conflict_detector(self):
        detector = ConflictDetector()
        sig_buy = buy_signal("s1", "strat1", "1.0", "BTCUSD", Decimal("50000"), Decimal("49000"), Decimal("52000"), 1)
        sig_sell = sell_signal("s2", "strat2", "1.0", "BTCUSD", Decimal("50000"), Decimal("51000"), Decimal("48000"), 1)

        detector.record_signal(sig_buy)
        detector.record_signal(sig_sell)
        conflicts = detector.detect_conflicts()
        assert "BTCUSD" in conflicts
        resolutions = detector.resolve_conflicts(conflicts)
        assert len(resolutions) > 0

    def test_signal_ranker(self):
        ranker = SignalRanker()
        sig_low = buy_signal("s1", "strat1", "1.0", "BTCUSD", Decimal("50000"), Decimal("49000"), Decimal("52000"), 1, confidence=Decimal("0.6"))
        sig_high = buy_signal("s2", "strat2", "1.0", "ETHUSD", Decimal("3000"), Decimal("2900"), Decimal("3200"), 1, confidence=Decimal("0.95"))

        score_low = ranker.rank(sig_low)
        score_high = ranker.rank(sig_high)
        assert score_high > score_low

        ranked_list = ranker.rank_signal_list([sig_low, sig_high])
        assert len(ranked_list) == 2
        assert ranked_list[0][0].symbol == "ETHUSD"

    def test_full_strategy_selector_pipeline(self):
        selector = StrategySelector()
        sig1 = buy_signal("s1", "strat1", "1.0", "BTCUSD", Decimal("50000"), Decimal("49000"), Decimal("52000"), 1, confidence=Decimal("0.6"))
        sig2 = buy_signal("s2", "strat2", "1.0", "ETHUSD", Decimal("3000"), Decimal("2900"), Decimal("3200"), 1, confidence=Decimal("0.95"))

        selected = selector.select([sig1, sig2])
        assert len(selected) == 2
        assert selected[0].symbol == "ETHUSD"
