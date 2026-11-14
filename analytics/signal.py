"""
Canonical TradeSignal Data Contract

Strict dataclass for trade signals prohibiting strategies from touching
execution handles. All strategies MUST emit TradeSignal objects; raw
HTTP/API order placement is disallowed at the strategy level.

Signal lifecycle:
1. Strategy emits TradeSignal → 2. Risk Gateway validates → 3. Execution
   Gateway routes → 4. Order lifecycle state machine

Note: strategies MUST NEVER import from exchange/delta_client.py or
trading/execution_manager.py. Violation of this rule triggers a runtime
TypeError at strategy registration time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone, timedelta


@dataclass(frozen=True)
class TradeSignal:
    """
    Immutable trade signal contract.

    Attributes:
        signal_id: UUID4 — unique per signal emission
        strategy_id: e.g. "trend_following_btc"
        strategy_version: e.g. "1.2.0"
        symbol: e.g. "BTCUSD" or "C-BTC-70000-270926"
        direction: "BUY" or "SELL"
        order_type: "limit_order" or "market_order"
        target_entry_price: entry price target
        stop_loss_price: protective stop-loss
        take_profit_price: profit target
        proposed_size: strategy recommendation (subject to Risk Gate)
        max_slippage_pct: e.g. 0.005 (0.5%)
        expected_holding_period_sec: estimated trade duration
        timestamp: signal generation time
        expiry_time: discard signal if not filled by this time
        confidence: statistical score 0.0 to 1.0
        rationale: supporting indicator values
        legs: optional multi-leg option definition
    """
    signal_id: str
    strategy_id: str
    strategy_version: str
    symbol: str
    direction: str
    order_type: str
    target_entry_price: Decimal
    stop_loss_price: Decimal
    take_profit_price: Decimal
    proposed_size: Optional[int]
    max_slippage_pct: Decimal
    expected_holding_period_sec: int
    timestamp: datetime
    expiry_time: datetime
    confidence: Decimal
    rationale: Dict[str, Any]
    legs: Optional[List[Dict[str, Any]]] = None


# Convenience constructor for common signal types
def buy_signal(
    signal_id: str,
    strategy_id: str,
    strategy_version: str,
    symbol: str,
    entry_price: Decimal,
    stop_loss: Decimal,
    take_profit: Decimal,
    proposed_size: int,
    confidence: Decimal = Decimal("0.5"),
    rationale: Optional[Dict[str, Any]] = None,
) -> TradeSignal:
    """Build a BUY signal with typical parameters."""
    now = datetime.now()

    return TradeSignal(
        signal_id=signal_id,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        symbol=symbol,
        direction="BUY",
        order_type="limit_order",
        target_entry_price=entry_price,
        stop_loss_price=stop_loss,
        take_profit_price=take_profit,
        proposed_size=proposed_size,
        max_slippage_pct=Decimal("0.005"),
        expected_holding_period_sec=3600,
        timestamp=now,
        expiry_time=now + timedelta(hours=24),
        confidence=confidence,
        rationale=rationale or {},
    )


def sell_signal(
    signal_id: str,
    strategy_id: str,
    strategy_version: str,
    symbol: str,
    entry_price: Decimal,
    stop_loss: Decimal,
    take_profit: Decimal,
    proposed_size: int,
    confidence: Decimal = Decimal("0.5"),
    rationale: Optional[Dict[str, Any]] = None,
) -> TradeSignal:
    """Build a SELL signal with typical parameters."""
    now = datetime.now()

    return TradeSignal(
        signal_id=signal_id,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        symbol=symbol,
        direction="SELL",
        order_type="limit_order",
        target_entry_price=entry_price,
        stop_loss_price=stop_loss,
        take_profit_price=take_profit,
        proposed_size=proposed_size,
        max_slippage_pct=Decimal("0.005"),
        expected_holding_period_sec=3600,
        timestamp=now,
        expiry_time=now + timedelta(hours=24),
        confidence=confidence,
        rationale=rationale or {},
    )