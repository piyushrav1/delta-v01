"""
Simulated Paper Broker

Institutional Paper Trading Engine providing a 100% drop-in replacement
for DeltaRestClient. Executes against real-time order books and ticks
with realistic fee modeling, bracket execution, and slippage simulation,
without transmitting any orders to live exchange endpoints.

Features:
- Identical method signatures and response structures as DeltaRestClient
- Real-time L2 order book and BBO matching engine
- Passive limit fills at bid/ask with 0.02% Maker fee
- Aggressive crosses and market orders with 0.05% Taker fee and configurable slippage
- Automated bracket order monitoring (Stop Loss and Take Profit triggers)
- Client Order ID idempotency enforcement
- Comprehensive wallet balance and open position reporting matching Delta v2 API
"""
from __future__ import annotations

import uuid
import logging
from decimal import Decimal
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple, Union

from simulation.paper_ledger import (
    PaperLedger, PaperOrderModel, PaperPositionModel, PaperLedgerModel
)

logger = logging.getLogger(__name__)


class PaperBroker:
    """
    Simulated Paper Broker for Delta Exchange India.

    Drop-in alternative to DeltaRestClient that operates exclusively in-memory/SQLite.
    """

    def __init__(
        self,
        account_id: str = "paper_account",
        paper_ledger: Optional[PaperLedger] = None,
        maker_fee_pct: Union[Decimal, float] = Decimal("0.0002"),  # 0.02%
        taker_fee_pct: Union[Decimal, float] = Decimal("0.0005"),  # 0.05%
        slippage_pct: Union[Decimal, float] = Decimal("0.0001"),   # 0.01%
        contract_values: Optional[Dict[str, Union[Decimal, float]]] = None,
        initial_balance: Union[Decimal, float] = Decimal("10000.0"),
        account: Optional[Any] = None,
    ):
        self.account_id = getattr(account, "account_id", account_id) if account else account_id
        self.maker_fee_pct = Decimal(str(maker_fee_pct))
        self.taker_fee_pct = Decimal(str(taker_fee_pct))
        self.slippage_pct = Decimal(str(slippage_pct))

        self.contract_values: Dict[str, Decimal] = {}
        if contract_values:
            for k, v in contract_values.items():
                self.contract_values[k] = Decimal(str(v))

        self.ledger = paper_ledger or PaperLedger(
            account_id=self.account_id,
            initial_balance=initial_balance,
        )

        # In-memory quote cache: symbol -> {'bid': Decimal, 'ask': Decimal, 'mark': Decimal, 'timestamp': datetime}
        self._quotes: Dict[str, Dict[str, Any]] = {}

    def get_contract_value(self, symbol: str) -> Decimal:
        """Get contract value multiplier for symbol (defaults to 1.0)."""
        return self.contract_values.get(symbol, Decimal("1.0"))

    # ──────────────────────────────────────────────
    # Market Data & Matching Engine
    # ──────────────────────────────────────────────
    def set_market_quote(
        self,
        symbol: str,
        best_bid: Union[float, Decimal],
        best_ask: Union[float, Decimal],
        mark_price: Optional[Union[float, Decimal]] = None,
    ) -> None:
        """Seed or update quote cache for symbol."""
        bid = Decimal(str(best_bid))
        ask = Decimal(str(best_ask))
        mark = Decimal(str(mark_price)) if mark_price is not None else (bid + ask) / Decimal("2.0")

        self._quotes[symbol] = {
            "bid": bid,
            "ask": ask,
            "mark": mark,
            "timestamp": datetime.utcnow(),
        }

    def on_orderbook_l2(
        self,
        symbol: str,
        bids: List[Tuple[Union[float, Decimal], Union[float, Decimal]]],
        asks: List[Tuple[Union[float, Decimal], Union[float, Decimal]]],
        mark_price: Optional[Union[float, Decimal]] = None,
    ) -> List[Dict[str, Any]]:
        """Update order book from L2 snapshot/update and trigger order matching."""
        if not bids or not asks:
            return []
        best_bid = bids[0][0]
        best_ask = asks[0][0]
        return self.on_market_data(symbol, best_bid, best_ask, mark_price=mark_price)

    def on_market_data(
        self,
        symbol: str,
        best_bid: Union[float, Decimal],
        best_ask: Union[float, Decimal],
        mark_price: Optional[Union[float, Decimal]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Process incoming market data tick, update valuations, match resting orders,
        and evaluate bracket triggers.

        Returns:
            List of execution event dictionaries.
        """
        bid = Decimal(str(best_bid))
        ask = Decimal(str(best_ask))
        mark = Decimal(str(mark_price)) if mark_price is not None else (bid + ask) / Decimal("2.0")

        self.set_market_quote(symbol, bid, ask, mark)

        # Update unrealized PnL on ledger
        self.ledger.update_mark_price(symbol, mark, account_id=self.account_id)

        executions: List[Dict[str, Any]] = []
        c_val = self.get_contract_value(symbol)

        # 1. Match Resting Limit Orders
        open_orders = self.ledger.get_open_orders(symbol=symbol, account_id=self.account_id)
        for order in open_orders:
            if order.order_type != "limit_order":
                continue

            order_price = Decimal(str(order.price)) if order.price else Decimal("0.0")

            if order.side.lower() == "buy":
                # Limit Buy matches when live ask crosses down to limit price
                if ask <= order_price:
                    fill_price = order_price
                    fill_size = order.unfilled_size
                    notional = fill_size * fill_price * c_val
                    fee = notional * self.maker_fee_pct

                    up_order, pos, pnl = self.ledger.record_fill(
                        order_id=order.order_id,
                        fill_price=fill_price,
                        fill_size=fill_size,
                        fee=fee,
                        contract_value=c_val,
                        is_maker=True,
                    )

                    # Create protective child bracket orders if defined on parent order
                    if order.bracket_stop_loss_price:
                        self.ledger.create_order(
                            symbol=symbol,
                            side="sell",
                            size=fill_size,
                            order_type="stop_loss_order",
                            price=order.bracket_stop_loss_price,
                            bracket_stop_loss_price=order.bracket_stop_loss_price,
                            parent_order_id=order.order_id,
                            account_id=self.account_id,
                            state="open",
                        )
                    if order.bracket_take_profit_price:
                        self.ledger.create_order(
                            symbol=symbol,
                            side="sell",
                            size=fill_size,
                            order_type="take_profit_order",
                            price=order.bracket_take_profit_price,
                            bracket_take_profit_price=order.bracket_take_profit_price,
                            parent_order_id=order.order_id,
                            account_id=self.account_id,
                            state="open",
                        )

                    exec_event = {
                        "event": "LIMIT_FILL",
                        "order_id": order.order_id,
                        "client_order_id": order.client_order_id,
                        "symbol": symbol,
                        "side": "buy",
                        "size": fill_size,
                        "price": float(fill_price),
                        "fee": float(fee),
                        "role": "maker",
                    }
                    executions.append(exec_event)
                    logger.info(f"[PAPER] Limit BUY filled: {fill_size} {symbol} @ {fill_price} (Maker fee: {fee})")

            elif order.side.lower() == "sell":
                # Limit Sell matches when live bid crosses up to limit price
                if bid >= order_price:
                    fill_price = order_price
                    fill_size = order.unfilled_size
                    notional = fill_size * fill_price * c_val
                    fee = notional * self.maker_fee_pct

                    up_order, pos, pnl = self.ledger.record_fill(
                        order_id=order.order_id,
                        fill_price=fill_price,
                        fill_size=fill_size,
                        fee=fee,
                        contract_value=c_val,
                        is_maker=True,
                    )

                    # Create protective child bracket orders if defined on parent order
                    if order.bracket_stop_loss_price:
                        self.ledger.create_order(
                            symbol=symbol,
                            side="buy",
                            size=fill_size,
                            order_type="stop_loss_order",
                            price=order.bracket_stop_loss_price,
                            bracket_stop_loss_price=order.bracket_stop_loss_price,
                            parent_order_id=order.order_id,
                            account_id=self.account_id,
                            state="open",
                        )
                    if order.bracket_take_profit_price:
                        self.ledger.create_order(
                            symbol=symbol,
                            side="buy",
                            size=fill_size,
                            order_type="take_profit_order",
                            price=order.bracket_take_profit_price,
                            bracket_take_profit_price=order.bracket_take_profit_price,
                            parent_order_id=order.order_id,
                            account_id=self.account_id,
                            state="open",
                        )

                    exec_event = {
                        "event": "LIMIT_FILL",
                        "order_id": order.order_id,
                        "client_order_id": order.client_order_id,
                        "symbol": symbol,
                        "side": "sell",
                        "size": fill_size,
                        "price": float(fill_price),
                        "fee": float(fee),
                        "role": "maker",
                    }
                    executions.append(exec_event)
                    logger.info(f"[PAPER] Limit SELL filled: {fill_size} {symbol} @ {fill_price} (Maker fee: {fee})")

        # 2. Evaluate Protective Bracket Triggers on Open Positions
        open_positions = self.ledger.get_open_positions(account_id=self.account_id, symbol=symbol)
        for pos in open_positions:
            if not pos.is_open or pos.size <= 0:
                continue

            sl_price = Decimal(str(pos.stop_loss_price)) if pos.stop_loss_price else None
            tp_price = Decimal(str(pos.take_profit_price)) if pos.take_profit_price else None

            if pos.side.lower() == "buy":
                # Long Position Stop Loss (Trigger when Mark <= SL)
                if sl_price is not None and mark <= sl_price:
                    fill_price = mark * (Decimal("1.0") - self.slippage_pct)
                    notional = pos.size * fill_price * c_val
                    fee = notional * self.taker_fee_pct

                    # Create protective exit order
                    exit_order = self.ledger.create_order(
                        symbol=symbol,
                        side="sell",
                        size=pos.size,
                        order_type="stop_loss_order",
                        price=fill_price,
                        account_id=self.account_id,
                        state="open",
                    )
                    up_order, _, realized_pnl = self.ledger.record_fill(
                        order_id=exit_order.order_id,
                        fill_price=fill_price,
                        fill_size=pos.size,
                        fee=fee,
                        contract_value=c_val,
                        is_maker=False,
                    )
                    # Cancel any other open bracket orders for this position/symbol
                    for co in self.ledger.get_open_orders(symbol=symbol, account_id=self.account_id):
                        if co.order_type in ("stop_loss_order", "take_profit_order") and co.order_id != exit_order.order_id:
                            self.ledger.cancel_order(co.order_id)

                    exec_event = {
                        "event": "STOP_LOSS_TRIGGERED",
                        "symbol": symbol,
                        "side": "sell",
                        "size": pos.size,
                        "trigger_price": float(mark),
                        "fill_price": float(fill_price),
                        "realized_pnl": float(realized_pnl),
                        "fee": float(fee),
                    }
                    executions.append(exec_event)
                    logger.warning(f"[PAPER] STOP LOSS triggered on Long {pos.size} {symbol}: Mark={mark} <= SL={sl_price}. Realized PnL: {realized_pnl}")

                # Long Position Take Profit (Trigger when Mark >= TP)
                elif tp_price is not None and mark >= tp_price:
                    fill_price = tp_price
                    notional = pos.size * fill_price * c_val
                    fee = notional * self.maker_fee_pct

                    exit_order = self.ledger.create_order(
                        symbol=symbol,
                        side="sell",
                        size=pos.size,
                        order_type="take_profit_order",
                        price=fill_price,
                        account_id=self.account_id,
                        state="open",
                    )
                    up_order, _, realized_pnl = self.ledger.record_fill(
                        order_id=exit_order.order_id,
                        fill_price=fill_price,
                        fill_size=pos.size,
                        fee=fee,
                        contract_value=c_val,
                        is_maker=True,
                    )
                    # Cancel any other open bracket orders for this position/symbol
                    for co in self.ledger.get_open_orders(symbol=symbol, account_id=self.account_id):
                        if co.order_type in ("stop_loss_order", "take_profit_order") and co.order_id != exit_order.order_id:
                            self.ledger.cancel_order(co.order_id)

                    exec_event = {
                        "event": "TAKE_PROFIT_TRIGGERED",
                        "symbol": symbol,
                        "side": "sell",
                        "size": pos.size,
                        "trigger_price": float(mark),
                        "fill_price": float(fill_price),
                        "realized_pnl": float(realized_pnl),
                        "fee": float(fee),
                    }
                    executions.append(exec_event)
                    logger.info(f"[PAPER] TAKE PROFIT triggered on Long {pos.size} {symbol}: Mark={mark} >= TP={tp_price}. Realized PnL: {realized_pnl}")

            elif pos.side.lower() == "sell":
                # Short Position Stop Loss (Trigger when Mark >= SL)
                if sl_price is not None and mark >= sl_price:
                    fill_price = mark * (Decimal("1.0") + self.slippage_pct)
                    notional = pos.size * fill_price * c_val
                    fee = notional * self.taker_fee_pct

                    exit_order = self.ledger.create_order(
                        symbol=symbol,
                        side="buy",
                        size=pos.size,
                        order_type="stop_loss_order",
                        price=fill_price,
                        account_id=self.account_id,
                        state="open",
                    )
                    up_order, _, realized_pnl = self.ledger.record_fill(
                        order_id=exit_order.order_id,
                        fill_price=fill_price,
                        fill_size=pos.size,
                        fee=fee,
                        contract_value=c_val,
                        is_maker=False,
                    )
                    # Cancel any other open bracket orders for this position/symbol
                    for co in self.ledger.get_open_orders(symbol=symbol, account_id=self.account_id):
                        if co.order_type in ("stop_loss_order", "take_profit_order") and co.order_id != exit_order.order_id:
                            self.ledger.cancel_order(co.order_id)

                    exec_event = {
                        "event": "STOP_LOSS_TRIGGERED",
                        "symbol": symbol,
                        "side": "buy",
                        "size": pos.size,
                        "trigger_price": float(mark),
                        "fill_price": float(fill_price),
                        "realized_pnl": float(realized_pnl),
                        "fee": float(fee),
                    }
                    executions.append(exec_event)
                    logger.warning(f"[PAPER] STOP LOSS triggered on Short {pos.size} {symbol}: Mark={mark} >= SL={sl_price}. Realized PnL: {realized_pnl}")

                # Short Position Take Profit (Trigger when Mark <= TP)
                elif tp_price is not None and mark <= tp_price:
                    fill_price = tp_price
                    notional = pos.size * fill_price * c_val
                    fee = notional * self.maker_fee_pct

                    exit_order = self.ledger.create_order(
                        symbol=symbol,
                        side="buy",
                        size=pos.size,
                        order_type="take_profit_order",
                        price=fill_price,
                        account_id=self.account_id,
                        state="open",
                    )
                    up_order, _, realized_pnl = self.ledger.record_fill(
                        order_id=exit_order.order_id,
                        fill_price=fill_price,
                        fill_size=pos.size,
                        fee=fee,
                        contract_value=c_val,
                        is_maker=True,
                    )
                    # Cancel any other open bracket orders for this position/symbol
                    for co in self.ledger.get_open_orders(symbol=symbol, account_id=self.account_id):
                        if co.order_type in ("stop_loss_order", "take_profit_order") and co.order_id != exit_order.order_id:
                            self.ledger.cancel_order(co.order_id)

                    exec_event = {
                        "event": "TAKE_PROFIT_TRIGGERED",
                        "symbol": symbol,
                        "side": "buy",
                        "size": pos.size,
                        "trigger_price": float(mark),
                        "fill_price": float(fill_price),
                        "realized_pnl": float(realized_pnl),
                        "fee": float(fee),
                    }
                    executions.append(exec_event)
                    logger.info(f"[PAPER] TAKE PROFIT triggered on Short {pos.size} {symbol}: Mark={mark} <= TP={tp_price}. Realized PnL: {realized_pnl}")

        return executions

    # ──────────────────────────────────────────────
    # DeltaRestClient Compatibility Interface
    # ──────────────────────────────────────────────
    def place_order(
        self,
        symbol: str,
        side: str,
        size: int,
        price: Optional[Union[float, Decimal]] = None,
        order_type: str = "limit_order",
        type: Optional[str] = None,
        client_order_id: Optional[str] = None,
        bracket_stop_loss_price: Optional[Union[float, Decimal]] = None,
        bracket_take_profit_price: Optional[Union[float, Decimal]] = None,
        stop_loss_price: Optional[Union[float, Decimal]] = None,
        take_profit_price: Optional[Union[float, Decimal]] = None,
        product_id: Optional[int] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        Place an order in the simulated environment.

        Matches immediate market orders or aggressive limit orders, or registers
        passive limit orders into the virtual book.
        """
        resolved_type = (type or order_type or "limit_order").lower()
        resolved_side = str(side).lower()
        resolved_sl = bracket_stop_loss_price if bracket_stop_loss_price is not None else stop_loss_price
        resolved_tp = bracket_take_profit_price if bracket_take_profit_price is not None else take_profit_price

        if size <= 0:
            raise ValueError(f"Order size must be positive, got {size}")

        # 1. Idempotency Check
        if client_order_id:
            existing = self.ledger.get_order_by_client_id(client_order_id, account_id=self.account_id)
            if existing:
                logger.warning(f"[PAPER] Idempotent order duplicate blocked: {client_order_id}")
                return self._format_order(existing)

        # 2. Check for Immediate Execution (Market order or Crossing Limit)
        quote = self._quotes.get(symbol)
        c_val = self.get_contract_value(symbol)
        immediate_fill = False
        fill_price: Optional[Decimal] = None
        is_maker = False

        if resolved_type == "market_order":
            immediate_fill = True
            is_maker = False
            if quote:
                if resolved_side == "buy":
                    fill_price = quote["ask"] * (Decimal("1.0") + self.slippage_pct)
                else:
                    fill_price = quote["bid"] * (Decimal("1.0") - self.slippage_pct)
            elif price is not None:
                fill_price = Decimal(str(price))
            else:
                raise ValueError(f"Cannot execute market order on {symbol}: No market quote or price provided")

        elif resolved_type == "limit_order":
            if price is None:
                raise ValueError("Limit order requires explicit price")
            l_price = Decimal(str(price))
            if quote:
                # Check aggressive cross
                if resolved_side == "buy" and l_price >= quote["ask"]:
                    immediate_fill = True
                    fill_price = quote["ask"]  # Crosses book at best ask
                    is_maker = False
                elif resolved_side == "sell" and l_price <= quote["bid"]:
                    immediate_fill = True
                    fill_price = quote["bid"]  # Crosses book at best bid
                    is_maker = False

        # 3. Create Order in Ledger
        order = self.ledger.create_order(
            symbol=symbol,
            side=resolved_side,
            size=size,
            order_type=resolved_type,
            price=price,
            client_order_id=client_order_id,
            bracket_stop_loss_price=resolved_sl,
            bracket_take_profit_price=resolved_tp,
            account_id=self.account_id,
            state="open",
        )

        # 4. Process Immediate Fill if Triggered
        if immediate_fill and fill_price is not None:
            notional = Decimal(str(size)) * fill_price * c_val
            fee = notional * (self.maker_fee_pct if is_maker else self.taker_fee_pct)

            order, pos, realized_pnl = self.ledger.record_fill(
                order_id=order.order_id,
                fill_price=fill_price,
                fill_size=size,
                fee=fee,
                contract_value=c_val,
                is_maker=is_maker,
            )
            logger.info(f"[PAPER] Immediate fill: {resolved_side.upper()} {size} {symbol} @ {fill_price} (Fee: {fee})")

            # Create protective child bracket orders if attached
            if resolved_sl is not None:
                sl_side = "sell" if resolved_side == "buy" else "buy"
                self.ledger.create_order(
                    symbol=symbol,
                    side=sl_side,
                    size=size,
                    order_type="stop_loss_order",
                    price=resolved_sl,
                    bracket_stop_loss_price=resolved_sl,
                    parent_order_id=order.order_id,
                    account_id=self.account_id,
                    state="open",
                )
            if resolved_tp is not None:
                tp_side = "sell" if resolved_side == "buy" else "buy"
                self.ledger.create_order(
                    symbol=symbol,
                    side=tp_side,
                    size=size,
                    order_type="take_profit_order",
                    price=resolved_tp,
                    bracket_take_profit_price=resolved_tp,
                    parent_order_id=order.order_id,
                    account_id=self.account_id,
                    state="open",
                )

        return self._format_order(order)

    def cancel_order(self, order_id: str, symbol: Optional[str] = None, **kwargs) -> Dict[str, Any]:
        """Cancel an open paper order."""
        order = self.ledger.cancel_order(order_id)
        if not order:
            # Check if order_id is a client_order_id
            order = self.ledger.get_order_by_client_id(order_id, account_id=self.account_id)
            if order:
                order = self.ledger.cancel_order(order.order_id)

        if not order:
            return {"id": order_id, "state": "not_found", "error": f"Order {order_id} not found"}

        return {"id": order.order_id, "state": order.state, "symbol": order.symbol}

    def cancel_all(self, symbol: Optional[str] = None, **kwargs) -> Dict[str, Any]:
        """Cancel all open resting orders for symbol or all symbols."""
        open_orders = self.ledger.get_open_orders(symbol=symbol, account_id=self.account_id)
        cancelled = []
        for o in open_orders:
            res = self.ledger.cancel_order(o.order_id)
            if res:
                cancelled.append(res.order_id)

        return {
            "success": True,
            "cancelled_count": len(cancelled),
            "cancelled_orders": cancelled,
        }

    def amend_order(
        self,
        order_id: str,
        price: Optional[Union[float, Decimal]] = None,
        size: Optional[int] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Amend resting limit order price and/or size."""
        # Support passing limit_price or price
        amend_price = price if price is not None else kwargs.get("limit_price")
        order = self.ledger.amend_order(order_id, price=amend_price, size=size)
        if not order:
            raise ValueError(f"Order {order_id} not found or cannot be amended")

        return self._format_order(order)

    def get_open_orders(self, symbol: Optional[str] = None, params: Optional[Dict] = None) -> List[Dict[str, Any]]:
        """Fetch open orders matching Delta v2 GET /v2/orders specification."""
        sym = symbol
        if params and "symbol" in params:
            sym = params["symbol"]
        orders = self.ledger.get_open_orders(symbol=sym, account_id=self.account_id)
        return [self._format_order(o) for o in orders]

    def get_orders(
        self,
        symbol: Optional[str] = None,
        state: Optional[str] = None,
        params: Optional[Dict] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch orders with state filtering."""
        sym = symbol
        st = state
        if params:
            sym = sym or params.get("symbol")
            st = st or params.get("state")
        orders = self.ledger.get_all_orders(symbol=sym, state=st, account_id=self.account_id)
        return [self._format_order(o) for o in orders]

    def get_positions_margined(self) -> List[Dict[str, Any]]:
        """Fetch open margined positions matching Delta API format."""
        positions = self.ledger.get_open_positions(account_id=self.account_id)
        formatted: List[Dict[str, Any]] = []

        for p in positions:
            mult = 1 if p.side.lower() == "buy" else -1
            signed_size = p.size * mult
            entry_p = Decimal(str(p.entry_price))
            c_val = Decimal(str(p.contract_value))
            margin = entry_p * Decimal(str(p.size)) * c_val * Decimal("0.1")

            formatted.append({
                "symbol": p.symbol,
                "size": signed_size,
                "entry_price": str(float(entry_p)),
                "mark_price": str(float(p.mark_price)),
                "unrealized_pnl": str(float(p.unrealized_pnl)),
                "realized_pnl": str(float(p.realized_pnl)),
                "margin": str(float(margin)),
            })

        return formatted

    def get_positions(self) -> List[Dict[str, Any]]:
        """Alias for get_positions_margined."""
        return self.get_positions_margined()

    def get_wallet_balances(self) -> List[Dict[str, Any]]:
        """Fetch wallet balances matching Delta API /v2/wallet/balances."""
        summary = self.ledger.get_balance_summary(account_id=self.account_id)
        return [
            {
                "asset_symbol": "USDT",
                "balance": str(float(summary["cash_balance"])),
                "available_balance": str(float(summary["available_balance"])),
                "order_margin": "0.0",
                "position_margin": str(float(summary["margin_used"])),
            }
        ]

    # ──────────────────────────────────────────────
    # Generic HTTP Route Emulation
    # ──────────────────────────────────────────────
    def get(self, path: str, params: Optional[Dict] = None) -> Any:
        """Route GET request to simulated paper methods."""
        p = path.split("?")[0].rstrip("/")
        if p == "/v2/orders":
            return self.get_open_orders(params=params)
        elif p == "/v2/positions/margined":
            return self.get_positions_margined()
        elif p == "/v2/wallet/balances":
            return self.get_wallet_balances()
        elif p == "/v2/products":
            return [{"id": 1, "symbol": "BTCUSD", "contract_type": "futures"}]
        logger.debug(f"[PAPER] GET unhandled path: {path}")
        return []

    def post(self, path: str, body: Optional[Dict] = None) -> Any:
        """Route POST request to simulated paper methods."""
        p = path.rstrip("/")
        if p == "/v2/orders":
            b = body or {}
            return self.place_order(**b)
        logger.debug(f"[PAPER] POST unhandled path: {path}")
        return {}

    def put(self, path: str, body: Optional[Dict] = None) -> Any:
        """Route PUT request to simulated paper methods."""
        p = path.rstrip("/")
        if p == "/v2/orders":
            b = body or {}
            oid = b.get("id") or b.get("order_id")
            return self.amend_order(order_id=oid, **b)
        logger.debug(f"[PAPER] PUT unhandled path: {path}")
        return {}

    def delete(self, path: str, params: Optional[Dict] = None) -> Any:
        """Route DELETE request to simulated paper methods."""
        p = path.rstrip("/")
        if p == "/v2/orders/all":
            return self.cancel_all()
        elif p == "/v2/orders":
            par = params or {}
            oid = par.get("id") or par.get("order_id")
            return self.cancel_order(order_id=oid)
        logger.debug(f"[PAPER] DELETE unhandled path: {path}")
        return {}

    # ──────────────────────────────────────────────
    # Internal Helpers
    # ──────────────────────────────────────────────
    def _format_order(self, order: PaperOrderModel) -> Dict[str, Any]:
        """Format PaperOrderModel into standard Delta Exchange v2 order dictionary."""
        return {
            "id": order.order_id,
            "client_order_id": order.client_order_id,
            "product_id": 1,
            "symbol": order.symbol,
            "side": order.side.lower(),
            "order_type": order.order_type,
            "size": order.size,
            "unfilled_size": order.unfilled_size,
            "price": str(float(order.price)) if order.price else None,
            "average_fill_price": str(float(order.avg_fill_price)) if order.avg_fill_price else None,
            "state": order.state,
            "bracket_stop_loss_price": str(float(order.bracket_stop_loss_price)) if order.bracket_stop_loss_price else None,
            "bracket_take_profit_price": str(float(order.bracket_take_profit_price)) if order.bracket_take_profit_price else None,
            "created_at": order.created_at.isoformat() if order.created_at else None,
        }
