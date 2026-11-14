"""
Central Risk Management Engine (FAIL-CLOSED)

Authoritative risk gateway that validates all TradeSignals before they can reach
the Execution Gateway. Every trade MUST pass this 11-step verification pipeline.
Zero orders may touch the exchange without a verified RiskDecision(approved=True).

If any check fails or if any unexpected exception occurs, the gateway fails CLOSED:
emitting RiskDecision(approved=False) with comprehensive diagnostic logs.
"""
from __future__ import annotations

import uuid
import logging
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Any, Set, Tuple

from analytics.signal import TradeSignal
from config.environments import TradingEnvironment, EnvironmentConfig
from config.settings import RiskSettings, get_settings
from risk.position_sizing import PositionSizer, SizingResult
from risk.pnl_tracker import PnLTracker
from risk.circuit_breaker import CircuitBreakerManager

logger = logging.getLogger(__name__)


class RiskDecisionResult:
    """In-memory representation of a completed risk evaluation."""
    def __init__(
        self,
        decision_id: str,
        signal_id: str,
        approved: bool,
        rejection_reason: Optional[str] = None,
        calculated_size: int = 0,
        allocated_risk_usd: Decimal = Decimal("0.0"),
        margin_required_usd: Decimal = Decimal("0.0"),
        checks_performed: Optional[Dict[str, Any]] = None,
        evaluated_at: Optional[datetime] = None,
    ):
        self.decision_id = decision_id
        self.signal_id = signal_id
        self.approved = approved
        self.rejection_reason = rejection_reason
        self.calculated_size = calculated_size
        self.allocated_risk_usd = allocated_risk_usd
        self.margin_required_usd = margin_required_usd
        self.checks_performed = checks_performed or {}
        self.evaluated_at = evaluated_at or datetime.now()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "signal_id": self.signal_id,
            "approved": self.approved,
            "rejection_reason": self.rejection_reason,
            "calculated_size": self.calculated_size,
            "allocated_risk_usd": float(self.allocated_risk_usd),
            "margin_required_usd": float(self.margin_required_usd),
            "checks_performed": self.checks_performed,
            "evaluated_at": self.evaluated_at.isoformat(),
        }


class CentralRiskManager:
    """
    Authoritative Central Risk Gateway.
    """

    def __init__(
        self,
        env_config: Optional[EnvironmentConfig] = None,
        risk_settings: Optional[RiskSettings] = None,
        position_sizer: Optional[PositionSizer] = None,
        pnl_tracker: Optional[PnLTracker] = None,
        circuit_breaker: Optional[CircuitBreakerManager] = None,
    ):
        self.settings = risk_settings or get_settings().risk
        self.env_config = env_config or EnvironmentConfig(TradingEnvironment.TESTNET)
        self.sizer = position_sizer or PositionSizer(
            default_risk_pct=self.settings.max_risk_per_trade_pct,
            default_leverage=self.settings.max_leverage,
        )
        self.pnl_tracker = pnl_tracker or PnLTracker()
        self.circuit_breaker = circuit_breaker or CircuitBreakerManager(
            heartbeat_timeout_sec=self.settings.heartbeat_timeout_sec,
            rejection_limit=self.settings.consecutive_rejection_limit,
        )

    def evaluate_signal(
        self,
        signal: TradeSignal,
        account_id: str,
        account_equity: Decimal,
        available_margin: Decimal,
        current_quote: Optional[Any],
        open_positions: Optional[List[Dict[str, Any]]] = None,
        active_orders: Optional[List[Dict[str, Any]]] = None,
        instrument_specs: Optional[Dict[str, Any]] = None,
    ) -> RiskDecisionResult:
        """
        Execute 11-step fail-closed risk verification on incoming TradeSignal.
        """
        decision_id = f"dec_{uuid.uuid4().hex[:12]}"
        checks: Dict[str, Any] = {}

        try:
            # ──────────────────────────────────────────────
            # Step 1: Environment Check
            # ──────────────────────────────────────────────
            if not self.env_config.environment.allows_auto_trading:
                return self._reject(
                    decision_id, signal.signal_id, checks,
                    f"Automated trading not permitted in environment {self.env_config.environment.value}",
                    step="1_environment"
                )
            if self.env_config.environment == TradingEnvironment.LIVE_AUTO and not self.env_config.live_auto_authorized:
                return self._reject(
                    decision_id, signal.signal_id, checks,
                    "LIVE_AUTO environment requires explicit authorization (LIVE_AUTO_AUTHORIZED=true)",
                    step="1_environment"
                )
            checks["1_environment"] = {"passed": True, "mode": self.env_config.environment.value}

            # ──────────────────────────────────────────────
            # Step 2: Circuit Breaker Check
            # ──────────────────────────────────────────────
            if self.circuit_breaker.is_tripped():
                active = self.circuit_breaker.get_active_breakers()
                return self._reject(
                    decision_id, signal.signal_id, checks,
                    f"Trading halted: active circuit breaker ({[b['breaker_type'] for b in active]})",
                    step="2_circuit_breaker",
                    extra={"active_breakers": active}
                )
            checks["2_circuit_breaker"] = {"passed": True}

            # ──────────────────────────────────────────────
            # Step 3: Quote Freshness & Sanity Check
            # ──────────────────────────────────────────────
            if current_quote is None:
                return self._reject(
                    decision_id, getattr(signal, 'signal_id', 'unknown'), checks,
                    f"Missing market quote for {signal.symbol if signal else 'unknown'}",
                    step="3_quote_freshness"
                )

            # Check timestamp/validity
            q_time = getattr(current_quote, 'timestamp', None) or (
                current_quote.get('timestamp') if isinstance(current_quote, dict) else None
            )
            if q_time is not None:
                now_ts = datetime.now()
                age_ms = (now_ts - q_time).total_seconds() * 1000.0
                if age_ms > self.settings.quote_freshness_max_ms:
                    return self._reject(
                        decision_id, signal.signal_id, checks,
                        f"Market quote stale: age {age_ms:.1f}ms > {self.settings.quote_freshness_max_ms}ms limit",
                        step="3_quote_freshness"
                    )

            bid = getattr(current_quote, 'bid', None) or (current_quote.get('bid') if isinstance(current_quote, dict) else None)
            ask = getattr(current_quote, 'ask', None) or (current_quote.get('ask') if isinstance(current_quote, dict) else None)
            if bid is not None and ask is not None:
                if float(bid) <= 0 or float(ask) <= 0 or float(bid) >= float(ask):
                    return self._reject(
                        decision_id, signal.signal_id, checks,
                        f"Inverted or non-positive orderbook quote: bid={bid}, ask={ask}",
                        step="3_quote_freshness"
                    )
            checks["3_quote_freshness"] = {"passed": True}

            # ──────────────────────────────────────────────
            # Step 4: Daily Loss Cap & Drawdown Check
            # ──────────────────────────────────────────────
            breached, loss_msg = self.pnl_tracker.is_daily_loss_breached(
                account_id=account_id,
                max_loss_usd=self.settings.max_daily_loss_usd,
                max_loss_pct=self.settings.max_daily_loss_pct,
            )
            if breached:
                # Automatically trip the circuit breaker
                self.circuit_breaker.trip(
                    breaker_type=CircuitBreakerManager.BREAKER_DAILY_LOSS,
                    trigger_value=0,
                    threshold_value=self.settings.max_daily_loss_usd,
                    description=loss_msg or "Daily loss cap breach",
                )
                return self._reject(
                    decision_id, signal.signal_id, checks,
                    f"Risk Gate: {loss_msg}",
                    step="4_daily_loss"
                )
            checks["4_daily_loss"] = {"passed": True}

            # ──────────────────────────────────────────────
            # Step 5: Max Open Positions Check
            # ──────────────────────────────────────────────
            positions = open_positions or []
            if len(positions) >= self.settings.max_open_positions:
                return self._reject(
                    decision_id, signal.signal_id, checks,
                    f"Max open positions reached ({len(positions)}/{self.settings.max_open_positions})",
                    step="5_max_positions"
                )
            checks["5_max_positions"] = {"passed": True, "count": len(positions)}

            # ──────────────────────────────────────────────
            # Step 6: Duplicate Position / Order Check
            # ──────────────────────────────────────────────
            active_symbols = {p.get('symbol') for p in positions}
            orders = active_orders or []
            active_order_symbols = {o.get('symbol') for o in orders}

            if signal.symbol in active_symbols:
                return self._reject(
                    decision_id, signal.signal_id, checks,
                    f"Active position already open for {signal.symbol}",
                    step="6_duplicate_position"
                )
            if signal.symbol in active_order_symbols:
                return self._reject(
                    decision_id, signal.signal_id, checks,
                    f"Pending order already exists for {signal.symbol}",
                    step="6_duplicate_position"
                )
            checks["6_duplicate_position"] = {"passed": True}

            # ──────────────────────────────────────────────
            # Step 7: Stop-Loss Sanity Check
            # ──────────────────────────────────────────────
            entry = Decimal(str(signal.target_entry_price))
            sl = Decimal(str(signal.stop_loss_price))
            tp = Decimal(str(signal.take_profit_price))

            if sl <= 0:
                return self._reject(
                    decision_id, signal.signal_id, checks,
                    "Stop-loss price must be specified and > 0",
                    step="7_stop_loss_sanity"
                )

            if signal.direction == "BUY" and sl >= entry:
                return self._reject(
                    decision_id, signal.signal_id, checks,
                    f"Invalid BUY stop-loss: SL ({sl}) must be below Entry ({entry})",
                    step="7_stop_loss_sanity"
                )
            if signal.direction == "SELL" and sl <= entry:
                return self._reject(
                    decision_id, signal.signal_id, checks,
                    f"Invalid SELL stop-loss: SL ({sl}) must be above Entry ({entry})",
                    step="7_stop_loss_sanity"
                )

            sl_pct = abs(entry - sl) / entry
            if sl_pct < Decimal("0.001"):  # < 0.1% is too tight for crypto
                return self._reject(
                    decision_id, signal.signal_id, checks,
                    f"Stop-loss distance ({float(sl_pct)*100:.2f}%) is dangerously tight (< 0.1%)",
                    step="7_stop_loss_sanity"
                )
            if sl_pct > Decimal("0.25"):  # > 25% is too wide
                return self._reject(
                    decision_id, signal.signal_id, checks,
                    f"Stop-loss distance ({float(sl_pct)*100:.2f}%) exceeds safety ceiling (25%)",
                    step="7_stop_loss_sanity"
                )
            checks["7_stop_loss_sanity"] = {"passed": True, "sl_distance_pct": float(sl_pct)}

            # ──────────────────────────────────────────────
            # Step 8: Dynamic Contract-Aware Sizing
            # ──────────────────────────────────────────────
            specs = instrument_specs or {}
            multiplier = Decimal(str(specs.get('contract_value', 1.0)))
            lot_size = int(specs.get('lot_size', 1))

            sizing: SizingResult = self.sizer.calculate_size(
                account_equity=account_equity,
                available_margin=available_margin,
                entry_price=entry,
                stop_loss_price=sl,
                contract_multiplier=multiplier,
                lot_size=lot_size,
                proposed_size=signal.proposed_size,
            )

            if not sizing.is_valid or sizing.size <= 0:
                return self._reject(
                    decision_id, signal.signal_id, checks,
                    f"Position sizing rejected: {sizing.reason}",
                    step="8_position_sizing"
                )
            checks["8_position_sizing"] = {
                "passed": True,
                "calculated_size": sizing.size,
                "allocated_risk": float(sizing.allocated_risk_usd),
                "margin_required": float(sizing.margin_required_usd),
            }

            # ──────────────────────────────────────────────
            # Step 9: Max Per-Trade Risk Check
            # ──────────────────────────────────────────────
            max_risk_allowed = account_equity * Decimal(str(self.settings.max_risk_per_trade_pct))
            if sizing.allocated_risk_usd > max_risk_allowed * Decimal("1.05"):  # 5% tolerance for lot rounding
                return self._reject(
                    decision_id, signal.signal_id, checks,
                    f"Allocated trade risk (${float(sizing.allocated_risk_usd):.2f}) exceeds budget (${float(max_risk_allowed):.2f})",
                    step="9_max_trade_risk"
                )
            checks["9_max_trade_risk"] = {"passed": True, "risk_pct": float(sizing.allocated_risk_usd / account_equity)}

            # ──────────────────────────────────────────────
            # Step 10: Margin & Leverage Check
            # ──────────────────────────────────────────────
            if sizing.margin_required_usd > available_margin:
                return self._reject(
                    decision_id, signal.signal_id, checks,
                    f"Margin required (${float(sizing.margin_required_usd):.2f}) exceeds available free margin (${float(available_margin):.2f})",
                    step="10_margin_leverage"
                )
            checks["10_margin_leverage"] = {"passed": True}

            # ──────────────────────────────────────────────
            # Step 11: Slippage Tolerance Check
            # ──────────────────────────────────────────────
            sig_slippage = Decimal(str(signal.max_slippage_pct))
            max_allowed_slip = Decimal(str(self.settings.max_slippage_pct))
            if sig_slippage > max_allowed_slip:
                return self._reject(
                    decision_id, signal.signal_id, checks,
                    f"Signal max slippage ({float(sig_slippage)*100:.2f}%) exceeds policy ({float(max_allowed_slip)*100:.2f}%)",
                    step="11_slippage"
                )
            checks["11_slippage"] = {"passed": True}

            # ──────────────────────────────────────────────
            # ALL 11 CHECKS PASSED -> APPROVE ORDER INTENT
            # ──────────────────────────────────────────────
            logger.info(
                f"✅ RISK GATE APPROVED: Signal {signal.signal_id} on {signal.symbol} "
                f"Size={sizing.size} Risk=${float(sizing.allocated_risk_usd):.2f} Margin=${float(sizing.margin_required_usd):.2f}"
            )
            return RiskDecisionResult(
                decision_id=decision_id,
                signal_id=signal.signal_id,
                approved=True,
                rejection_reason=None,
                calculated_size=sizing.size,
                allocated_risk_usd=sizing.allocated_risk_usd,
                margin_required_usd=sizing.margin_required_usd,
                checks_performed=checks,
            )

        except Exception as e:
            # FAIL-CLOSED: Any unforeseen runtime exception rejects the signal
            logger.critical(f"🚨 Risk Gateway UNHANDLED EXCEPTION (Failing closed): {e}", exc_info=True)
            sig_id = getattr(signal, 'signal_id', 'unknown') if signal else 'unknown'
            return self._reject(
                decision_id, sig_id, checks,
                f"Fail-closed on system error: {str(e)}",
                step="exception"
            )

    def _reject(
        self,
        decision_id: str,
        signal_id: str,
        checks: Dict[str, Any],
        reason: str,
        step: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> RiskDecisionResult:
        """Helper to create fail-closed rejected decision."""
        checks[step] = {"passed": False, "reason": reason}
        if extra:
            checks[step].update(extra)

        logger.warning(f"🛑 RISK GATE REJECTED [{step}]: {reason}")
        return RiskDecisionResult(
            decision_id=decision_id,
            signal_id=signal_id,
            approved=False,
            rejection_reason=reason,
            calculated_size=0,
            allocated_risk_usd=Decimal("0.0"),
            margin_required_usd=Decimal("0.0"),
            checks_performed=checks,
        )
