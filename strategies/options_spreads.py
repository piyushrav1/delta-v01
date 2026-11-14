"""
Strategy E: Defined-Risk Vertical Debit Spreads

Implements Bull Call Debit Spreads and Bear Put Debit Spreads with
strictly bounded maximum loss and verified risk-to-reward ratio >= 1.5.

Key Features:
- Bull Call Debit Spread: Buy Lower Call (Ask), Sell Higher Call (Bid)
- Bear Put Debit Spread: Buy Higher Put (Ask), Sell Lower Put (Bid)
- Strictly defined risk: Max Loss = Net Debit Paid
- Verified profit potential: Max Gain = Strike Width - Net Debit
- R:R Filter: (Max Gain / Net Debit) >= 1.5
- Multi-leg TradeSignal emission
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Any, Tuple

from strategies.base import BaseStrategy, register_strategy
from analytics.signal import TradeSignal, buy_signal


def parse_option_symbol(symbol: str) -> Optional[Dict[str, Any]]:
    """
    Parse Delta Exchange option symbols into metadata.
    Supported patterns:
    1. Standard: (C|P)-UNDERLYING-STRIKE-EXPIRY (e.g. C-BTC-70000-270926)
    2. Underscore: (C|P)_UNDERLYING_STRIKE_EXPIRY
    """
    pattern = r"^(C|P)[-_]([A-Z0-9]+)[-_]([0-9]+(?:\.[0-9]+)?)[-_]([0-9]{6})$"
    m = re.match(pattern, symbol.strip())
    if not m:
        return None
    opt_type, underlying, strike_str, expiry_str = m.groups()
    return {
        "symbol": symbol,
        "opt_type": opt_type.upper(),
        "underlying": underlying.upper(),
        "strike": float(strike_str),
        "expiry": expiry_str,
    }


@register_strategy
class OptionsSpreads(BaseStrategy):
    """
    Strategy E: Vertical Debit Spreads with Defined Risk and R:R >= 1.5.

    Parameters:
    - min_rr_ratio: 1.5 (minimum risk-to-reward ratio)
    - max_debit_pct_of_width: 0.40 (net debit should not exceed 40% of strike width)
    """
    strategy_id: str = "options_spreads"
    strategy_version: str = "1.0.0"
    display_name: str = "Defined-Risk Options Spreads (Strategy E)"
    warmup_period: int = 10
    allowed_environments = {"BACKTEST", "PAPER", "TESTNET", "LIVE_AUTO"}

    def __init__(
        self,
        min_rr_ratio: float = 1.5,
        max_debit_pct_of_width: float = 0.40,
    ):
        self.min_rr_ratio = Decimal(str(min_rr_ratio))
        self.max_debit_pct_of_width = Decimal(str(max_debit_pct_of_width))

    def on_quote(self, quote: Any) -> Optional[TradeSignal]:
        """Single quote ticks cannot evaluate a spread on their own."""
        return None

    def on_candle(self, open_: float, high: float, low: float, close: float,
                  volume: int, timestamp: datetime) -> Optional[TradeSignal]:
        return None

    def find_bull_call_spreads(self, quotes: Dict[str, Any]) -> List[TradeSignal]:
        """
        Scan call options for valid Bull Call Debit Spreads.
        Buy Lower Strike Call (Ask), Sell Higher Strike Call (Bid).
        """
        signals: List[TradeSignal] = []
        parsed_options = []

        for sym, q in quotes.items():
            meta = parse_option_symbol(sym)
            if not meta or meta['opt_type'] != 'C':
                continue
            bid = getattr(q, 'bid', None) or (q.get('bid') if isinstance(q, dict) else None)
            ask = getattr(q, 'ask', None) or (q.get('ask') if isinstance(q, dict) else None)
            if bid is not None and ask is not None and float(bid) > 0 and float(ask) > float(bid):
                meta['bid'] = float(bid)
                meta['ask'] = float(ask)
                parsed_options.append(meta)

        # Group by (underlying, expiry)
        grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for opt in parsed_options:
            key = (opt['underlying'], opt['expiry'])
            grouped.setdefault(key, []).append(opt)

        for (und, exp), opts in grouped.items():
            opts.sort(key=lambda x: x['strike'])
            for i in range(len(opts)):
                for j in range(i + 1, min(i + 4, len(opts))):
                    lower = opts[i]
                    higher = opts[j]

                    strike_width = Decimal(str(round(higher['strike'] - lower['strike'], 4)))
                    if strike_width <= 0:
                        continue

                    # Buy lower at ask, sell higher at bid
                    buy_price = Decimal(str(round(lower['ask'], 4)))
                    sell_price = Decimal(str(round(higher['bid'], 4)))
                    net_debit = buy_price - sell_price

                    if net_debit <= 0:
                        continue

                    max_profit = strike_width - net_debit
                    if max_profit <= 0:
                        continue

                    rr = max_profit / net_debit
                    if rr >= self.min_rr_ratio and (net_debit / strike_width) <= self.max_debit_pct_of_width:
                        signal_id = f"sig_bcs_{und}_{exp}_{int(lower['strike'])}_{int(higher['strike'])}"
                        now = datetime.now()
                        expiry = now + timedelta(hours=4)
                        confidence = min(Decimal("0.95"), Decimal("0.70") + min(Decimal("0.25"), (rr - self.min_rr_ratio) * Decimal("0.1")))

                        legs = [
                            {"symbol": lower['symbol'], "side": "BUY", "size": 1, "target_price": float(buy_price)},
                            {"symbol": higher['symbol'], "side": "SELL", "size": 1, "target_price": float(sell_price)},
                        ]

                        sig = buy_signal(
                            signal_id=signal_id,
                            strategy_id=self.strategy_id,
                            strategy_version=self.strategy_version,
                            symbol=lower['symbol'],
                            entry_price=round(net_debit, 4),
                            stop_loss=Decimal("0.0"),  # Debit spread max loss is net debit
                            take_profit=round(max_profit, 4),
                            proposed_size=1,
                            confidence=confidence,
                            expiry_time=expiry,
                            rationale={
                                "spread_type": "bull_call_spread",
                                "underlying": und,
                                "expiry": exp,
                                "long_leg": lower['symbol'],
                                "short_leg": higher['symbol'],
                                "strike_lower": lower['strike'],
                                "strike_higher": higher['strike'],
                                "strike_width": float(strike_width),
                                "net_debit": float(net_debit),
                                "max_profit": float(max_profit),
                                "risk_reward_ratio": float(rr),
                            },
                            legs=legs,
                        )
                        signals.append(sig)

        return signals

    def find_bear_put_spreads(self, quotes: Dict[str, Any]) -> List[TradeSignal]:
        """
        Scan put options for valid Bear Put Debit Spreads.
        Buy Higher Strike Put (Ask), Sell Lower Strike Put (Bid).
        """
        signals: List[TradeSignal] = []
        parsed_options = []

        for sym, q in quotes.items():
            meta = parse_option_symbol(sym)
            if not meta or meta['opt_type'] != 'P':
                continue
            bid = getattr(q, 'bid', None) or (q.get('bid') if isinstance(q, dict) else None)
            ask = getattr(q, 'ask', None) or (q.get('ask') if isinstance(q, dict) else None)
            if bid is not None and ask is not None and float(bid) > 0 and float(ask) > float(bid):
                meta['bid'] = float(bid)
                meta['ask'] = float(ask)
                parsed_options.append(meta)

        grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for opt in parsed_options:
            key = (opt['underlying'], opt['expiry'])
            grouped.setdefault(key, []).append(opt)

        for (und, exp), opts in grouped.items():
            opts.sort(key=lambda x: x['strike'])
            for i in range(len(opts)):
                for j in range(i + 1, min(i + 4, len(opts))):
                    lower = opts[i]
                    higher = opts[j]

                    strike_width = Decimal(str(round(higher['strike'] - lower['strike'], 4)))
                    if strike_width <= 0:
                        continue

                    # Buy higher put at ask, sell lower put at bid
                    buy_price = Decimal(str(round(higher['ask'], 4)))
                    sell_price = Decimal(str(round(lower['bid'], 4)))
                    net_debit = buy_price - sell_price

                    if net_debit <= 0:
                        continue

                    max_profit = strike_width - net_debit
                    if max_profit <= 0:
                        continue

                    rr = max_profit / net_debit
                    if rr >= self.min_rr_ratio and (net_debit / strike_width) <= self.max_debit_pct_of_width:
                        signal_id = f"sig_bps_{und}_{exp}_{int(lower['strike'])}_{int(higher['strike'])}"
                        now = datetime.now()
                        expiry = now + timedelta(hours=4)
                        confidence = min(Decimal("0.95"), Decimal("0.70") + min(Decimal("0.25"), (rr - self.min_rr_ratio) * Decimal("0.1")))

                        legs = [
                            {"symbol": higher['symbol'], "side": "BUY", "size": 1, "target_price": float(buy_price)},
                            {"symbol": lower['symbol'], "side": "SELL", "size": 1, "target_price": float(sell_price)},
                        ]

                        sig = buy_signal(
                            signal_id=signal_id,
                            strategy_id=self.strategy_id,
                            strategy_version=self.strategy_version,
                            symbol=higher['symbol'],
                            entry_price=round(net_debit, 4),
                            stop_loss=Decimal("0.0"),
                            take_profit=round(max_profit, 4),
                            proposed_size=1,
                            confidence=confidence,
                            expiry_time=expiry,
                            rationale={
                                "spread_type": "bear_put_spread",
                                "underlying": und,
                                "expiry": exp,
                                "long_leg": higher['symbol'],
                                "short_leg": lower['symbol'],
                                "strike_lower": lower['strike'],
                                "strike_higher": higher['strike'],
                                "strike_width": float(strike_width),
                                "net_debit": float(net_debit),
                                "max_profit": float(max_profit),
                                "risk_reward_ratio": float(rr),
                            },
                            legs=legs,
                        )
                        signals.append(sig)

        return signals

    def generate_signals(self, quotes: Dict[str, Any]) -> List[TradeSignal]:
        """
        Batch generate both bull call and bear put defined-risk debit spreads.
        """
        signals = []
        signals.extend(self.find_bull_call_spreads(quotes))
        signals.extend(self.find_bear_put_spreads(quotes))
        return signals
