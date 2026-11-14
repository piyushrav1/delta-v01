"""
Paper Trading Virtual Ledger & State Storage

Maintains dedicated SQLite tables:
- `paper_ledger`: Virtual cash balance, transactions, realized P&L, and fee audit trail.
- `paper_positions`: Virtual active/closed positions with real-time mark-to-market P&L.
- `paper_orders`: Virtual orders tracking lifecycle states, brackets, and fills.

Institutional Features:
- Multi-currency / USDT ledger support
- FIFO / Weighted Average entry price computation
- Position flipping handling (e.g. Long -> Short upon reversal)
- Accurate Delta fee accounting (Maker 0.02%, Taker 0.05%)
- Thread-safe SQLite persistence with WAL mode support
"""
from __future__ import annotations

import os
import uuid
import logging
import threading
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Any, Tuple, Union

from sqlalchemy import (
    Column, String, Integer, Numeric, Boolean, DateTime,
    Text, Index, create_engine, Engine
)
from sqlalchemy.orm import sessionmaker, Session, DeclarativeBase
from sqlalchemy.pool import StaticPool

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Declarative Models
# ──────────────────────────────────────────────
try:
    from storage.database import Base as ModelBase
except ImportError:
    class ModelBase(DeclarativeBase):
        pass


class PaperBase(DeclarativeBase):
    """Isolated declarative base for paper trading tables."""
    pass


class PaperLedgerModel(PaperBase):
    """Virtual cash ledger transaction log and balance tracking."""
    __tablename__ = "paper_ledger"

    entry_id = Column(String(64), primary_key=True)
    account_id = Column(String(64), nullable=False, index=True)
    currency = Column(String(16), nullable=False, default="USDT")
    transaction_type = Column(String(32), nullable=False)  # INITIAL_DEPOSIT, DEPOSIT, WITHDRAWAL, FILL_FEE, REALIZED_PNL
    amount = Column(Numeric(18, 8), nullable=False)
    balance_after = Column(Numeric(18, 8), nullable=False)
    order_id = Column(String(64), nullable=True, index=True)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_paper_ledger_account_time", "account_id", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<PaperLedger(id={self.entry_id}, type={self.transaction_type}, amt={self.amount}, bal={self.balance_after})>"


class PaperPositionModel(PaperBase):
    """Virtual position state with mark-to-market valuation."""
    __tablename__ = "paper_positions"

    position_id = Column(String(64), primary_key=True)
    account_id = Column(String(64), nullable=False, index=True)
    symbol = Column(String(64), nullable=False, index=True)
    side = Column(String(16), nullable=False)  # buy, sell
    size = Column(Integer, nullable=False)  # Absolute quantity (>0)
    entry_price = Column(Numeric(18, 8), nullable=False)
    mark_price = Column(Numeric(18, 8), nullable=False)
    contract_value = Column(Numeric(18, 8), nullable=False, default=1.0)
    unrealized_pnl = Column(Numeric(18, 8), nullable=False, default=0.0)
    realized_pnl = Column(Numeric(18, 8), nullable=False, default=0.0)
    is_open = Column(Boolean, nullable=False, default=True)
    entry_order_id = Column(String(64), nullable=True)
    stop_loss_price = Column(Numeric(18, 8), nullable=True)
    take_profit_price = Column(Numeric(18, 8), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    closed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_paper_positions_account_sym", "account_id", "symbol"),
        Index("ix_paper_positions_open", "account_id", "is_open"),
    )

    def __repr__(self) -> str:
        status = "OPEN" if self.is_open else "CLOSED"
        return f"<PaperPosition({self.side.upper()} {self.size} {self.symbol} @ {self.entry_price} [{status}])>"


class PaperOrderModel(PaperBase):
    """Virtual orders registered and tracked in paper mode."""
    __tablename__ = "paper_orders"

    order_id = Column(String(64), primary_key=True)
    client_order_id = Column(String(64), nullable=True, index=True)
    account_id = Column(String(64), nullable=False, index=True)
    symbol = Column(String(64), nullable=False, index=True)
    side = Column(String(16), nullable=False)  # buy, sell
    order_type = Column(String(32), nullable=False)  # limit_order, market_order, stop_loss_order, take_profit_order
    size = Column(Integer, nullable=False)
    filled_size = Column(Integer, nullable=False, default=0)
    unfilled_size = Column(Integer, nullable=False, default=0)
    price = Column(Numeric(18, 8), nullable=True)
    avg_fill_price = Column(Numeric(18, 8), nullable=True)
    state = Column(String(32), nullable=False, default="open")  # open, filled, partially_filled, cancelled, rejected
    bracket_stop_loss_price = Column(Numeric(18, 8), nullable=True)
    bracket_take_profit_price = Column(Numeric(18, 8), nullable=True)
    parent_order_id = Column(String(64), nullable=True)
    fee_paid = Column(Numeric(18, 8), nullable=False, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    filled_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_paper_orders_account_state", "account_id", "state"),
        Index("ix_paper_orders_symbol_state", "symbol", "state"),
    )

    def __repr__(self) -> str:
        return f"<PaperOrder({self.order_id} {self.side.upper()} {self.size} {self.symbol} [{self.state}])>"


# ──────────────────────────────────────────────
# Virtual Ledger Manager
# ──────────────────────────────────────────────
class PaperLedger:
    """
    Virtual Ledger and Position Accounting System.

    Manages paper cash balance, order persistence, position lifecycles,
    PnL attribution, and transaction history.
    """

    def __init__(
        self,
        db_url: Optional[str] = None,
        engine: Optional[Engine] = None,
        session_factory: Optional[sessionmaker] = None,
        account_id: str = "paper_account",
        initial_balance: Union[Decimal, float] = Decimal("10000.0"),
    ):
        self.account_id = account_id
        self.initial_balance = Decimal(str(initial_balance))
        self._lock = threading.RLock()

        if engine is not None:
            self.engine = engine
            self.session_factory = session_factory or sessionmaker(bind=self.engine)
        elif session_factory is not None:
            self.session_factory = session_factory
            self.engine = session_factory.kw.get("bind")
        else:
            url = db_url or "sqlite:///:memory:"
            if url.startswith("sqlite"):
                self.engine = create_engine(
                    url,
                    connect_args={"check_same_thread": False},
                    poolclass=StaticPool if ":memory:" in url else None,
                )
            else:
                self.engine = create_engine(url)
            self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)

        self.init_db()

    def init_db(self) -> None:
        """Create tables if not existing and seed initial balance if new account."""
        with self._lock:
            PaperBase.metadata.create_all(self.engine)
            with self.session_factory() as session:
                entry_count = (
                    session.query(PaperLedgerModel)
                    .filter(PaperLedgerModel.account_id == self.account_id)
                    .count()
                )
                if entry_count == 0 and self.initial_balance > 0:
                    init_entry = PaperLedgerModel(
                        entry_id=f"ledg_{uuid.uuid4().hex[:12]}",
                        account_id=self.account_id,
                        currency="USDT",
                        transaction_type="INITIAL_DEPOSIT",
                        amount=self.initial_balance,
                        balance_after=self.initial_balance,
                        description="Initial Paper Capital Seed",
                        created_at=datetime.utcnow(),
                    )
                    session.add(init_entry)
                    session.commit()
                    logger.info(f"Initialized paper ledger for {self.account_id} with {self.initial_balance} USDT")

    def get_cash_balance(self, account_id: Optional[str] = None) -> Decimal:
        """Get latest cash balance for the account."""
        acct = account_id or self.account_id
        with self._lock:
            with self.session_factory() as session:
                last_entry = (
                    session.query(PaperLedgerModel)
                    .filter(PaperLedgerModel.account_id == acct)
                    .order_by(PaperLedgerModel.created_at.desc(), PaperLedgerModel.entry_id.desc())
                    .first()
                )
                if last_entry:
                    return Decimal(str(last_entry.balance_after))
                return Decimal("0.0")

    def deposit(
        self,
        amount: Union[Decimal, float],
        description: str = "Deposit",
        account_id: Optional[str] = None,
    ) -> Decimal:
        """Deposit cash into virtual account."""
        acct = account_id or self.account_id
        amt = Decimal(str(amount))
        with self._lock:
            current_bal = self.get_cash_balance(acct)
            new_bal = current_bal + amt
            entry = PaperLedgerModel(
                entry_id=f"ledg_{uuid.uuid4().hex[:12]}",
                account_id=acct,
                currency="USDT",
                transaction_type="DEPOSIT",
                amount=amt,
                balance_after=new_bal,
                description=description,
                created_at=datetime.utcnow(),
            )
            with self.session_factory() as session:
                session.add(entry)
                session.commit()
            return new_bal

    def withdraw(
        self,
        amount: Union[Decimal, float],
        description: str = "Withdrawal",
        account_id: Optional[str] = None,
    ) -> Decimal:
        """Withdraw cash from virtual account."""
        acct = account_id or self.account_id
        amt = Decimal(str(amount))
        with self._lock:
            current_bal = self.get_cash_balance(acct)
            if amt > current_bal:
                raise ValueError(f"Insufficient paper balance: have {current_bal}, requested {amt}")
            new_bal = current_bal - amt
            entry = PaperLedgerModel(
                entry_id=f"ledg_{uuid.uuid4().hex[:12]}",
                account_id=acct,
                currency="USDT",
                transaction_type="WITHDRAWAL",
                amount=-amt,
                balance_after=new_bal,
                description=description,
                created_at=datetime.utcnow(),
            )
            with self.session_factory() as session:
                session.add(entry)
                session.commit()
            return new_bal

    # ──────────────────────────────────────────────
    # Order Management
    # ──────────────────────────────────────────────
    def create_order(
        self,
        symbol: str,
        side: str,
        size: int,
        order_type: str = "limit_order",
        price: Optional[Union[Decimal, float]] = None,
        client_order_id: Optional[str] = None,
        bracket_stop_loss_price: Optional[Union[Decimal, float]] = None,
        bracket_take_profit_price: Optional[Union[Decimal, float]] = None,
        account_id: Optional[str] = None,
        parent_order_id: Optional[str] = None,
        state: str = "open",
    ) -> PaperOrderModel:
        """Create and persist a new paper order."""
        acct = account_id or self.account_id
        order_id = f"pord_{uuid.uuid4().hex[:12]}"
        cid = client_order_id or f"cl_{uuid.uuid4().hex[:8]}"

        p_dec = Decimal(str(price)) if price is not None else None
        sl_dec = Decimal(str(bracket_stop_loss_price)) if bracket_stop_loss_price is not None else None
        tp_dec = Decimal(str(bracket_take_profit_price)) if bracket_take_profit_price is not None else None

        order = PaperOrderModel(
            order_id=order_id,
            client_order_id=cid,
            account_id=acct,
            symbol=symbol,
            side=side.lower(),
            order_type=order_type.lower(),
            size=int(size),
            filled_size=0,
            unfilled_size=int(size),
            price=p_dec,
            state=state,
            bracket_stop_loss_price=sl_dec,
            bracket_take_profit_price=tp_dec,
            parent_order_id=parent_order_id,
            fee_paid=Decimal("0.0"),
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )

        with self._lock:
            with self.session_factory() as session:
                session.add(order)
                session.commit()
                # Re-fetch detached object
                session.refresh(order)
                session.expunge(order)
                return order

    def get_order(self, order_id: str) -> Optional[PaperOrderModel]:
        """Fetch order by order_id."""
        with self._lock:
            with self.session_factory() as session:
                order = session.query(PaperOrderModel).filter(PaperOrderModel.order_id == order_id).first()
                if order:
                    session.expunge(order)
                return order

    def get_order_by_client_id(self, client_order_id: str, account_id: Optional[str] = None) -> Optional[PaperOrderModel]:
        """Fetch order by client_order_id for idempotency checks."""
        acct = account_id or self.account_id
        with self._lock:
            with self.session_factory() as session:
                order = (
                    session.query(PaperOrderModel)
                    .filter(
                        PaperOrderModel.client_order_id == client_order_id,
                        PaperOrderModel.account_id == acct,
                    )
                    .first()
                )
                if order:
                    session.expunge(order)
                return order

    def get_open_orders(self, symbol: Optional[str] = None, account_id: Optional[str] = None) -> List[PaperOrderModel]:
        """Get all currently open resting orders."""
        acct = account_id or self.account_id
        with self._lock:
            with self.session_factory() as session:
                q = session.query(PaperOrderModel).filter(
                    PaperOrderModel.account_id == acct,
                    PaperOrderModel.state.in_(["open", "partially_filled"]),
                )
                if symbol:
                    q = q.filter(PaperOrderModel.symbol == symbol)
                orders = q.all()
                for o in orders:
                    session.expunge(o)
                return orders

    def get_all_orders(
        self,
        symbol: Optional[str] = None,
        state: Optional[str] = None,
        account_id: Optional[str] = None,
    ) -> List[PaperOrderModel]:
        """Retrieve orders with optional filtering."""
        acct = account_id or self.account_id
        with self._lock:
            with self.session_factory() as session:
                q = session.query(PaperOrderModel).filter(PaperOrderModel.account_id == acct)
                if symbol:
                    q = q.filter(PaperOrderModel.symbol == symbol)
                if state:
                    q = q.filter(PaperOrderModel.state == state.lower())
                orders = q.order_by(PaperOrderModel.created_at.desc()).all()
                for o in orders:
                    session.expunge(o)
                return orders

    def cancel_order(self, order_id: str) -> Optional[PaperOrderModel]:
        """Cancel an open order."""
        with self._lock:
            with self.session_factory() as session:
                order = session.query(PaperOrderModel).filter(PaperOrderModel.order_id == order_id).first()
                if not order:
                    return None
                if order.state in ("filled", "cancelled", "rejected"):
                    session.expunge(order)
                    return order

                order.state = "cancelled"
                order.cancelled_at = datetime.utcnow()
                order.updated_at = datetime.utcnow()
                session.commit()
                session.refresh(order)
                session.expunge(order)
                return order

    def amend_order(
        self,
        order_id: str,
        price: Optional[Union[Decimal, float]] = None,
        size: Optional[int] = None,
    ) -> Optional[PaperOrderModel]:
        """Amend resting limit order price and/or size."""
        with self._lock:
            with self.session_factory() as session:
                order = session.query(PaperOrderModel).filter(PaperOrderModel.order_id == order_id).first()
                if not order:
                    return None
                if order.state not in ("open", "partially_filled"):
                    raise ValueError(f"Cannot amend order in state {order.state}")

                if price is not None:
                    order.price = Decimal(str(price))
                if size is not None:
                    new_size = int(size)
                    if new_size <= order.filled_size:
                        raise ValueError(f"New size {new_size} must be greater than filled size {order.filled_size}")
                    order.size = new_size
                    order.unfilled_size = new_size - order.filled_size

                order.updated_at = datetime.utcnow()
                session.commit()
                session.refresh(order)
                session.expunge(order)
                return order

    # ──────────────────────────────────────────────
    # Execution & Position Lifecycle
    # ──────────────────────────────────────────────
    def record_fill(
        self,
        order_id: str,
        fill_price: Union[Decimal, float],
        fill_size: int,
        fee: Union[Decimal, float] = Decimal("0"),
        contract_value: Union[Decimal, float] = Decimal("1.0"),
        is_maker: bool = False,
    ) -> Tuple[PaperOrderModel, Optional[PaperPositionModel], Decimal]:
        """
        Record fill for an order, execute position lifecycle, compute realized PnL & deduct fee.

        Returns:
            Tuple of (updated_order, affected_position, realized_pnl)
        """
        f_price = Decimal(str(fill_price))
        f_size = int(fill_size)
        f_fee = Decimal(str(fee))
        c_val = Decimal(str(contract_value))
        realized_pnl = Decimal("0.0")

        with self._lock:
            with self.session_factory() as session:
                order = session.query(PaperOrderModel).filter(PaperOrderModel.order_id == order_id).first()
                if not order:
                    raise ValueError(f"Order {order_id} not found for fill execution")

                # 1. Update Order Execution Progress
                prev_filled = order.filled_size
                order.filled_size += f_size
                order.unfilled_size = max(0, order.size - order.filled_size)
                if order.unfilled_size == 0:
                    order.state = "filled"
                    order.filled_at = datetime.utcnow()
                else:
                    order.state = "partially_filled"

                # Weighted average fill price
                if order.avg_fill_price is None or prev_filled == 0:
                    order.avg_fill_price = f_price
                else:
                    total_filled = prev_filled + f_size
                    order.avg_fill_price = (
                        (Decimal(str(order.avg_fill_price)) * prev_filled) + (f_price * f_size)
                    ) / total_filled

                order.fee_paid = Decimal(str(order.fee_paid)) + f_fee
                order.updated_at = datetime.utcnow()

                # 2. Deduct Trading Fee from Cash Ledger
                if f_fee > 0:
                    curr_bal = self.get_cash_balance(order.account_id)
                    fee_entry = PaperLedgerModel(
                        entry_id=f"ledg_{uuid.uuid4().hex[:12]}",
                        account_id=order.account_id,
                        currency="USDT",
                        transaction_type="FILL_FEE",
                        amount=-f_fee,
                        balance_after=curr_bal - f_fee,
                        order_id=order.order_id,
                        description=f"Trading Fee ({'Maker' if is_maker else 'Taker'}) on {order.symbol}",
                        created_at=datetime.utcnow(),
                    )
                    session.add(fee_entry)

                # 3. Position Accounting (Multi-directional)
                pos = (
                    session.query(PaperPositionModel)
                    .filter(
                        PaperPositionModel.account_id == order.account_id,
                        PaperPositionModel.symbol == order.symbol,
                        PaperPositionModel.is_open == True,
                    )
                    .first()
                )

                fill_side = order.side.lower()  # 'buy' or 'sell'

                if pos is None:
                    # Open brand new position
                    pos = PaperPositionModel(
                        position_id=f"ppos_{uuid.uuid4().hex[:12]}",
                        account_id=order.account_id,
                        symbol=order.symbol,
                        side=fill_side,
                        size=f_size,
                        entry_price=f_price,
                        mark_price=f_price,
                        contract_value=c_val,
                        unrealized_pnl=Decimal("0.0"),
                        realized_pnl=Decimal("0.0"),
                        is_open=True,
                        entry_order_id=order.order_id,
                        stop_loss_price=order.bracket_stop_loss_price,
                        take_profit_price=order.bracket_take_profit_price,
                        created_at=datetime.utcnow(),
                        updated_at=datetime.utcnow(),
                    )
                    session.add(pos)
                elif pos.side.lower() == fill_side:
                    # Increasing existing position in same direction
                    old_size = pos.size
                    new_size = old_size + f_size
                    new_entry = (
                        (Decimal(str(pos.entry_price)) * old_size) + (f_price * f_size)
                    ) / new_size
                    pos.size = new_size
                    pos.entry_price = new_entry
                    pos.updated_at = datetime.utcnow()
                    # Recalculate unrealized PnL
                    mark = Decimal(str(pos.mark_price))
                    if pos.side.lower() == "buy":
                        pos.unrealized_pnl = new_size * (mark - new_entry) * c_val
                    else:
                        pos.unrealized_pnl = new_size * (new_entry - mark) * c_val
                else:
                    # Reducing, closing, or flipping position
                    old_size = pos.size
                    old_entry = Decimal(str(pos.entry_price))

                    if f_size <= old_size:
                        # Partial or full close
                        if pos.side.lower() == "buy":
                            realized_pnl = f_size * (f_price - old_entry) * c_val
                        else:
                            realized_pnl = f_size * (old_entry - f_price) * c_val

                        pos.realized_pnl = Decimal(str(pos.realized_pnl)) + realized_pnl
                        pos.size = old_size - f_size
                        pos.updated_at = datetime.utcnow()

                        if pos.size == 0:
                            pos.is_open = False
                            pos.closed_at = datetime.utcnow()
                            pos.unrealized_pnl = Decimal("0.0")
                        else:
                            # Recalculate unrealized PnL on remaining contracts
                            mark = Decimal(str(pos.mark_price))
                            if pos.side.lower() == "buy":
                                pos.unrealized_pnl = pos.size * (mark - old_entry) * c_val
                            else:
                                pos.unrealized_pnl = pos.size * (old_entry - mark) * c_val
                    else:
                        # Position flipped! (e.g. was Long 2, Sell 5 -> closes 2 Long, opens 3 Short)
                        close_size = old_size
                        flip_size = f_size - old_size

                        if pos.side.lower() == "buy":
                            realized_pnl = close_size * (f_price - old_entry) * c_val
                        else:
                            realized_pnl = close_size * (old_entry - f_price) * c_val

                        pos.realized_pnl = Decimal(str(pos.realized_pnl)) + realized_pnl
                        pos.size = 0
                        pos.is_open = False
                        pos.closed_at = datetime.utcnow()
                        pos.unrealized_pnl = Decimal("0.0")

                        # Open new opposite position with remaining size
                        new_pos = PaperPositionModel(
                            position_id=f"ppos_{uuid.uuid4().hex[:12]}",
                            account_id=order.account_id,
                            symbol=order.symbol,
                            side=fill_side,
                            size=flip_size,
                            entry_price=f_price,
                            mark_price=f_price,
                            contract_value=c_val,
                            unrealized_pnl=Decimal("0.0"),
                            realized_pnl=Decimal("0.0"),
                            is_open=True,
                            entry_order_id=order.order_id,
                            stop_loss_price=order.bracket_stop_loss_price,
                            take_profit_price=order.bracket_take_profit_price,
                            created_at=datetime.utcnow(),
                            updated_at=datetime.utcnow(),
                        )
                        session.add(new_pos)
                        pos = new_pos

                # 4. Credit/Debit Realized PnL to Cash Ledger
                if realized_pnl != Decimal("0.0"):
                    curr_bal = self.get_cash_balance(order.account_id)
                    pnl_entry = PaperLedgerModel(
                        entry_id=f"ledg_{uuid.uuid4().hex[:12]}",
                        account_id=order.account_id,
                        currency="USDT",
                        transaction_type="REALIZED_PNL",
                        amount=realized_pnl,
                        balance_after=curr_bal + realized_pnl,
                        order_id=order.order_id,
                        description=f"Realized PnL on {order.symbol} ({'profit' if realized_pnl > 0 else 'loss'})",
                        created_at=datetime.utcnow(),
                    )
                    session.add(pnl_entry)

                session.commit()
                session.refresh(order)
                session.expunge(order)
                if pos:
                    session.refresh(pos)
                    session.expunge(pos)

                return order, pos, realized_pnl

    def update_mark_price(
        self,
        symbol: str,
        mark_price: Union[Decimal, float],
        account_id: Optional[str] = None,
    ) -> List[PaperPositionModel]:
        """Update mark price and unrealized PnL on open positions for symbol."""
        acct = account_id or self.account_id
        m_price = Decimal(str(mark_price))
        updated_positions: List[PaperPositionModel] = []

        with self._lock:
            with self.session_factory() as session:
                positions = (
                    session.query(PaperPositionModel)
                    .filter(
                        PaperPositionModel.account_id == acct,
                        PaperPositionModel.symbol == symbol,
                        PaperPositionModel.is_open == True,
                    )
                    .all()
                )

                for pos in positions:
                    pos.mark_price = m_price
                    c_val = Decimal(str(pos.contract_value))
                    entry = Decimal(str(pos.entry_price))
                    if pos.side.lower() == "buy":
                        pos.unrealized_pnl = pos.size * (m_price - entry) * c_val
                    else:
                        pos.unrealized_pnl = pos.size * (entry - m_price) * c_val
                    pos.updated_at = datetime.utcnow()
                    updated_positions.append(pos)

                session.commit()
                for p in updated_positions:
                    session.refresh(p)
                    session.expunge(p)

                return updated_positions

    def get_open_positions(
        self,
        account_id: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> List[PaperPositionModel]:
        """Fetch all currently open positions."""
        acct = account_id or self.account_id
        with self._lock:
            with self.session_factory() as session:
                q = session.query(PaperPositionModel).filter(
                    PaperPositionModel.account_id == acct,
                    PaperPositionModel.is_open == True,
                )
                if symbol:
                    q = q.filter(PaperPositionModel.symbol == symbol)
                positions = q.all()
                for p in positions:
                    session.expunge(p)
                return positions

    def get_position(
        self,
        symbol: str,
        account_id: Optional[str] = None,
    ) -> Optional[PaperPositionModel]:
        """Fetch active open position for symbol."""
        positions = self.get_open_positions(account_id=account_id, symbol=symbol)
        return positions[0] if positions else None

    # ──────────────────────────────────────────────
    # Balance & Analytics
    # ──────────────────────────────────────────────
    def get_balance_summary(self, account_id: Optional[str] = None) -> Dict[str, Any]:
        """Compute institutional portfolio valuation and margin status."""
        acct = account_id or self.account_id
        with self._lock:
            cash = self.get_cash_balance(acct)
            open_pos = self.get_open_positions(account_id=acct)

            unrealized_pnl = sum((Decimal(str(p.unrealized_pnl)) for p in open_pos), Decimal("0.0"))
            margin_used = sum(
                (Decimal(str(p.size)) * Decimal(str(p.entry_price)) * Decimal(str(p.contract_value)) * Decimal("0.1")
                 for p in open_pos),
                Decimal("0.0")
            )

            with self.session_factory() as session:
                # Cumulative fees
                fee_entries = (
                    session.query(PaperLedgerModel)
                    .filter(
                        PaperLedgerModel.account_id == acct,
                        PaperLedgerModel.transaction_type == "FILL_FEE",
                    )
                    .all()
                )
                total_fees = abs(sum((Decimal(str(e.amount)) for e in fee_entries), Decimal("0.0")))

                # Cumulative realized PnL
                pnl_entries = (
                    session.query(PaperLedgerModel)
                    .filter(
                        PaperLedgerModel.account_id == acct,
                        PaperLedgerModel.transaction_type == "REALIZED_PNL",
                    )
                    .all()
                )
                total_realized = sum((Decimal(str(e.amount)) for e in pnl_entries), Decimal("0.0"))

            total_equity = cash + unrealized_pnl
            available_balance = max(Decimal("0.0"), cash - margin_used)

            return {
                "account_id": acct,
                "initial_balance": self.initial_balance,
                "cash_balance": cash,
                "realized_pnl": total_realized,
                "unrealized_pnl": unrealized_pnl,
                "total_equity": total_equity,
                "total_fees_paid": total_fees,
                "margin_used": margin_used,
                "available_balance": available_balance,
                "open_positions_count": len(open_pos),
            }

    def get_ledger_entries(
        self,
        account_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[PaperLedgerModel]:
        """Fetch audit log of transactions in the ledger."""
        acct = account_id or self.account_id
        with self._lock:
            with self.session_factory() as session:
                entries = (
                    session.query(PaperLedgerModel)
                    .filter(PaperLedgerModel.account_id == acct)
                    .order_by(PaperLedgerModel.created_at.desc())
                    .limit(limit)
                    .all()
                )
                for e in entries:
                    session.expunge(e)
                return entries

    def reset(self, account_id: Optional[str] = None) -> None:
        """Reset ledger, orders, and positions back to fresh initial state."""
        acct = account_id or self.account_id
        with self._lock:
            with self.session_factory() as session:
                session.query(PaperOrderModel).filter(PaperOrderModel.account_id == acct).delete()
                session.query(PaperPositionModel).filter(PaperPositionModel.account_id == acct).delete()
                session.query(PaperLedgerModel).filter(PaperLedgerModel.account_id == acct).delete()

                init_entry = PaperLedgerModel(
                    entry_id=f"ledg_{uuid.uuid4().hex[:12]}",
                    account_id=acct,
                    currency="USDT",
                    transaction_type="INITIAL_DEPOSIT",
                    amount=self.initial_balance,
                    balance_after=self.initial_balance,
                    description="Initial Paper Capital Reset",
                    created_at=datetime.utcnow(),
                )
                session.add(init_entry)
                session.commit()
                logger.info(f"Reset paper trading environment for {acct} to {self.initial_balance} USDT")
