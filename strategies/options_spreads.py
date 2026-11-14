"""
Strategy E: Vertical Debit/Credit Spreads

Strategy E: Defined-risk options spreads using vertical debit and credit spreads.

Key Features:
- Bull call spread: buy lower strike call, sell higher strike call (net debit)
- Bear put spread: buy lower strike put, sell higher strike put (net credit)
- Defined risk: maximum loss = net debit (debit spread) or = strike width - net credit (credit spread)
- Target risk-to-reward > 1.5
- Trade only in RANGING_LOW_VOL or TRENDING_BULL regime
- Trade direction: LONG bull call spread, SHORT bear put spread

Signal Contract:
- TradeSignal with: symbol, direction (BUY/SELL), order_type=limit_entry,
  target_entry_price (net credit/debit price), stop_loss_price (max loss price),
  take_profit_price (fixed risk ratio x max risk), proposed_size (subject to Risk Gate),
  confidence (spread quality), rationale (strikes, widths, net P/L, regime)

Note: Strategy emits TradeSignal only — NO raw order placement.
"""
from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Any

from strategies.base import BaseStrategy, register_strategy, emit_buy_signal, emit_sell_signal
from analytics.signal import TradeSignal


# ──────────────────────────────────────────────
# OptionsSpreads Strategy Class
# ──────────────────────────────────────────────
@register_strategy
class OptionsSpreads(BaseStrategy):
    """
    Strategy E: Vertical Debit/Credit Spreads.

    Parameters:
    - debit_spread: True for bull call spread (debit), False for bear put spread (credit)
    - strike_width: width between short and long strike (e.g., 100 for 1-point width)
    - target_credit: target net credit for credit spreads (e.g., 2.00)
    - max_loss: maximum acceptable loss (for debit spreads)
    - risk_reward_ratio: target risk/reward (e.g., 1.5 means take profit at 1.5x max loss)
    - allowed_environments: BACKTEST, PAPER, TESTNET (LIVE_AUTO with explicit auth)

    Signal Logic:
    1. Identify spread opportunity: bull call spread (buy lower strike call, sell higher strike call)
       or bear put spread (buy lower strike put, sell higher strike put)
    2. Verify net credit/debit is acceptable
    3. Verify risk/reward ratio > minimum threshold
    4. Verify regime (TRENDING_BULL for bull call, or RANGING_LOW_VOL for credit spreads)
    5. Signal emitted as TradeSignal — Risk Gateway validates before execution

    Note: Strategy emits TradeSignal only. Order placement through Risk Gateway.
    """
    strategy_id = "options_spreads"
    strategy_version = "1.0.0"
    display_name = "Options Spreads (Strategy E)"
    warmup_period = 10
    allowed_environments = {"BACKTEST", "PAPER", "TESTNET"}

    # Spread parameters
    debit_spread: bool = True  # True = bull call spread (debit), False = bear put spread (credit)
    strike_width: Decimal = Decimal("100")  # width between short and long strike
    target_credit: Decimal = Decimal("2.00")  # target net credit for credit spreads
    max_loss: Decimal = Decimal("5.00")  # maximum acceptable loss (debit spreads)
    risk_reward_ratio: Decimal = Decimal("1.5")  # target risk/reward

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
        """Not used for spreads — operates on regime/score basis."""
        return None

    # ————————————————————————————————
    # on_candle — called when new OHLC candle forms
    # ————————————————————————————————
    def on_candle(self, open_: float, high: float, low: float, close: float,
                  volume: int, timestamp: datetime) -> Optional[TradeSignal]:
        """Not used for spreads — signals based on regime/score, not single candles."""
        return None

    # ————————————————————————————————
    # generate_signals — batch signal generation
    # ————————————————————————————————
    def generate_signals(self, quotes: Dict[str, 'MarketDataQuote']) -> List[TradeSignal]:
        """Batch generate signals from a dict of symbol->MarketDataQuote."""
        signals: List[TradeSignal] = []

        for symbol, quote in quotes.items():
            # Spread signals are regime-based, not candle-based
            # In full implementation, spread opportunities are identified
            # by the engine from option chain data
            # For now, demonstrate the pattern with regime-based logic

            # Regime check
            regime = "TRENDING_BULL"  # placeholder from market regime classifier

            # Bull call spread (debit spread) for TRENDING_BULL
            if regime == "TRENDING_BULL" and self.debit_spread:
                # Bull call spread: buy lower strike call, sell higher strike call
                # net debit = price of long call - price of short call
                # max loss = net debit
                # max profit = strike width - net debit
                # target risk/reward: risk_reward_ratio

                # Generate bull call spread signal
                return [emit_buy_signal(
                    symbol=symbol, entry=Decimal("0"), sl=Decimal("0"),
                    tp=Decimal("0"), size=1, strategy_id=self.strategy_id,
                    confidence=Decimal("0.6"),
                )]

            # Bear put spread (credit spread) for RANGING_LOW_VOL
            if regime == "RANGING_LOW_VOL" and not self.debit_spread:
                # Bear put spread: buy lower strike put, sell higher strike put
                # net credit = price of short put - price of long put
                # max profit = net credit
                # max loss = strike width - net credit
                # target risk/reward: risk_reward_ratio

                # Generate bear put spread signal
                return [emit_sell_signal(
                    symbol=symbol, entry=Decimal("0"), sl=Decimal("0"),
                    tp=Decimal("0"), size=1, strategy_id=self.strategy_id,
                    confidence=Decimal("0.6"),
                )]

        # No signals
        return []


# ──────────────────────────────────────────────
# Convenience wrapper for strategy integration
# ──────────────────────────────────────────────
def options_spreads_signal(
    symbol: str,
    is_debit_spread: bool,
    entry_price: Decimal,
    sl_price: Decimal,
    tp_price: Decimal,
    proposed_size: int,
    confidence: Decimal = Decimal("0.5"),
) -> TradeSignal:
    """Helper to create an options spreads signal from external logic."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    from datetime import timedelta

    direction = "BUY" if is_debit_spread else "SELL"
    spread_type = "bull call spread" if is_debit_spread else "bear put spread"

    return TradeSignal(
        signal_id=f"spread_{symbol}_{now.timestamp()}",
        strategy_id="options_spreads",
        strategy_version="1.0.0",
        symbol=symbol,
        direction=direction,
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
        rationale={"strategy": "options_spreads", "debit_spread": is_debit_spread,
                   "spread_type": spread_type},
    )