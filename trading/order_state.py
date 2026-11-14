"""
Order Lifecycle State Machine

Enforces strict, auditable state transitions for all orders.
Prevents illegal state skips and invalid execution workflows.

States:
    CREATED -> VALIDATED -> RISK_APPROVED -> SUBMITTED -> ACKNOWLEDGED
            -> PARTIALLY_FILLED -> FILLED (or REJECTED, CANCELLED, EXPIRED)
"""
from __future__ import annotations

import uuid
import logging
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Tuple, Set, Any
from dataclasses import dataclass, field

from storage.models import OrderState, OrderSide, OrderType
from core.exceptions import OrderExecutionError

logger = logging.getLogger(__name__)


class InvalidStateTransitionError(OrderExecutionError):
    """Raised when an order attempts an unauthorized state transition."""
    def __init__(self, order_id: str, from_state: OrderState, to_state: OrderState, reason: str = ""):
        super().__init__(
            f"Invalid order transition for {order_id}: {from_state.value} -> {to_state.value}. {reason}"
        )
        self.order_id = order_id
        self.from_state = from_state
        self.to_state = to_state


# Directed transition graph of permitted state movements
VALID_TRANSITIONS: Dict[OrderState, Set[OrderState]] = {
    OrderState.CREATED: {
        OrderState.VALIDATED,
        OrderState.REJECTED,
    },
    OrderState.VALIDATED: {
        OrderState.RISK_APPROVED,
        OrderState.REJECTED,
    },
    OrderState.RISK_APPROVED: {
        OrderState.SUBMITTED,
        OrderState.REJECTED,
        OrderState.CANCELLED,
    },
    OrderState.SUBMITTED: {
        OrderState.ACKNOWLEDGED,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,  # Immediate fill (e.g. taker order)
        OrderState.REJECTED,
    },
    OrderState.ACKNOWLEDGED: {
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.EXPIRED,
    },
    OrderState.PARTIALLY_FILLED: {
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.CANCELLED,
    },
    # Terminal states: no further transitions allowed
    OrderState.FILLED: set(),
    OrderState.REJECTED: set(),
    OrderState.CANCELLED: set(),
    OrderState.EXPIRED: set(),
}


@dataclass
class StateTransitionEvent:
    """Historical audit record of an order state transition."""
    from_state: OrderState
    to_state: OrderState
    timestamp: datetime
    message: str


@dataclass
class ManagedOrder:
    """In-memory active order model managed by the execution gateway."""
    order_id: str
    client_order_id: str
    account_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    size: int
    price: Optional[Decimal] = None
    unfilled_size: int = 0
    filled_size: int = 0
    state: OrderState = OrderState.CREATED
    exchange_order_id: Optional[str] = None
    bracket_stop_loss_price: Optional[Decimal] = None
    bracket_take_profit_price: Optional[Decimal] = None
    decision_id: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    history: List[StateTransitionEvent] = field(default_factory=list)

    def __post_init__(self):
        if self.unfilled_size == 0 and self.size > 0:
            self.unfilled_size = self.size

    @property
    def is_terminal(self) -> bool:
        """True if order has reached a final state."""
        return self.state in {OrderState.FILLED, OrderState.REJECTED, OrderState.CANCELLED, OrderState.EXPIRED}

    @property
    def is_active(self) -> bool:
        """True if order is in-flight or resting on the exchange."""
        return self.state in {OrderState.SUBMITTED, OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED}

    def transition_to(self, target_state: OrderState, message: str = "") -> None:
        """
        Transition order to a new state if authorized by FSM.
        """
        if target_state not in VALID_TRANSITIONS.get(self.state, set()):
            raise InvalidStateTransitionError(
                order_id=self.order_id,
                from_state=self.state,
                to_state=target_state,
                reason=f"Transition from {self.state.value} to {target_state.value} is forbidden."
            )

        now = datetime.now()
        event = StateTransitionEvent(
            from_state=self.state,
            to_state=target_state,
            timestamp=now,
            message=message,
        )
        self.history.append(event)
        self.state = target_state
        self.updated_at = now

        logger.info(f"Order [{self.order_id}] state changed: {event.from_state.value} -> {target_state.value} ({message})")

    def record_fill(self, fill_qty: int, fill_price: Optional[Decimal] = None) -> None:
        """Record fill and advance state machine to PARTIALLY_FILLED or FILLED."""
        if fill_qty <= 0:
            return

        self.filled_size += fill_qty
        self.unfilled_size = max(0, self.size - self.filled_size)

        if self.unfilled_size == 0:
            self.transition_to(OrderState.FILLED, f"Filled {self.filled_size}/{self.size} @ {fill_price}")
        else:
            self.transition_to(
                OrderState.PARTIALLY_FILLED,
                f"Partially filled {self.filled_size}/{self.size} (remaining {self.unfilled_size})"
            )

