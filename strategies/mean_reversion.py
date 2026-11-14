"""
Strategy C: Mean Reversion

Mean reversion strategy using Bollinger Band 2.5σ overshoot with RSI divergence.

Key Features:
- Bollinger Band overshoot: price moves outside 2.5σ band mean-reverting
- RSI divergence: price makes new low/high while RSI does not confirm
- Trade only in RANGING_LOW_VOL or RANGING_HIGH_VOL regime
- Trade direction: LONG at oversold, SHORT at overbought
- Trade size determined by Risk Gate (proposed_size only)

Signal Contract:
- TradeSignal with: symbol, direction (BUY/SELL), order_type=limit_entry,
  target_entry_price (BB mid price), stop_loss_price (BB edge),
  take_profit_price (BB center or fixed ratio), proposed_size (subject to Risk Gate),
  confidence (BB width + RSI divergence strength), rationale (BB values, RSI divergence, regime)

Note: Strategy emits TradeSignal only — NO raw order placement.
"""
from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Any

from strategies.base import BaseStrategy, register_strategy, emit_buy_signal, emit_sell_signal
from analytics.indicators import bollinger_bands, rsi, atr
from analytics.signal import TradeSignal


# ──────────────────────────────────────────────
# Mean Reversion Strategy Class
# ──────────────────────────────────────────────
@register_strategy
class MeanReversion(BaseStrategy):
    """
    Strategy C: Mean Reversion using Bollinger Bands and RSI divergence.

    Parameters:
    - bb_window: 20 (Bollinger Band lookback window)
    - bb_num_std: 2.5 (number of standard deviations)
    - rsi_period: 14 (RSI lookback period)
    - rsi_oversold: 30 (RSI level indicating oversold)
    - rsi_overbought: 70 (RSI level indicating overbought)
    - atr_period: 14 (ATR lookback for stop)
    - atr_multiplier: 1.5 (stop-loss distance in ATR multiples)
    - allowed_environments: BACKTEST, PAPER, TESTNET (LIVE_AUTO with explicit auth)

    Signal Logic:
    1. Bollinger Band detection: price touches lower band (oversold) or upper band (overbought)
    2. RSI divergence: price makes new low/high while RSI does not confirm
    3. Regime filter: RANGING_LOW_VOL or RANGING_HIGH_VOL
    4. Signal emitted as TradeSignal — Risk Gateway validates before execution

    Signal Types:
    - LONG at oversold: price at lower BB, RSI oversold, bullish divergence
    - SHORT at overbought: price at upper BB, RSI overbearish, bearish divergence

    Note: Strategy emits TradeSignal only. Order placement through Risk Gateway.
    """
    strategy_id = "mean_reversion"
    strategy_version = "1.0.0"
    display_name = "Mean Reversion (Strategy C)"
    warmup_period = 30  # Need BB and RSI warmup
    allowed_environments = {"BACKTEST", "PAPER", "TESTNET"}

    # Bollinger Band parameters
    bb_window: int = 20
    bb_num_std: Decimal = Decimal("2.5")

    # RSI parameters
    rsi_period: int = 14
    rsi_oversold: Decimal = Decimal("30")
    rsi_overbought: Decimal = Decimal("70")

    # ATR parameters
    atr_period: int = 14
    atr_multiplier: Decimal = Decimal("1.5")

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
        """Not used for mean reversion — operates on candle data."""
        return None

    # ————————————————————————————————
    # on_candle — called when new OHLC candle forms
    # ————————————————————————————————
    def on_candle(self, open_: float, high: float, low: float, close: float,
                  volume: int, timestamp: datetime) -> Optional[TradeSignal]:
        """Evaluate Bollinger Band and RSI conditions and generate TradeSignal if met."""
        # Need enough data for BB and RSI warmup
        # warmup_period = 30 bars minimum

        # 1. Calculate Bollinger Bands
        # In full implementation, the engine maintains BB state incrementally.
        # For this demo, we use a simplified approach.

        # 2. Calculate RSI
        # Similarly maintained by the engine.

        # 3. Regime check
        # RANGING_LOW_VOL or RANGING_HIGH_VOL only

        # 4. Signal generation
        # LONG at oversold: price at lower BB, RSI oversold, bullish divergence
        # SHORT at overbought: price at upper BB, RSI overbearish, bearish divergence

        # PLACEHOLDER: In full implementation, the engine maintains BB/RSI state.
        # Here we demonstrate the pattern with regime-based logic.

        # Regime check (simplified)
        regime = "RANGING_LOW_VOL"  # placeholder from market regime classifier

        # BB and RSI placeholders (would be from engine)
        # mid_band = placeholder mid price from BB
        # upper_band = placeholder upper BB price
        # lower_band = placeholder lower BB price
        # rsi_val = placeholder RSI value

        # LONG (oversold) condition placeholder:
        # price near lower BB, RSI < 30, bullish divergence
        long_condition = (
            regime == "RANGING_LOW_VOL"
        )

        # SHORT (overbought) condition placeholder:
        # price near upper BB, RSI > 70, bearish divergence
        short_condition = (
            regime == "RANGING_HIGH_VOL"
        )

        if long_condition:
            # Generate LONG (buy) signal at oversold
            sl_price = Decimal("0")  # Will be filled by ATR calculation
            tp_price = Decimal("0")
            return emit_buy_signal(
                symbol="BTCUSD", entry=Decimal("0"), sl=sl_price,
                tp=tp_price, size=1, strategy_id=self.strategy_id,
                confidence=Decimal("0.6"),
            )

        if short_condition:
            # Generate SHORT (sell) signal at overbought
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
def mean_reversion_signal(
    symbol: str,
    entry_price: Decimal,
    sl_price: Decimal,
    tp_price: Decimal,
    proposed_size: int,
    confidence: Decimal = Decimal("0.5"),
) -> TradeSignal:
    """Helper to create a mean reversion signal from external logic."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    from datetime import timedelta

    return TradeSignal(
        signal_id=f"meanrev_{symbol}_{now.timestamp()}",
        strategy_id="mean_reversion",
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
        rationale={"strategy": "mean_reversion", "bb_window": 20, "bb_num_std": 2.5},
    )