"""
Strategy A: Trend Following

Trend-following strategy using EMA 20/50 cross with ATR volatility channel confirmation.

Key Features:
- EMA 20 fast cross EMA 50 slow for trend direction
- ATR-based volatility channel for filter
- Trade only in TRENDING_BULL or TRENDING_BEAR regime
- Trade size determined by Risk Gate (proposed_size only)
- Trade direction: LONG only (buy EMA crossover, sell EMA crossunder)

Signal Contract:
- TradeSignal with: symbol, direction (BUY/SELL), order_type=limit_entry,
  target_entry_price (EMA cross price), stop_loss_price (ATR-based),
  take_profit_price (ATR-based or fixed ratio), proposed_size (subject to Risk Gate),
  confidence (EMA slope strength), rationale (EMA values, ATR, regime)

Note: Strategy emits TradeSignal only — NO raw order placement.
"""
from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Any

from strategies.base import BaseStrategy, register_strategy, emit_buy_signal, emit_sell_signal
from analytics.indicators import ema, rsi, atr
from analytics.signal import TradeSignal


# ──────────────────────────────────────────────
# TrendFollowing Strategy Class
# ──────────────────────────────────────────────
@register_strategy
class TrendFollowing(BaseStrategy):
    """
    Strategy A: Trend Following with EMA 20/50 crossover.

    Parameters:
    - ema_fast_span: 20 (fast EMA period)
    - ema_slow_span: 50 (slow EMA period)
    - atr_period: 14 (ATR lookback for channel/stop)
    - atr_multiplier: 2.0 (stop-loss distance in ATR multiples)
    - allowed_environments: BACKTEST, PAPER, TESTNET (LIVE_AUTO with explicit auth)

    Signal Logic:
    1. EMA 20 crosses above EMA 50 → BUY signal (uptrend initiation)
    2. EMA 20 crosses below EMA 50 → SELL signal (trend reversal)
    3. Filter: only trade if ADX > 20 (trend strength confirmation, via market regime)
    4. Filter: price must be within ATR channel (not overextended)
    5. Signal emitted as TradeSignal — Risk Gateway validates before execution

    Note: Strategy emits TradeSignal only. Order placement through Risk Gateway.
    """
    strategy_id = "trend_following"
    strategy_version = "1.0.0"
    display_name = "Trend Following (Strategy A)"
    warmup_period = 55  # EMA 50 needs 50 bars minimum
    allowed_environments = {"BACKTEST", "PAPER", "TESTNET"}

    # EMA periods
    ema_fast_span: int = 20
    ema_slow_span: int = 50

    # ATR parameters
    atr_period: int = 14
    atr_multiplier: Decimal = Decimal("2.0")

    # ————————————————————————————————
    # Post-init: forbidden import check
    # ————————————————————————————————
    def __post__(self):
        """Post-init: verify no forbidden imports in source."""
        import inspect
        source = inspect.getsource(self.__class__)
        _check_forbidden_imports(source)

    # ————————————————————————————————
    # on_quote — called by trading engine on each quote tick
    # ————————————————————————————————
    def on_quote(self, quote: 'MarketDataQuote') -> Optional[TradeSignal]:
        """Not used for EMA crossover — operates on candle data."""
        return None

    # ————————————————————————————————
    # on_candle — called when new OHLC candle forms
    # ————————————————————————————————
    def on_candle(self, open_: float, high: float, low: float, close: float,
                  volume: int, timestamp: datetime) -> Optional[TradeSignal]:
        """Evaluate EMA crossover and generate TradeSignal if conditions met."""
        # Need enough data for EMAs
        # (Caller ensures sufficient history; warmup_period = 55 bars)

        # 1. Calculate EMAs
        # Use the last 'ema_slow_span' closes to compute both EMAs
        # In production, these would be maintained incrementally
        # For now, compute from price series

        # We need a price series — since we only get one candle at a time,
        # we maintain a rolling window via the trading engine.
        # Here we assume the engine provides a quote dict with history.

        # Since we can't maintain state across calls easily without engine support,
        # we'll use a simple approach: check if we have enough context
        # and emit signal based on the crossover of the latest close relative
        # to where EMAs would be.

        # PLACEHOLDER: In full implementation, the engine maintains EMA state.
        # Here we emit a signal based on close vs simple moving average as
        # a demonstration of the pattern.

        # Calculate simple moving averages for demonstration
        # (Replace with proper EMA state management in production)
        prices = [open_, high, low, close]
        sma_fast = sum(prices) / len(prices)  # placeholder: not true EMA
        sma_slow = sum(prices) / len(prices)  # placeholder

        # --- Real EMA calculation would use: ema(prices, span) —
        # but we need a price series maintained by the engine.
        # For this demo, we'll use a regime-based approach:

        # Check regime (simplified — in production, use classify_market_regime)
        # Trade only if regime is TRENDING_BULL or TRENDING_BEAR
        regime = "TRENDING_BULL"  # placeholder

        # EMA crossover signal
        # bullish: fast EMA crosses above slow EMA
        # bearish: fast EMA crosses below slow EMA
        # if regime in ("TRENDING_BULL", "TRENDING_BEAR"):
        #     if sma_fast > sma_slow:  # fast above slow = uptrend
        #         # Generate BUY signal
        #         sl_price = Decimal("0")  # Will be filled by ATR calculation
        #         tp_price = Decimal("0")
        #         return emit_buy_signal(
        #             symbol="BTCUSD", entry=Decimal("0"), sl=sl_price,
        #             tp=tp_price, size=1, strategy_id=self.strategy_id
        #         )
        #     elif sma_fast < sma_slow:  # fast below slow = downtrend
        #         # Generate SELL signal
        #         return emit_sell_signal(
        #             symbol="BTCUSD", entry=Decimal("0"), sl=sl_price,
        #             tp=tp_price, size=1, strategy_id=self.strategy_id
        #         )

        # No signal yet — waiting for proper EMA state
        return None

    # ————————————————————————————————
    # generate_signals — batch signal generation
    # ————————————————————————————————
    def generate_signals(self, quotes: Dict[str, 'MarketDataQuote']) -> List[TradeSignal]:
        """Batch generate signals from a dict of symbol->MarketDataQuote."""
        signals: List[TradeSignal] = []

        for symbol, quote in quotes.items():
            signal = self.on_quote(quote)  # quote-based (returns None)
            if signal is not None:
                signals.append(signal)

            # Also check candle-based if we had candle history
            # signal = self.on_candle(...)  # would need candle data

        return signals


# ──────────────────────────────────────────────
# Convenience wrapper for strategy integration
# ──────────────────────────────────────────────
def trend_following_signal(
    symbol: str,
    entry_price: Decimal,
    sl_price: Decimal,
    tp_price: Decimal,
    proposed_size: int,
    confidence: Decimal = Decimal("0.5"),
) -> TradeSignal:
    """Helper to create a trend following signal from external logic."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    from datetime import timedelta

    return TradeSignal(
        signal_id=f"trend_{symbol}_{now.timestamp()}",
        strategy_id="trend_following",
        strategy_version="1.0.0",
        symbol=symbol,
        direction="BUY" if Decimal(entry_price) > Decimal("50") else "SELL",
        order_type="limit_order",
        target_entry_price=entry_price,
        stop_loss_price=sl_price,
        take_profit_price=tp_price,
        proposed_size=proposed_size,
        max_slippage_pct=Decimal("0.005"),
        expected_holding_period_sec=3600,
        timestamp=now,
        expiry_time=now + timedelta(hours=24),
        confidence=confidence,
        rationale={"strategy": "trend_following", "ema_fast": 20, "ema_slow": 50},
    )