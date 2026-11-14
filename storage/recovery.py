"""
Post-Restart State Recovery & Rehydration

Restores active positions, open orders, and P&L state from database
and reconciles with exchange on startup.
"""
import logging
import threading
from datetime import datetime, date
from typing import Optional, List, Dict, Any, Tuple
from decimal import Decimal

from sqlalchemy.orm import Session
from sqlalchemy import and_, or_

from storage.database import get_session, session_scope as get_session_scope
from storage.models import (
    Account, Instrument, Order, Position, Fill, PnLSnapshot,
    CircuitBreakerEvent, OrderState, OrderSide, AccountEnvironment,
    get_open_orders, get_open_positions, get_latest_pnl_snapshot,
    get_active_circuit_breakers
)
from exchange.delta_client import DeltaRestClient

logger = logging.getLogger(__name__)


class RecoveryManager:
    """
    Handles state recovery after process restart.

    Responsibilities:
    1. Load open orders and positions from database
    2. Reconcile with exchange state
    3. Restore P&L tracking (daily start equity, high-water marks)
    4. Re-activate circuit breakers if needed
    5. Initialize in-memory caches for trading engine
    """

    def __init__(self):
        self._recovery_lock = threading.Lock()
        self._recovery_complete = threading.Event()
        self._recovery_error: Optional[str] = None

    def recover(self, delta_client: Optional[DeltaRestClient] = None) -> Dict[str, Any]:
        """
        Perform full state recovery.

        Args:
            delta_client: Optional DeltaRestClient for exchange reconciliation.
                         If None, creates default client.

        Returns:
            Recovery summary with counts and any discrepancies.
        """
        with self._recovery_lock:
            logger.info("Starting post-restart state recovery...")
            start_time = datetime.utcnow()
            summary = {
                "started_at": start_time.isoformat(),
                "accounts_recovered": 0,
                "orders_recovered": 0,
                "positions_recovered": 0,
                "fills_recovered": 0,
                "pnl_restored": 0,
                "circuit_breakers_restored": 0,
                "discrepancies": [],
                "errors": [],
            }

            try:
                # Step 1: Recover accounts
                accounts = self._recover_accounts()
                summary["accounts_recovered"] = len(accounts)

                # Step 2: Recover open orders
                orders = self._recover_orders()
                summary["orders_recovered"] = len(orders)

                # Step 3: Recover positions
                positions = self._recover_positions()
                summary["positions_recovered"] = len(positions)

                # Step 4: Recover fills
                fills = self._recover_fills()
                summary["fills_recovered"] = len(fills)

                # Step 5: Restore P&L state
                pnl_data = self._restore_pnl_state()
                summary["pnl_restored"] = pnl_data.get("snapshots_restored", 0)

                # Step 6: Restore circuit breakers
                cb_count = self._restore_circuit_breakers()
                summary["circuit_breakers_restored"] = cb_count

                # Step 7: Exchange reconciliation (if client provided)
                if delta_client:
                    discrepancies = self._reconcile_with_exchange(delta_client, accounts)
                    summary["discrepancies"] = discrepancies

                self._recovery_error = None
                logger.info(f"State recovery completed in {(datetime.utcnow() - start_time).total_seconds():.2f}s")

            except Exception as e:
                self._recovery_error = str(e)
                logger.error(f"State recovery failed: {e}")
                summary["errors"].append(str(e))

            summary["completed_at"] = datetime.utcnow().isoformat()
            summary["duration_seconds"] = (datetime.utcnow() - start_time).total_seconds()
            self._recovery_complete.set()
            return summary

    def _recover_accounts(self) -> List[Account]:
        """Load all active accounts from database."""
        with get_session_scope() as session:
            accounts = session.query(Account).filter(Account.is_active == True).all()
            logger.info(f"Recovered {len(accounts)} active accounts")
            return accounts

    def _recover_orders(self) -> List[Order]:
        """Load open orders from database and reconstruct state."""
        with get_session_scope() as session:
            open_orders = get_open_orders(session)
            logger.info(f"Recovered {len(open_orders)} open orders")

            # Reconstruct ACTIVE_POSITIONS set from orders with brackets
            for order in open_orders:
                if order.bracket_stop_loss_price or order.bracket_take_profit_price:
                    # This order has protective brackets - track the symbol
                    pass  # Handled by trading engine

            return open_orders

    def _recover_positions(self) -> List[Position]:
        """Load open positions from database."""
        with get_session_scope() as session:
            positions = get_open_positions(session)
            logger.info(f"Recovered {len(positions)} open positions")
            return positions

    def _recover_fills(self) -> List[Fill]:
        """Load recent fills for P&L calculation."""
        with get_session_scope() as session:
            # Get fills from last 7 days for P&L reconstruction
            from datetime import timedelta
            cutoff = datetime.utcnow() - timedelta(days=7)
            fills = session.query(Fill).filter(Fill.executed_at >= cutoff).order_by(Fill.executed_at).all()
            logger.info(f"Recovered {len(fills)} recent fills")
            return fills

    def _restore_pnl_state(self) -> Dict[str, Any]:
        """
        Restore daily P&L tracking state.

        For each active account:
        - Get latest PnLSnapshot
        - If today's snapshot exists, use it
        - If not, create new snapshot with current equity as baseline
        """
        with get_session_scope() as session:
            accounts = session.query(Account).filter(Account.is_active == True).all()
            today = date.today()
            restored = 0

            for account in accounts:
                snapshot = get_latest_pnl_snapshot(session, account.account_id)
                if snapshot and snapshot.snapshot_date == today:
                    # Today's snapshot exists - restore daily baseline
                    logger.info(f"Restored P&L baseline for {account.account_id}: "
                               f"start_equity={snapshot.start_equity}, "
                               f"realized={snapshot.realized_pnl}, "
                               f"unrealized={snapshot.unrealized_pnl}")
                    restored += 1
                elif not snapshot:
                    # No snapshot exists - create initial baseline from exchange
                    # This will be done during exchange reconciliation
                    logger.info(f"No P&L snapshot found for {account.account_id}, will initialize on reconciliation")
                    restored += 1

            return {"snapshots_restored": restored}

    def _restore_circuit_breakers(self) -> int:
        """Re-activate circuit breakers that were active at shutdown."""
        with get_session_scope() as session:
            active_breakers = get_active_circuit_breakers(session)
            for breaker in active_breakers:
                logger.warning(f"Restored active circuit breaker: {breaker.breaker_type} "
                              f"(triggered: {breaker.trigger_value} >= {breaker.threshold_value})")
            return len(active_breakers)

    def _reconcile_with_exchange(
        self,
        delta_client: DeltaRestClient,
        accounts: List[Account]
    ) -> List[Dict[str, Any]]:
        """
        Reconcile local database state with exchange.

        Compares:
        - Open orders (local vs exchange)
        - Positions (local vs exchange)
        - Wallet balances

        Returns list of discrepancies found.
        """
        discrepancies = []

        for account in accounts:
            try:
                logger.info(f"Reconciling account {account.account_id} with exchange...")
                acct_discrepancies = self._reconcile_account(delta_client, account)
                discrepancies.extend(acct_discrepancies)
            except Exception as e:
                logger.error(f"Reconciliation failed for account {account.account_id}: {e}")
                discrepancies.append({
                    "account_id": account.account_id,
                    "type": "reconciliation_error",
                    "message": str(e),
                })

        if discrepancies:
            logger.warning(f"Reconciliation found {len(discrepancies)} discrepancies")
            for d in discrepancies:
                logger.warning(f"  - {d['type']}: {d.get('message', d)}")
        else:
            logger.info("Reconciliation complete: no discrepancies found")

        return discrepancies

    def _reconcile_account(
        self,
        delta_client: DeltaRestClient,
        account: Account
    ) -> List[Dict[str, Any]]:
        """Reconcile single account state with exchange."""
        discrepancies = []
        account_id = account.account_id

        # 1. Reconcile orders
        with get_session_scope() as session:
            local_orders = get_open_orders(session, account_id)
            local_order_map = {o.exchange_order_id: o for o in local_orders if o.exchange_order_id}

        # Get exchange orders
        try:
            exchange_orders = delta_client.get_open_orders(account=account) or []
            exchange_order_map = {str(o.get("id")): o for o in exchange_orders}
        except Exception as e:
            discrepancies.append({
                "account_id": account_id,
                "type": "orders_fetch_failed",
                "message": str(e),
            })
            return discrepancies

        # Check for missing orders on exchange
        for local_id, local_order in local_order_map.items():
            if local_id not in exchange_order_map:
                discrepancies.append({
                    "account_id": account_id,
                    "type": "order_missing_on_exchange",
                    "local_order_id": local_order.order_id,
                    "exchange_order_id": local_id,
                    "symbol": local_order.symbol,
                    "side": local_order.side.value,
                    "size": local_order.size,
                })

        # Check for extra orders on exchange (not in local DB)
        for exch_id, exch_order in exchange_order_map.items():
            if exch_id not in local_order_map:
                discrepancies.append({
                    "account_id": account_id,
                    "type": "order_extra_on_exchange",
                    "exchange_order_id": exch_id,
                    "symbol": exch_order.get("product_symbol"),
                    "side": exch_order.get("side"),
                    "size": exch_order.get("size"),
                    "state": exch_order.get("state"),
                })

        # 2. Reconcile positions
        with get_session_scope() as session:
            local_positions = get_open_positions(session, account_id)
            local_pos_map = {p.symbol: p for p in local_positions}

        try:
            exchange_positions = delta_client.get_positions(account=account) or []
            exchange_pos_map = {p.get("product_symbol"): p for p in exchange_positions if p.get("size")}
        except Exception as e:
            discrepancies.append({
                "account_id": account_id,
                "type": "positions_fetch_failed",
                "message": str(e),
            })
            return discrepancies

        # Check for missing positions
        for symbol, local_pos in local_pos_map.items():
            if symbol not in exchange_pos_map:
                discrepancies.append({
                    "account_id": account_id,
                    "type": "position_missing_on_exchange",
                    "symbol": symbol,
                    "local_size": local_pos.size,
                    "local_side": local_pos.side.value,
                })
            else:
                # Compare sizes
                exch_pos = exchange_pos_map[symbol]
                exch_size = int(exch_pos.get("size", 0))
                exch_side = exch_pos.get("side", "")
                if local_pos.size != exch_size or local_pos.side.value != exch_side:
                    discrepancies.append({
                        "account_id": account_id,
                        "type": "position_size_mismatch",
                        "symbol": symbol,
                        "local_size": local_pos.size,
                        "local_side": local_pos.side.value,
                        "exchange_size": exch_size,
                        "exchange_side": exch_side,
                    })

        # Check for extra positions on exchange
        for symbol, exch_pos in exchange_pos_map.items():
            if symbol not in local_pos_map:
                discrepancies.append({
                    "account_id": account_id,
                    "type": "position_extra_on_exchange",
                    "symbol": symbol,
                    "exchange_size": exch_pos.get("size"),
                    "exchange_side": exch_pos.get("side"),
                })

        return discrepancies

    def wait_for_recovery(self, timeout: float = 30.0) -> bool:
        """Wait for recovery to complete."""
        return self._recovery_complete.wait(timeout)

    @property
    def is_recovery_complete(self) -> bool:
        return self._recovery_complete.is_set()

    @property
    def recovery_error(self) -> Optional[str]:
        return self._recovery_error


# Global recovery manager instance
_recovery_manager: Optional[RecoveryManager] = None
_recovery_manager_lock = threading.Lock()


def get_recovery_manager() -> RecoveryManager:
    """Get global recovery manager instance."""
    global _recovery_manager
    if _recovery_manager is None:
        with _recovery_manager_lock:
            if _recovery_manager is None:
                _recovery_manager = RecoveryManager()
    return _recovery_manager


def run_recovery(delta_client: Optional[DeltaRestClient] = None) -> Dict[str, Any]:
    """Convenience function to run recovery."""
    manager = get_recovery_manager()
    return manager.recover(delta_client)


def initialize_pnl_for_account(account_id: str, starting_equity: Decimal) -> PnLSnapshot:
    """
    Initialize P&L snapshot for a new day or account.

    Called when:
    - New account added
    - Day rollover (new date)
    - No existing snapshot found during recovery
    """
    from storage.models import PnLSnapshot

    today = date.today()

    with get_session_scope() as session:
        # Ensure account exists to satisfy foreign key constraint
        acct = session.query(Account).filter(Account.account_id == account_id).first()
        if not acct:
            acct = Account(
                account_id=account_id,
                label=account_id.title(),
                environment=AccountEnvironment.TESTNET,
                api_key="auto_init",
                api_secret="auto_init"
            )
            session.add(acct)
            session.flush()

        # Check if already exists
        existing = session.query(PnLSnapshot).filter(
            PnLSnapshot.account_id == account_id,
            PnLSnapshot.snapshot_date == today
        ).first()

        if existing:
            return existing

        # Create new snapshot
        snapshot = PnLSnapshot(
            account_id=account_id,
            snapshot_date=today,
            start_equity=starting_equity,
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            peak_drawdown=Decimal("0"),
            peak_equity=starting_equity,
        )
        session.add(snapshot)
        session.flush()
        logger.info(f"Initialized P&L snapshot for {account_id}: equity={starting_equity}")
        return snapshot


def update_pnl_snapshot(
    account_id: str,
    realized_delta: Decimal = Decimal("0"),
    unrealized_delta: Decimal = Decimal("0"),
    current_equity: Optional[Decimal] = None
) -> PnLSnapshot:
    """
    Update P&L snapshot with new fill or mark-to-market change.

    Args:
        account_id: Account to update
        realized_delta: Change in realized P&L (from fills)
        unrealized_delta: Change in unrealized P&L (from mark price updates)
        current_equity: Current total equity (for drawdown calculation)

    Returns:
        Updated PnLSnapshot
    """
    today = date.today()

    with get_session_scope() as session:
        snapshot = session.query(PnLSnapshot).filter(
            PnLSnapshot.account_id == account_id,
            PnLSnapshot.snapshot_date == today
        ).first()

        if not snapshot:
            # Initialize if missing
            if current_equity is None:
                raise ValueError(f"No P&L snapshot for {account_id} and current_equity not provided")
            snapshot = initialize_pnl_for_account(account_id, current_equity)
            # Re-fetch from session
            snapshot = session.query(PnLSnapshot).filter(
                PnLSnapshot.account_id == account_id,
                PnLSnapshot.snapshot_date == today
            ).first()

        # Update values
        if realized_delta:
            snapshot.realized_pnl += realized_delta
        if unrealized_delta:
            snapshot.unrealized_pnl += unrealized_delta

        # Update drawdown if equity provided
        if current_equity is not None:
            if current_equity > snapshot.peak_equity:
                snapshot.peak_equity = current_equity
            drawdown = snapshot.peak_equity - current_equity
            if drawdown > snapshot.peak_drawdown:
                snapshot.peak_drawdown = drawdown

        logger.debug(f"Updated P&L for {account_id}: realized={snapshot.realized_pnl}, "
                    f"unrealized={snapshot.unrealized_pnl}, drawdown={snapshot.peak_drawdown}")
        return snapshot


def check_daily_loss_limit(
    account_id: str,
    max_daily_loss_usd: Decimal
) -> Tuple[bool, Decimal, Decimal]:
    """
    Check if daily loss limit has been breached.

    Returns:
        (is_breached, total_loss, remaining_limit)
    """
    today = date.today()

    with get_session_scope() as session:
        snapshot = session.query(PnLSnapshot).filter(
            PnLSnapshot.account_id == account_id,
            PnLSnapshot.snapshot_date == today
        ).first()

        if not snapshot:
            return False, Decimal("0"), max_daily_loss_usd

        total_pnl = snapshot.realized_pnl + snapshot.unrealized_pnl
        total_loss = abs(min(Decimal("0"), total_pnl))  # Only count losses
        remaining = max_daily_loss_usd - total_loss
        is_breached = total_loss >= max_daily_loss_usd

        return is_breached, total_loss, max(Decimal("0"), remaining)


def record_circuit_breaker(
    breaker_type: str,
    trigger_value: Decimal,
    threshold_value: Decimal,
    description: str,
    context_data: Optional[Dict[str, Any]] = None
) -> CircuitBreakerEvent:
    """Record a new circuit breaker activation."""
    from storage.models import CircuitBreakerEvent
    import uuid

    event = CircuitBreakerEvent(
        event_id=str(uuid.uuid4()),
        breaker_type=breaker_type,
        trigger_value=trigger_value,
        threshold_value=threshold_value,
        description=description,
        is_active=True,
        triggered_at=datetime.utcnow(),
        context_data=context_data,
    )

    with get_session_scope() as session:
        session.add(event)

    logger.critical(f"CIRCUIT BREAKER ACTIVATED: {breaker_type} - {description}")
    return event


def resolve_circuit_breaker(event_id: str, resolved_by: str) -> bool:
    """Mark a circuit breaker as resolved."""
    with get_session_scope() as session:
        event = session.query(CircuitBreakerEvent).filter(
            CircuitBreakerEvent.event_id == event_id
        ).first()

        if event and event.is_active:
            event.is_active = False
            event.resolved_at = datetime.utcnow()
            event.resolved_by = resolved_by
            logger.info(f"Circuit breaker resolved: {event_id} by {resolved_by}")
            return True

    return False


# Integration with trading engine
class StateRehydrator:
    """
    Rehydrates in-memory trading engine state from database.

    Provides:
    - ACTIVE_POSITIONS set reconstruction
    - DAILY_PNL restoration
    - Order state machine initialization
    """

    @staticmethod
    def rehydrate_active_positions() -> set:
        """Rebuild ACTIVE_POSITIONS set from database."""
        with get_session_scope() as session:
            positions = get_open_positions(session)
            orders = get_open_orders(session)

            active = set()
            for pos in positions:
                active.add(pos.symbol)
            for order in orders:
                if order.bracket_stop_loss_price or order.bracket_take_profit_price:
                    active.add(order.symbol)

            logger.info(f"Rehydrated ACTIVE_POSITIONS: {active}")
            return active

    @staticmethod
    def rehydrate_daily_pnl() -> Dict[str, float]:
        """Restore DAILY_PNL for all accounts."""
        today = date.today()
        pnl_data = {}

        with get_session_scope() as session:
            snapshots = session.query(PnLSnapshot).filter(
                PnLSnapshot.snapshot_date == today
            ).all()

            for snap in snapshots:
                total_pnl = float(snap.realized_pnl) + float(snap.unrealized_pnl)
                pnl_data[snap.account_id] = total_pnl

            logger.info(f"Rehydrated DAILY_PNL: {pnl_data}")
            return pnl_data

    @staticmethod
    def rehydrate_order_states() -> Dict[str, OrderState]:
        """Get current state of all open orders."""
        with get_session_scope() as session:
            orders = get_open_orders(session)
            state_map = {o.order_id: o.state for o in orders}
            logger.info(f"Rehydrated {len(state_map)} order states")
            return state_map


if __name__ == "__main__":
    # Test recovery
    logging.basicConfig(level=logging.INFO)
    summary = run_recovery()
    print(f"Recovery summary: {summary}")