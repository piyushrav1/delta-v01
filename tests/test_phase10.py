"""
Phase 10 Tests: Order Execution and Position Management

Tests:
1. Order Lifecycle State Machine (Task 10.2: FSM valid/invalid transitions, fill tracking)
2. Protective Bracket Order Verification (Task 10.3: Native bracket params, SL/TP protection auditing)
3. Authoritative Execution Gateway (Task 10.1: Risk approval gate, client idempotency, amendment, cancellation)
4. Periodic Exchange State Reconciliation (Task 10.4: Orphan/ghost orders, position mismatches, unprotected positions)
"""
import pytest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import Mock

from storage.models import OrderState, OrderSide, OrderType
from core.exceptions import RiskViolation, OrderExecutionError
from analytics.signal import buy_signal, sell_signal
from risk.risk_manager import RiskDecisionResult
from trading.order_state import ManagedOrder, InvalidStateTransitionError, VALID_TRANSITIONS
from trading.bracket_manager import BracketManager, ProtectionStatus
from trading.execution_manager import ExecutionManager
from trading.reconciliation import StateReconciler, ReconciliationReport


# ──────────────────────────────────────────────
# 1. Order Lifecycle State Machine Tests
# ──────────────────────────────────────────────
class TestOrderLifecycleStateMachine:
    def test_happy_path_state_transitions(self):
        order = ManagedOrder(
            order_id="ord_1",
            client_order_id="cl_1",
            account_id="main",
            symbol="BTCUSD",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT_ORDER,
            size=10,
            price=Decimal("60000"),
        )
        assert order.state == OrderState.CREATED

        order.transition_to(OrderState.VALIDATED, "Params valid")
        assert order.state == OrderState.VALIDATED

        order.transition_to(OrderState.RISK_APPROVED, "Risk gate passed")
        assert order.state == OrderState.RISK_APPROVED

        order.transition_to(OrderState.SUBMITTED, "Sent to exchange")
        assert order.state == OrderState.SUBMITTED

        order.transition_to(OrderState.ACKNOWLEDGED, "Exchange acknowledged")
        assert order.state == OrderState.ACKNOWLEDGED
        assert order.is_active is True

        order.record_fill(10, Decimal("60000"))
        assert order.state == OrderState.FILLED
        assert order.unfilled_size == 0
        assert order.is_terminal is True
        assert len(order.history) == 5

    def test_partial_fill_sequence(self):
        order = ManagedOrder(
            order_id="ord_part",
            client_order_id="cl_part",
            account_id="main",
            symbol="BTCUSD",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT_ORDER,
            size=10,
            price=Decimal("60000"),
            state=OrderState.ACKNOWLEDGED,
        )

        # Fill 4 out of 10
        order.record_fill(4, Decimal("60000"))
        assert order.state == OrderState.PARTIALLY_FILLED
        assert order.filled_size == 4
        assert order.unfilled_size == 6

        # Fill remaining 6
        order.record_fill(6, Decimal("60000"))
        assert order.state == OrderState.FILLED
        assert order.filled_size == 10
        assert order.unfilled_size == 0

    def test_invalid_state_transition_rejected(self):
        order = ManagedOrder(
            order_id="ord_bad",
            client_order_id="cl_bad",
            account_id="main",
            symbol="BTCUSD",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT_ORDER,
            size=5,
        )
        # Attempt illegal skip: CREATED -> FILLED
        with pytest.raises(InvalidStateTransitionError):
            order.transition_to(OrderState.FILLED)

    def test_terminal_states_cannot_transition(self):
        order = ManagedOrder(
            order_id="ord_term",
            client_order_id="cl_term",
            account_id="main",
            symbol="BTCUSD",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT_ORDER,
            size=5,
            state=OrderState.CANCELLED,
        )
        with pytest.raises(InvalidStateTransitionError):
            order.transition_to(OrderState.SUBMITTED)


# ──────────────────────────────────────────────
# 2. Bracket Order Manager Tests
# ──────────────────────────────────────────────
class TestBracketManager:
    def test_build_bracket_params(self):
        params = BracketManager.build_bracket_params(
            stop_loss_price=Decimal("59000.5"),
            take_profit_price=Decimal("62000.0"),
        )
        assert params["bracket_stop_loss_price"] == "59000.5"
        assert params["bracket_take_profit_price"] == "62000.0"

    def test_verify_position_protected(self):
        open_orders = [
            {"symbol": "BTCUSD", "side": "sell", "order_type": "stop_order", "id": "sl_123"},
            {"symbol": "BTCUSD", "side": "sell", "bracket_take_profit_price": "62000", "id": "tp_123"},
        ]
        res = BracketManager.verify_position_protection(
            symbol="BTCUSD",
            position_size=2,
            position_side="BUY",
            open_exchange_orders=open_orders,
        )
        assert res.status == ProtectionStatus.PROTECTED
        assert res.is_safe is True
        assert res.sl_order_id == "sl_123"
        assert res.tp_order_id == "tp_123"

    def test_verify_unprotected_position_detected(self):
        # Open orders on other symbol, no stop for BTCUSD
        open_orders = [{"symbol": "ETHUSD", "side": "sell", "order_type": "stop_order"}]
        res = BracketManager.verify_position_protection(
            symbol="BTCUSD",
            position_size=5,
            position_side="BUY",
            open_exchange_orders=open_orders,
        )
        assert res.status == ProtectionStatus.UNPROTECTED
        assert res.is_safe is False
        assert "CRITICAL" in res.message


# ──────────────────────────────────────────────
# 3. Authoritative Execution Gateway Tests
# ──────────────────────────────────────────────
class TestExecutionManager:
    @pytest.fixture
    def setup_execution(self):
        exec_mgr = ExecutionManager()
        sig = buy_signal(
            signal_id="sig_exec_1",
            strategy_id="trend_following",
            strategy_version="1.0.0",
            symbol="BTCUSD",
            entry_price=Decimal("60000"),
            stop_loss=Decimal("59000"),
            take_profit=Decimal("62000"),
            proposed_size=3,
        )
        decision = RiskDecisionResult(
            decision_id="dec_exec_1",
            signal_id="sig_exec_1",
            approved=True,
            calculated_size=3,
            allocated_risk_usd=Decimal("100"),
            margin_required_usd=Decimal("500"),
        )
        return exec_mgr, sig, decision

    def test_reject_unapproved_risk_decision(self, setup_execution):
        exec_mgr, sig, _ = setup_execution
        bad_decision = RiskDecisionResult(
            decision_id="dec_bad",
            signal_id="sig_bad",
            approved=False,
            rejection_reason="Quote stale",
        )
        with pytest.raises(RiskViolation, match="Cannot execute unapproved RiskDecision"):
            exec_mgr.execute_decision(bad_decision, sig, account_id="main")

    def test_execute_approved_decision_standalone(self, setup_execution):
        exec_mgr, sig, decision = setup_execution
        order = exec_mgr.execute_decision(
            risk_decision=decision,
            signal=sig,
            account_id="main",
            delta_client=None,  # Standalone mode
        )

        assert order.state == OrderState.ACKNOWLEDGED
        assert order.size == 3
        assert order.symbol == "BTCUSD"
        assert order.price == Decimal("60000")
        assert order.bracket_stop_loss_price == Decimal("59000")
        assert order.decision_id == "dec_exec_1"
        assert order.exchange_order_id is not None

    def test_client_id_idempotency(self, setup_execution):
        exec_mgr, sig, decision = setup_execution
        cid = "idempotent_key_999"

        order1 = exec_mgr.execute_decision(
            risk_decision=decision, signal=sig, account_id="main",
            delta_client=None, client_order_id=cid
        )
        # Re-submit with same client_order_id
        order2 = exec_mgr.execute_decision(
            risk_decision=decision, signal=sig, account_id="main",
            delta_client=None, client_order_id=cid
        )

        assert order1.order_id == order2.order_id
        assert len(exec_mgr._orders) == 1

    def test_cancel_order(self, setup_execution):
        exec_mgr, sig, decision = setup_execution
        order = exec_mgr.execute_decision(decision, sig, account_id="main")
        cancelled = exec_mgr.cancel_order(order.order_id)
        assert cancelled.state == OrderState.CANCELLED

    def test_amend_order(self, setup_execution):
        exec_mgr, sig, decision = setup_execution
        order = exec_mgr.execute_decision(decision, sig, account_id="main")
        amended = exec_mgr.amend_order(
            order_id=order.order_id,
            new_price=Decimal("60500"),
            new_size=5,
        )
        assert amended.price == Decimal("60500")
        assert amended.size == 5


# ──────────────────────────────────────────────
# 4. Exchange State Reconciliation Tests
# ──────────────────────────────────────────────
class TestStateReconciliation:
    def test_perfect_sync(self):
        reconciler = StateReconciler()
        local_orders = [
            ManagedOrder("o1", "c1", "main", "BTCUSD", OrderSide.BUY, OrderType.LIMIT_ORDER, 1, exchange_order_id="ex_1", state=OrderState.ACKNOWLEDGED),
            ManagedOrder("o_sl", "c_sl", "main", "BTCUSD", OrderSide.SELL, OrderType.LIMIT_ORDER, 1, exchange_order_id="sl_1", state=OrderState.ACKNOWLEDGED),
        ]
        exchange_orders = [
            {"id": "ex_1", "symbol": "BTCUSD", "side": "buy"},
            {"id": "sl_1", "symbol": "BTCUSD", "side": "sell", "order_type": "stop_order"},
        ]
        local_positions = [{"symbol": "BTCUSD", "size": 1}]
        exchange_positions = [{"symbol": "BTCUSD", "size": 1}]

        report = reconciler.reconcile(local_orders, local_positions, exchange_orders, exchange_positions)
        assert report.is_synced is True
        assert report.reconciliation_required is False
        assert len(report.discrepancies) == 0

    def test_detect_orphan_exchange_order(self):
        reconciler = StateReconciler()
        local_orders = []
        exchange_orders = [{"id": "orphan_999", "symbol": "ETHUSD", "side": "buy"}]

        report = reconciler.reconcile(local_orders, [], exchange_orders, [])
        assert report.is_synced is False
        assert len(report.orphan_orders) == 1
        assert report.orphan_orders[0]["id"] == "orphan_999"

    def test_detect_ghost_local_order(self):
        reconciler = StateReconciler()
        # Order marked active locally with exchange_order_id "ex_missing", but exchange returns empty
        local_orders = [
            ManagedOrder("o_ghost", "c_ghost", "main", "BTCUSD", OrderSide.BUY, OrderType.LIMIT_ORDER, 1, exchange_order_id="ex_missing", state=OrderState.ACKNOWLEDGED)
        ]
        exchange_orders = []

        report = reconciler.reconcile(local_orders, [], exchange_orders, [])
        assert report.is_synced is False
        assert len(report.ghost_orders) == 1
        assert report.ghost_orders[0] == "o_ghost"

    def test_detect_position_mismatch_and_unprotected(self):
        reconciler = StateReconciler()
        local_positions = [{"symbol": "BTCUSD", "size": 2}]
        # Exchange has 5 lots, but no stop-loss!
        exchange_positions = [{"symbol": "BTCUSD", "size": 5}]
        exchange_orders = []

        report = reconciler.reconcile([], local_positions, exchange_orders, exchange_positions)
        assert report.is_synced is False
        assert len(report.position_mismatches) == 1
        assert report.position_mismatches[0]["delta"] == 3
        assert "BTCUSD" in report.unprotected_positions
