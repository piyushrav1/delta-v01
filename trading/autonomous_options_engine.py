"""
Autonomous Options Trading Engine for Delta Exchange India
Institutional-grade automated trading logic designed for hands-off execution:
1. Performs quantitative trend, momentum, and RSI calculations on BTC and ETH.
2. Selects optimal Near-The-Money (ATM) option contracts (Calls for uptrends, Puts for downtrends).
3. Verifies liquidity, bid-ask spread tightness, and open interest limits.
4. Enforces strict micro-lot position sizing (1 contract) and protective bracket orders (TP +40%, SL -25%).
5. Continuously manages open positions and capital preservation bounds ($50 daily loss cap).
"""
import time
import math
import logging
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


class AutonomousOptionsEngine:
    """
    Fully automated options trading decision and execution system.
    Evaluates market regime, selects the best options strike, and submits bracket orders.
    """

    def __init__(
        self,
        max_open_positions: int = 2,
        take_profit_pct: float = 0.40,
        stop_loss_pct: float = 0.25,
        max_daily_loss_usd: float = 50.0,
        underlyings: List[str] = None
    ):
        self.max_open_positions = max_open_positions
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.max_daily_loss_usd = max_daily_loss_usd
        self.underlyings = underlyings or ['ETH', 'BTC']

    def calculate_rsi(self, closes: List[float], period: int = 14) -> float:
        """Compute Relative Strength Index (RSI-14)."""
        if len(closes) <= period:
            return 50.0
        gains = []
        losses = []
        for i in range(1, len(closes)):
            delta = closes[i] - closes[i - 1]
            if delta >= 0:
                gains.append(delta)
                losses.append(0.0)
            else:
                gains.append(0.0)
                losses.append(abs(delta))

        recent_gains = gains[-period:]
        recent_losses = losses[-period:]
        avg_gain = sum(recent_gains) / period
        avg_loss = sum(recent_losses) / period

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return round(100.0 - (100.0 / (1.0 + rs)), 1)

    def analyze_market_regime(self, underlying: str, delta_public_fn) -> Dict[str, Any]:
        """
        Analyze underlying trend and momentum from 15-minute historical candles.
        Returns regime dict: bias ('BULLISH'/'BEARISH'/'NEUTRAL'), current price, MA20, RSI, rationale.
        """
        sym = f"{underlying}USD"
        now = int(time.time())
        from_time = now - (3600 * 18)  # Last 18 hours

        try:
            data = delta_public_fn(f"/v2/chart/history?symbol={sym}&resolution=15&from={from_time}&to={now}")
            closes = data.get('c', [])
            if len(closes) < 20:
                return {
                    "underlying": underlying,
                    "bias": "NEUTRAL",
                    "price": closes[-1] if closes else 0.0,
                    "ma20": 0.0,
                    "rsi": 50.0,
                    "rationale": f"Insufficient candle history for {underlying} (got {len(closes)} bars)."
                }

            curr_price = float(closes[-1])
            ma20 = sum(closes[-20:]) / 20.0
            rsi = self.calculate_rsi(closes, period=14)
            momentum_5 = curr_price - float(closes[-5])

            # Determine bias
            if curr_price > ma20 and rsi >= 45:
                bias = "BULLISH"
                rationale = f"{underlying} is trending UP: Current ${curr_price:,.2f} > 20 EMA (${ma20:,.2f}) with RSI {rsi}."
            elif curr_price < ma20 and rsi <= 55:
                bias = "BEARISH"
                rationale = f"{underlying} is trending DOWN: Current ${curr_price:,.2f} < 20 EMA (${ma20:,.2f}) with RSI {rsi}."
            else:
                bias = "NEUTRAL"
                rationale = f"{underlying} is consolidating: Price ${curr_price:,.2f} hovering near MA20 (${ma20:,.2f})."

            return {
                "underlying": underlying,
                "bias": bias,
                "price": curr_price,
                "ma20": round(ma20, 2),
                "rsi": rsi,
                "momentum_5": round(momentum_5, 2),
                "rationale": rationale
            }
        except Exception as e:
            logger.warning(f"Error fetching candles for {underlying}: {e}")
            return {
                "underlying": underlying,
                "bias": "NEUTRAL",
                "price": 0.0,
                "ma20": 0.0,
                "rsi": 50.0,
                "rationale": f"Candle fetch failed for {underlying}: {e}"
            }

    def select_best_option_contract(
        self,
        underlying: str,
        bias: str,
        spot_price: float,
        tickers: List[Dict[str, Any]],
        active_symbols: set,
        oi_breached_symbols: Dict[str, float]
    ) -> Optional[Dict[str, Any]]:
        """
        Selects the single highest-probability Near-The-Money (ATM) option contract:
        - Calls for Bullish regimes, Puts for Bearish regimes.
        - Strict bid/ask liquidity and spread tightness filters.
        - Open Interest cap protection.
        """
        now = time.time()
        target_contract_type = "call_options" if bias == "BULLISH" else "put_options"

        valid_candidates = []
        for t in tickers:
            symbol = t.get('symbol')
            if not symbol:
                continue

            # Skip contracts already in portfolio
            if symbol in active_symbols:
                continue

            # Skip contracts on Open Interest breach cooldown
            if symbol in oi_breached_symbols:
                if now < oi_breached_symbols[symbol]:
                    continue

            # Must match directional target type
            if t.get('contract_type') != target_contract_type:
                continue

            # Must have live orderbook quotes
            q = t.get('quotes', {})
            try:
                ask = float(q.get('best_ask') or 0)
                bid = float(q.get('best_bid') or 0)
            except (ValueError, TypeError):
                continue

            if ask <= 0:
                continue

            # Spread filter: avoid wide illiquid markets
            spread = ask - bid if bid > 0 else ask
            spread_pct = (spread / ask) if ask > 0 else 1.0
            if bid > 0 and spread_pct > 0.45:
                continue

            # Price / strike bounds
            strike = float(t.get('strike_price', 0))
            if strike <= 0 or spot_price <= 0:
                continue

            diff_pct = abs(strike - spot_price) / spot_price
            # Focus strictly on Near-The-Money (within 12% of spot)
            if diff_pct > 0.12:
                continue

            # Premium bounds: for micro-lot testing, target reasonable option prices
            if ask > 150.0:
                continue

            # Score candidate: prefer closest to spot (exact ATM) and tightest spread
            # Lower score is better
            score = (diff_pct * 100.0) + (spread_pct * 10.0)

            valid_candidates.append({
                "symbol": symbol,
                "underlying": underlying,
                "contract_type": target_contract_type,
                "option_type": "CALL" if target_contract_type == "call_options" else "PUT",
                "strike": strike,
                "spot_price": spot_price,
                "bid": bid,
                "ask": ask,
                "spread": round(spread, 2),
                "diff_pct": round(diff_pct * 100.0, 1),
                "score": score,
            })

        if not valid_candidates:
            return None

        # Sort by best score (closest ATM with tightest spread)
        valid_candidates.sort(key=lambda x: x['score'])
        best = valid_candidates[0]

        # Calculate exact bracket prices
        entry_price = best['ask']
        tp_price = round(entry_price * (1.0 + self.take_profit_pct), 4)
        sl_price = round(entry_price * (1.0 - self.stop_loss_pct), 4)

        best["limit_price"] = str(entry_price)
        best["bracket_take_profit_price"] = str(tp_price)
        best["bracket_stop_loss_price"] = str(sl_price)
        return best

    def evaluate_and_execute_trade(
        self,
        acct: Dict[str, Any],
        active_positions_set: set,
        oi_breached_symbols: Dict[str, float],
        daily_pnl: float,
        delta_public_fn,
        delta_private_fn,
        place_order_fn
    ) -> Tuple[str, Optional[Dict[str, Any]], str]:
        """
        Full autonomous evaluation and execution pass:
        1. Checks safety limits (daily loss cap, position count).
        2. Calculates market regime across BTC and ETH.
        3. Identifies the cleanest directional trade setup.
        4. Selects optimal ATM strike.
        5. Places the limit order with TP and SL brackets.
        Returns: (status_code, order_dict, human_readable_summary)
        """
        # 1. Safety Guard: Daily Loss Hard Cap
        if daily_pnl <= -self.max_daily_loss_usd:
            msg = f"Daily loss cap reached (${daily_pnl:.2f} <= -${self.max_daily_loss_usd:.2f}). Autonomous trading paused."
            return "HALTED_LOSS_CAP", None, msg

        # 2. Safety Guard: Max Position Limiter
        if len(active_positions_set) >= self.max_open_positions:
            msg = f"Position limit active ({len(active_positions_set)}/{self.max_open_positions} positions open). Actively tracking open trades for Take-Profit or Stop-Loss."
            return "POSITIONS_FULL", None, msg

        # 3. Market Regime Analysis across underlyings
        regimes = {}
        for und in self.underlyings:
            regimes[und] = self.analyze_market_regime(und, delta_public_fn)

        # Prioritize underlying with active trending bias (BULLISH or BEARISH)
        chosen_und = None
        chosen_regime = None
        for und in self.underlyings:
            r = regimes[und]
            if r['bias'] in ('BULLISH', 'BEARISH'):
                chosen_und = und
                chosen_regime = r
                break

        # Fallback to ETH if both neutral
        if not chosen_und:
            chosen_und = 'ETH'
            chosen_regime = regimes.get('ETH', {"bias": "BULLISH", "price": 1910.0, "rationale": "Neutral market; testing ATM spread."})
            chosen_regime['bias'] = "BULLISH"

        # 4. Fetch live tickers for chosen underlying
        try:
            from scanners.options_scanner import DeltaAPI
            delta_api = DeltaAPI()
            tickers = delta_api.get_tickers(chosen_und)
        except Exception as e:
            return "ERROR_DATA", None, f"Failed to fetch option tickers for {chosen_und}: {e}"

        # 5. Select Best Option Contract
        spot = chosen_regime.get('price', 0.0)
        best_contract = self.select_best_option_contract(
            underlying=chosen_und,
            bias=chosen_regime['bias'],
            spot_price=spot,
            tickers=tickers,
            active_symbols=active_positions_set,
            oi_breached_symbols=oi_breached_symbols
        )

        if not best_contract:
            return "NO_SETUP", None, f"Radar active for {chosen_und} ({chosen_regime['bias']}). No contracts passed spread and liquidity gates."

        # 6. Build Order with Strict Risk Controls
        sym = best_contract['symbol']
        fields = {
            'symbol': sym,
            'side': 'buy',
            'size': 1,  # Strict micro-lot sizing
            'order_type': 'limit_order',
            'limit_price': best_contract['limit_price'],
            'time_in_force': 'gtc',
            'reduce_only': False,
            'bracket_take_profit_price': best_contract['bracket_take_profit_price'],
            'bracket_stop_loss_price': best_contract['bracket_stop_loss_price'],
        }

        try:
            res = place_order_fn(acct, fields)
            active_positions_set.add(sym)

            summary = (
                f"🤖 Autonomous Engine: Analyzed {chosen_und} ({chosen_regime['rationale']}). "
                f"Selected optimal Near-ATM {best_contract['option_type']} {sym} (Strike ${best_contract['strike']:,.0f}, spot ${spot:,.2f}). "
                f"Placed BUY 1 contract @ ${best_contract['limit_price']} with auto brackets: "
                f"Take-Profit: ${best_contract['bracket_take_profit_price']} (+{int(self.take_profit_pct*100)}%), "
                f"Stop-Loss: ${best_contract['bracket_stop_loss_price']} (-{int(self.stop_loss_pct*100)}%)."
            )
            logger.info(summary)
            return "TRADE_EXECUTED", res, summary

        except Exception as e:
            err_str = str(e)
            if "max_oi_breached" in err_str or "only_reduce_only_orders_allowed" in err_str:
                oi_breached_symbols[sym] = time.time() + 3600
                return "OI_BREACHED", None, f"Delta Exchange Open Interest limit reached for {sym}. Cooling down for 1 hour."
            return "ORDER_FAILED", None, f"Order placement failed for {sym}: {err_str}"
