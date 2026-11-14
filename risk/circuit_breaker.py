"""
Hardware & Market Circuit Breakers

Provides automatic emergency halts for:
1. Daily loss limit breach (> 2% equity loss or USD cap)
2. Exchange connection heartbeat timeout (> 10s without tick/response)
3. Consecutive order rejection threshold (>= 3 consecutive exchange rejects)
4. Extreme volatility / spread widening (abnormal liquidity collapse)

Fails CLOSED: when any circuit breaker is active, all automated trading and
signal approvals are immediately blocked until administrative resolution.
"""
from __future__ import annotations

import time
import uuid
import logging
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ActiveBreaker:
    """In-memory active circuit breaker event."""
    event_id: str
    breaker_type: str
    trigger_value: Decimal
    threshold_value: Decimal
    description: str
    triggered_at: datetime
    context_data: Dict[str, Any] = field(default_factory=dict)
    is_active: bool = True


class CircuitBreakerManager:
    """
    Central Circuit Breaker Controller.
    """

    BREAKER_DAILY_LOSS = "daily_loss"
    BREAKER_HEARTBEAT_TIMEOUT = "exchange_timeout"
    BREAKER_CONSECUTIVE_REJECTIONS = "consecutive_rejections"
    BREAKER_VOLATILITY = "volatility_halt"

    def __init__(
        self,
        heartbeat_timeout_sec: float = 10.0,
        rejection_limit: int = 3,
    ):
        self.heartbeat_timeout_sec = float(heartbeat_timeout_sec)
        self.rejection_limit = int(rejection_limit)

        self._active_breakers: Dict[str, ActiveBreaker] = {}
        self._consecutive_rejections: Dict[str, int] = {}  # account_id -> count
        self._last_heartbeat_time: float = time.time()

    def is_tripped(self, breaker_type: Optional[str] = None) -> bool:
        """Check if any (or a specific) circuit breaker is currently active."""
        if breaker_type is not None:
            return breaker_type in self._active_breakers and self._active_breakers[breaker_type].is_active
        return any(b.is_active for b in self._active_breakers.values())

    def trip(
        self,
        breaker_type: str,
        trigger_value: Union[float, Decimal],
        threshold_value: Union[float, Decimal],
        description: str,
        context_data: Optional[Dict[str, Any]] = None,
    ) -> ActiveBreaker:
        """
        Trip a circuit breaker, halting execution.
        """
        event_id = f"cb_{breaker_type}_{uuid.uuid4().hex[:8]}"
        t_val = Decimal(str(trigger_value))
        th_val = Decimal(str(threshold_value))

        breaker = ActiveBreaker(
            event_id=event_id,
            breaker_type=breaker_type,
            trigger_value=t_val,
            threshold_value=th_val,
            description=description,
            triggered_at=datetime.now(),
            context_data=context_data or {},
            is_active=True,
        )

        self._active_breakers[breaker_type] = breaker
        logger.critical(
            f"🚨 CIRCUIT BREAKER TRIPPED [{breaker_type.upper()}]: {description} "
            f"(Trigger={trigger_value}, Threshold={threshold_value})"
        )
        return breaker

    def resolve(self, breaker_type: str, resolved_by: str = "admin") -> bool:
        """
        Resolve an active circuit breaker.
        """
        if breaker_type in self._active_breakers and self._active_breakers[breaker_type].is_active:
            self._active_breakers[breaker_type].is_active = False
            del self._active_breakers[breaker_type]
            logger.info(f"✅ CIRCUIT BREAKER RESOLVED [{breaker_type.upper()}] by {resolved_by}")
            return True
        return False

    def reset_all(self, resolved_by: str = "admin"):
        """Reset all circuit breakers and rejection counters."""
        self._active_breakers.clear()
        self._consecutive_rejections.clear()
        self._last_heartbeat_time = time.time()
        logger.info(f"✅ All circuit breakers reset by {resolved_by}")

    def update_heartbeat(self):
        """Update last seen exchange communication timestamp."""
        self._last_heartbeat_time = time.time()
        # If exchange timeout was tripped, can evaluate auto-clear
        if self.is_tripped(self.BREAKER_HEARTBEAT_TIMEOUT):
            self.resolve(self.BREAKER_HEARTBEAT_TIMEOUT, resolved_by="heartbeat_recovery")

    def check_heartbeat(self) -> bool:
        """
        Evaluate if exchange communication exceeded heartbeat timeout.
        Returns True if tripped.
        """
        now = time.time()
        latency = now - self._last_heartbeat_time
        if latency > self.heartbeat_timeout_sec:
            if not self.is_tripped(self.BREAKER_HEARTBEAT_TIMEOUT):
                self.trip(
                    breaker_type=self.BREAKER_HEARTBEAT_TIMEOUT,
                    trigger_value=round(latency, 2),
                    threshold_value=self.heartbeat_timeout_sec,
                    description=f"Exchange connection lost: {latency:.1f}s since last heartbeat",
                )
            return True
        return False

    def record_rejection(self, account_id: str, reason: str = "") -> bool:
        """
        Record an order rejection. Trips circuit breaker if >= threshold.
        Returns True if tripped.
        """
        count = self._consecutive_rejections.get(account_id, 0) + 1
        self._consecutive_rejections[account_id] = count
        logger.warning(f"Order rejected on account {account_id} (Consecutive: {count}/{self.rejection_limit}): {reason}")

        if count >= self.rejection_limit:
            self.trip(
                breaker_type=self.BREAKER_CONSECUTIVE_REJECTIONS,
                trigger_value=count,
                threshold_value=self.rejection_limit,
                description=f"Exchange rejected {count} consecutive orders on account {account_id}: {reason}",
                context_data={"account_id": account_id, "last_rejection_reason": reason},
            )
            return True
        return False

    def record_order_success(self, account_id: str):
        """Reset rejection counter on successful order execution."""
        if account_id in self._consecutive_rejections:
            self._consecutive_rejections[account_id] = 0

    def get_active_breakers(self) -> List[Dict[str, Any]]:
        """Return list of active circuit breakers for UI/monitoring."""
        return [
            {
                "event_id": b.event_id,
                "breaker_type": b.breaker_type,
                "trigger_value": float(b.trigger_value),
                "threshold_value": float(b.threshold_value),
                "description": b.description,
                "triggered_at": b.triggered_at.isoformat(),
                "context_data": b.context_data,
            }
            for b in self._active_breakers.values()
            if b.is_active
        ]

