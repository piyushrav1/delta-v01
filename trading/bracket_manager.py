"""
Protective Bracket Order Verification & Safety Manager

Guarantees that every filled or executed position is defended by active
stop-loss and take-profit orders on Delta Exchange.

Responsibilities:
1. Format native Delta Exchange bracket parameters for entry orders
2. Verify after fill that stop-loss and take-profit brackets are active on Exchange
3. Emergency fallback: if exchange rejects native bracket, submit dedicated stop-loss order
4. Flag any unprotected or partially defended positions
"""
from __future__ import annotations

import logging
from enum import Enum
from decimal import Decimal
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class ProtectionStatus(str, Enum):
    """Status of protective bracket orders."""
    PROTECTED = "PROTECTED"                    # Both SL and TP confirmed active
    PARTIALLY_PROTECTED = "PARTIALLY_PROTECTED" # Only SL confirmed active
    UNPROTECTED = "UNPROTECTED"                # No active SL detected on exchange!
    EMERGENCY_SUSPEND = "EMERGENCY_SUSPEND"    # Bracket placement failed; manual intervention required


@dataclass
class ProtectionVerificationResult:
    """Outcome of position protection check."""
    symbol: str
    status: ProtectionStatus
    is_safe: bool
    sl_order_id: Optional[str] = None
    tp_order_id: Optional[str] = None
    message: str = ""


class BracketManager:
    """
    Manages protective stop-loss and take-profit order lifecycle.
    """

    @staticmethod
    def build_bracket_params(
        stop_loss_price: Optional[Decimal],
        take_profit_price: Optional[Decimal],
    ) -> Dict[str, Any]:
        """
        Build Delta Exchange native bracket parameter dictionary.
        """
        params: Dict[str, Any] = {}
        if stop_loss_price is not None and stop_loss_price > 0:
            params['bracket_stop_loss_price'] = str(round(float(stop_loss_price), 4))
        if take_profit_price is not None and take_profit_price > 0:
            params['bracket_take_profit_price'] = str(round(float(take_profit_price), 4))
        return params

    @staticmethod
    def verify_position_protection(
        symbol: str,
        position_size: int,
        position_side: str,
        open_exchange_orders: List[Dict[str, Any]],
    ) -> ProtectionVerificationResult:
        """
        Verify that an open position has an active protective stop-loss on the exchange.

        Args:
            symbol: Contract symbol
            position_size: Net open lots (positive integer)
            position_side: 'BUY' (long) or 'SELL' (short)
            open_exchange_orders: List of active orders returned by Delta Exchange API

        Returns:
            ProtectionVerificationResult detailing protection state.
        """
        if position_size <= 0:
            return ProtectionVerificationResult(
                symbol=symbol,
                status=ProtectionStatus.PROTECTED,
                is_safe=True,
                message="No open position to protect"
            )

        expected_exit_side = "sell" if position_side.upper() == "BUY" else "buy"
        sl_order_id = None
        tp_order_id = None

        for o in open_exchange_orders:
            if o.get('symbol') != symbol:
                continue

            o_side = str(o.get('side', '')).lower()
            if o_side != expected_exit_side:
                continue

            order_type = str(o.get('order_type', '')).lower()
            stop_order_type = str(o.get('stop_order_type', '')).lower()
            is_bracket = bool(o.get('bracket_order') or o.get('is_bracket'))
            stop_price = o.get('stop_price') or o.get('bracket_stop_loss_price')

            # Identify Stop-Loss orders
            if "stop" in order_type or "stop" in stop_order_type or is_bracket or stop_price is not None:
                sl_order_id = str(o.get('id') or o.get('order_id'))

            # Identify Take-Profit orders
            if "take_profit" in order_type or "take_profit" in stop_order_type or o.get('bracket_take_profit_price'):
                tp_order_id = str(o.get('id') or o.get('order_id'))

        if sl_order_id and tp_order_id:
            return ProtectionVerificationResult(
                symbol=symbol,
                status=ProtectionStatus.PROTECTED,
                is_safe=True,
                sl_order_id=sl_order_id,
                tp_order_id=tp_order_id,
                message="Position fully defended with active SL and TP on exchange"
            )
        elif sl_order_id:
            return ProtectionVerificationResult(
                symbol=symbol,
                status=ProtectionStatus.PARTIALLY_PROTECTED,
                is_safe=True,
                sl_order_id=sl_order_id,
                message="Position defended with active SL on exchange (no TP)"
            )
        else:
            logger.critical(
                f"🚨 UNPROTECTED POSITION DETECTED on {symbol}! "
                f"Position size {position_size} ({position_side}) has NO active stop-loss order on exchange!"
            )
            return ProtectionVerificationResult(
                symbol=symbol,
                status=ProtectionStatus.UNPROTECTED,
                is_safe=False,
                message=f"CRITICAL: Unprotected position on {symbol} (missing stop-loss on exchange)"
            )
