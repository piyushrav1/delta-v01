"""
Strategy B: Momentum Burst

Momentum strategy using RSI burst with volume acceleration filter.

Key Features:
- RSI momentum burst: RSI crosses above 50 with rising momentum
- Volume acceleration: current volume > 2x average volume
- Trade only in TRENDING_BULL or TRENDING_BEAR regime
- Trade direction: LONG only (buy on momentum burst, sell on crossunder)
- Trade size determined by Risk Gate (proposed_size only)

Signal Contract:
- TradeSignal with: symbol, direction (BUY/SELL), order_type=limit_entry,
  target_entry_price (RSI confirmation price), stop_loss_price (ATR-based),
  take_profit_price (fixed risk ratio), proposed_size (subject to Risk Gate),
  confidence (RSI strength), rationale (RSI value, volume, regime)

Note: Strategy emits TradeSignal only — NO raw order placement.
"""
from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Any

from strategies.base import BaseStrategy, register_strategy, emit_buy_signal, emit_sell_signal
from analytics.indicators import rsi, atr
from analytics.signal import TradeSignal


# ──────────────────────────────────────────────
# Momentum Strategy Class
# ──────────────────────────────────────────────
@register_strategy
class Momentum(BaseStrategy):
    """
    Strategy B: Momentum Burst.

    Parameters:
    - rsi_period: 14 (RSI lookback period)
    - rsi_enter: 50 (RSI level to enter long/short)
    - rsi_exit: 45 (RSI level to exit)
    - volume_period: 20 (average volume lookback)
    - volume_multiplier: 2.0 (minimum volume acceleration)
    - atr_period: 14 (ATR lookback for stop)
    - atr_multiplier: 2.0 (stop-loss distance in ATR multiples)
    - allowed_environments: BACKTEST, PAPER, TESTNET (LIVE_AUTO with explicit auth)

    Signal Logic:
    1. RSI crosses above 50 with rising momentum (RSI now > RSI previous) → BUY signal
    2. RSI crosses below 50 with falling momentum → SELL signal
    3. Volume filter: current volume > 2x average volume (volume period)
    4. ATR filter: price not more than 2 ATR from recent extreme (volatility filter)
    5. Regime filter: TRENDING_BULL or TRENDING_BEAR (via market regime classifier)
    6. Signal emitted as TradeSignal — Risk Gateway validates before execution

    Note: Strategy emits TradeSignal only. Order placement through Risk Gateway.
    """
    strategy_id = "momentum"
    strategy_version = "1.0.0"
    display_name = "Momentum Burst (Strategy B)"
    warmup_period = 30  # Need enough bars for RSI and volume calculation
    allowed_environments = {"BACKTEST", "PAPER", "TESTNET"}

    # RSI parameters
    rsi_period: int = 14
    rsi_enter: Decimal = Decimal("50")
    rsi_exit: Decimal = Decimal("45")

    # Volume parameters
    volume_period: int = 20
    volume_multiplier: Decimal = Decimal("2.0")

    # ATR parameters
    atr_period: int = 14
    atr_multiplier: Decimal = Decimal("2.0")

    # Registry auto-call (forbidden import check happens in BaseStrategy.__init__)
    def __post__(self):
        """Post-init: verify no forbidden imports in source."""
        import inspect
        source = inspect.getsource(self.__class__)
        _check_forbidden_imports(source)

    # ————————————————————————————————
    # on_quote — called by trading engine on each quote tick
    # ————————————————————————————————
    def on_quote(self, quote: 'MarketDataQuote') -> Optional[TradeSignal]:
        """Not used for momentum — operates on candle data."""
        return None

    # ————————————————————————————————
    # on_candle — called when new OHLC candle forms
    # ————————————————————————————————
    def on_candle(self, open_: float, high: float, low: float, close: float,
                  volume: int, timestamp: datetime) -> Optional[TradeSignal]:
        """Evaluate RMI burst and generate TradeSignal if conditions met."""
        # Need enough data for RSI and volume calculation
        # warmup_period = 30 bars minimum

        # 1. Calculate RSI
        # In full implementation, the engine maintains RSI state incrementally.
        # For this demo, we use a simplified approach.

        # 2. Calculate volume acceleration
        # current_volume > 2x average_volume over volume_period

        # 3. Regime check
        # TRENDING_BULL or TRENDING_BEAR only

        # 4. Signal generation
        # RSI > 50 and rising + volume acceleration = BUY
        # RSI < 50 and falling + volume acceleration = SELL

        # PLACEHOLDER: In full implementation, the engine maintains RSI/volume state.
        # Here we demonstrate the pattern with regime-based logic.

        # Regime check (simplified)
        regime = "TRENDING_BULL"  # placeholder from market regime classifier

        # RSI placeholder (would be computed from price series by engine)
        rsi_val = Decimal("55")  # > 50 = bullish momentum
        rsi_prev = Decimal("50")  # previous RSI

        # Volume placeholder (would be from engine)
        curr_volume = Decimal("1000")  # placeholder
        avg_volume = Decimal("500")  # placeholder

        # Generate signal if conditions met
        # BUY: RSI crosses above 50 + volume acceleration + bullish regime
        # SELL: RSI crosses below 50 + volume acceleration + bearish regime
        # (In full implementation, check rsi_val vs rsi_exit too)

        # BUY condition placeholder
        buy_condition = (
            rsi_val > self.rsi_enter
            and curr_volume >= self.volume_multiplier * avg_volume
            and regime in ("TRENDING_BULL",)
        )

        # SELL condition placeholder
        sell_condition = (
            rsi_val < self.rsi_exit
            and curr_volume >= self.volume_multiplier * avg_volume
            and regime in ("TRENDING_BEAR",)
        )

        if buy_condition:
            # Generate BUY signal
            sl_price = Decimal("0")  # Will be filled by ATR calculation
            tp_price = Decimal("0")
            return emit_buy_signal(
                symbol="BTCUSD", entry=Decimal("0"), sl=sl_price,
                tp=tp_price, size=1, strategy_id=self.strategy_id,
                confidence=Decimal("0.6"),
            )

        # SELL condition placeholder
        if sell_condition:
            sl_price = Decimal("0")
            tp_price = Decimal("0")
            return emit_sell_signal(
                symbol="BTCUSD", entry=Decimal("0"), sl=sl_price,
                tp=tp_price, size=1, strategy_id=self.strategy_id,
                confidence=Decimal("0.6"),
            )

        # No signal yet
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

        return signals


# ──────────────────────────────────────────────
# Convenience wrapper for strategy integration
# ──────────────────────────────────────────────
def momentum_signal(
    symbol: str,
    entry_price: Decimal,
    sl_price: Decimal,
    tp_price: Decimal,
    proposed_size: int,
    confidence: Decimal = Decimal("0.5"),
) -> TradeSignal:
    """Helper to create a momentum signal from external logic."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    from datetime import timedelta

    return TradeSignal(
        signal_id=f"momentum_{symbol}_{now.timestamp()}",
        strategy_id="momentum",
        strategy_version="1.0.0",
        symbol=symbol,
        direction="BUY",
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
        rationale={"strategy": "momentum", "rsi_period": 14, "rsi_enter": 50},
    )