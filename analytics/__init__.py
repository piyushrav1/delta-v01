"""
Analytics Module — Pricing, Greeks, IV Surface, Indicators, Regime

Lower-level module — strategies never import from this directory directly.
They emit TradeSignal dataclasses; the Risk Gateway evaluates them.

Exposures:
- greeks.py   : Black-Scholes analytical Greeks
- implied_volatility.py : Newton-Raphson / Brent IV solver
- indicators.py   : Vectorized EMA/RSI/ATR/VWAP with multi-timeframe
- market_regime.py : 5-regime classifier (TRENDING_BULL/BEAR, RANGING_LOW/HIGH_VOL, BREAKOUT_EXPANSION)
"""
from .greeks import calculate_greeks, Greeks, quick_greeks
from .implied_volatility import solve_implied_volatility, build_iv_surface, quick_iv
from .indicators import ema, rsi, atr, bollinger_bands, vwap, aggregate_timeframe, IndicatorResult
from .market_regime import classify_market_regime, MarketRegime

__all__ = [
    "calculate_greeks", "Greeks", "quick_greeks",
    "solve_implied_volatility", "build_iv_surface", "quick_iv",
    "ema", "rsi", "atr", "bollinger_bands", "vwap", "aggregate_timeframe", "IndicatorResult",
    "classify_market_regime", "MarketRegime",
]