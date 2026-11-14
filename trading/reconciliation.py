"""
Periodic Exchange State Reconciliation Engine

Continuously audits and synchronizes local order/position tables against
the authoritative live state on Delta Exchange.

Detects:
1. Orphan Exchange Orders: Resting orders on exchange not recorded locally
2. Ghost Local Orders: Open in local state machine but closed/cancelled on exchange
3. Position Mismatches: Local lot count diverges from exchange margined position
4. Unprotected Positions: Open positions lacking stop-loss bracket protection
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Any, Set, Tuple
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class ReconciliationReport:
    """Summary of exchange state reconciliation audit."""
    timestamp: datetime
    is_synced: bool
    reconciliation_required: bool
    discrepancies: List[str] = field(default_factory=list)
    orphan_orders: List[Dict[str, Any]] = field(default_factory=list)
    ghost_orders: List[str] = field(default_factory=list)
    position_mismatches: List[Dict[str, Any]] = field(default_factory=list)
    unprotected_positions: List[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.is_synced:
            return f"State fully reconciled (0 discrepancies at {self.timestamp.strftime('%H:%M:%S')})"
        return (
            f"RECONCILIATION REQUIRED: {len(self.discrepancies)} discrepancies "
            f"(Orphans: {len(self.orphan_orders)}, Ghosts: {len(self.ghost_orders)}, "
            f"Position Mismatches: {len(self.position_mismatches)}, "
            f"Unprotected: {len(self.unprotected_positions)})"
        )


class StateReconciler:
    """
    Audits and synchronizes local trading state with Delta Exchange.
    """

    def __init__(self, bracket_manager: Optional[Any] = None):
        self.bracket_manager = bracket_manager

    def reconcile(
        self,
        local_orders: List[Any],
        local_positions: List[Dict[str, Any]],
        exchange_orders: List[Dict[str, Any]],
        exchange_positions: List[Dict[str, Any]],
    ) -> ReconciliationReport:
        """
        Perform bidirectional state reconciliation between local tables and exchange.
        """
        now = datetime.now()
        discrepancies: List[str] = []
        orphan_orders: List[Dict[str, Any]] = []
        ghost_orders: List[str] = []
        position_mismatches: List[Dict[str, Any]] = []
        unprotected: List[str] = []

        # ──────────────────────────────────────────────
        # 1. Order Reconciliation
        # ──────────────────────────────────────────────
        # Map local active orders by exchange_order_id or client_order_id
        local_active_map: Dict[str, Any] = {}
        for lo in local_orders:
            ex_id = getattr(lo, 'exchange_order_id', None) or (lo.get('exchange_order_id') if isinstance(lo, dict) else None)
            cid = getattr(lo, 'client_order_id', None) or (lo.get('client_order_id') if isinstance(lo, dict) else None)
            oid = getattr(lo, 'order_id', None) or (lo.get('order_id') if isinstance(lo, dict) else None)

            is_act = getattr(lo, 'is_active', True)
            if is_act:
                if ex_id:
                    local_active_map[str(ex_id)] = lo
                if cid:
                    local_active_map[str(cid)] = lo

        # Map exchange open orders by ID and client_order_id
        ex_active_ids: Set[str] = set()
        for eo in exchange_orders:
            e_id = str(eo.get('id') or eo.get('order_id', ''))
            e_cid = str(eo.get('client_order_id') or '')
            ex_active_ids.add(e_id)
            if e_cid:
                ex_active_ids.add(e_cid)

            # Check if this exchange order is tracked locally
            matched = local_active_map.get(e_id) or (local_active_map.get(e_cid) if e_cid else None)
            if not matched:
                orphan_orders.append(eo)
                discrepancies.append(f"Orphan order detected on exchange: ID {e_id} ({eo.get('symbol')} {eo.get('side')})")

        # Check for ghost orders (active locally, but absent on exchange)
        for lo in local_orders:
            is_act = getattr(lo, 'is_active', True)
            if not is_act:
                continue

            ex_id = str(getattr(lo, 'exchange_order_id', '') or '')
            cid = str(getattr(lo, 'client_order_id', '') or '')
            oid = str(getattr(lo, 'order_id', '') or '')

            if ex_id and ex_id not in ex_active_ids and (not cid or cid not in ex_active_ids):
                ghost_orders.append(oid or ex_id)
                discrepancies.append(f"Ghost local order detected: {oid} (Exchange ID {ex_id} not found on exchange)")

        # ──────────────────────────────────────────────
        # 2. Position Reconciliation
        # ──────────────────────────────────────────────
        local_pos_by_sym: Dict[str, int] = {}
        for lp in local_positions:
            sym = lp.get('symbol')
            size = int(lp.get('size', 0))
            if sym:
                local_pos_by_sym[sym] = size

        ex_pos_by_sym: Dict[str, Dict[str, Any]] = {}
        for ep in exchange_positions:
            sym = ep.get('symbol')
            size = int(ep.get('size', 0))
            if sym and size != 0:
                ex_pos_by_sym[sym] = ep

        # Compare positions
        all_symbols = set(local_pos_by_sym.keys()).union(set(ex_pos_by_sym.keys()))
        for sym in all_symbols:
            l_sz = local_pos_by_sym.get(sym, 0)
            e_pos = ex_pos_by_sym.get(sym, {})
            e_sz = int(e_pos.get('size', 0))

            if l_sz != e_sz:
                mismatch = {
                    "symbol": sym,
                    "local_size": l_sz,
                    "exchange_size": e_sz,
                    "delta": e_sz - l_sz,
                }
                position_mismatches.append(mismatch)
                discrepancies.append(f"Position size mismatch on {sym}: local={l_sz} vs exchange={e_sz}")

            # 3. Check for unprotected open positions on exchange
            if e_sz != 0:
                e_side = "BUY" if e_sz > 0 else "SELL"
                has_stop = False
                for eo in exchange_orders:
                    if eo.get('symbol') == sym:
                        otype = str(eo.get('order_type', '')).lower()
                        s_type = str(eo.get('stop_order_type', '')).lower()
                        if "stop" in otype or "stop" in s_type or eo.get('bracket_order') or eo.get('stop_price'):
                            has_stop = True
                            break
                if not has_stop:
                    unprotected.append(sym)
                    discrepancies.append(f"Unprotected position on exchange: {sym} size={e_sz} has no stop-loss order")

        is_synced = len(discrepancies) == 0
        return ReconciliationReport(
            timestamp=now,
            is_synced=is_synced,
            reconciliation_required=not is_synced,
            discrepancies=discrepancies,
            orphan_orders=orphan_orders,
            ghost_orders=ghost_orders,
            position_mismatches=position_mismatches,
            unprotected_positions=unprotected,
        )

