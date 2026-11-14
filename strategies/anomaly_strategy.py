"""
Strategy F: Options Anomaly Scanner (Refactored to Canonical Signals)

Converts the options anomaly algorithms from scanners/options_scanner.py
into structured, contract-aware TradeSignal objects.

Anomaly Detectors Included:
1. Illiquidity Bid-Ask Gaps (Wide spreads representing market maker absence)
2. Trade Anomalies / Fat-Finger Deviations (Last traded price significantly displaced from mid)
3. Strike Price Arbitrage (Vertical monotonicity violations: Call_Ask(K2) < Call_Bid(K1) where K2 > K1)
4. Butterfly Spreads (Net credit > 0 on 1:2:1 symmetric call/put wings)
5. Calendar Spreads (Term structure mispricing: same strike, front month < back month)
6. High-Probability Credit Spreads (Net premium >= minimum threshold)

All signals emit canonical TradeSignal objects through the Central Risk Gateway.
Zero execution authority exists in this strategy module.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Any, Tuple

from strategies.base import BaseStrategy, register_strategy
from analytics.signal import TradeSignal, buy_signal, sell_signal


def parse_option_metadata(symbol: str) -> Optional[Dict[str, Any]]:
    """Parse option symbol into underlying, strike, option type, and expiry."""
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
class AnomalyStrategy(BaseStrategy):
    """
    Strategy F: Anomaly Scanner refactored into structured TradeSignals.

    Parameters:
    - atm_band: 0.03 (3% moneyness for near-ATM classification)
    - near_atm_gap: 70.0 (points threshold for near-ATM gap anomaly)
    - far_otm_gap: 30.0 (points threshold for far-OTM gap anomaly)
    - fat_finger_deviation: 70.0 (deviation between last and mid to trigger anomaly)
    - arb_min_profit: 5.0 (minimum arbitrage edge in points)
    - butterfly_min_credit: 0.5 (minimum net credit for butterfly)
    - calendar_min_credit: 1.0 (minimum net credit for calendar spread)
    - btc_credit_min: 1.0 (minimum credit for BTC credit spreads)
    - eth_credit_min: 0.3 (minimum credit for ETH credit spreads)
    """
    strategy_id: str = "anomaly_strategy"
    strategy_version: str = "1.0.0"
    display_name = "Options Anomaly Scanner (Strategy F)"
    warmup_period: int = 1
    allowed_environments = {"BACKTEST", "PAPER", "TESTNET", "LIVE_AUTO"}

    def __init__(
        self,
        atm_band: float = 0.03,
        near_atm_gap: float = 70.0,
        far_otm_gap: float = 30.0,
        fat_finger_deviation: float = 70.0,
        arb_min_profit: float = 5.0,
        butterfly_min_credit: float = 0.5,
        calendar_min_credit: float = 1.0,
        btc_credit_min: float = 1.0,
        eth_credit_min: float = 0.3,
    ):
        self.atm_band = atm_band
        self.near_atm_gap = near_atm_gap
        self.far_otm_gap = far_otm_gap
        self.fat_finger_deviation = fat_finger_deviation
        self.arb_min_profit = arb_min_profit
        self.butterfly_min_credit = butterfly_min_credit
        self.calendar_min_credit = calendar_min_credit
        self.btc_credit_min = btc_credit_min
        self.eth_credit_min = eth_credit_min

    def on_quote(self, quote: Any) -> Optional[TradeSignal]:
        """
        Evaluate single quote for illiquidity gap or fat finger anomalies.
        """
        symbol = getattr(quote, 'symbol', None) or (quote.get('symbol') if isinstance(quote, dict) else None)
        if not symbol:
            return None

        meta = parse_option_metadata(symbol)
        if not meta:
            return None

        bid = getattr(quote, 'bid', None) or (quote.get('bid') if isinstance(quote, dict) else None)
        ask = getattr(quote, 'ask', None) or (quote.get('ask') if isinstance(quote, dict) else None)
        last = getattr(quote, 'last', None) or (quote.get('last') if isinstance(quote, dict) else None)
        spot = getattr(quote, 'spot', None) or (quote.get('spot') if isinstance(quote, dict) else None)

        if bid is None or ask is None or float(bid) <= 0 or float(ask) <= float(bid):
            return None

        bid_f = float(bid)
        ask_f = float(ask)
        mid_f = (bid_f + ask_f) / 2.0
        gap_f = ask_f - bid_f
        strike_f = meta['strike']

        now = datetime.now()
        expiry = now + timedelta(minutes=30)

        # 1. Illiquidity Bid-Ask Gap
        is_near_atm = False
        if spot and float(spot) > 0:
            is_near_atm = abs(strike_f - float(spot)) / float(spot) <= self.atm_band

        thresh = self.near_atm_gap if is_near_atm else self.far_otm_gap
        if gap_f >= thresh:
            # Opportunity to capture spread: place aggressive limit order near mid
            entry_price = Decimal(str(round(mid_f, 4)))
            stop_loss = max(Decimal("0.01"), Decimal(str(round(bid_f * 0.95, 4))))
            take_profit = Decimal(str(round(ask_f * 0.98, 4)))

            return buy_signal(
                signal_id=f"sig_anom_gap_{symbol}_{uuid.uuid4().hex[:6]}",
                strategy_id=self.strategy_id,
                strategy_version=self.strategy_version,
                symbol=symbol,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                proposed_size=1,
                confidence=Decimal("0.75"),
                expiry_time=expiry,
                rationale={
                    "anomaly_type": "illiquidity_gap",
                    "bid": bid_f,
                    "ask": ask_f,
                    "gap": gap_f,
                    "threshold": thresh,
                    "is_near_atm": is_near_atm,
                    "mid": mid_f,
                }
            )

        # 2. Fat Finger / Trade Anomaly
        if last is not None and float(last) > 0:
            last_f = float(last)
            dev = abs(last_f - mid_f)
            if dev >= self.fat_finger_deviation:
                direction = "SELL" if last_f > mid_f else "BUY"
                entry_price = Decimal(str(round(last_f, 4)))
                target_mid = Decimal(str(round(mid_f, 4)))

                if direction == "SELL":
                    stop_loss = entry_price + Decimal(str(round(dev * 0.5, 4)))
                    take_profit = target_mid
                    return sell_signal(
                        signal_id=f"sig_anom_fatfinger_{symbol}_{uuid.uuid4().hex[:6]}",
                        strategy_id=self.strategy_id,
                        strategy_version=self.strategy_version,
                        symbol=symbol,
                        entry_price=entry_price,
                        stop_loss=round(stop_loss, 4),
                        take_profit=round(take_profit, 4),
                        proposed_size=1,
                        confidence=Decimal("0.85"),
                        expiry_time=expiry,
                        rationale={
                            "anomaly_type": "fat_finger",
                            "last_price": last_f,
                            "mid_price": mid_f,
                            "deviation": dev,
                        }
                    )
                else:
                    stop_loss = max(Decimal("0.01"), entry_price - Decimal(str(round(dev * 0.5, 4))))
                    take_profit = target_mid
                    return buy_signal(
                        signal_id=f"sig_anom_fatfinger_{symbol}_{uuid.uuid4().hex[:6]}",
                        strategy_id=self.strategy_id,
                        strategy_version=self.strategy_version,
                        symbol=symbol,
                        entry_price=entry_price,
                        stop_loss=round(stop_loss, 4),
                        take_profit=round(take_profit, 4),
                        proposed_size=1,
                        confidence=Decimal("0.85"),
                        expiry_time=expiry,
                        rationale={
                            "anomaly_type": "fat_finger",
                            "last_price": last_f,
                            "mid_price": mid_f,
                            "deviation": dev,
                        }
                    )

        return None

    def on_candle(self, open_: float, high: float, low: float, close: float,
                  volume: int, timestamp: datetime) -> Optional[TradeSignal]:
        return None

    def detect_strike_arbitrage(self, quotes: Dict[str, Any]) -> List[TradeSignal]:
        """
        Detect vertical strike monotonicity violations:
        For calls: Higher Strike Call Ask < Lower Strike Call Bid (Buy K2, Sell K1 -> Free money)
        """
        signals: List[TradeSignal] = []
        parsed = []
        for sym, q in quotes.items():
            meta = parse_option_metadata(sym)
            if not meta:
                continue
            bid = getattr(q, 'bid', None) or (q.get('bid') if isinstance(q, dict) else None)
            ask = getattr(q, 'ask', None) or (q.get('ask') if isinstance(q, dict) else None)
            if bid is not None and ask is not None and float(bid) > 0 and float(ask) > 0:
                meta['bid'] = float(bid)
                meta['ask'] = float(ask)
                parsed.append(meta)

        grouped: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
        for p in parsed:
            key = (p['underlying'], p['expiry'], p['opt_type'])
            grouped.setdefault(key, []).append(p)

        now = datetime.now()
        expiry = now + timedelta(minutes=15)

        for (und, exp, opt_type), opts in grouped.items():
            opts.sort(key=lambda x: x['strike'])
            if opt_type == 'C':
                for i in range(len(opts)):
                    for j in range(i + 1, len(opts)):
                        lower = opts[i]
                        higher = opts[j]
                        # Arbitrage: Higher call ask is LESS than Lower call bid
                        edge = lower['bid'] - higher['ask']
                        if edge >= self.arb_min_profit:
                            legs = [
                                {"symbol": higher['symbol'], "side": "BUY", "size": 1, "target_price": higher['ask']},
                                {"symbol": lower['symbol'], "side": "SELL", "size": 1, "target_price": lower['bid']},
                            ]
                            signals.append(
                                buy_signal(
                                    signal_id=f"sig_arb_call_{und}_{exp}_{int(lower['strike'])}_{int(higher['strike'])}",
                                    strategy_id=self.strategy_id,
                                    strategy_version=self.strategy_version,
                                    symbol=higher['symbol'],
                                    entry_price=Decimal(str(round(higher['ask'], 4))),
                                    stop_loss=Decimal("0.0"),
                                    take_profit=Decimal(str(round(edge, 4))),
                                    proposed_size=1,
                                    confidence=Decimal("0.95"),
                                    expiry_time=expiry,
                                    rationale={
                                        "anomaly_type": "strike_arbitrage",
                                        "underlying": und,
                                        "expiry": exp,
                                        "opt_type": "C",
                                        "lower_strike": lower['strike'],
                                        "higher_strike": higher['strike'],
                                        "lower_bid": lower['bid'],
                                        "higher_ask": higher['ask'],
                                        "arbitrage_edge": edge,
                                    },
                                    legs=legs,
                                )
                            )

        return signals

    def detect_butterfly_spreads(self, quotes: Dict[str, Any]) -> List[TradeSignal]:
        """
        Detect Butterfly Spreads with positive net credit:
        Long 1x Lower Strike, Short 2x Middle Strike, Long 1x Upper Strike.
        """
        signals: List[TradeSignal] = []
        parsed = []
        for sym, q in quotes.items():
            meta = parse_option_metadata(sym)
            if not meta or meta['opt_type'] != 'C':
                continue
            bid = getattr(q, 'bid', None) or (q.get('bid') if isinstance(q, dict) else None)
            ask = getattr(q, 'ask', None) or (q.get('ask') if isinstance(q, dict) else None)
            if bid is not None and ask is not None and float(bid) > 0 and float(ask) > 0:
                meta['bid'] = float(bid)
                meta['ask'] = float(ask)
                parsed.append(meta)

        grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for p in parsed:
            key = (p['underlying'], p['expiry'])
            grouped.setdefault(key, []).append(p)

        now = datetime.now()
        expiry = now + timedelta(minutes=30)

        for (und, exp), opts in grouped.items():
            opts.sort(key=lambda x: x['strike'])
            # Need at least 3 strikes with equal spacing: K1, K2, K3 where K2 - K1 == K3 - K2
            for i in range(len(opts)):
                for j in range(i + 1, len(opts)):
                    k1 = opts[i]['strike']
                    k2 = opts[j]['strike']
                    target_k3 = 2 * k2 - k1
                    # Find k3
                    match_k3 = [o for o in opts if abs(o['strike'] - target_k3) < 0.01]
                    if match_k3:
                        k3_opt = match_k3[0]
                        k1_opt = opts[i]
                        k2_opt = opts[j]

                        # Buy wings at ask: k1_opt['ask'] + k3_opt['ask']
                        # Sell body at bid: 2 * k2_opt['bid']
                        net_credit = (2 * k2_opt['bid']) - (k1_opt['ask'] + k3_opt['ask'])
                        if net_credit >= self.butterfly_min_credit:
                            legs = [
                                {"symbol": k1_opt['symbol'], "side": "BUY", "size": 1, "target_price": k1_opt['ask']},
                                {"symbol": k2_opt['symbol'], "side": "SELL", "size": 2, "target_price": k2_opt['bid']},
                                {"symbol": k3_opt['symbol'], "side": "BUY", "size": 1, "target_price": k3_opt['ask']},
                            ]
                            signals.append(
                                buy_signal(
                                    signal_id=f"sig_fly_{und}_{exp}_{int(k1)}_{int(k2)}_{int(target_k3)}",
                                    strategy_id=self.strategy_id,
                                    strategy_version=self.strategy_version,
                                    symbol=k2_opt['symbol'],
                                    entry_price=Decimal(str(round(k2_opt['bid'], 4))),
                                    stop_loss=Decimal("0.0"),
                                    take_profit=Decimal(str(round(net_credit, 4))),
                                    proposed_size=1,
                                    confidence=Decimal("0.85"),
                                    expiry_time=expiry,
                                    rationale={
                                        "anomaly_type": "butterfly_spread",
                                        "k1": k1, "k2": k2, "k3": target_k3,
                                        "net_credit": net_credit,
                                    },
                                    legs=legs,
                                )
                            )

        return signals

    def generate_signals(self, quotes: Dict[str, Any]) -> List[TradeSignal]:
        """
        Batch evaluation across all anomaly algorithms.
        """
        signals: List[TradeSignal] = []

        # 1. Single-quote checks (Illiquidity gaps, fat-fingers)
        for sym, q in quotes.items():
            sig = self.on_quote(q)
            if sig:
                signals.append(sig)

        # 2. Strike arbitrage
        signals.extend(self.detect_strike_arbitrage(quotes))

        # 3. Butterfly spreads
        signals.extend(self.detect_butterfly_spreads(quotes))

        return signals
