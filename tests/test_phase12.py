"""
Phase 12 Tests: Paper Trading Simulator

Tests:
1. PaperLedger balance management (deposits, withdrawals, ledger tracking)
2. PaperLedger order lifecycle (create, fetch, amend, cancel)
3. PaperLedger fill accounting (fees, weighted avg entry, realized PnL, position flipping)
4. PaperBroker identical interface to DeltaRestClient
5. PaperBroker client_order_id idempotency
6. PaperBroker market order immediate execution & slippage
7. PaperBroker limit order placement, aggressive crosses, and passive resting fills
8. PaperBroker bracket order triggers (Stop-Loss and Take-Profit) for long & short
9. PaperBroker REST method routing (get, post, put, delete)
10. End-to-end integration: ExecutionManager + PaperBroker + ReconciliationAuditor
"""
import pytest
from decimal import Decimal
from typing import Dict, Any

from simulation.paper_ledger import (
    PaperLedger, PaperOrderModel, PaperPositionModel, PaperLedgerModel
)
from simulation.paper_broker import PaperBroker
from trading.execution_manager import ExecutionManager
from trading.bracket_manager import BracketManager
from trading.order_state import ManagedOrder
from trading.reconciliation import StateReconciler, ReconciliationReport
from risk.risk_manager import RiskDecisionResult
from analytics.signal import buy_signal, sell_signal, TradeSignal
from storage.models import OrderState, OrderSide, OrderType


# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────
@pytest.fixture
def memory_ledger():
    """In-memory paper ledger with 10,000 USDT initial capital."""
    return PaperLedger(db_url="sqlite:///:memory:", account_id="test_paper", initial_balance=10000.0)


@pytest.fixture
def paper_broker(memory_ledger):
    """Paper broker instance using memory_ledger."""
    return PaperBroker(
        account_id="test_paper",
        paper_ledger=memory_ledger,
        maker_fee_pct=0.0002,   # 0.02%
        taker_fee_pct=0.0005,   # 0.05%
        slippage_pct=0.0001,    # 0.01%
        contract_values={"BTCUSD": 1.0, "ETHUSD": 1.0},
    )


# ──────────────────────────────────────────────
# 1. PaperLedger Tests
# ──────────────────────────────────────────────
class TestPaperLedger:
    def test_initial_balance_seed(self, memory_ledger):
        balance = memory_ledger.get_cash_balance()
        assert balance == Decimal("10000.0")

        entries = memory_ledger.get_ledger_entries()
        assert len(entries) == 1
        assert entries[0].transaction_type == "INITIAL_DEPOSIT"
        assert entries[0].amount == Decimal("10000.0")

    def test_deposit_and_withdrawal(self, memory_ledger):
        new_bal = memory_ledger.deposit(2500.0, description="Test Deposit")
        assert new_bal == Decimal("12500.0")
        assert memory_ledger.get_cash_balance() == Decimal("12500.0")

        withdrew = memory_ledger.withdraw(1000.0, description="Test Withdrawal")
        assert withdrew == Decimal("11500.0")
        assert memory_ledger.get_cash_balance() == Decimal("11500.0")

    def test_withdrawal_insufficient_funds(self, memory_ledger):
        with pytest.raises(ValueError, match="Insufficient paper balance"):
            memory_ledger.withdraw(20000.0)

    def test_order_creation_and_query(self, memory_ledger):
        order = memory_ledger.create_order(
            symbol="BTCUSD",
            side="buy",
            size=2,
            order_type="limit_order",
            price=60000.0,
            client_order_id="cid_123",
            bracket_stop_loss_price=59000.0,
            bracket_take_profit_price=62000.0,
        )
        assert order.order_id.startswith("pord_")
        assert order.symbol == "BTCUSD"
        assert order.side == "buy"
        assert order.size == 2
        assert order.unfilled_size == 2
        assert order.state == "open"

        # Lookup by order_id
        fetched = memory_ledger.get_order(order.order_id)
        assert fetched is not None
        assert fetched.client_order_id == "cid_123"

        # Lookup by client_order_id
        fetched_cid = memory_ledger.get_order_by_client_id("cid_123")
        assert fetched_cid is not None
        assert fetched_cid.order_id == order.order_id

    def test_order_amendment_and_cancellation(self, memory_ledger):
        order = memory_ledger.create_order(
            symbol="BTCUSD",
            side="buy",
            size=2,
            price=60000.0,
        )
        amended = memory_ledger.amend_order(order.order_id, price=60500.0, size=3)
        assert amended.price == Decimal("60500.0")
        assert amended.size == 3
        assert amended.unfilled_size == 3

        cancelled = memory_ledger.cancel_order(order.order_id)
        assert cancelled.state == "cancelled"

    def test_order_fill_and_position_management(self, memory_ledger):
        order = memory_ledger.create_order(
            symbol="BTCUSD",
            side="buy",
            size=2,
            price=60000.0,
        )
        up_order, pos, realized_pnl = memory_ledger.record_fill(
            order_id=order.order_id,
            fill_price=60000.0,
            fill_size=2,
            fee=24.0,  # 2 * 60,000 * 0.0002 = 24.0
            is_maker=True,
        )
        assert up_order.state == "filled"
        assert up_order.filled_size == 2
        assert up_order.unfilled_size == 0
        assert up_order.avg_fill_price == Decimal("60000.0")

        # Cash balance reduced by fee
        assert memory_ledger.get_cash_balance() == Decimal("10000.0") - Decimal("24.0")

        # Position created
        assert pos is not None
        assert pos.symbol == "BTCUSD"
        assert pos.side == "buy"
        assert pos.size == 2
        assert pos.entry_price == Decimal("60000.0")

        # Scale in: buy another 1 contract @ 63000
        order2 = memory_ledger.create_order(symbol="BTCUSD", side="buy", size=1, price=63000.0)
        _, pos2, _ = memory_ledger.record_fill(order2.order_id, fill_price=63000.0, fill_size=1, fee=12.0)
        assert pos2.size == 3
        # Weighted average: (2*60000 + 1*63000) / 3 = 61000
        assert pos2.entry_price == Decimal("61000.0")

        # Sell 1 contract @ 64000 (Take partial profit)
        sell_order = memory_ledger.create_order(symbol="BTCUSD", side="sell", size=1, price=64000.0)
        _, pos3, pnl = memory_ledger.record_fill(sell_order.order_id, fill_price=64000.0, fill_size=1, fee=10.0)
        # Realized profit: 1 * (64000 - 61000) = 3000
        assert pnl == Decimal("3000.0")
        assert pos3.size == 2
        assert pos3.realized_pnl == Decimal("3000.0")

    def test_position_flipping(self, memory_ledger):
        # Open Long 1 @ 50000
        buy_ord = memory_ledger.create_order(symbol="BTCUSD", side="buy", size=1, price=50000.0)
        memory_ledger.record_fill(buy_ord.order_id, 50000.0, 1)

        # Sell 3 @ 52000 -> Should close 1 Long (+2000 PnL) and open 2 Short @ 52000
        sell_ord = memory_ledger.create_order(symbol="BTCUSD", side="sell", size=3, price=52000.0)
        _, pos, pnl = memory_ledger.record_fill(sell_ord.order_id, 52000.0, 3)

        assert pnl == Decimal("2000.0")
        assert pos.is_open is True
        assert pos.side == "sell"
        assert pos.size == 2
        assert pos.entry_price == Decimal("52000.0")

    def test_mark_to_market_and_summary(self, memory_ledger):
        buy_ord = memory_ledger.create_order(symbol="BTCUSD", side="buy", size=1, price=60000.0)
        memory_ledger.record_fill(buy_ord.order_id, 60000.0, 1)

        # Mark price moves to 65000
        memory_ledger.update_mark_price("BTCUSD", 65000.0)
        pos = memory_ledger.get_position("BTCUSD")
        assert pos.unrealized_pnl == Decimal("5000.0")

        summary = memory_ledger.get_balance_summary()
        assert summary["initial_balance"] == Decimal("10000.0")
        assert summary["unrealized_pnl"] == Decimal("5000.0")
        assert summary["total_equity"] == summary["cash_balance"] + Decimal("5000.0")


# ──────────────────────────────────────────────
# 2. PaperBroker Tests
# ──────────────────────────────────────────────
class TestPaperBroker:
    def test_market_order_immediate_execution_buy(self, paper_broker):
        paper_broker.set_market_quote("BTCUSD", best_bid=60000.0, best_ask=60010.0, mark_price=60005.0)

        # Market Buy fills at ask * (1 + slippage)
        # 60010 * 1.0001 = 60016.001
        res = paper_broker.place_order(
            symbol="BTCUSD",
            side="buy",
            size=1,
            order_type="market_order",
        )
        assert res["state"] == "filled"
        assert res["unfilled_size"] == 0
        fill_p = float(res["average_fill_price"])
        assert fill_p == pytest.approx(60010.0 * 1.0001, rel=1e-4)

        positions = paper_broker.get_positions()
        assert len(positions) == 1
        assert positions[0]["symbol"] == "BTCUSD"
        assert positions[0]["size"] == 1

    def test_market_order_immediate_execution_sell(self, paper_broker):
        paper_broker.set_market_quote("BTCUSD", best_bid=60000.0, best_ask=60010.0, mark_price=60005.0)

        # Market Sell fills at bid * (1 - slippage)
        res = paper_broker.place_order(
            symbol="BTCUSD",
            side="sell",
            size=2,
            order_type="market_order",
        )
        assert res["state"] == "filled"
        fill_p = float(res["average_fill_price"])
        assert fill_p == pytest.approx(60000.0 * 0.9999, rel=1e-4)

        positions = paper_broker.get_positions()
        assert len(positions) == 1
        assert positions[0]["size"] == -2

    def test_limit_order_passive_resting_and_tick_fill(self, paper_broker):
        paper_broker.set_market_quote("BTCUSD", best_bid=60000.0, best_ask=60050.0)

        # Passive Limit Buy at 59900 (does not cross book)
        order = paper_broker.place_order(
            symbol="BTCUSD",
            side="buy",
            size=1,
            price=59900.0,
            order_type="limit_order",
        )
        assert order["state"] == "open"
        assert len(paper_broker.get_open_orders()) == 1

        # Market tick arrives, ask still above 59900
        events = paper_broker.on_market_data("BTCUSD", best_bid=59950.0, best_ask=59920.0)
        assert len(events) == 0
        assert len(paper_broker.get_open_orders()) == 1

        # Market tick drops ask to 59890 (crosses 59900)
        events = paper_broker.on_market_data("BTCUSD", best_bid=59880.0, best_ask=59890.0)
        assert len(events) == 1
        assert events[0]["event"] == "LIMIT_FILL"
        assert events[0]["role"] == "maker"
        assert len(paper_broker.get_open_orders()) == 0

    def test_limit_order_aggressive_cross(self, paper_broker):
        paper_broker.set_market_quote("BTCUSD", best_bid=60000.0, best_ask=60050.0)

        # Limit buy with price >= best_ask (60100 >= 60050) -> Aggressive cross, fills at 60050 as taker
        order = paper_broker.place_order(
            symbol="BTCUSD",
            side="buy",
            size=1,
            price=60100.0,
            order_type="limit_order",
        )
        assert order["state"] == "filled"
        assert float(order["average_fill_price"]) == 60050.0

    def test_client_order_id_idempotency(self, paper_broker):
        paper_broker.set_market_quote("BTCUSD", best_bid=60000.0, best_ask=60050.0)
        cid = "my_custom_client_id_001"

        order1 = paper_broker.place_order(
            symbol="BTCUSD",
            side="buy",
            size=1,
            price=59000.0,
            client_order_id=cid,
        )
        order2 = paper_broker.place_order(
            symbol="BTCUSD",
            side="buy",
            size=1,
            price=59000.0,
            client_order_id=cid,
        )
        assert order1["id"] == order2["id"]
        assert len(paper_broker.get_open_orders()) == 1

    def test_cancel_order_and_cancel_all(self, paper_broker):
        paper_broker.set_market_quote("BTCUSD", best_bid=60000.0, best_ask=60050.0)
        o1 = paper_broker.place_order(symbol="BTCUSD", side="buy", size=1, price=59000.0)
        o2 = paper_broker.place_order(symbol="BTCUSD", side="buy", size=2, price=58000.0)

        assert len(paper_broker.get_open_orders()) == 2

        # Cancel one
        c1 = paper_broker.cancel_order(o1["id"])
        assert c1["state"] == "cancelled"
        assert len(paper_broker.get_open_orders()) == 1

        # Cancel all
        c_all = paper_broker.cancel_all()
        assert c_all["cancelled_count"] == 1
        assert len(paper_broker.get_open_orders()) == 0

    def test_amend_order(self, paper_broker):
        paper_broker.set_market_quote("BTCUSD", best_bid=60000.0, best_ask=60050.0)
        o = paper_broker.place_order(symbol="BTCUSD", side="buy", size=1, price=59000.0)

        amended = paper_broker.amend_order(o["id"], price=59500.0, size=2)
        assert float(amended["price"]) == 59500.0
        assert amended["size"] == 2

    def test_stop_loss_and_take_profit_triggers_long(self, paper_broker):
        paper_broker.set_market_quote("BTCUSD", best_bid=60000.0, best_ask=60010.0, mark_price=60005.0)

        # Enter Long 1 contract with SL @ 58000, TP @ 65000
        paper_broker.place_order(
            symbol="BTCUSD",
            side="buy",
            size=1,
            order_type="market_order",
            bracket_stop_loss_price=58000.0,
            bracket_take_profit_price=65000.0,
        )
        assert len(paper_broker.get_positions()) == 1

        # Price drops to 57900 -> SL triggers!
        events = paper_broker.on_market_data("BTCUSD", best_bid=57890.0, best_ask=57910.0, mark_price=57900.0)
        assert len(events) == 1
        assert events[0]["event"] == "STOP_LOSS_TRIGGERED"
        assert len(paper_broker.get_positions()) == 0

    def test_take_profit_triggers_short(self, paper_broker):
        paper_broker.set_market_quote("BTCUSD", best_bid=60000.0, best_ask=60010.0, mark_price=60005.0)

        # Enter Short 1 contract with SL @ 62000, TP @ 58000
        paper_broker.place_order(
            symbol="BTCUSD",
            side="sell",
            size=1,
            order_type="market_order",
            bracket_stop_loss_price=62000.0,
            bracket_take_profit_price=58000.0,
        )
        assert len(paper_broker.get_positions()) == 1

        # Price drops to 57900 -> TP triggers!
        events = paper_broker.on_market_data("BTCUSD", best_bid=57890.0, best_ask=57910.0, mark_price=57900.0)
        assert len(events) == 1
        assert events[0]["event"] == "TAKE_PROFIT_TRIGGERED"
        assert len(paper_broker.get_positions()) == 0

    def test_rest_api_routing(self, paper_broker):
        paper_broker.set_market_quote("BTCUSD", best_bid=60000.0, best_ask=60050.0)

        # POST /v2/orders
        res = paper_broker.post("/v2/orders", body={"symbol": "BTCUSD", "side": "buy", "size": 1, "price": 59000.0})
        assert res["state"] == "open"
        oid = res["id"]

        # GET /v2/orders
        open_orders = paper_broker.get("/v2/orders")
        assert len(open_orders) == 1

        # GET /v2/wallet/balances
        balances = paper_broker.get("/v2/wallet/balances")
        assert len(balances) == 1
        assert balances[0]["asset_symbol"] == "USDT"

        # PUT /v2/orders (Amend)
        amended = paper_broker.put("/v2/orders", body={"id": oid, "limit_price": "59200.0"})
        assert float(amended["price"]) == 59200.0

        # DELETE /v2/orders (Cancel)
        cancelled = paper_broker.delete("/v2/orders", params={"id": oid})
        assert cancelled["state"] == "cancelled"


# ──────────────────────────────────────────────
# 3. End-to-End Integration Suite
# ──────────────────────────────────────────────
class TestPaperTradingEndToEndIntegration:
    def test_execution_manager_with_paper_broker(self, paper_broker):
        """Verify ExecutionManager operates seamlessly with PaperBroker as delta_client."""
        exec_mgr = ExecutionManager()
        paper_broker.set_market_quote("BTCUSD", best_bid=60000.0, best_ask=60010.0, mark_price=60005.0)

        signal = buy_signal(
            signal_id="sig_paper_e2e_1",
            strategy_id="TrendFollowing",
            strategy_version="1.0.0",
            symbol="BTCUSD",
            entry_price=Decimal("60010.0"),
            stop_loss=Decimal("59000.0"),
            take_profit=Decimal("62000.0"),
            proposed_size=1,
            order_type="market_order",
        )

        decision = RiskDecisionResult(
            decision_id="dec_paper_1",
            signal_id="sig_paper_e2e_1",
            approved=True,
            calculated_size=1,
            allocated_risk_usd=Decimal("1000.0"),
            margin_required_usd=Decimal("600.0"),
        )

        # Execute decision through ExecutionManager with paper_broker
        managed_order = exec_mgr.execute_decision(
            risk_decision=decision,
            signal=signal,
            account_id="test_paper",
            delta_client=paper_broker,
        )

        assert managed_order.state == OrderState.FILLED
        assert managed_order.exchange_order_id.startswith("pord_")
        assert managed_order.filled_size == 1

        # Verify paper position was created
        positions = paper_broker.get_positions()
        assert len(positions) == 1
        assert positions[0]["symbol"] == "BTCUSD"
        assert positions[0]["size"] == 1

        # Run state reconciler
        reconciler = StateReconciler()
        local_positions = [{"symbol": "BTCUSD", "size": 1}]
        exchange_orders = paper_broker.get_open_orders()
        local_orders = [managed_order]
        for eo in exchange_orders:
            local_orders.append(
                ManagedOrder(
                    order_id=f"local_{eo['id']}",
                    client_order_id=eo['client_order_id'],
                    account_id="test_paper",
                    symbol=eo['symbol'],
                    side=OrderSide.SELL if eo['side'] == 'sell' else OrderSide.BUY,
                    order_type=OrderType.LIMIT_ORDER,
                    size=eo['size'],
                    price=Decimal(eo['price']) if eo.get('price') else None,
                    exchange_order_id=eo['id'],
                    state=OrderState.ACKNOWLEDGED,
                )
            )
        report = reconciler.reconcile(
            local_orders=local_orders,
            local_positions=local_positions,
            exchange_orders=exchange_orders,
            exchange_positions=positions,
        )
        # Should be in sync
        assert report.is_synced is True
        assert len(report.orphan_orders) == 0
        assert len(report.ghost_orders) == 0
