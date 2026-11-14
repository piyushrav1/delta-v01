"""
Phase 3 Tests: Persistent Storage & Recovery

Tests for:
- storage/database.py: Engine creation, WAL mode, session management
- storage/models.py: All declarative models, relationships, constraints
- storage/recovery.py: State rehydration, reconciliation, P&L tracking
"""
import os
import sys
import pytest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, date, timedelta
from decimal import Decimal
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestDatabaseEngine:
    """Test database engine creation and configuration."""

    def test_get_database_url_defaults_to_sqlite(self):
        from storage.database import get_database_url
        with patch.dict(os.environ, {}, clear=True):
            url = get_database_url()
            assert url.startswith("sqlite:///")
            assert "delta_terminal.sqlite" in url

    def test_get_database_url_from_env(self):
        from storage.database import get_database_url
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://user:pass@localhost/db"}, clear=True):
            url = get_database_url()
            assert url == "postgresql://user:pass@localhost/db"

    def test_create_sqlite_engine(self):
        from storage.database import create_database_engine, close_database
        # Use in-memory SQLite for testing
        engine = create_database_engine("sqlite:///:memory:")
        assert engine is not None
        assert engine.dialect.name == "sqlite"
        close_database()

    def test_create_session_factory(self):
        from storage.database import create_database_engine, create_session_factory, close_database
        engine = create_database_engine("sqlite:///:memory:")
        factory = create_session_factory(engine)
        assert factory is not None
        session = factory()
        assert session is not None
        session.close()
        close_database()

    def test_session_scope_commits_on_success(self):
        from storage.database import create_database_engine, session_scope, close_database
        from storage.models import Base, Account, AccountEnvironment
        engine = create_database_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)

        with session_scope() as session:
            account = Account(
                account_id="test",
                label="Test Account",
                environment=AccountEnvironment.TESTNET,
                api_key="key",
                api_secret="secret",
            )
            session.add(account)

        # Verify committed
        with session_scope() as session:
            found = session.query(Account).filter(Account.account_id == "test").first()
            assert found is not None
            assert found.label == "Test Account"

        close_database()

    def test_session_scope_rollbacks_on_exception(self):
        from storage.database import create_database_engine, session_scope, close_database
        from storage.models import Base, Account, AccountEnvironment
        engine = create_database_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)

        try:
            with session_scope() as session:
                account = Account(
                    account_id="test",
                    label="Test Account",
                    environment=AccountEnvironment.TESTNET,
                    api_key="key",
                    api_secret="secret",
                )
                session.add(account)
                raise ValueError("Force rollback")
        except ValueError:
            pass

        # Verify rolled back
        with session_scope() as session:
            found = session.query(Account).filter(Account.account_id == "test").first()
            assert found is None

        close_database()

    def test_check_database_connection(self):
        from storage.database import create_database_engine, check_database_connection, close_database
        engine = create_database_engine("sqlite:///:memory:")
        assert check_database_connection() is True
        close_database()

    def test_get_database_info(self):
        from storage.database import create_database_engine, get_database_info, close_database
        from storage.models import Base
        engine = create_database_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        info = get_database_info()
        assert info["connected"] is True
        assert info["driver"] == "sqlite"
        assert len(info["tables"]) > 0
        close_database()


class TestModels:
    """Test SQLAlchemy declarative models."""

    @pytest.fixture(autouse=True)
    def setup_db(self):
        from storage.database import create_database_engine, close_database
        from storage.models import Base
        self.engine = create_database_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        yield
        close_database()

    def test_account_model(self):
        from storage.database import session_scope
        from storage.models import Account, AccountEnvironment
        with session_scope() as session:
            account = Account(
                account_id="main",
                label="Main Account",
                environment=AccountEnvironment.TESTNET,
                api_key="test_key",
                api_secret="test_secret",
            )
            session.add(account)
            session.flush()
            assert account.account_id == "main"
            assert account.environment == AccountEnvironment.TESTNET
            assert account.is_active is True
            assert account.created_at is not None

    def test_account_unique_id(self):
        from storage.database import session_scope
        from storage.models import Account, AccountEnvironment
        from sqlalchemy.exc import IntegrityError
        with session_scope() as session:
            account1 = Account(
                account_id="main", label="Main", environment=AccountEnvironment.TESTNET,
                api_key="key1", api_secret="secret1",
            )
            session.add(account1)
            session.flush()
            # Unique primary key should succeed (first insert)
            assert account1.account_id == "main"
            # Second insert with same PK should fail
            account2 = Account(
                account_id="main", label="Main 2", environment=AccountEnvironment.PAPER,
                api_key="key2", api_secret="secret2",
            )
            session.add(account2)
            with pytest.raises(IntegrityError):
                session.flush()
            session.rollback()
            # Rollback to clean state
            session.rollback()

    def test_instrument_model(self):
        from storage.database import session_scope
        from storage.models import Instrument, ContractType
        with session_scope() as session:
            inst = Instrument(
                symbol="BTCUSD",
                product_id=1,
                contract_type=ContractType.FUTURES,
                underlying="BTC",
                contract_value=Decimal("1"),
                tick_size=Decimal("0.5"),
                lot_size=1,
            )
            session.add(inst)
            session.flush()
            assert inst.symbol == "BTCUSD"
            assert inst.contract_type == ContractType.FUTURES
            assert inst.contract_value == Decimal("1")

    def test_strategy_model(self):
        from storage.database import session_scope
        from storage.models import Strategy
        with session_scope() as session:
            strat = Strategy(
                strategy_id="trend_following_btc",
                name="Trend Following BTC",
                version="1.0.0",
                is_enabled=True,
                parameters={"ema_fast": 20, "ema_slow": 50},
            )
            session.add(strat)
            session.flush()
            assert strat.strategy_id == "trend_following_btc"
            assert strat.parameters == {"ema_fast": 20, "ema_slow": 50}

    def test_signal_model(self):
        from storage.database import session_scope
        from storage.models import Signal, Strategy, Instrument, ContractType, SignalDirection, OrderType
        with session_scope() as session:
            strat = Strategy(strategy_id="test", name="Test", version="1.0")
            inst = Instrument(
                symbol="BTCUSD", product_id=1, contract_type=ContractType.FUTURES,
                underlying="BTC", contract_value=Decimal("1"), tick_size=Decimal("0.5"), lot_size=1,
            )
            session.add_all([strat, inst])
            session.flush()

            signal = Signal(
                signal_id="sig_123",
                strategy_id="test",
                strategy_version="1.0",
                symbol="BTCUSD",
                direction=SignalDirection.BUY,
                order_type=OrderType.LIMIT_ORDER,
                target_entry_price=Decimal("50000"),
                stop_loss_price=Decimal("49000"),
                take_profit_price=Decimal("51000"),
                proposed_size=1,
                max_slippage_pct=Decimal("0.005"),
                expected_holding_period_sec=3600,
                confidence=Decimal("0.85"),
                expiry_time=datetime.utcnow() + timedelta(hours=1),
            )
            session.add(signal)
            session.flush()
            assert signal.signal_id == "sig_123"
            assert signal.direction == SignalDirection.BUY

    def test_risk_decision_model(self):
        from storage.database import session_scope
        from storage.models import RiskDecision, Signal, Strategy, Instrument, ContractType, SignalDirection, OrderType
        with session_scope() as session:
            strat = Strategy(strategy_id="test", name="Test", version="1.0")
            inst = Instrument(
                symbol="BTCUSD", product_id=1, contract_type=ContractType.FUTURES,
                underlying="BTC", contract_value=Decimal("1"), tick_size=Decimal("0.5"), lot_size=1,
            )
            signal = Signal(
                signal_id="sig_123", strategy_id="test", strategy_version="1.0", symbol="BTCUSD",
                direction=SignalDirection.BUY, order_type=OrderType.LIMIT_ORDER,
                target_entry_price=Decimal("50000"), stop_loss_price=Decimal("49000"),
                take_profit_price=Decimal("51000"), proposed_size=1,
                max_slippage_pct=Decimal("0.005"), expected_holding_period_sec=3600,
                confidence=Decimal("0.85"), expiry_time=datetime.utcnow() + timedelta(hours=1),
            )
            session.add_all([strat, inst, signal])
            session.flush()

            decision = RiskDecision(
                decision_id="dec_123",
                signal_id="sig_123",
                approved=True,
                calculated_size=1,
                allocated_risk_usd=Decimal("100"),
                margin_required_usd=Decimal("500"),
            )
            session.add(decision)
            session.flush()
            assert decision.decision_id == "dec_123"
            assert decision.approved is True

    def test_order_model(self):
        from storage.database import session_scope
        from storage.models import Order, Account, Instrument, ContractType, AccountEnvironment, OrderSide, OrderType, OrderState, TimeInForce
        with session_scope() as session:
            acct = Account(account_id="main", label="Main", environment=AccountEnvironment.TESTNET,
                          api_key="key", api_secret="secret")
            inst = Instrument(symbol="BTCUSD", product_id=1, contract_type=ContractType.FUTURES,
                             underlying="BTC", contract_value=Decimal("1"), tick_size=Decimal("0.5"), lot_size=1)
            session.add_all([acct, inst])
            session.flush()

            order = Order(
                order_id="ord_123",
                client_order_id="client_123",
                account_id="main",
                symbol="BTCUSD",
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT_ORDER,
                price=Decimal("50000"),
                size=1,
                unfilled_size=1,
                time_in_force=TimeInForce.GTC,
                state=OrderState.CREATED,
            )
            session.add(order)
            session.flush()
            assert order.order_id == "ord_123"
            assert order.state == OrderState.CREATED
            assert order.client_order_id == "client_123"

    def test_order_positive_size_constraint(self):
        from storage.database import session_scope
        from storage.models import Order, Account, Instrument, ContractType, AccountEnvironment, OrderSide, OrderType, TimeInForce
        from sqlalchemy.exc import IntegrityError
        # Use fresh engine session to avoid pending rollover
        from storage.database import create_database_engine
        from storage.models import Base
        # Table already created from previous test, just use new session
        with session_scope() as session:
            acct = Account(account_id="test2", label="Test2", environment=AccountEnvironment.TESTNET,
                          api_key="key", api_secret="secret")
            inst = Instrument(symbol="ETHUSD", product_id=2, contract_type=ContractType.FUTURES,
                             underlying="ETH", contract_value=Decimal("1"), tick_size=Decimal("0.1"), lot_size=1)
            session.add_all([acct, inst])
            session.flush()

            order = Order(
                order_id="ord_123", client_order_id="client_123", account_id="test2", symbol="ETHUSD",
                side=OrderSide.BUY, order_type=OrderType.LIMIT_ORDER, price=Decimal("3000"),
                size=0, unfilled_size=0, time_in_force=TimeInForce.GTC,
            )
            session.add(order)
            with pytest.raises(IntegrityError):
                session.flush()
            session.rollback()
            # Rollback to clean state
            session.rollback()

    def test_fill_model(self):
        from storage.database import session_scope
        from storage.models import Fill, Order, Account, Instrument, ContractType, AccountEnvironment, OrderSide, OrderType, TimeInForce
        with session_scope() as session:
            acct = Account(account_id="main", label="Main", environment=AccountEnvironment.TESTNET,
                          api_key="key", api_secret="secret")
            inst = Instrument(symbol="BTCUSD", product_id=1, contract_type=ContractType.FUTURES,
                             underlying="BTC", contract_value=Decimal("1"), tick_size=Decimal("0.5"), lot_size=1)
            order = Order(
                order_id="ord_123", client_order_id="client_123", account_id="main", symbol="BTCUSD",
                side=OrderSide.BUY, order_type=OrderType.LIMIT_ORDER, price=Decimal("50000"),
                size=1, unfilled_size=0, time_in_force=TimeInForce.GTC,
            )
            session.add_all([acct, inst, order])
            session.flush()

            fill = Fill(
                fill_id="fill_123",
                order_id="ord_123",
                fill_price=Decimal("50000"),
                fill_size=1,
                fee_paid=Decimal("2.5"),
                fee_asset="USDT",
                executed_at=datetime.utcnow(),
            )
            session.add(fill)
            session.flush()
            assert fill.fill_id == "fill_123"
            assert fill.fill_price == Decimal("50000")

    def test_position_model(self):
        from storage.database import session_scope
        from storage.models import Position, Account, Instrument, ContractType, AccountEnvironment, OrderSide
        with session_scope() as session:
            acct = Account(account_id="main", label="Main", environment=AccountEnvironment.TESTNET,
                          api_key="key", api_secret="secret")
            inst = Instrument(symbol="BTCUSD", product_id=1, contract_type=ContractType.FUTURES,
                             underlying="BTC", contract_value=Decimal("1"), tick_size=Decimal("0.5"), lot_size=1)
            session.add_all([acct, inst])
            session.flush()

            pos = Position(
                position_id="pos_123",
                account_id="main",
                symbol="BTCUSD",
                side=OrderSide.BUY,
                size=1,
                entry_price=Decimal("50000"),
                mark_price=Decimal("51000"),
                unrealized_pnl=Decimal("1000"),
                realized_pnl=Decimal("0"),
            )
            session.add(pos)
            session.flush()
            assert pos.position_id == "pos_123"
            assert pos.unrealized_pnl == Decimal("1000")

    def test_position_unique_account_symbol(self):
        from storage.database import create_database_engine, session_scope, close_database
        from storage.models import Base, Position, Account, Instrument, ContractType, AccountEnvironment, OrderSide
        from sqlalchemy.exc import IntegrityError
        # Use fresh database engine
        engine = create_database_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        with session_scope() as session:
            acct = Account(account_id="main", label="Main", environment=AccountEnvironment.TESTNET,
                          api_key="key", api_secret="secret")
            inst = Instrument(symbol="BTCUSD", product_id=1, contract_type=ContractType.FUTURES,
                             underlying="BTC", contract_value=Decimal("1"), tick_size=Decimal("0.5"), lot_size=1)
            session.add_all([acct, inst])
            session.flush()

            pos1 = Position(position_id="pos_1", account_id="main", symbol="BTCUSD",
                           side=OrderSide.BUY, size=1, entry_price=Decimal("50000"), mark_price=Decimal("51000"))
            pos2 = Position(position_id="pos_2", account_id="main", symbol="BTCUSD",
                           side=OrderSide.SELL, size=1, entry_price=Decimal("50000"), mark_price=Decimal("49000"))
            session.add(pos1)
            session.add(pos2)
            with pytest.raises(IntegrityError):
                session.flush()
            session.rollback()
        close_database()

    def test_pnl_snapshot_model(self):
        from storage.database import session_scope
        from storage.models import PnLSnapshot, Account, AccountEnvironment
        with session_scope() as session:
            acct = Account(account_id="main", label="Main", environment=AccountEnvironment.TESTNET,
                          api_key="key", api_secret="secret")
            session.add(acct)
            session.flush()

            snap = PnLSnapshot(
                account_id="main",
                snapshot_date=date.today(),
                start_equity=Decimal("10000"),
                realized_pnl=Decimal("100"),
                unrealized_pnl=Decimal("50"),
                peak_drawdown=Decimal("0"),
                peak_equity=Decimal("10150"),
            )
            session.add(snap)
            session.flush()
            assert snap.start_equity == Decimal("10000")
            assert snap.realized_pnl == Decimal("100")

    def test_circuit_breaker_model(self):
        from storage.database import session_scope
        from storage.models import CircuitBreakerEvent
        with session_scope() as session:
            cb = CircuitBreakerEvent(
                event_id="cb_123",
                breaker_type="daily_loss",
                trigger_value=Decimal("500"),
                threshold_value=Decimal("500"),
                description="Daily loss limit reached",
                is_active=True,
            )
            session.add(cb)
            session.flush()
            assert cb.event_id == "cb_123"
            assert cb.is_active is True

    def test_order_event_model(self):
        from storage.database import session_scope
        from storage.models import OrderEvent, Order, Account, Instrument, ContractType, AccountEnvironment, OrderSide, OrderType, OrderState, TimeInForce
        with session_scope() as session:
            acct = Account(account_id="main", label="Main", environment=AccountEnvironment.TESTNET,
                          api_key="key", api_secret="secret")
            inst = Instrument(symbol="BTCUSD", product_id=1, contract_type=ContractType.FUTURES,
                             underlying="BTC", contract_value=Decimal("1"), tick_size=Decimal("0.5"), lot_size=1)
            order = Order(
                order_id="ord_123", client_order_id="client_123", account_id="main", symbol="BTCUSD",
                side=OrderSide.BUY, order_type=OrderType.LIMIT_ORDER, price=Decimal("50000"),
                size=1, unfilled_size=1, time_in_force=TimeInForce.GTC, state=OrderState.CREATED,
            )
            session.add_all([acct, inst, order])
            session.flush()

            event = OrderEvent(
                event_id="evt_123",
                order_id="ord_123",
                from_state=OrderState.CREATED,
                to_state=OrderState.VALIDATED,
                triggered_by="system",
                message="Order validated",
            )
            session.add(event)
            session.flush()
            assert event.event_id == "evt_123"
            assert event.from_state == OrderState.CREATED
            assert event.to_state == OrderState.VALIDATED


class TestRecovery:
    """Test state recovery and rehydration."""

    @pytest.fixture(autouse=True)
    def setup_db(self):
        from storage.database import create_database_engine, close_database
        from storage.models import Base
        self.engine = create_database_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        yield
        close_database()

    def test_rehydrate_active_positions(self):
        from storage.database import session_scope
        from storage.models import Account, Instrument, Position, Order, ContractType, AccountEnvironment, OrderSide, OrderType, TimeInForce, OrderState
        from storage.recovery import StateRehydrator
        with session_scope() as session:
            acct = Account(account_id="main", label="Main", environment=AccountEnvironment.TESTNET,
                          api_key="key", api_secret="secret")
            inst1 = Instrument(symbol="BTCUSD", product_id=1, contract_type=ContractType.FUTURES,
                              underlying="BTC", contract_value=Decimal("1"), tick_size=Decimal("0.5"), lot_size=1)
            inst2 = Instrument(symbol="ETHUSD", product_id=2, contract_type=ContractType.FUTURES,
                              underlying="ETH", contract_value=Decimal("1"), tick_size=Decimal("0.1"), lot_size=1)
            pos = Position(position_id="pos_1", account_id="main", symbol="BTCUSD",
                          side=OrderSide.BUY, size=1, entry_price=Decimal("50000"), mark_price=Decimal("51000"))
            order = Order(order_id="ord_1", client_order_id="client_1", account_id="main", symbol="ETHUSD",
                         side=OrderSide.BUY, order_type=OrderType.LIMIT_ORDER, price=Decimal("3000"),
                         size=1, unfilled_size=1, time_in_force=TimeInForce.GTC, state=OrderState.ACKNOWLEDGED,
                         bracket_stop_loss_price=Decimal("2900"), bracket_take_profit_price=Decimal("3100"))
            session.add_all([acct, inst1, inst2, pos, order])
            session.flush()

        active = StateRehydrator.rehydrate_active_positions()
        assert "BTCUSD" in active
        assert "ETHUSD" in active
        assert len(active) == 2

    def test_rehydrate_daily_pnl(self):
        from storage.database import session_scope
        from storage.models import Account, PnLSnapshot, AccountEnvironment
        from storage.recovery import StateRehydrator
        with session_scope() as session:
            acct = Account(account_id="main", label="Main", environment=AccountEnvironment.TESTNET,
                          api_key="key", api_secret="secret")
            snap = PnLSnapshot(
                account_id="main", snapshot_date=date.today(),
                start_equity=Decimal("10000"), realized_pnl=Decimal("100"),
                unrealized_pnl=Decimal("50"), peak_drawdown=Decimal("0"), peak_equity=Decimal("10150"),
            )
            session.add_all([acct, snap])
            session.flush()

        pnl_data = StateRehydrator.rehydrate_daily_pnl()
        assert "main" in pnl_data
        assert pnl_data["main"] == 150.0  # 100 + 50

    def test_rehydrate_order_states(self):
        from storage.database import session_scope
        from storage.models import Account, Instrument, Order, ContractType, AccountEnvironment, OrderSide, OrderType, TimeInForce, OrderState
        from storage.recovery import StateRehydrator
        with session_scope() as session:
            acct = Account(account_id="main", label="Main", environment=AccountEnvironment.TESTNET,
                          api_key="key", api_secret="secret")
            inst = Instrument(symbol="BTCUSD", product_id=1, contract_type=ContractType.FUTURES,
                             underlying="BTC", contract_value=Decimal("1"), tick_size=Decimal("0.5"), lot_size=1)
            order1 = Order(order_id="ord_1", client_order_id="c1", account_id="main", symbol="BTCUSD",
                          side=OrderSide.BUY, order_type=OrderType.LIMIT_ORDER, price=Decimal("50000"),
                          size=1, unfilled_size=1, time_in_force=TimeInForce.GTC, state=OrderState.ACKNOWLEDGED)
            order2 = Order(order_id="ord_2", client_order_id="c2", account_id="main", symbol="BTCUSD",
                          side=OrderSide.SELL, order_type=OrderType.LIMIT_ORDER, price=Decimal("51000"),
                          size=1, unfilled_size=1, time_in_force=TimeInForce.GTC, state=OrderState.PARTIALLY_FILLED)
            session.add_all([acct, inst, order1, order2])
            session.flush()

        states = StateRehydrator.rehydrate_order_states()
        assert len(states) == 2
        assert states["ord_1"] == OrderState.ACKNOWLEDGED
        assert states["ord_2"] == OrderState.PARTIALLY_FILLED

    def test_initialize_pnl_for_account(self):
        from storage.recovery import initialize_pnl_for_account
        from storage.database import session_scope
        from storage.models import PnLSnapshot
        snap = initialize_pnl_for_account("test_acct", Decimal("5000"))
        assert snap.account_id == "test_acct"
        assert snap.start_equity == Decimal("5000")
        assert snap.realized_pnl == Decimal("0")
        assert snap.unrealized_pnl == Decimal("0")

        # Call again - should return existing
        snap2 = initialize_pnl_for_account("test_acct", Decimal("6000"))
        assert snap2.id == snap.id

    def test_update_pnl_snapshot(self):
        from storage.recovery import initialize_pnl_for_account, update_pnl_snapshot
        from storage.database import session_scope
        from storage.models import PnLSnapshot

        initialize_pnl_for_account("test_acct", Decimal("5000"))

        # Update with realized P&L
        snap = update_pnl_snapshot("test_acct", realized_delta=Decimal("100"))
        assert snap.realized_pnl == Decimal("100")

        # Update with unrealized P&L
        snap = update_pnl_snapshot("test_acct", unrealized_delta=Decimal("50"))
        assert snap.unrealized_pnl == Decimal("50")

        # Update with equity for drawdown
        snap = update_pnl_snapshot("test_acct", current_equity=Decimal("5150"))
        assert snap.peak_equity == Decimal("5150")
        assert snap.peak_drawdown == Decimal("0")

        # Drop equity - should track drawdown
        snap = update_pnl_snapshot("test_acct", current_equity=Decimal("5000"))
        assert snap.peak_drawdown == Decimal("150")

    def test_check_daily_loss_limit(self):
        from storage.recovery import initialize_pnl_for_account, check_daily_loss_limit
        from decimal import Decimal

        # No snapshot - should not breach
        breached, loss, remaining = check_daily_loss_limit("nonexistent", Decimal("500"))
        assert breached is False
        assert loss == Decimal("0")
        assert remaining == Decimal("500")

        # Within limit
        initialize_pnl_for_account("test", Decimal("10000"))
        from storage.recovery import update_pnl_snapshot
        update_pnl_snapshot("test", realized_delta=Decimal("-100"), unrealized_delta=Decimal("-50"))
        breached, loss, remaining = check_daily_loss_limit("test", Decimal("500"))
        assert breached is False
        assert loss == Decimal("150")
        assert remaining == Decimal("350")

        # Breach limit
        update_pnl_snapshot("test", realized_delta=Decimal("-400"))
        breached, loss, remaining = check_daily_loss_limit("test", Decimal("500"))
        assert breached is True
        assert loss == Decimal("550")
        assert remaining == Decimal("0")

    def test_record_circuit_breaker(self):
        from storage.recovery import record_circuit_breaker, resolve_circuit_breaker
        from storage.database import session_scope
        from storage.models import CircuitBreakerEvent
        from decimal import Decimal

        event = record_circuit_breaker(
            breaker_type="daily_loss",
            trigger_value=Decimal("550"),
            threshold_value=Decimal("500"),
            description="Daily loss limit exceeded",
        )
        assert event.event_id is not None
        assert event.is_active is True
        assert event.breaker_type == "daily_loss"

        # Resolve it
        resolved = resolve_circuit_breaker(event.event_id, "admin")
        assert resolved is True

        # Verify resolved
        with session_scope() as session:
            cb = session.query(CircuitBreakerEvent).filter(CircuitBreakerEvent.event_id == event.event_id).first()
            assert cb.is_active is False
            assert cb.resolved_by == "admin"

    def test_record_duplicate_circuit_breaker(self):
        from storage.recovery import record_circuit_breaker
        from decimal import Decimal

        event1 = record_circuit_breaker("daily_loss", Decimal("550"), Decimal("500"), "Test 1")
        event2 = record_circuit_breaker("daily_loss", Decimal("600"), Decimal("500"), "Test 2")
        assert event1.event_id != event2.event_id


class TestModelRelationships:
    """Test model relationships and cascading."""

    @pytest.fixture(autouse=True)
    def setup_db(self):
        from storage.database import create_database_engine, close_database
        from storage.models import Base
        self.engine = create_database_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        yield
        close_database()

    def test_account_positions_relationship(self):
        from storage.database import session_scope
        from storage.models import Account, Instrument, Position, ContractType, AccountEnvironment, OrderSide
        with session_scope() as session:
            acct = Account(account_id="main", label="Main", environment=AccountEnvironment.TESTNET,
                          api_key="key", api_secret="secret")
            inst = Instrument(symbol="BTCUSD", product_id=1, contract_type=ContractType.FUTURES,
                             underlying="BTC", contract_value=Decimal("1"), tick_size=Decimal("0.5"), lot_size=1)
            pos = Position(position_id="pos_1", account_id="main", symbol="BTCUSD",
                          side=OrderSide.BUY, size=1, entry_price=Decimal("50000"), mark_price=Decimal("51000"))
            session.add_all([acct, inst, pos])
            session.flush()

            # Test relationship
            assert pos in acct.positions
            assert acct == pos.account

    def test_order_fills_relationship(self):
        from storage.database import session_scope
        from storage.models import Account, Instrument, Order, Fill, ContractType, AccountEnvironment, OrderSide, OrderType, TimeInForce, OrderState
        with session_scope() as session:
            acct = Account(account_id="main", label="Main", environment=AccountEnvironment.TESTNET,
                          api_key="key", api_secret="secret")
            inst = Instrument(symbol="BTCUSD", product_id=1, contract_type=ContractType.FUTURES,
                             underlying="BTC", contract_value=Decimal("1"), tick_size=Decimal("0.5"), lot_size=1)
            order = Order(order_id="ord_1", client_order_id="c1", account_id="main", symbol="BTCUSD",
                         side=OrderSide.BUY, order_type=OrderType.LIMIT_ORDER, price=Decimal("50000"),
                         size=1, unfilled_size=0, time_in_force=TimeInForce.GTC, state=OrderState.FILLED)
            fill = Fill(fill_id="fill_1", order_id="ord_1", fill_price=Decimal("50000"),
                       fill_size=1, fee_paid=Decimal("2.5"), fee_asset="USDT", executed_at=datetime.utcnow())
            session.add_all([acct, inst, order, fill])
            session.flush()

            assert fill in order.fills
            assert order == fill.order

    def test_signal_risk_decision_relationship(self):
        from storage.database import session_scope
        from storage.models import Strategy, Signal, RiskDecision, Instrument, ContractType, SignalDirection, OrderType
        with session_scope() as session:
            strat = Strategy(strategy_id="test", name="Test", version="1.0")
            inst = Instrument(symbol="BTCUSD", product_id=1, contract_type=ContractType.FUTURES,
                             underlying="BTC", contract_value=Decimal("1"), tick_size=Decimal("0.5"), lot_size=1)
            signal = Signal(signal_id="sig_1", strategy_id="test", strategy_version="1.0", symbol="BTCUSD",
                           direction=SignalDirection.BUY, order_type=OrderType.LIMIT_ORDER,
                           target_entry_price=Decimal("50000"), stop_loss_price=Decimal("49000"),
                           take_profit_price=Decimal("51000"), proposed_size=1,
                           max_slippage_pct=Decimal("0.005"), expected_holding_period_sec=3600,
                           confidence=Decimal("0.85"), expiry_time=datetime.utcnow() + timedelta(hours=1))
            decision = RiskDecision(decision_id="dec_1", signal_id="sig_1", approved=True)
            session.add_all([strat, inst, signal, decision])
            session.flush()

            assert decision in signal.risk_decisions
            assert signal == decision.signal


if __name__ == "__main__":
    pytest.main([__file__, "-v"])