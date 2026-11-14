"""
Real-Time P&L & Drawdown Tracking Engine

Continuously computes:
1. Realized P&L from fills and trade exits
2. Unrealized P&L from open positions against live mark prices
3. Daily baseline equity, peak equity high-water mark, and peak drawdown %
4. Daily loss limit breach detection (Realized + Unrealized <= -MaxDailyLoss)
"""
from __future__ import annotations

import logging
from datetime import datetime, date
from decimal import Decimal
from typing import Dict, List, Optional, Tuple, Any

from storage.models import PnLSnapshot

logger = logging.getLogger(__name__)


class AccountPnLState:
    """In-memory state tracker for an individual trading account."""

    def __init__(self, account_id: str, starting_equity: Decimal):
        self.account_id = account_id
        self.starting_equity = Decimal(str(starting_equity))
        self.realized_pnl = Decimal("0.0")
        self.unrealized_pnl = Decimal("0.0")
        self.peak_equity = Decimal(str(starting_equity))
        self.peak_drawdown = Decimal("0.0")
        self.last_update_date = datetime.now().date()

    @property
    def current_equity(self) -> Decimal:
        """Current account equity = Start + Realized + Unrealized."""
        return self.starting_equity + self.realized_pnl + self.unrealized_pnl

    @property
    def total_daily_pnl(self) -> Decimal:
        """Total daily P&L = Realized + Unrealized."""
        return self.realized_pnl + self.unrealized_pnl

    @property
    def current_drawdown_pct(self) -> Decimal:
        """Drawdown from high-water mark: (Peak - Current) / Peak."""
        if self.peak_equity <= 0:
            return Decimal("0.0")
        dd_usd = max(Decimal("0.0"), self.peak_equity - self.current_equity)
        return Decimal(str(round(float(dd_usd / self.peak_equity), 4)))

    def check_and_reset_day(self, current_equity: Optional[Decimal] = None):
        """Reset baseline on a new calendar day."""
        today = datetime.now().date()
        if today != self.last_update_date:
            logger.info(f"Account {self.account_id}: New trading day detected ({today}). Rolling baseline.")
            if current_equity is not None:
                self.starting_equity = current_equity
            else:
                self.starting_equity = self.current_equity

            self.realized_pnl = Decimal("0.0")
            self.unrealized_pnl = Decimal("0.0")
            self.peak_equity = self.starting_equity
            self.peak_drawdown = Decimal("0.0")
            self.last_update_date = today

    def record_fill(
        self,
        side: str,
        fill_price: Decimal,
        fill_size: int,
        entry_price: Decimal,
        contract_multiplier: Decimal = Decimal("1.0"),
        fee_paid: Decimal = Decimal("0.0"),
    ) -> Decimal:
        """
        Record fill realized P&L when closing or reducing an existing position.
        """
        self.check_and_reset_day()

        p_fill = Decimal(str(fill_price))
        p_entry = Decimal(str(entry_price))
        mult = Decimal(str(contract_multiplier))
        size = Decimal(str(fill_size))
        fee = Decimal(str(fee_paid))

        # If side == SELL, we closed a LONG: PnL = (fill - entry) * size * mult - fee
        # If side == BUY, we closed a SHORT: PnL = (entry - fill) * size * mult - fee
        if side.upper() == "SELL":
            pnl = ((p_fill - p_entry) * size * mult) - fee
        else:
            pnl = ((p_entry - p_fill) * size * mult) - fee

        self.realized_pnl += pnl

        # Update high-water mark
        curr_eq = self.current_equity
        if curr_eq > self.peak_equity:
            self.peak_equity = curr_eq

        # Update peak drawdown
        dd_usd = max(Decimal("0.0"), self.peak_equity - curr_eq)
        if dd_usd > self.peak_drawdown:
            self.peak_drawdown = dd_usd

        return pnl

    def update_unrealized_pnl(
        self,
        open_positions: List[Dict[str, Any]],
        quotes: Dict[str, Any],
    ) -> Decimal:
        """
        Recompute net unrealized P&L across all open positions.
        """
        self.check_and_reset_day()
        net_unrealized = Decimal("0.0")

        for pos in open_positions:
            symbol = pos.get('symbol')
            size = Decimal(str(pos.get('size', 0)))
            entry = Decimal(str(pos.get('entry_price', 0)))
            side = pos.get('side', 'BUY').upper()
            mult = Decimal(str(pos.get('contract_multiplier', 1.0)))

            if size == 0 or entry == 0 or symbol not in quotes:
                continue

            q = quotes[symbol]
            mark = getattr(q, 'mark', None) or getattr(q, 'mid', None)
            if mark is None and isinstance(q, dict):
                mark = q.get('mark') or q.get('mid') or q.get('close')

            if mark is None or float(mark) <= 0:
                continue

            p_mark = Decimal(str(mark))
            if side == "BUY":
                pos_pnl = (p_mark - entry) * size * mult
            else:
                pos_pnl = (entry - p_mark) * size * mult

            net_unrealized += pos_pnl

        self.unrealized_pnl = net_unrealized

        # Update high-water mark & peak drawdown
        curr_eq = self.current_equity
        if curr_eq > self.peak_equity:
            self.peak_equity = curr_eq

        dd_usd = max(Decimal("0.0"), self.peak_equity - curr_eq)
        if dd_usd > self.peak_drawdown:
            self.peak_drawdown = dd_usd

        return self.unrealized_pnl


class PnLTracker:
    """
    Central Real-Time P&L & Drawdown Tracking Engine.
    """

    def __init__(self, default_start_equity: float = 10000.0):
        self.default_start_equity = Decimal(str(default_start_equity))
        self._accounts: Dict[str, AccountPnLState] = {}

    def get_or_create_account(self, account_id: str, equity: Optional[Decimal] = None) -> AccountPnLState:
        """Get or initialize P&L state for an account."""
        if account_id not in self._accounts:
            eq = equity if equity is not None else self.default_start_equity
            self._accounts[account_id] = AccountPnLState(account_id, eq)
        return self._accounts[account_id]

    def record_fill(
        self,
        account_id: str,
        side: str,
        fill_price: Decimal,
        fill_size: int,
        entry_price: Decimal,
        contract_multiplier: Decimal = Decimal("1.0"),
        fee_paid: Decimal = Decimal("0.0"),
    ) -> Decimal:
        acct = self.get_or_create_account(account_id)
        return acct.record_fill(side, fill_price, fill_size, entry_price, contract_multiplier, fee_paid)

    def update_positions_mark(
        self,
        account_id: str,
        positions: List[Dict[str, Any]],
        quotes: Dict[str, Any],
    ) -> Decimal:
        acct = self.get_or_create_account(account_id)
        return acct.update_unrealized_pnl(positions, quotes)

    def get_pnl_summary(self, account_id: str) -> Dict[str, Any]:
        acct = self.get_or_create_account(account_id)
        return {
            "account_id": account_id,
            "starting_equity": float(acct.starting_equity),
            "current_equity": float(acct.current_equity),
            "realized_pnl": float(acct.realized_pnl),
            "unrealized_pnl": float(acct.unrealized_pnl),
            "total_pnl": float(acct.total_daily_pnl),
            "peak_equity": float(acct.peak_equity),
            "peak_drawdown_usd": float(acct.peak_drawdown),
            "drawdown_pct": float(acct.current_drawdown_pct),
        }

    def is_daily_loss_breached(
        self,
        account_id: str,
        max_loss_usd: float = 500.0,
        max_loss_pct: float = 0.02,
    ) -> Tuple[bool, Optional[str]]:
        """
        Check if total daily loss (Realized + Unrealized) violates risk limit.
        """
        acct = self.get_or_create_account(account_id)
        tot_pnl = acct.total_daily_pnl
        loss_usd = -tot_pnl if tot_pnl < 0 else Decimal("0.0")

        # 1. Absolute USD loss check
        if loss_usd >= Decimal(str(max_loss_usd)):
            msg = f"Daily loss cap reached: ${float(loss_usd):.2f} loss exceeds limit ${max_loss_usd:.2f}"
            return True, msg

        # 2. Percentage loss check
        if acct.starting_equity > 0:
            loss_pct = loss_usd / acct.starting_equity
            if loss_pct >= Decimal(str(max_loss_pct)):
                msg = f"Daily drawdown cap reached: {float(loss_pct)*100:.2f}% loss exceeds limit {max_loss_pct*100:.2f}%"
                return True, msg

        return False, None

