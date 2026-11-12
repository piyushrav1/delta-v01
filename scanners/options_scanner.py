"""
Options Anomaly Scanner for Delta Exchange India
Detects various options anomalies including:
- Illiquidity/bid-ask price gaps
- Huge bid-ask gaps with trades
- Credit spread opportunities
- Butterfly spreads
- Calendar spreads
- Bull spreads
"""

import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import os
import math
import logging
from dotenv import load_dotenv

load_dotenv()

# Configuration
DELTA_API_BASE = os.getenv("DELTA_API_BASE_URL", "https://api.india.delta.exchange")
logger = logging.getLogger(__name__)

class DeltaAPI:
    """Wrapper for Delta Exchange India API"""

    def __init__(self, base_url=None):
        self.base_url = base_url or DELTA_API_BASE.rstrip('/')
        self.session = requests.Session()

    def get_products(self):
        """Fetch all options products"""
        url = f"{self.base_url}/v2/products?contract_types=call_options,put_options"
        try:
            resp = self.session.get(url, timeout=10)
            data = resp.json()
            return data.get('result', [])
        except Exception as e:
            logger.error(f"Error fetching products: {e}")
            return []

    def get_tickers(self, underlying=None, expiry_date=None):
        """Fetch tickers for options"""
        url = f"{self.base_url}/v2/tickers?contract_types=call_options,put_options"
        if underlying:
            url += f"&underlying_asset_symbols={underlying}"
        if expiry_date:
            # expiry_date expected in DD-MM-YYYY
            url += f"&expiry_date={expiry_date}"

        try:
            resp = self.session.get(url, timeout=10)
            data = resp.json()
            return data.get('result', [])
        except Exception as e:
            logger.error(f"Error fetching tickers: {e}")
            return []

class AnomalyDetector:
    """Detects various options anomalies"""

    def __init__(self):
        self.delta_api = DeltaAPI()

    def _parse_ticker(self, t):
        try:
            q = t.get('quotes', {})
            bb = q.get('best_bid')
            ba = q.get('best_ask')
            return {
                'symbol': t.get('symbol'),
                'product_id': t.get('product_id'),
                'strike': float(t.get('strike_price', 0)),
                'opt_type': 'C' if t.get('contract_type') == 'call_options' else 'P',
                'bid': float(bb) if bb is not None and bb != '' else None,
                'ask': float(ba) if ba is not None and ba != '' else None,
                'last': float(t.get('close')) if t.get('close') is not None and t.get('close') != '' else None,
                'spot': float(t.get('spot_price')) if t.get('spot_price') is not None else None,
            }
        except Exception:
            return None

    def _mid(self, q):
        return (q['bid'] + q['ask']) / 2 if q['bid'] is not None and q['ask'] is not None else None

    def _gap_of(self, q):
        return (q['ask'] - q['bid']) if q['bid'] is not None and q['ask'] is not None else None

    def _is_near_atm(self, strike, spot, band=0.03):
        return abs(strike - spot) / spot <= band if spot else False

    def _is_otm(self, q, spot):
        if not spot: return True
        return q['strike'] >= spot if q['opt_type'] == 'C' else q['strike'] <= spot

    def _gcd(self, a, b):
        a, b = abs(int(a)), abs(int(b))
        while b:
            a, b = b, a % b
        return a or 1

    def scan(self, underlyings=['BTC', 'ETH'], config=None):
        """Run full scan cycle for given underlyings"""
        if config is None:
            config = {
                'atm_band': 0.03,
                'far_otm_gap': 30,
                'near_atm_gap': 70,
                'fat_finger': 70,
                'arb_thresh': 200,
                'fly_thresh': 0,
                'cal_thresh': 0,
                'eth_credit_min': 0.3,
                'btc_credit_min': 1.0
            }

        all_anomalies = []

        for underlying in underlyings:
            try:
                products = self.delta_api.get_products()
                expiries = sorted(list(set(
                    p['settlement_time'][:10] for p in products
                    if p.get('underlying_asset', {}).get('symbol') == underlying
                )))[:3]

                if not expiries:
                    continue

                by_expiry = {}
                spot = 0

                for idx, expiry_ymd in enumerate(expiries):
                    y, m, d = expiry_ymd.split('-')
                    expiry_param = f"{d}-{m}-{y}"
                    raw_tickers = self.delta_api.get_tickers(underlying, expiry_param)

                    all_quotes = [q for q in [self._parse_ticker(t) for t in raw_tickers] if q]
                    if not spot:
                        for q in all_quotes:
                            if q['spot']:
                                spot = q['spot']
                                break

                    quotes = [q for q in all_quotes if self._is_otm(q, spot)]
                    by_expiry[expiry_ymd] = quotes
                    label = ['current', '+1', '+2'][idx] if idx < 3 else f"+{idx}"

                    # Detect anomalies
                    all_anomalies.extend(self.detect_illiquidity_gaps(quotes, spot, underlying, label, config))
                    all_anomalies.extend(self.detect_trade_anomaly(quotes, spot, underlying, label, config))
                    all_anomalies.extend(self.detect_strike_price_anomalies(quotes, underlying, label, config))
                    all_anomalies.extend(self.detect_butterfly_spreads(quotes, underlying, label, config))

                    if idx <= 1:
                        min_credit = config['eth_credit_min'] if underlying == 'ETH' else config['btc_credit_min']
                        all_anomalies.extend(self.detect_credit_spreads(quotes, underlying, label, min_credit))

                if len(expiries) >= 2:
                    all_anomalies.extend(self.detect_calendar_spreads(by_expiry[expiries[0]], by_expiry[expiries[1]], underlying, config))

            except Exception as e:
                logger.error(f"Error scanning {underlying}: {e}")

        return all_anomalies

    def detect_illiquidity_gaps(self, quotes, spot, underlying, label, c):
        anomalies = []
        for q in quotes:
            g = self._gap_of(q)
            if g is None: continue
            near = self._is_near_atm(q['strike'], spot, c['atm_band'])
            th = c['near_atm_gap'] if near else c['far_otm_gap']
            if g >= th:
                anomalies.append({
                    'strategy': 'liquidity_gap',
                    'category': 'illiquidity',
                    'severity': 'info',
                    'text': f"<b>{underlying} {label}</b> {q['symbol']} ({'NEAR-ATM' if near else 'FAR-OTM'}): bid {q['bid']} / ask {q['ask']} → gap {g:.1f} (≥{th})"
                })
        return anomalies

    def detect_trade_anomaly(self, quotes, spot, underlying, label, c):
        anomalies = []
        for q in quotes:
            if not self._is_near_atm(q['strike'], spot, c['atm_band']): continue
            m = self._mid(q)
            if q['last'] is None or m is None: continue
            dev = q['last'] - m
            if abs(dev) >= c['fat_finger']:
                anomalies.append({
                    'strategy': 'fat_finger',
                    'category': 'trade_anomalies',
                    'severity': 'action',
                    'text': f"<b>{underlying} {label}</b> {q['symbol']}: last trade {q['last']} is {abs(dev):.1f} {'ABOVE' if dev > 0 else 'BELOW'} mid {m:.1f} (bid {q['bid']}/ask {q['ask']})"
                })
        return anomalies

    def detect_strike_price_anomalies(self, quotes, underlying, label, c):
        """Strike arbitrage / glitch detection"""
        anomalies = []
        for opt_type in ['C', 'P']:
            legs = sorted([q for q in quotes if q['opt_type'] == opt_type and self._mid(q) is not None], key=lambda x: x['strike'])
            for i in range(len(legs) - 1):
                a, b = legs[i], legs[i+1]
                ma, mb = self._mid(a), self._mid(b)
                violation = (mb - ma) if opt_type == 'C' else (ma - mb)
                if violation >= c['arb_thresh']:
                    anomalies.append({
                        'strategy': 'arbitrage',
                        'category': 'strike_anomalies',
                        'severity': 'action',
                        'text': f"<b>{underlying} {label}</b> {opt_type}: {a['symbol']} mid {ma:.1f} vs {b['symbol']} mid {mb:.1f} → violation {violation:.1f} (≥{c['arb_thresh']}). 1:1 arb = buy cheap / sell rich."
                    })
        return anomalies

    def detect_butterfly_spreads(self, quotes, underlying, label, c):
        anomalies = []
        for opt_type in ['C', 'P']:
            legs = sorted([q for q in quotes if q['opt_type'] == opt_type and self._mid(q) is not None], key=lambda x: x['strike'])
            for i in range(len(legs) - 2):
                k1, k2, k3 = legs[i], legs[i+1], legs[i+2]
                gap1 = k2['strike'] - k1['strike']
                gap2 = k3['strike'] - k2['strike']
                if gap1 <= 0 or gap2 <= 0: continue
                g = self._gcd(gap1, gap2)
                r1, r3 = gap2/g, gap1/g
                r_body = r1 + r3
                m1, m2, m3 = self._mid(k1), self._mid(k2), self._mid(k3)
                net_credit = (r_body * m2) - (r1 * m1) - (r3 * m3)
                if net_credit >= c['fly_thresh']:
                    anomalies.append({
                        'strategy': 'butterfly',
                        'category': 'butterfly_spreads',
                        'severity': 'info',
                        'text': f"<b>{underlying} {label}</b> {opt_type} {int(r1)}:{int(r_body)}:{int(r3)}: {k1['symbol']}/{k2['symbol']}/{k3['symbol']} net credit {net_credit:.1f} (≥{c['fly_thresh']})"
                    })
        return anomalies

    def detect_calendar_spreads(self, near_quotes, far_quotes, underlying, c):
        anomalies = []
        far_by_key = {f"{q['strike']}_{q['opt_type']}": q for q in far_quotes}
        for nq in near_quotes:
            key = f"{nq['strike']}_{nq['opt_type']}"
            if key not in far_by_key: continue
            fq = far_by_key[key]
            nm, fm = self._mid(nq), self._mid(fq)
            if nm is None or fm is None: continue
            credit = nm - fm
            if credit >= c['cal_thresh']:
                anomalies.append({
                    'strategy': 'calendar',
                    'category': 'calendar_spreads',
                    'severity': 'info',
                    'text': f"<b>{underlying}</b> {nq['opt_type']} {nq['strike']}: near {nq['symbol']} mid {nm:.2f} > far {fq['symbol']} mid {fm:.2f} → credit {credit:.2f} (≥{c['cal_thresh']})"
                })
        return anomalies

    def detect_credit_spreads(self, quotes, underlying, label, min_credit):
        anomalies = []
        strat_key = 'credit_spread_eth' if underlying == 'ETH' else 'credit_spread_btc'
        for opt_type in ['C', 'P']:
            legs = sorted([q for q in quotes if q['opt_type'] == opt_type and self._mid(q) is not None], key=lambda x: x['strike'])
            for i in range(len(legs) - 1):
                a, b = legs[i], legs[i+1]
                ma, mb = self._mid(a), self._mid(b)
                sell_leg = a if ma >= mb else b
                buy_leg = b if ma >= mb else a
                credit = abs(ma - mb)
                if credit >= min_credit:
                    kind = 'call-to-call' if opt_type == 'C' else 'put-to-put'
                    anomalies.append({
                        'strategy': strat_key,
                        'category': 'credit_spreads',
                        'severity': 'info',
                        'text': f"<b>{underlying} {label}</b> {kind} credit spread: sell {sell_leg['symbol']} ({self._mid(sell_leg):.2f}) / buy {buy_leg['symbol']} ({self._mid(buy_leg):.2f}) → credit {credit:.2f} (min {min_credit})"
                    })
        return anomalies

    def get_segmented_scans(self, all_anomalies):
        segregated = {
            "arbitrage": [],
            "fat_finger": [],
            "liquidity_gap": [],
            "butterfly": [],
            "calendar": [],
            "credit_spread_eth": [],
            "credit_spread_btc": []
        }
        for anomaly in all_anomalies:
            strat = anomaly.get("strategy")
            if strat in segregated:
                segregated[strat].append(anomaly)
        return segregated
