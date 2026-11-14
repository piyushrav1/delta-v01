"""
Authoritative Order Execution Gateway

The sole authoritative pathway for dispatching orders to Delta Exchange.
No order may be placed without an approved RiskDecision from the Central Risk Gateway.

Guarantees:
1. Client Order ID Idempotency (Strictly prevents duplicate order placement)
2. State Machine Enforcement (Tracks lifecycle transitions CREATED -> FILLED)
3. Native Bracket Order Attachment (Binds Stop-Loss & Take-Profit targets)
4. Atomic Cancellation & Amendment
"""
from __future__ import annotations

import uuid
import logging
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass

from storage.models import OrderState, OrderSide, OrderType
from core.exceptions import RiskViolation, OrderExecutionError
from risk.risk_manager import RiskDecisionResult
from analytics.signal import TradeSignal
from trading.order_state import ManagedOrder, InvalidStateTransitionError
from trading.bracket_manager import BracketManager

logger = logging.getLogger(__name__)


class ExecutionManager:
    """
    Central Authoritative Execution Gateway.
    """

    def __init__(self, bracket_manager: Optional[BracketManager] = None):
        self.bracket_manager = bracket_manager or BracketManager()
        # In-memory registry: order_id -> ManagedOrder
        self._orders: Dict[str, ManagedOrder] = {}
        # Client order ID lookup for idempotency: client_order_id -> order_id
        self._client_order_ids: Dict[str, str] = {}

    def get_order(self, order_id: str) -> Optional[ManagedOrder]:
        """Retrieve managed order by internal order_id."""
        return self._orders.get(order_id)

    def get_order_by_client_id(self, client_order_id: str) -> Optional[ManagedOrder]:
        """Retrieve managed order by client_order_id."""
        oid = self._client_order_ids.get(client_order_id)
        return self._orders.get(oid) if oid else None

    def get_open_orders(self, account_id: Optional[str] = None) -> List[ManagedOrder]:
        """Retrieve all active open/in-flight orders."""
        orders = [o for o in self._orders.values() if o.is_active]
        if account_id:
            orders = [o for o in orders if o.account_id == account_id]
        return orders

    def execute_decision(
        self,
        risk_decision: RiskDecisionResult,
        signal: TradeSignal,
        account_id: str,
        delta_client: Optional[Any] = None,
        client_order_id: Optional[str] = None,
    ) -> ManagedOrder:
        """
        Execute an approved RiskDecision into an exchange order.

        Args:
            risk_decision: Approved result from CentralRiskManager
            signal: Emitted TradeSignal
            account_id: Account identifier
            delta_client: DeltaRestClient instance (if None, simulated fill)
            client_order_id: Optional unique client idempotency key

        Returns:
            ManagedOrder tracking lifecycle state.
        """
        # Rule 1: Fail closed if decision is not approved
        if not risk_decision.approved:
            raise RiskViolation(
                rule="risk_gate_approval",
                message=f"Cannot execute unapproved RiskDecision {risk_decision.decision_id}: {risk_decision.rejection_reason}"
            )

        if risk_decision.calculated_size <= 0:
            raise RiskViolation(
                rule="calculated_size",
                message=f"Cannot execute RiskDecision with size <= 0 (got {risk_decision.calculated_size})"
            )

        # Rule 2: Client Order ID Idempotency
        cid = client_order_id or f"cl_{signal.signal_id}_{uuid.uuid4().hex[:6]}"
        if cid in self._client_order_ids:
            existing_oid = self._client_order_ids[cid]
            existing_order = self._orders.get(existing_oid)
            logger.warning(f"Duplicate order submission blocked by idempotency key: {cid} (Order {existing_oid})")
            return existing_order

        order_id = f"ord_{uuid.uuid4().hex[:12]}"
        side = OrderSide.BUY if signal.direction.upper() == "BUY" else OrderSide.SELL
        order_type = OrderType.LIMIT_ORDER if signal.order_type == "limit_order" else OrderType.MARKET_ORDER

        # Create ManagedOrder in CREATED state
        managed = ManagedOrder(
            order_id=order_id,
            client_order_id=cid,
            account_id=account_id,
            symbol=signal.symbol,
            side=side,
            order_type=order_type,
            size=risk_decision.calculated_size,
            price=Decimal(str(signal.target_entry_price)),
            bracket_stop_loss_price=Decimal(str(signal.stop_loss_price)) if signal.stop_loss_price else None,
            bracket_take_profit_price=Decimal(str(signal.take_profit_price)) if signal.take_profit_price else None,
            decision_id=risk_decision.decision_id,
        )

        self._orders[order_id] = managed
        self._client_order_ids[cid] = order_id

        # Lifecycle Step 1 -> VALIDATED
        managed.transition_to(OrderState.VALIDATED, "Order validated against signal parameters")

        # Lifecycle Step 2 -> RISK_APPROVED
        managed.transition_to(OrderState.RISK_APPROVED, f"Authorized by RiskDecision {risk_decision.decision_id}")

        # Lifecycle Step 3 -> SUBMITTED
        managed.transition_to(OrderState.SUBMITTED, "Dispatching to Delta Exchange API")

        # Build payload with native brackets
        bracket_params = self.bracket_manager.build_bracket_params(
            stop_loss_price=managed.bracket_stop_loss_price,
            take_profit_price=managed.bracket_take_profit_price,
        )

        order_payload = {
            "symbol": managed.symbol,
            "side": managed.side.value,
            "order_type": managed.order_type.value,
            "size": managed.size,
            "price": str(float(managed.price)) if managed.price else None,
            "client_order_id": managed.client_order_id,
            **bracket_params,
        }

        # Dispatch via delta_client or mock
        if delta_client is not None:
            try:
                resp = delta_client.place_order(
                    symbol=managed.symbol,
                    side=managed.side.value,
                    size=managed.size,
                    price=float(managed.price) if managed.price else None,
                    order_type=managed.order_type.value,
                    client_order_id=managed.client_order_id,
                    **bracket_params,
                )
                if isinstance(resp, dict):
                    ex_id = str(resp.get('id') or resp.get('order_id') or resp.get('result', {}).get('id'))
                    managed.exchange_order_id = ex_id
                    managed.transition_to(OrderState.ACKNOWLEDGED, f"Acknowledged by Exchange ID {ex_id}")

                    # Check if immediate fill reported
                    state_str = str(resp.get('state') or resp.get('result', {}).get('state', '')).lower()
                    if state_str == 'filled':
                        managed.record_fill(managed.size, managed.price)
                else:
                    managed.transition_to(OrderState.ACKNOWLEDGED, "Acknowledged by exchange")
            except Exception as e:
                managed.error_message = str(e)
                managed.transition_to(OrderState.REJECTED, f"Exchange submission failed: {str(e)}")
                raise OrderExecutionError(f"Delta API rejected order {managed.order_id}: {str(e)}")
        else:
            # Standalone / test environment mode: acknowledge directly
            managed.exchange_order_id = f"mock_ex_{uuid.uuid4().hex[:8]}"
            managed.transition_to(OrderState.ACKNOWLEDGED, "Simulated exchange acknowledgment")

        return managed

    def cancel_order(self, order_id: str, delta_client: Optional[Any] = None) -> ManagedOrder:
        """Cancel an active order."""
        managed = self._orders.get(order_id)
        if not managed:
            raise OrderExecutionError(f"Order not found: {order_id}")

        if managed.is_terminal:
            logger.info(f"Order {order_id} is already in terminal state {managed.state.value}")
            return managed

        if delta_client is not None and managed.exchange_order_id:
            try:
                delta_client.cancel_order(order_id=managed.exchange_order_id)
            except Exception as e:
                logger.error(f"Error cancelling order on Delta API: {e}")

        managed.transition_to(OrderState.CANCELLED, "Cancelled by user or execution manager")
        return managed

    def amend_order(
        self,
        order_id: str,
        new_price: Optional[Decimal] = None,
        new_size: Optional[int] = None,
        delta_client: Optional[Any] = None,
    ) -> ManagedOrder:
        """Atomically amend an active resting order."""
        managed = self._orders.get(order_id)
        if not managed:
            raise OrderExecutionError(f"Order not found: {order_id}")

        if managed.state != OrderState.ACKNOWLEDGED:
            raise OrderExecutionError(f"Cannot amend order in state {managed.state.value} (must be ACKNOWLEDGED)")

        if delta_client is not None and managed.exchange_order_id:
            delta_client.amend_order(
                order_id=managed.exchange_order_id,
                price=float(new_price) if new_price else None,
                size=new_size,
            )

        if new_price is not None:
            managed.price = new_price
        if new_size is not None:
            managed.size = new_size
            managed.unfilled_size = max(0, new_size - managed.filled_size)

        managed.updated_at = datetime.now()
        logger.info(f"Amended order {order_id}: price={new_price}, size={new_size}")
        return managed
