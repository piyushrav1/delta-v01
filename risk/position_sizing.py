"""
Contract-Aware Position Sizing Engine

Calculates risk-adjusted lot quantities based on:
1. Account equity & max per-trade risk percentage
2. Distance to stop-loss & contract value multiplier
3. Available free margin & initial margin per contract (leverage)
4. Instrument specifications: lot size, tick size, contract multiplier
5. Strict mathematical boundaries (non-negative, lot step rounding)
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Optional, Dict, Any
import logging

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SizingResult:
    """Calculated position sizing output."""
    size: int
    allocated_risk_usd: Decimal
    margin_required_usd: Decimal
    max_loss_usd: Decimal
    is_valid: bool
    reason: Optional[str] = None


class PositionSizer:
    """
    Contract-Aware Position Sizing Calculator.

    Formulas:
        MaxRiskUSD = AccountEquity * RiskPctPerTrade
        UnitLossUSD = |EntryPrice - StopLossPrice| * ContractMultiplier
        FeeUSD = EntryPrice * FeeRate * ContractMultiplier
        Size_risk = floor(MaxRiskUSD / (UnitLossUSD + FeeUSD))
        InitialMargin = (EntryPrice * ContractMultiplier) / Leverage
        Size_margin = floor((AvailableMargin * MaxMarginPct) / InitialMargin)
        FinalLots = max(0, min(Size_risk, Size_margin, MaxAllowedLots))
    """

    def __init__(
        self,
        default_risk_pct: float = 0.01,         # 1% equity risk per trade
        max_margin_pct: float = 0.50,           # Max 50% available margin per trade
        default_leverage: float = 10.0,         # 10x default leverage
        taker_fee_rate: float = 0.0005,         # 0.05% Delta taker fee
        max_allowed_lots: int = 100,            # Safety ceiling on lots
    ):
        self.default_risk_pct = Decimal(str(default_risk_pct))
        self.max_margin_pct = Decimal(str(max_margin_pct))
        self.default_leverage = Decimal(str(default_leverage))
        self.taker_fee_rate = Decimal(str(taker_fee_rate))
        self.max_allowed_lots = int(max_allowed_lots)

    def calculate_size(
        self,
        account_equity: Decimal,
        available_margin: Decimal,
        entry_price: Decimal,
        stop_loss_price: Decimal,
        contract_multiplier: Decimal = Decimal("1.0"),
        lot_size: int = 1,
        leverage: Optional[Decimal] = None,
        risk_pct: Optional[Decimal] = None,
        proposed_size: Optional[int] = None,
    ) -> SizingResult:
        """
        Calculate safe, contract-aware lot size for an order intent.
        """
        # Ensure Decimal types
        equity = Decimal(str(account_equity))
        margin = Decimal(str(available_margin))
        entry = Decimal(str(entry_price))
        sl = Decimal(str(stop_loss_price))
        mult = Decimal(str(contract_multiplier))
        lev = Decimal(str(leverage)) if leverage is not None else self.default_leverage
        r_pct = Decimal(str(risk_pct)) if risk_pct is not None else self.default_risk_pct

        if equity <= 0:
            return SizingResult(0, Decimal("0"), Decimal("0"), Decimal("0"), False, "Zero or negative account equity")

        if margin <= 0:
            return SizingResult(0, Decimal("0"), Decimal("0"), Decimal("0"), False, "Zero or negative available margin")

        if entry <= 0:
            return SizingResult(0, Decimal("0"), Decimal("0"), Decimal("0"), False, "Invalid entry price <= 0")

        # 1. Stop-Loss distance check
        sl_dist = abs(entry - sl)
        if sl_dist <= 0:
            # Fallback to 2% default stop distance if SL equals entry or is missing
            sl_dist = entry * Decimal("0.02")

        # 2. Maximum risk budget in USD
        max_risk_usd = equity * r_pct

        # 3. Unit loss per contract (price movement loss + roundtrip taker fee)
        # Roundtrip fee = entry * fee_rate + (entry - sl_dist) * fee_rate ~ 2 * entry * fee_rate
        fee_buffer_usd = entry * (self.taker_fee_rate * Decimal("2.0")) * mult
        unit_loss_usd = (sl_dist * mult) + fee_buffer_usd

        if unit_loss_usd <= 0:
            return SizingResult(0, Decimal("0"), Decimal("0"), Decimal("0"), False, "Unit loss calculation resulted in <= 0")

        # Risk-based sizing
        size_risk_raw = max_risk_usd / unit_loss_usd
        size_risk = int(math.floor(float(size_risk_raw)))

        # 4. Margin-based sizing
        if lev <= 0:
            lev = Decimal("1.0")

        initial_margin_per_contract = (entry * mult) / lev
        if initial_margin_per_contract <= 0:
            return SizingResult(0, Decimal("0"), Decimal("0"), Decimal("0"), False, "Initial margin per contract is <= 0")

        usable_margin = margin * self.max_margin_pct
        size_margin_raw = usable_margin / initial_margin_per_contract
        size_margin = int(math.floor(float(size_margin_raw)))

        # 5. Combined bounds
        calculated_lots = min(size_risk, size_margin, self.max_allowed_lots)

        # 6. Apply proposed size ceiling if provided
        if proposed_size is not None and proposed_size > 0:
            calculated_lots = min(calculated_lots, proposed_size)

        # 7. Round down to nearest multiple of instrument lot_size
        step = max(1, lot_size)
        final_lots = (calculated_lots // step) * step

        if final_lots <= 0:
            reason = (
                f"Sizing yielded 0 lots (risk_lots={size_risk}, margin_lots={size_margin}, "
                f"lot_step={step}, max_risk=${float(max_risk_usd):.2f})"
            )
            return SizingResult(0, Decimal("0"), Decimal("0"), Decimal("0"), False, reason)

        # Compute actual risk & margin requirements
        allocated_risk = Decimal(str(round(final_lots * float(unit_loss_usd), 2)))
        margin_req = Decimal(str(round(final_lots * float(initial_margin_per_contract), 2)))
        max_loss = Decimal(str(round(final_lots * float(sl_dist * mult), 2)))

        return SizingResult(
            size=final_lots,
            allocated_risk_usd=allocated_risk,
            margin_required_usd=margin_req,
            max_loss_usd=max_loss,
            is_valid=True,
            reason=None,
        )

