"""
Phase 16 Tests: Controlled Live Automation Release

Tests:
1. Phased Capital Rollout Constraints (Task 16.2: Micro-lot, $20 hard stop, strategy whitelist)
2. Live Readiness Checklist Verification (Task 16.1: Cryptographic signing, SSRF whitelist, DB WAL, circuit breakers)
"""
import os
import pytest
from decimal import Decimal
from unittest.mock import patch

from config.live_rollout import (
    RolloutStage, LiveRolloutConfig, get_rollout_config, STAGE_CONFIGS
)
from core.exceptions import RiskViolation
from scripts.verify_live_readiness import ChecklistVerifier


class TestLiveRolloutConfig:
    """Verifies that phased capital rollout rules strictly protect capital."""

    def test_stage_micro_lot_constraints(self):
        cfg = STAGE_CONFIGS[RolloutStage.MICRO_LOT]
        assert cfg.max_lots_per_order == 1
        assert cfg.max_open_positions == 1
        assert cfg.max_daily_loss_usd == Decimal("20.00")
        assert cfg.require_stop_loss is True

        # 1. Compliant order passes
        valid, reason = cfg.validate_order(
            strategy_id="strat_trend_futures",
            size=1,
            current_open_positions=0,
            current_daily_loss_usd=Decimal("0.0"),
            has_stop_loss=True,
        )
        assert valid is True
        assert reason is None

        # 2. Oversized order rejected
        valid, reason = cfg.validate_order(
            strategy_id="strat_trend_futures",
            size=2,
            current_open_positions=0,
        )
        assert valid is False
        assert "exceeds" in reason

        # 3. Position limit reached rejected
        valid, reason = cfg.validate_order(
            strategy_id="strat_trend_futures",
            size=1,
            current_open_positions=1,
        )
        assert valid is False
        assert "reached" in reason

        # 4. Daily hard stop loss breached rejected
        valid, reason = cfg.validate_order(
            strategy_id="strat_trend_futures",
            size=1,
            current_daily_loss_usd=Decimal("20.00"),
        )
        assert valid is False
        assert "breached" in reason

        # 5. Non-whitelisted strategy rejected
        valid, reason = cfg.validate_order(
            strategy_id="unauthorized_random_strategy",
            size=1,
        )
        assert valid is False
        assert "not authorized" in reason

        # 6. Missing stop-loss rejected
        valid, reason = cfg.validate_order(
            strategy_id="strat_trend_futures",
            size=1,
            has_stop_loss=False,
        )
        assert valid is False
        assert "Stop-Loss" in reason

    def test_assert_order_allowed_raises_risk_violation(self):
        cfg = STAGE_CONFIGS[RolloutStage.MICRO_LOT]
        with pytest.raises(RiskViolation, match="exceeds"):
            cfg.assert_order_allowed(
                strategy_id="strat_trend_futures",
                size=5,
            )

    def test_get_rollout_config_defaults_to_micro_lot(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = get_rollout_config()
            assert cfg.stage == RolloutStage.MICRO_LOT


class TestLiveReadinessChecklist:
    """Verifies that multi-point sign-off protects against accidental live trading."""

    def test_checklist_happy_path(self):
        verifier = ChecklistVerifier(verbose=False)
        passed = verifier.verify_all()
        assert passed is True
        assert len(verifier.results) == 6

    def test_checklist_catches_unauthorized_live_auto(self):
        with patch.dict(os.environ, {"TRADING_ENVIRONMENT": "LIVE_AUTO", "LIVE_AUTO_AUTHORIZED": "false"}):
            verifier = ChecklistVerifier(verbose=False)
            passed = verifier.verify_all()
            assert passed is False
            chk_01 = next(r for r in verifier.results if r["code"] == "CHK-01")
            assert chk_01["passed"] is False
            assert "Fail-Closed" in chk_01["message"]

    def test_checklist_catches_malicious_ssrf_host(self):
        with patch.dict(os.environ, {"DELTA_API_BASE_URL": "https://malicious-phishing-host.com"}):
            verifier = ChecklistVerifier(verbose=False)
            passed = verifier.verify_all()
            assert passed is False
            chk_06 = next(r for r in verifier.results if r["code"] == "CHK-06")
            assert chk_06["passed"] is False
            assert "NOT in authorized SSRF whitelist" in chk_06["message"]

    def test_checklist_catches_short_credentials(self):
        with patch.dict(os.environ, {"DELTA_API_KEY": "short", "DELTA_API_SECRET": "short"}):
            verifier = ChecklistVerifier(verbose=False)
            passed = verifier.verify_all()
            assert passed is False
            chk_02 = next(r for r in verifier.results if r["code"] == "CHK-02")
            assert chk_02["passed"] is False
            assert "too short" in chk_02["message"]
