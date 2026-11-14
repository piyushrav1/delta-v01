"""
Phase 8 Tests: Strategy Selection and Portfolio Coordination

Tests:
1. Task 8.1: Signal Deduplication and Freshness Filter (age <= 5000ms, active positions filter)
2. Task 8.2: Cross-Strategy Conflict Detection and Market Regime Arbitration
3. Task 8.3: Multi-Factor Signal Ranking (R:R, confidence, regime, liquidity)
4. End-to-End StrategySelector Pipeline
"""
import pytest
import time
from datetime import datetime, timedelta
from decimal import Decimal

from trading.signal import TradeSignal, buy_signal, sell_signal
from trading.selector import StrategySelector, SignalDeduplicator
from trading.conflict_resolver import ConflictResolver
from trading.ranker import SignalRanker
from analytics.market_regime import MarketRegime


class TestSignalFreshnessAndDeduplication:
    def test_fresh_signal_accepted(self):
        dedup = SignalDeduplicator(max_age_ms=5000.0)
        sig = buy_signal(
            signal_id="sig_fresh_1",
            strategy_id="strat_a",
            strategy_version="1.0.0",
            symbol="BTCUSD",
            entry_price=Decimal("60000"),
            stop_loss=Decimal("59000"),
            take_profit=Decimal("62000"),
            proposed_size=1,
        )
        assert dedup.is_fresh(sig) is True

    def test_stale_signal_rejected(self):
        dedup = SignalDeduplicator(max_age_ms=5000.0)
        old_time = datetime.now() - timedelta(seconds=10)
        sig = TradeSignal(
            signal_id="sig_stale_1",
            strategy_id="strat_a",
            strategy_version="1.0.0",
            symbol="BTCUSD",
            direction="BUY",
            order_type="limit_order",
            target_entry_price=Decimal("60000"),
            stop_loss_price=Decimal("59000"),
            take_profit_price=Decimal("62000"),
            proposed_size=1,
            max_slippage_pct=Decimal("0.005"),
            expected_holding_period_sec=3600,
            timestamp=old_time,
            expiry_time=old_time + timedelta(hours=1),
            confidence=Decimal("0.8"),
            rationale={},
        )
        assert dedup.is_fresh(sig) is False

    def test_duplicate_signal_detection(self):
        dedup = SignalDeduplicator(max_age_ms=5000.0, deduplication_window_sec=10.0)
        sig1 = buy_signal(
            signal_id="sig_1",
            strategy_id="trend_following",
            strategy_version="1.0.0",
            symbol="BTCUSD",
            entry_price=Decimal("60000"),
            stop_loss=Decimal("59000"),
            take_profit=Decimal("62000"),
            proposed_size=1,
        )
        assert dedup.is_duplicate(sig1) is False
        dedup.record(sig1)
        assert dedup.is_duplicate(sig1) is True


class TestConflictResolutionAndRegimeArbitration:
    def test_detect_directional_conflicts(self):
        resolver = ConflictResolver()
        buy_sig = buy_signal("b1", "strat_a", "1.0", "BTCUSD", Decimal("60000"), Decimal("59000"), Decimal("62000"), 1)
        sell_sig = sell_signal("s1", "strat_b", "1.0", "BTCUSD", Decimal("60000"), Decimal("61000"), Decimal("58000"), 1)

        conflicts = resolver.detect_conflicts([buy_sig, sell_sig])
        assert "BTCUSD" in conflicts
        assert len(conflicts["BTCUSD"]) == 2

    def test_arbitrate_with_bull_trend_regime(self):
        resolver = ConflictResolver()
        buy_sig = buy_signal("b1", "strat_a", "1.0", "BTCUSD", Decimal("60000"), Decimal("59000"), Decimal("62000"), 1, confidence=Decimal("0.70"))
        sell_sig = sell_signal("s1", "strat_b", "1.0", "BTCUSD", Decimal("60000"), Decimal("61000"), Decimal("58000"), 1, confidence=Decimal("0.75"))

        regimes = {"BTCUSD": MarketRegime.TRENDING_BULL}
        resolved = resolver.resolve([buy_sig, sell_sig], regimes=regimes)

        assert len(resolved) == 1
        assert resolved[0].direction == "BUY"
        assert resolved[0].signal_id == "b1"

    def test_arbitrate_with_bear_trend_regime(self):
        resolver = ConflictResolver()
        buy_sig = buy_signal("b1", "strat_a", "1.0", "BTCUSD", Decimal("60000"), Decimal("59000"), Decimal("62000"), 1, confidence=Decimal("0.75"))
        sell_sig = sell_signal("s1", "strat_b", "1.0", "BTCUSD", Decimal("60000"), Decimal("61000"), Decimal("58000"), 1, confidence=Decimal("0.70"))

        regimes = {"BTCUSD": MarketRegime.TRENDING_BEAR}
        resolved = resolver.resolve([buy_sig, sell_sig], regimes=regimes)

        assert len(resolved) == 1
        assert resolved[0].direction == "SELL"
        assert resolved[0].signal_id == "s1"

    def test_arbitrate_with_ranging_regime_favors_mean_reversion(self):
        resolver = ConflictResolver()
        trend_long = buy_signal("t1", "trend_following", "1.0", "ETHUSD", Decimal("3000"), Decimal("2900"), Decimal("3200"), 1, confidence=Decimal("0.75"))
        mr_short = sell_signal("m1", "mean_reversion", "1.0", "ETHUSD", Decimal("3000"), Decimal("3100"), Decimal("2900"), 1, confidence=Decimal("0.70"))

        regimes = {"ETHUSD": MarketRegime.RANGING_LOW_VOL}
        resolved = resolver.resolve([trend_long, mr_short], regimes=regimes)

        assert len(resolved) == 1
        assert resolved[0].strategy_id == "mean_reversion"
        assert resolved[0].direction == "SELL"

    def test_suppress_both_when_ambiguous_and_no_edge(self):
        resolver = ConflictResolver(min_confidence_edge=0.20)
        buy_sig = buy_signal("b1", "strat_a", "1.0", "BTCUSD", Decimal("60000"), Decimal("59000"), Decimal("62000"), 1, confidence=Decimal("0.70"))
        sell_sig = sell_signal("s1", "strat_b", "1.0", "BTCUSD", Decimal("60000"), Decimal("61000"), Decimal("58000"), 1, confidence=Decimal("0.72"))

        # No regime provided and confidence delta (0.02) < 0.20
        resolved = resolver.resolve([buy_sig, sell_sig], regimes={})
        assert len(resolved) == 0  # Both suppressed to prevent whipsaws


class TestMultiFactorSignalRanking:
    def test_rank_higher_rr_and_confidence(self):
        ranker = SignalRanker()
        # High R:R (3:1) and high confidence (0.90)
        sig_great = buy_signal(
            "s_great", "strat_a", "1.0", "BTCUSD",
            Decimal("60000"), Decimal("59000"), Decimal("63000"), 1, confidence=Decimal("0.90")
        )
        # Low R:R (1:1) and low confidence (0.55)
        sig_weak = buy_signal(
            "s_weak", "strat_b", "1.0", "ETHUSD",
            Decimal("3000"), Decimal("2900"), Decimal("3100"), 1, confidence=Decimal("0.55")
        )

        score_great = ranker.calculate_score(sig_great)
        score_weak = ranker.calculate_score(sig_weak)
        assert score_great > score_weak

        ranked = ranker.rank([sig_weak, sig_great])
        assert ranked[0][0].signal_id == "s_great"

    def test_liquidity_impact_on_score(self):
        ranker = SignalRanker()
        sig = buy_signal("s1", "strat", "1.0", "BTCUSD", Decimal("60000"), Decimal("59000"), Decimal("62000"), 1)

        tight_book = {"bid": 59990.0, "ask": 60000.0}  # ~0.016% spread
        wide_book = {"bid": 58000.0, "ask": 60000.0}   # ~3.4% spread

        score_tight = ranker.calculate_score(sig, quote=tight_book)
        score_wide = ranker.calculate_score(sig, quote=wide_book)
        assert score_tight > score_wide


class TestFullStrategySelectorPipeline:
    def test_end_to_end_selector_pipeline(self):
        selector = StrategySelector(max_selected_signals=3)

        # 1. Fresh buy on BTCUSD
        sig_btc_buy = buy_signal("btc_b", "trend", "1.0", "BTCUSD", Decimal("60000"), Decimal("59000"), Decimal("62000"), 1, confidence=Decimal("0.85"))
        # 2. Conflicting sell on BTCUSD
        sig_btc_sell = sell_signal("btc_s", "mean_rev", "1.0", "BTCUSD", Decimal("60000"), Decimal("61000"), Decimal("58000"), 1, confidence=Decimal("0.60"))
        # 3. Fresh buy on ETHUSD
        sig_eth_buy = buy_signal("eth_b", "momentum", "1.0", "ETHUSD", Decimal("3000"), Decimal("2900"), Decimal("3200"), 1, confidence=Decimal("0.90"))
        # 4. Fresh buy on SOLUSD, but SOLUSD already has active open position
        sig_sol_buy = buy_signal("sol_b", "trend", "1.0", "SOLUSD", Decimal("150"), Decimal("140"), Decimal("170"), 1, confidence=Decimal("0.95"))

        regimes = {
            "BTCUSD": MarketRegime.TRENDING_BULL,
            "ETHUSD": MarketRegime.BREAKOUT_EXPANSION,
        }
        active_positions = {"SOLUSD"}

        selected = selector.select(
            signals=[sig_btc_buy, sig_btc_sell, sig_eth_buy, sig_sol_buy],
            regimes=regimes,
            active_symbols=active_positions,
        )

        # Expect:
        # - SOLUSD rejected (active position)
        # - BTCUSD sell rejected by conflict resolution (bull trend favors buy)
        # - BTCUSD buy and ETHUSD buy selected
        assert len(selected) == 2
        symbols = [s.symbol for s in selected]
        assert "SOLUSD" not in symbols
        assert "BTCUSD" in symbols
        assert "ETHUSD" in symbols

        # Verify only BUY is kept for BTCUSD
        btc_sig = [s for s in selected if s.symbol == "BTCUSD"][0]
        assert btc_sig.direction == "BUY"
