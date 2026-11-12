"""
SQLAlchemy Declarative Models

Database schema for Delta India Terminal.
Compatible with SQLite (WAL) and PostgreSQL.

Tables:
- accounts: Trading account configurations
- instruments: Product specifications from Delta
- signals: Strategy-generated trade signals
- risk_decisions: Central risk gateway decisions
- orders: Order lifecycle tracking
- fills: Execution fills from exchange
- positions: Net positions with P&L
- pnl_snapshots: Daily P&L tracking
- circuit_breaker_events: Emergency halt events
"""
import enum
from datetime import datetime, date
from typing import Optional, List
from decimal import Decimal
from sqlalchemy import (
    Column, Integer, String, DateTime, Date, Boolean, ForeignKey,
    Enum as SQLEnum, Numeric, Text, Index, UniqueConstraint, CheckConstraint,
    event, DDL
)
from sqlalchemy.orm import relationship, declared_attr
from sqlalchemy.dialects.sqlite import JSON as SQLiteJSON
from sqlalchemy.dialects.postgresql import JSONB as PGJSONB
from sqlalchemy.types import TypeDecorator, Text as SQLText

from storage.database import Base


class JSON(TypeDecorator):
    """Cross-dialect JSON type: JSONB for PostgreSQL, JSON for SQLite."""
    impl = SQLText
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PGJSONB())
        return dialect.type_descriptor(SQLiteJSON())


class OrderState(str, enum.Enum):
    """Order lifecycle states."""
    CREATED = "CREATED"
    VALIDATED = "VALIDATED"
    RISK_APPROVED = "RISK_APPROVED"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class OrderSide(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, enum.Enum):
    LIMIT_ORDER = "limit_order"
    MARKET_ORDER = "market_order"


class SignalDirection(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class TimeInForce(str, enum.Enum):
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"


class AccountEnvironment(str, enum.Enum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    TESTNET = "TESTNET"
    LIVE_MANUAL = "LIVE_MANUAL"
    LIVE_AUTO = "LIVE_AUTO"


class ContractType(str, enum.Enum):
    FUTURES = "futures"
    CALL_OPTIONS = "call_options"
    PUT_OPTIONS = "put_options"
    PERPETUAL = "perpetual"


class Account(Base):
    """Trading account configuration."""
    __tablename__ = "accounts"

    account_id = Column(String(64), primary_key=True)
    label = Column(String(128), nullable=False)
    environment = Column(SQLEnum(AccountEnvironment), nullable=False)
    api_key = Column(String(256), nullable=False)
    api_secret = Column(String(256), nullable=False)  # Encrypted in production
    base_url = Column(String(256), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationships
    positions = relationship("Position", back_populates="account", lazy="dynamic")
    orders = relationship("Order", back_populates="account", lazy="dynamic")
    pnl_snapshots = relationship("PnLSnapshot", back_populates="account", lazy="dynamic")

    def __repr__(self):
        return f"<Account(id={self.account_id}, label={self.label}, env={self.environment.value})>"


class Instrument(Base):
    """Product/Instrument specification from Delta Exchange."""
    __tablename__ = "instruments"

    symbol = Column(String(64), primary_key=True)
    product_id = Column(Integer, nullable=False, unique=True)
    contract_type = Column(SQLEnum(ContractType), nullable=False)
    underlying = Column(String(32), nullable=False)
    contract_value = Column(Numeric(18, 8), nullable=False)  # Contract multiplier
    tick_size = Column(Numeric(18, 8), nullable=False)
    lot_size = Column(Integer, nullable=False, default=1)
    strike_price = Column(Numeric(18, 8), nullable=True)
    expiry_date = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Raw product data from API
    raw_data = Column(JSON, nullable=True)

    # Relationships
    orders = relationship("Order", back_populates="instrument", lazy="dynamic")
    positions = relationship("Position", back_populates="instrument", lazy="dynamic")

    __table_args__ = (
        Index("ix_instruments_underlying", "underlying"),
        Index("ix_instruments_contract_type", "contract_type"),
        Index("ix_instruments_expiry", "expiry_date"),
    )

    def __repr__(self):
        return f"<Instrument(symbol={self.symbol}, type={self.contract_type.value}, underlying={self.underlying})>"


class Strategy(Base):
    """Registered trading strategy."""
    __tablename__ = "strategies"

    strategy_id = Column(String(64), primary_key=True)
    name = Column(String(128), nullable=False)
    version = Column(String(32), nullable=False, default="1.0.0")
    is_enabled = Column(Boolean, default=True, nullable=False)
    parameters = Column(JSON, nullable=True)  # Strategy-specific config
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationships
    signals = relationship("Signal", back_populates="strategy", lazy="dynamic")

    def __repr__(self):
        return f"<Strategy(id={self.strategy_id}, name={self.name}, enabled={self.is_enabled})>"


class Signal(Base):
    """Trade signal emitted by a strategy."""
    __tablename__ = "signals"

    signal_id = Column(String(64), primary_key=True)
    strategy_id = Column(String(64), ForeignKey("strategies.strategy_id"), nullable=False)
    strategy_version = Column(String(32), nullable=False)
    symbol = Column(String(64), ForeignKey("instruments.symbol"), nullable=False)
    direction = Column(SQLEnum(SignalDirection), nullable=False)
    order_type = Column(SQLEnum(OrderType), nullable=False)
    target_entry_price = Column(Numeric(18, 8), nullable=False)
    stop_loss_price = Column(Numeric(18, 8), nullable=False)
    take_profit_price = Column(Numeric(18, 8), nullable=False)
    proposed_size = Column(Integer, nullable=True)
    max_slippage_pct = Column(Numeric(6, 4), nullable=False, default=0.005)
    expected_holding_period_sec = Column(Integer, nullable=False, default=3600)
    confidence = Column(Numeric(4, 3), nullable=False)  # 0.000 to 1.000
    rationale = Column(JSON, nullable=True)  # Supporting indicator values
    legs = Column(JSON, nullable=True)  # Multi-leg option definition
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)
    expiry_time = Column(DateTime, nullable=False)

    # Relationships
    strategy = relationship("Strategy", back_populates="signals")
    instrument = relationship("Instrument")
    risk_decisions = relationship("RiskDecision", back_populates="signal", lazy="dynamic")

    __table_args__ = (
        Index("ix_signals_strategy_time", "strategy_id", "timestamp"),
        Index("ix_signals_symbol_time", "symbol", "timestamp"),
        Index("ix_signals_expiry", "expiry_time"),
    )

    def __repr__(self):
        return f"<Signal(id={self.signal_id}, {self.direction.value} {self.symbol} @ {self.target_entry_price})>"


class RiskDecision(Base):
    """Central risk gateway decision for a signal."""
    __tablename__ = "risk_decisions"

    decision_id = Column(String(64), primary_key=True)
    signal_id = Column(String(64), ForeignKey("signals.signal_id"), nullable=False)
    approved = Column(Boolean, nullable=False)
    rejection_reason = Column(Text, nullable=True)
    calculated_size = Column(Integer, nullable=True)
    allocated_risk_usd = Column(Numeric(18, 2), nullable=True)
    margin_required_usd = Column(Numeric(18, 2), nullable=True)
    checks_performed = Column(JSON, nullable=True)  # Detailed check results
    evaluated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    signal = relationship("Signal", back_populates="risk_decisions")
    orders = relationship("Order", back_populates="risk_decision", lazy="dynamic")

    __table_args__ = (
        Index("ix_risk_decisions_signal", "signal_id"),
        Index("ix_risk_decisions_approved", "approved"),
        Index("ix_risk_decisions_time", "evaluated_at"),
    )

    def __repr__(self):
        status = "APPROVED" if self.approved else "REJECTED"
        return f"<RiskDecision(id={self.decision_id}, signal={self.signal_id}, {status})>"


class Order(Base):
    """Order lifecycle tracking."""
    __tablename__ = "orders"

    order_id = Column(String(64), primary_key=True)
    client_order_id = Column(String(64), unique=True, nullable=False, index=True)
    account_id = Column(String(64), ForeignKey("accounts.account_id"), nullable=False)
    symbol = Column(String(64), ForeignKey("instruments.symbol"), nullable=False)
    decision_id = Column(String(64), ForeignKey("risk_decisions.decision_id"), nullable=True)

    side = Column(SQLEnum(OrderSide), nullable=False)
    order_type = Column(SQLEnum(OrderType), nullable=False)
    price = Column(Numeric(18, 8), nullable=True)  # Limit price
    size = Column(Integer, nullable=False)
    unfilled_size = Column(Integer, nullable=False, default=0)
    time_in_force = Column(SQLEnum(TimeInForce), nullable=False, default=TimeInForce.GTC)
    reduce_only = Column(Boolean, default=False, nullable=False)

    # Bracket orders
    bracket_stop_loss_price = Column(Numeric(18, 8), nullable=True)
    bracket_take_profit_price = Column(Numeric(18, 8), nullable=True)
    bracket_order_id = Column(String(64), nullable=True)  # Parent bracket order ID

    # State machine
    state = Column(SQLEnum(OrderState), default=OrderState.CREATED, nullable=False)
    exchange_order_id = Column(String(64), nullable=True, index=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    submitted_at = Column(DateTime, nullable=True)
    acknowledged_at = Column(DateTime, nullable=True)
    filled_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)

    # Error tracking
    error_message = Column(Text, nullable=True)
    retry_count = Column(Integer, default=0, nullable=False)

    # Raw exchange response
    raw_data = Column(JSON, nullable=True)

    # Relationships
    account = relationship("Account", back_populates="orders")
    instrument = relationship("Instrument", back_populates="orders")
    risk_decision = relationship("RiskDecision", back_populates="orders")
    fills = relationship("Fill", back_populates="order", lazy="dynamic")
    events = relationship("OrderEvent", back_populates="order", lazy="dynamic")

    __table_args__ = (
        Index("ix_orders_account_state", "account_id", "state"),
        Index("ix_orders_symbol_state", "symbol", "state"),
        Index("ix_orders_exchange_id", "exchange_order_id"),
        Index("ix_orders_client_id", "client_order_id"),
        CheckConstraint("size > 0", name="ck_order_positive_size"),
        CheckConstraint("unfilled_size >= 0", name="ck_order_nonnegative_unfilled"),
    )

    def __repr__(self):
        return f"<Order(id={self.order_id}, {self.side.value} {self.size} {self.symbol} @ {self.price} [{self.state.value}])>"


class Fill(Base):
    """Execution fill from exchange."""
    __tablename__ = "fills"

    fill_id = Column(String(64), primary_key=True)
    order_id = Column(String(64), ForeignKey("orders.order_id"), nullable=False)
    fill_price = Column(Numeric(18, 8), nullable=False)
    fill_size = Column(Integer, nullable=False)
    fee_paid = Column(Numeric(18, 8), nullable=False, default=0)
    fee_asset = Column(String(16), nullable=False, default="USDT")
    executed_at = Column(DateTime, nullable=False)

    # Raw fill data from exchange
    raw_data = Column(JSON, nullable=True)

    # Relationships
    order = relationship("Order", back_populates="fills")

    __table_args__ = (
        Index("ix_fills_order", "order_id"),
        Index("ix_fills_executed", "executed_at"),
        CheckConstraint("fill_size > 0", name="ck_fill_positive_size"),
        CheckConstraint("fill_price > 0", name="ck_fill_positive_price"),
    )

    def __repr__(self):
        return f"<Fill(id={self.fill_id}, order={self.order_id}, {self.fill_size} @ {self.fill_price})>"


class Position(Base):
    """Net position with mark-to-market P&L."""
    __tablename__ = "positions"

    position_id = Column(String(64), primary_key=True)
    account_id = Column(String(64), ForeignKey("accounts.account_id"), nullable=False)
    symbol = Column(String(64), ForeignKey("instruments.symbol"), nullable=False)

    side = Column(SQLEnum(OrderSide), nullable=False)
    size = Column(Integer, nullable=False)
    entry_price = Column(Numeric(18, 8), nullable=False)
    mark_price = Column(Numeric(18, 8), nullable=False)
    unrealized_pnl = Column(Numeric(18, 2), nullable=False, default=0)
    realized_pnl = Column(Numeric(18, 2), nullable=False, default=0)

    # Bracket order references
    entry_order_id = Column(String(64), nullable=True)
    stop_loss_order_id = Column(String(64), nullable=True)
    take_profit_order_id = Column(String(64), nullable=True)

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    closed_at = Column(DateTime, nullable=True)

    # Relationships
    account = relationship("Account", back_populates="positions")
    instrument = relationship("Instrument", back_populates="positions")

    __table_args__ = (
        Index("ix_positions_account", "account_id"),
        Index("ix_positions_symbol", "symbol"),
        Index("ix_positions_account_symbol", "account_id", "symbol", unique=True),
        CheckConstraint("size != 0", name="ck_position_nonzero_size"),
    )

    def __repr__(self):
        return f"<Position({self.side.value} {self.size} {self.symbol} entry={self.entry_price} mark={self.mark_price} uPnL={self.unrealized_pnl})>"


class PnLSnapshot(Base):
    """Daily P&L snapshot for drawdown tracking."""
    __tablename__ = "pnl_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(String(64), ForeignKey("accounts.account_id"), nullable=False)
    snapshot_date = Column(Date, nullable=False)

    start_equity = Column(Numeric(18, 2), nullable=False)
    realized_pnl = Column(Numeric(18, 2), nullable=False, default=0)
    unrealized_pnl = Column(Numeric(18, 2), nullable=False, default=0)
    peak_drawdown = Column(Numeric(18, 2), nullable=False, default=0)
    peak_equity = Column(Numeric(18, 2), nullable=False, default=0)

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationships
    account = relationship("Account", back_populates="pnl_snapshots")

    __table_args__ = (
        Index("ix_pnl_snapshots_account_date", "account_id", "snapshot_date", unique=True),
        Index("ix_pnl_snapshots_date", "snapshot_date"),
    )

    def __repr__(self):
        total_pnl = float(self.realized_pnl) + float(self.unrealized_pnl)
        return f"<PnLSnapshot(account={self.account_id}, date={self.snapshot_date}, total_pnl={total_pnl})>"


class CircuitBreakerEvent(Base):
    """Emergency circuit breaker activation events."""
    __tablename__ = "circuit_breaker_events"

    event_id = Column(String(64), primary_key=True)
    breaker_type = Column(String(64), nullable=False)  # daily_loss, exchange_timeout, order_rejections
    trigger_value = Column(Numeric(18, 8), nullable=False)  # Value that triggered the breaker
    threshold_value = Column(Numeric(18, 8), nullable=False)  # Configured threshold
    description = Column(Text, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    triggered_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    resolved_at = Column(DateTime, nullable=True)
    resolved_by = Column(String(64), nullable=True)  # User/system that resolved

    # Context data
    context_data = Column(JSON, nullable=True)

    __table_args__ = (
        Index("ix_circuit_breaker_type", "breaker_type"),
        Index("ix_circuit_breaker_active", "is_active"),
        Index("ix_circuit_breaker_time", "triggered_at"),
    )

    def __repr__(self):
        status = "ACTIVE" if self.is_active else "RESOLVED"
        return f"<CircuitBreakerEvent(id={self.event_id}, type={self.breaker_type}, {status})>"


class OrderEvent(Base):
    """Audit trail for order state transitions."""
    __tablename__ = "order_events"

    event_id = Column(String(64), primary_key=True)
    order_id = Column(String(64), ForeignKey("orders.order_id"), nullable=False)
    from_state = Column(SQLEnum(OrderState), nullable=True)
    to_state = Column(SQLEnum(OrderState), nullable=False)
    triggered_by = Column(String(64), nullable=True)  # user, system, exchange, risk_engine
    message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    raw_data = Column(JSON, nullable=True)

    # Relationships
    order = relationship("Order", back_populates="events")

    __table_args__ = (
        Index("ix_order_events_order", "order_id"),
        Index("ix_order_events_time", "created_at"),
    )

    def __repr__(self):
        return f"<OrderEvent(order={self.order_id}, {self.from_state} -> {self.to_state})>"


# Event listeners for automatic timestamp updates
@event.listens_for(Account, "before_update")
@event.listens_for(Instrument, "before_update")
@event.listens_for(Strategy, "before_update")
def update_updated_at(mapper, connection, target):
    target.updated_at = datetime.utcnow()


# Helper functions for common queries
def get_active_accounts(session) -> List[Account]:
    """Get all active accounts."""
    return session.query(Account).filter(Account.is_active == True).all()


def get_account_by_id(session, account_id: str) -> Optional[Account]:
    """Get account by ID."""
    return session.query(Account).filter(Account.account_id == account_id).first()


def get_open_orders(session, account_id: Optional[str] = None) -> List[Order]:
    """Get open/pending orders."""
    states = [OrderState.CREATED, OrderState.VALIDATED, OrderState.RISK_APPROVED,
              OrderState.SUBMITTED, OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED]
    query = session.query(Order).filter(Order.state.in_(states))
    if account_id:
        query = query.filter(Order.account_id == account_id)
    return query.all()


def get_open_positions(session, account_id: Optional[str] = None) -> List[Position]:
    """Get open positions (non-zero size)."""
    query = session.query(Position).filter(Position.size != 0)
    if account_id:
        query = query.filter(Position.account_id == account_id)
    return query.all()


def get_latest_pnl_snapshot(session, account_id: str, snapshot_date: Optional[date] = None) -> Optional[PnLSnapshot]:
    """Get latest P&L snapshot for account."""
    query = session.query(PnLSnapshot).filter(PnLSnapshot.account_id == account_id)
    if snapshot_date:
        query = query.filter(PnLSnapshot.snapshot_date == snapshot_date)
    return query.order_by(PnLSnapshot.snapshot_date.desc()).first()


def get_active_circuit_breakers(session) -> List[CircuitBreakerEvent]:
    """Get all active circuit breakers."""
    return session.query(CircuitBreakerEvent).filter(CircuitBreakerEvent.is_active == True).all()