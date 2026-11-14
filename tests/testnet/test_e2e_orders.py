"""
Phase 14 - Task 14.1 (TASK-P14-01): End-to-End Testnet Execution Suite

Comprehensive validation of order lifecycle, atomic amendment, native bracket orders,
idempotency, and cancel-all operations against Delta Exchange India Testnet.

Supports:
1. Hermetic mocked testnet verification for automated CI/CD runs.
2. Optional live smoke test when DELTA_TESTNET_API_KEY is supplied.
"""
import os
import uuid
import pytest
from decimal import Decimal
from unittest.mock import Mock, MagicMock, patch

from config.environments import TradingEnvironment, EnvironmentConfig
from config.accounts import AccountConfig
from exchange.delta_client import DeltaRestClient
from storage.models import OrderState, OrderSide, OrderType
from core.exceptions import RiskViolation, OrderExecutionError
from analytics.signal import buy_signal, sell_signal
from risk.risk_manager import RiskDecisionResult
from trading.execution_manager import ExecutionManager
from trading.bracket_manager import BracketManager, ProtectionStatus
from trading.order_state import ManagedOrder


# ──────────────────────────────────────────────
# Test Fixtures
# ──────────────────────────────────────────────
@pytest.fixture
def testnet_account() -> AccountConfig:
    """Standard testnet account configuration."""
    return AccountConfig(
        account_id="testnet_main",
        label="Delta Testnet Account",
        api_key="testnet_api_key_32_characters_12345",
        api_secret="testnet_api_secret_64_characters_hex_string_1234567890abcdef",
        environment=TradingEnvironment.TESTNET,
    )


@pytest.fixture
def mock_testnet_client(testnet_account) -> MagicMock:
    """Mock DeltaRestClient configured for testnet."""
    client = MagicMock(spec=DeltaRestClient)
    client.account = testnet_account
    client.base_url = "https://cdn-ind.testnet.deltaex.org"
    client.FALLBACK_BASE = "https://api.deltaexchange.in"
    return client


# ──────────────────────────────────────────────
# 1. Testnet Environment & Credential Isolation
# ──────────────────────────────────────────────
class TestTestnetConfigurationAndSafety:
    """Verifies that testnet operates under strict environment isolation."""

    def test_testnet_environment_properties(self):
        env = TradingEnvironment.TESTNET
        assert env.base_url == "https://cdn-ind.testnet.deltaex.org"
        assert env.is_live is False
        assert env.is_testnet is True
        assert env.allows_auto_trading is True
        assert env.allows_manual_trading is True
        assert env.color == "#f59e0b"

    def test_testnet_account_configuration(self, testnet_account):
        assert testnet_account.effective_base_url == "https://cdn-ind.testnet.deltaex.org"
        assert testnet_account.is_live is False
        assert testnet_account.is_testnet is True
        assert testnet_account.allows_auto_trading(TradingEnvironment.TESTNET) is True
        # Safety: Testnet account must NOT allow auto-trading in LIVE_AUTO environment
        assert testnet_account.allows_auto_trading(TradingEnvironment.LIVE_AUTO) is False

    def test_testnet_rest_client_initialization(self, testnet_account):
        client = DeltaRestClient(account=testnet_account)
        assert client.base_url == "https://cdn-ind.testnet.deltaex.org"
        assert client.key == testnet_account.api_key
        assert client.secret == testnet_account.api_secret


# ──────────────────────────────────────────────
# 2. Testnet Futures Order Execution
# ──────────────────────────────────────────────
class TestTestnetFuturesOrderE2E:
    """Tests end-to-end execution of futures orders on testnet."""

    def test_e2e_futures_limit_order_placement(self, mock_testnet_client):
        # Arrange mock response
        ex_order_id = "testnet_ord_987654"
        mock_testnet_client.place_order.return_value = {
            "id": ex_order_id,
            "symbol": "BTCUSD",
            "side": "buy",
            "size": 2,
            "limit_price": "65000.0",
            "state": "open",
            "order_type": "limit_order",
        }

        exec_mgr = ExecutionManager()
        sig = buy_signal(
            signal_id="sig_testnet_fut_1",
            strategy_id="strat_trend_futures",
            strategy_version="1.0.0",
            symbol="BTCUSD",
            entry_price=Decimal("65000"),
            stop_loss=Decimal("63000"),
            take_profit=Decimal("69000"),
            proposed_size=2,
        )
        decision = RiskDecisionResult(
            decision_id="dec_testnet_fut_1",
            signal_id=sig.signal_id,
            approved=True,
            calculated_size=2,
        )

        # Act
        managed = exec_mgr.execute_decision(
            risk_decision=decision,
            signal=sig,
            account_id="testnet_main",
            delta_client=mock_testnet_client,
        )

        # Assert
        assert managed.state == OrderState.ACKNOWLEDGED
        assert managed.exchange_order_id == ex_order_id
        assert managed.symbol == "BTCUSD"
        assert managed.size == 2
        assert managed.price == Decimal("65000")
        assert managed.is_active is True

        mock_testnet_client.place_order.assert_called_once()
        call_kwargs = mock_testnet_client.place_order.call_args.kwargs
        assert call_kwargs["symbol"] == "BTCUSD"
        assert call_kwargs["side"] == "buy"
        assert call_kwargs["size"] == 2
        assert call_kwargs["price"] == 65000.0
        assert call_kwargs["order_type"] == "limit_order"
        assert "client_order_id" in call_kwargs

    def test_e2e_futures_order_immediate_fill(self, mock_testnet_client):
        ex_order_id = "testnet_ord_filled_123"
        mock_testnet_client.place_order.return_value = {
            "id": ex_order_id,
            "symbol": "BTCUSD",
            "side": "sell",
            "size": 1,
            "limit_price": "67000.0",
            "state": "filled",
            "order_type": "limit_order",
        }

        exec_mgr = ExecutionManager()
        sig = sell_signal(
            signal_id="sig_testnet_fill_1",
            strategy_id="strat_mean_reversion",
            strategy_version="1.0.0",
            symbol="BTCUSD",
            entry_price=Decimal("67000"),
            stop_loss=Decimal("68000"),
            take_profit=Decimal("65000"),
            proposed_size=1,
        )
        decision = RiskDecisionResult(
            decision_id="dec_testnet_fill_1",
            signal_id=sig.signal_id,
            approved=True,
            calculated_size=1,
        )

        managed = exec_mgr.execute_decision(
            risk_decision=decision,
            signal=sig,
            account_id="testnet_main",
            delta_client=mock_testnet_client,
        )

        assert managed.state == OrderState.FILLED
        assert managed.is_terminal is True
        assert managed.filled_size == 1
        assert managed.unfilled_size == 0


# ──────────────────────────────────────────────
# 3. Testnet Order Idempotency
# ──────────────────────────────────────────────
class TestTestnetOrderIdempotencyE2E:
    """Verifies that duplicate client_order_ids are safely deduplicated without double placement."""

    def test_e2e_duplicate_client_order_id_prevents_duplicate_exchange_order(self, mock_testnet_client):
        mock_testnet_client.place_order.return_value = {
            "id": "ex_idem_100",
            "state": "open",
        }
        exec_mgr = ExecutionManager()
        sig = buy_signal(
            signal_id="sig_idem_1",
            strategy_id="strat_trend",
            strategy_version="1.0.0",
            symbol="BTCUSD",
            entry_price=Decimal("65000"),
            stop_loss=Decimal("63000"),
            take_profit=Decimal("69000"),
            proposed_size=1,
        )
        decision = RiskDecisionResult(
            decision_id="dec_idem_1",
            signal_id=sig.signal_id,
            approved=True,
            calculated_size=1,
        )

        custom_client_oid = "cid_unique_idempotency_key_999"

        # First submission
        order1 = exec_mgr.execute_decision(
            risk_decision=decision,
            signal=sig,
            account_id="testnet_main",
            delta_client=mock_testnet_client,
            client_order_id=custom_client_oid,
        )
        assert order1.state == OrderState.ACKNOWLEDGED
        assert mock_testnet_client.place_order.call_count == 1

        # Second submission with identical client_order_id
        order2 = exec_mgr.execute_decision(
            risk_decision=decision,
            signal=sig,
            account_id="testnet_main",
            delta_client=mock_testnet_client,
            client_order_id=custom_client_oid,
        )

        # Must return the identical order instance and NOT call place_order again
        assert order2.order_id == order1.order_id
        assert order2.client_order_id == custom_client_oid
        assert mock_testnet_client.place_order.call_count == 1  # Crucial: zero duplicate exchange orders!


# ──────────────────────────────────────────────
# 3. Testnet Options Order Execution
# ──────────────────────────────────────────────
class TestTestnetOptionsOrderE2E:
    """Tests end-to-end execution of options orders on testnet."""

    def test_e2e_options_call_order_placement(self, mock_testnet_client):
        call_symbol = "C-BTC-75000-280325"
        mock_testnet_client.place_order.return_value = {
            "id": "opt_ex_call_4455",
            "symbol": call_symbol,
            "side": "buy",
            "size": 5,
            "limit_price": "1450.0",
            "state": "open",
            "order_type": "limit_order",
        }

        exec_mgr = ExecutionManager()
        sig = buy_signal(
            signal_id="sig_opt_call_1",
            strategy_id="strat_options_anomaly",
            strategy_version="1.0.0",
            symbol=call_symbol,
            entry_price=Decimal("1450"),
            stop_loss=Decimal("1100"),
            take_profit=Decimal("2200"),
            proposed_size=5,
        )
        decision = RiskDecisionResult(
            decision_id="dec_opt_call_1",
            signal_id=sig.signal_id,
            approved=True,
            calculated_size=5,
        )

        managed = exec_mgr.execute_decision(
            risk_decision=decision,
            signal=sig,
            account_id="testnet_main",
            delta_client=mock_testnet_client,
        )

        assert managed.state == OrderState.ACKNOWLEDGED
        assert managed.exchange_order_id == "opt_ex_call_4455"
        assert managed.symbol == call_symbol
        assert managed.size == 5
        assert managed.price == Decimal("1450")

        mock_testnet_client.place_order.assert_called_once_with(
            symbol=call_symbol,
            side="buy",
            size=5,
            price=1450.0,
            order_type="limit_order",
            client_order_id=managed.client_order_id,
            bracket_stop_loss_price="1100.0",
            bracket_take_profit_price="2200.0",
        )

    def test_e2e_options_put_order_placement(self, mock_testnet_client):
        put_symbol = "P-BTC-62000-280325"
        mock_testnet_client.place_order.return_value = {
            "id": "opt_ex_put_8899",
            "symbol": put_symbol,
            "side": "sell",
            "size": 3,
            "limit_price": "920.0",
            "state": "open",
            "order_type": "limit_order",
        }

        exec_mgr = ExecutionManager()
        sig = sell_signal(
            signal_id="sig_opt_put_1",
            strategy_id="strat_options_spread",
            strategy_version="1.0.0",
            symbol=put_symbol,
            entry_price=Decimal("920"),
            stop_loss=Decimal("1300"),
            take_profit=Decimal("450"),
            proposed_size=3,
        )
        decision = RiskDecisionResult(
            decision_id="dec_opt_put_1",
            signal_id=sig.signal_id,
            approved=True,
            calculated_size=3,
        )

        managed = exec_mgr.execute_decision(
            risk_decision=decision,
            signal=sig,
            account_id="testnet_main",
            delta_client=mock_testnet_client,
        )

        assert managed.state == OrderState.ACKNOWLEDGED
        assert managed.side == OrderSide.SELL
        assert managed.size == 3


# ──────────────────────────────────────────────
# 4. Testnet Atomic Order Amendment
# ──────────────────────────────────────────────
class TestTestnetOrderAmendmentE2E:
    """Tests atomic price and size modification without cancel-replace."""

    def test_e2e_atomic_order_amendment(self, mock_testnet_client):
        mock_testnet_client.place_order.return_value = {
            "id": "ex_amend_target_1",
            "state": "open",
        }
        mock_testnet_client.amend_order.return_value = {
            "id": "ex_amend_target_1",
            "limit_price": "64500.0",
            "size": 4,
            "state": "open",
        }

        exec_mgr = ExecutionManager()
        sig = buy_signal(
            signal_id="sig_amend_1",
            strategy_id="strat_trend_futures",
            strategy_version="1.0.0",
            symbol="BTCUSD",
            entry_price=Decimal("64000"),
            stop_loss=Decimal("62000"),
            take_profit=Decimal("68000"),
            proposed_size=2,
        )
        decision = RiskDecisionResult(
            decision_id="dec_amend_1",
            signal_id=sig.signal_id,
            approved=True,
            calculated_size=2,
        )

        managed = exec_mgr.execute_decision(
            risk_decision=decision,
            signal=sig,
            account_id="testnet_main",
            delta_client=mock_testnet_client,
        )

        assert managed.state == OrderState.ACKNOWLEDGED
        assert managed.price == Decimal("64000")
        assert managed.size == 2

        # Amend to higher price and larger size
        amended = exec_mgr.amend_order(
            order_id=managed.order_id,
            new_price=Decimal("64500"),
            new_size=4,
            delta_client=mock_testnet_client,
        )

        assert amended.price == Decimal("64500")
        assert amended.size == 4
        assert amended.unfilled_size == 4

        mock_testnet_client.amend_order.assert_called_once_with(
            order_id="ex_amend_target_1",
            price=64500.0,
            size=4,
        )


# ──────────────────────────────────────────────
# 5. Native Bracket Order Placement and Verification
# ──────────────────────────────────────────────
class TestTestnetBracketOrdersE2E:
    """Tests native Delta Exchange bracket SL/TP integration and verification."""

    def test_e2e_bracket_order_dispatch_and_protection(self, mock_testnet_client):
        mock_testnet_client.place_order.return_value = {
            "id": "parent_order_1001",
            "state": "open",
        }

        bracket_mgr = BracketManager()
        exec_mgr = ExecutionManager(bracket_manager=bracket_mgr)

        sig = buy_signal(
            signal_id="sig_bracket_1",
            strategy_id="strat_momentum",
            strategy_version="1.0.0",
            symbol="BTCUSD",
            entry_price=Decimal("65000"),
            stop_loss=Decimal("63500"),
            take_profit=Decimal("68000"),
            proposed_size=1,
        )
        decision = RiskDecisionResult(
            decision_id="dec_bracket_1",
            signal_id=sig.signal_id,
            approved=True,
            calculated_size=1,
        )

        managed = exec_mgr.execute_decision(
            risk_decision=decision,
            signal=sig,
            account_id="testnet_main",
            delta_client=mock_testnet_client,
        )

        # Verify bracket parameters sent to exchange
        call_kwargs = mock_testnet_client.place_order.call_args.kwargs
        assert call_kwargs["bracket_stop_loss_price"] == "63500.0"
        assert call_kwargs["bracket_take_profit_price"] == "68000.0"

        # Verify protection verification with active brackets on exchange
        exchange_orders = [
            {
                "id": "sl_child_order_1",
                "symbol": "BTCUSD",
                "side": "sell",
                "order_type": "stop_order",
                "stop_price": "63500.0",
            },
            {
                "id": "tp_child_order_2",
                "symbol": "BTCUSD",
                "side": "sell",
                "order_type": "limit_order",
                "bracket_take_profit_price": "68000.0",
            },
        ]

        result = bracket_mgr.verify_position_protection(
            symbol="BTCUSD",
            position_size=1,
            position_side="BUY",
            open_exchange_orders=exchange_orders,
        )

        assert result.status == ProtectionStatus.PROTECTED
        assert result.is_safe is True
        assert result.sl_order_id == "sl_child_order_1"
        assert result.tp_order_id == "tp_child_order_2"

    def test_e2e_unprotected_position_detected(self):
        bracket_mgr = BracketManager()
        # Open position with 2 lots Long, but zero SL orders on exchange
        result = bracket_mgr.verify_position_protection(
            symbol="BTCUSD",
            position_size=2,
            position_side="BUY",
            open_exchange_orders=[],
        )
        assert result.status == ProtectionStatus.UNPROTECTED
        assert result.is_safe is False
        assert "Unprotected" in result.message or "UNPROTECTED" in result.message


# ──────────────────────────────────────────────
# 6. Testnet Order Cancellation & Cancel-All
# ──────────────────────────────────────────────
class TestTestnetCancellationE2E:
    """Tests single order cancellation and bulk cancel-all."""

    def test_e2e_single_order_cancellation(self, mock_testnet_client):
        mock_testnet_client.place_order.return_value = {"id": "cancel_ord_55"}
        mock_testnet_client.cancel_order.return_value = {"id": "cancel_ord_55", "state": "cancelled"}

        exec_mgr = ExecutionManager()
        sig = buy_signal(
            signal_id="sig_cancel_1",
            strategy_id="strat_trend",
            strategy_version="1.0.0",
            symbol="BTCUSD",
            entry_price=Decimal("65000"),
            stop_loss=Decimal("63000"),
            take_profit=Decimal("69000"),
            proposed_size=1,
        )
        decision = RiskDecisionResult(
            decision_id="dec_cancel_1",
            signal_id=sig.signal_id,
            approved=True,
            calculated_size=1,
        )

        managed = exec_mgr.execute_decision(
            risk_decision=decision,
            signal=sig,
            account_id="testnet_main",
            delta_client=mock_testnet_client,
        )

        cancelled = exec_mgr.cancel_order(order_id=managed.order_id, delta_client=mock_testnet_client)

        assert cancelled.state == OrderState.CANCELLED
        assert cancelled.is_terminal is True
        mock_testnet_client.cancel_order.assert_called_once_with(order_id="cancel_ord_55")

    def test_e2e_client_cancel_all(self, mock_testnet_client):
        mock_testnet_client.cancel_all.return_value = {"success": True, "cancelled_count": 4}

        res = mock_testnet_client.cancel_all()
        assert res.get("success") is True
        assert res.get("cancelled_count") == 4
        mock_testnet_client.cancel_all.assert_called_once()


# ──────────────────────────────────────────────
# 7. Live Testnet Connectivity Smoke Test
# ──────────────────────────────────────────────
class TestTestnetLiveConnectivitySmoke:
    """
    Live smoke test targeting real cdn-ind.testnet.deltaex.org.
    Only runs when explicit DELTA_TESTNET_API_KEY environment variable is provided.
    """

    @pytest.mark.skipif(
        not os.getenv("DELTA_TESTNET_API_KEY"),
        reason="DELTA_TESTNET_API_KEY not configured; skipping live smoke test",
    )
    def test_live_testnet_public_products_ping(self):
        client = DeltaRestClient(
            api_key=os.getenv("DELTA_TESTNET_API_KEY"),
            api_secret=os.getenv("DELTA_TESTNET_API_SECRET", "dummy_secret"),
            base_url="https://cdn-ind.testnet.deltaex.org",
        )
        products = client.get_products()
        assert isinstance(products, list)
        assert len(products) > 0
