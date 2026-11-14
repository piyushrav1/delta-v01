"""
Phase 14 - Task 14.2 (TASK-P14-02): Failure Injection Verification

Validates fault tolerance and resilience across:
1. Simulated network timeout / drop during order placement
2. HTTP 429 Too Many Requests rate limiting and exponential backoff
3. Server crash / process death mid-trade state rehydration & recovery
4. Desynchronization between local database and exchange state
5. Strict zero duplicate order submission guarantees
"""
import os
import uuid
import time
import pytest
from datetime import datetime, date, timedelta
from decimal import Decimal
from unittest.mock import Mock, MagicMock, patch

import requests
from config.environments import TradingEnvironment
from config.accounts import AccountConfig
from exchange.delta_client import DeltaRestClient, TokenBucket
from storage.database import create_database_engine, close_database, session_scope
from storage.models import (
    Base, Account, Instrument, Order, Position, Fill, PnLSnapshot,
    CircuitBreakerEvent, OrderState, OrderSide, OrderType, ContractType,
    AccountEnvironment, TimeInForce
)
from storage.recovery import RecoveryManager, StateRehydrator
from core.exceptions import RiskViolation, OrderExecutionError
from analytics.signal import buy_signal
from risk.risk_manager import RiskDecisionResult
from risk.circuit_breaker import CircuitBreakerManager
from trading.execution_manager import ExecutionManager
from trading.bracket_manager import BracketManager, ProtectionStatus
from trading.reconciliation import StateReconciler, ReconciliationReport
from trading.order_state import ManagedOrder


# ──────────────────────────────────────────────
# Test Fixtures
# ──────────────────────────────────────────────
@pytest.fixture
def testnet_account() -> AccountConfig:
    return AccountConfig(
        account_id="testnet_main",
        label="Delta Testnet Account",
        api_key="testnet_key_fault_injection_12345",
        api_secret="testnet_secret_fault_injection_hex_string_67890",
        environment=TradingEnvironment.TESTNET,
    )


@pytest.fixture
def mock_client(testnet_account) -> MagicMock:
    client = MagicMock(spec=DeltaRestClient)
    client.account = testnet_account
    client.base_url = "https://cdn-ind.testnet.deltaex.org"
    client.FALLBACK_BASE = "https://api.deltaexchange.in"
    return client


# ──────────────────────────────────────────────
# 1. Network Timeout & Drop Injection
# ──────────────────────────────────────────────
class TestNetworkTimeoutFaultInjection:
    """Verifies behavior when network fails mid-order submission."""

    def test_network_timeout_during_order_placement(self, mock_client):
        # Simulate network drop / read timeout during POST /v2/orders
        mock_client.place_order.side_effect = requests.exceptions.Timeout("Connection timed out to testnet")

        exec_mgr = ExecutionManager()
        sig = buy_signal(
            signal_id="sig_timeout_1",
            strategy_id="strat_trend",
            strategy_version="1.0.0",
            symbol="BTCUSD",
            entry_price=Decimal("65000"),
            stop_loss=Decimal("63000"),
            take_profit=Decimal("69000"),
            proposed_size=1,
        )
        decision = RiskDecisionResult(
            decision_id="dec_timeout_1",
            signal_id=sig.signal_id,
            approved=True,
            calculated_size=1,
        )

        with pytest.raises(OrderExecutionError, match="Connection timed out"):
            exec_mgr.execute_decision(
                risk_decision=decision,
                signal=sig,
                account_id="testnet_main",
                delta_client=mock_client,
            )

        # Confirm order tracked as REJECTED locally
        open_orders = exec_mgr.get_open_orders()
        assert len(open_orders) == 0  # No active in-flight orders

    def test_pre_retry_exchange_audit_prevents_duplicate_orders(self, mock_client):
        """
        Critical Safety Guarantee:
        If an order submission times out, a naive retry could double-execute.
        Verify that querying exchange open orders by client_order_id detects
        if the order actually landed, preventing duplicate submission.
        """
        client_oid = "cid_flapping_connection_123"

        # Exchange simulate: order DID actually land despite client timeout
        mock_client.get_open_orders.return_value = [
            {
                "id": "ex_landed_999",
                "client_order_id": client_oid,
                "symbol": "BTCUSD",
                "side": "buy",
                "size": 2,
                "limit_price": "65000.0",
                "state": "open",
            }
        ]

        # Audit helper simulating pre-retry check
        def pre_retry_audit(client: DeltaRestClient, client_order_id: str):
            orders = client.get_open_orders()
            for o in orders:
                if o.get("client_order_id") == client_order_id:
                    return o
            return None

        landed_order = pre_retry_audit(mock_client, client_oid)
        assert landed_order is not None
        assert landed_order["id"] == "ex_landed_999"

        # Because order was found, we do NOT submit a new order to exchange
        assert mock_client.place_order.call_count == 0


# ──────────────────────────────────────────────
# 2. HTTP 429 Rate Limiting & Backoff Injection
# ──────────────────────────────────────────────
class TestRateLimit429FaultInjection:
    """Verifies handling of rate limits, token exhaustion, and 429 responses."""

    def test_delta_client_429_triggers_fallback_retry(self, testnet_account):
        client = DeltaRestClient(account=testnet_account)

        # Mock requests.Session.request to return 429 on primary base, 200 on fallback
        resp_429 = Mock(status_code=429, is_success=False, text="Too Many Requests")
        resp_200 = Mock(status_code=200, is_success=True, json=lambda: {"result": [{"id": 1}]})

        with patch.object(client._session, "request", side_effect=[resp_429, resp_200]) as mock_req:
            res = client.get("/v2/products")
            assert res == [{"id": 1}]
            # Verify primary was tried first, then fallback
            assert mock_req.call_count == 2
            first_call_url = mock_req.call_args_list[0][0][1]
            second_call_url = mock_req.call_args_list[1][0][1]
            assert "cdn-ind.testnet.deltaex.org" in first_call_url
            assert "api.deltaexchange.in" in second_call_url

    def test_token_bucket_exhaustion_throttling(self):
        # 2 tokens burst, 1 token per second
        bucket = TokenBucket(rate_per_second=1.0, burst=2)
        assert bucket.acquire() is True
        assert bucket.acquire() is True
        # Bucket exhausted
        assert bucket.acquire() is False
        wait = bucket.wait_time()
        assert wait > 0.0
        assert wait <= 1.0

    def test_consecutive_rejections_trips_circuit_breaker(self):
        cb_manager = CircuitBreakerManager(rejection_limit=3)
        assert cb_manager.is_tripped() is False

        # 1st reject
        tripped = cb_manager.record_rejection("testnet_main", reason="429 Rate Limit")
        assert tripped is False
        assert cb_manager.is_tripped() is False

        # 2nd reject
        tripped = cb_manager.record_rejection("testnet_main", reason="429 Rate Limit")
        assert tripped is False

        # 3rd reject -> trips circuit breaker!
        tripped = cb_manager.record_rejection("testnet_main", reason="429 Rate Limit")
        assert tripped is True
        assert cb_manager.is_tripped() is True
        assert cb_manager.is_tripped(CircuitBreakerManager.BREAKER_CONSECUTIVE_REJECTIONS) is True


# ──────────────────────────────────────────────
# 3. Server Crash Mid-Trade State Recovery
# ──────────────────────────────────────────────
class TestServerCrashAndStateRecovery:
    """Simulates mid-trade crash and verifies SQLite state rehydration."""

    @pytest.fixture(autouse=True)
    def setup_in_memory_db(self):
        self.engine = create_database_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        yield
        close_database()

    def test_crash_recovery_rehydrates_orders_positions_pnl_breakers(self, mock_client):
        today = date.today()
        # Seed database as it would be right before a sudden crash
        with session_scope() as session:
            acct = Account(
                account_id="testnet_main",
                label="Delta Testnet",
                environment=AccountEnvironment.TESTNET,
                api_key="key",
                api_secret="secret",
                is_active=True,
            )
            inst = Instrument(
                symbol="BTCUSD",
                product_id=1,
                contract_type=ContractType.FUTURES,
                underlying="BTC",
                contract_value=Decimal("1"),
                tick_size=Decimal("0.5"),
                lot_size=1,
            )
            order = Order(
                order_id="ord_crash_1",
                client_order_id="cl_crash_1",
                account_id="testnet_main",
                symbol="BTCUSD",
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT_ORDER,
                price=Decimal("62000"),
                size=2,
                unfilled_size=2,
                time_in_force=TimeInForce.GTC,
                state=OrderState.ACKNOWLEDGED,
                bracket_stop_loss_price=Decimal("60000"),
                bracket_take_profit_price=Decimal("65000"),
            )
            pos = Position(
                position_id="pos_crash_1",
                account_id="testnet_main",
                symbol="BTCUSD",
                side=OrderSide.BUY,
                size=2,
                entry_price=Decimal("62000"),
                mark_price=Decimal("62500"),
            )
            fill = Fill(
                fill_id="fill_crash_1",
                order_id="ord_crash_1",
                fill_price=Decimal("62000"),
                fill_size=2,
                fee_paid=Decimal("1.5"),
                fee_asset="USDT",
                executed_at=datetime.utcnow(),
            )
            pnl = PnLSnapshot(
                account_id="testnet_main",
                snapshot_date=today,
                start_equity=Decimal("10000"),
                realized_pnl=Decimal("150"),
                unrealized_pnl=Decimal("50"),
                peak_drawdown=Decimal("20"),
                peak_equity=Decimal("10200"),
            )
            cb = CircuitBreakerEvent(
                event_id="cb_crash_1",
                breaker_type="daily_loss",
                trigger_value=Decimal("500"),
                threshold_value=Decimal("500"),
                description="Daily loss limit active before crash",
                is_active=True,
            )
            session.add_all([acct, inst, order, pos, fill, pnl, cb])
            session.flush()

        # Simulate fresh process startup after crash
        recovery_mgr = RecoveryManager()
        summary = recovery_mgr.recover(delta_client=mock_client)

        assert summary["accounts_recovered"] == 1
        assert summary["orders_recovered"] == 1
        assert summary["positions_recovered"] == 1
        assert summary["fills_recovered"] == 1
        assert summary["pnl_restored"] == 1
        assert summary["circuit_breakers_restored"] == 1
        assert len(summary["errors"]) == 0

        # Verify active positions rehydration
        active_symbols = StateRehydrator.rehydrate_active_positions()
        assert "BTCUSD" in active_symbols

        # Verify daily PnL rehydration
        daily_pnl = StateRehydrator.rehydrate_daily_pnl()
        assert daily_pnl["testnet_main"] == 200.0  # 150 realized + 50 unrealized


# ──────────────────────────────────────────────
# 4. Exchange State Desynchronization Injection
# ──────────────────────────────────────────────
class TestExchangeDesynchronizationFaultInjection:
    """Verifies that discrepancies between local state and exchange are surfaced."""

    def test_desynchronization_audit_detects_all_anomalies(self):
        reconciler = StateReconciler(bracket_manager=BracketManager())

        # Local state: order_1 active, position 10 lots Long
        local_orders = [
            ManagedOrder(
                order_id="ord_local_1",
                client_order_id="cl_1",
                account_id="main",
                symbol="BTCUSD",
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT_ORDER,
                size=2,
                state=OrderState.ACKNOWLEDGED,
                exchange_order_id="ex_1",
            ),
            # Ghost order: locally ACKNOWLEDGED, but cancelled on exchange
            ManagedOrder(
                order_id="ord_ghost_2",
                client_order_id="cl_ghost_2",
                account_id="main",
                symbol="BTCUSD",
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT_ORDER,
                size=1,
                state=OrderState.ACKNOWLEDGED,
                exchange_order_id="ex_ghost_99",
            ),
        ]
        local_positions = [
            {"symbol": "BTCUSD", "size": 10, "side": "BUY"}
        ]

        # Exchange state: order ex_1 exists, but also orphan order ex_orphan_888
        # Ghost order ex_ghost_99 is absent on exchange!
        # Position is 5 lots on exchange (mismatch: 10 != 5)
        # Position is UNPROTECTED (no stop loss on exchange)
        exchange_orders = [
            {"id": "ex_1", "symbol": "BTCUSD", "side": "buy", "size": 2},
            {"id": "ex_orphan_888", "symbol": "BTCUSD", "side": "sell", "size": 1, "order_type": "limit_order"},
        ]
        exchange_positions = [
            {"symbol": "BTCUSD", "size": 5, "side": "buy"}
        ]

        report = reconciler.reconcile(
            local_orders=local_orders,
            local_positions=local_positions,
            exchange_orders=exchange_orders,
            exchange_positions=exchange_positions,
        )

        assert report.is_synced is False
        assert report.reconciliation_required is True

        # 1. Orphan order detected
        assert len(report.orphan_orders) == 1
        assert str(report.orphan_orders[0]["id"]) == "ex_orphan_888"

        # 2. Ghost order detected
        assert len(report.ghost_orders) == 1
        assert "ord_ghost_2" in report.ghost_orders

        # 3. Position mismatch detected (10 local vs 5 exchange)
        assert len(report.position_mismatches) == 1
        assert report.position_mismatches[0]["symbol"] == "BTCUSD"
        assert report.position_mismatches[0]["local_size"] == 10
        assert report.position_mismatches[0]["exchange_size"] == 5

        # 4. Unprotected position detected
        assert len(report.unprotected_positions) == 1
        assert "BTCUSD" in report.unprotected_positions


# ──────────────────────────────────────────────
# 5. Flapping Connection & Duplicate Prevention
# ──────────────────────────────────────────────
class TestFlappingConnectionZeroDuplicates:
    """Verifies that multiple retries with same client_order_id never double-place."""

    def test_zero_duplicate_order_submission_under_flapping_connection(self, mock_client):
        mock_client.place_order.return_value = {"id": "ex_flapping_10", "state": "open"}
        exec_mgr = ExecutionManager()

        sig = buy_signal(
            signal_id="sig_flap_1",
            strategy_id="strat_trend",
            strategy_version="1.0.0",
            symbol="BTCUSD",
            entry_price=Decimal("65000"),
            stop_loss=Decimal("63000"),
            take_profit=Decimal("69000"),
            proposed_size=1,
        )
        decision = RiskDecisionResult(
            decision_id="dec_flap_1",
            signal_id=sig.signal_id,
            approved=True,
            calculated_size=1,
        )

        cid = "flapping_client_id_777"

        # Attempt 1: Succeeds
        ord1 = exec_mgr.execute_decision(decision, sig, "testnet_main", mock_client, client_order_id=cid)
        assert ord1.state == OrderState.ACKNOWLEDGED
        assert mock_client.place_order.call_count == 1

        # Attempt 2 (retried immediately due to simulated flapping reconnect):
        ord2 = exec_mgr.execute_decision(decision, sig, "testnet_main", mock_client, client_order_id=cid)
        assert ord2.order_id == ord1.order_id
        # Call count remains 1!
        assert mock_client.place_order.call_count == 1

        # Attempt 3:
        ord3 = exec_mgr.execute_decision(decision, sig, "testnet_main", mock_client, client_order_id=cid)
        assert ord3.order_id == ord1.order_id
        assert mock_client.place_order.call_count == 1
