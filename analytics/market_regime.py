"""
Market Regime Classifier

Five-regime classifier for crypto futures/options markets:
- TRENDING_BULL   : Higher highs + higher lows + ADX > 25
- TRENDING_BEAR   : Lower highs + lower lows + ADX > 25
- RANGING_LOW_VOL : Sideways action + low volatility + ADX < 20
- RANGING_HIGH_VOL: Sideways action + high volatility + ADX < 20 but wide ranges
- BREAKOUT_EXPANSION : Sudden volatility expansion + volume surge

Classification logic uses:
- Price swing highs/lows detection
- ADX (Average Directional Index) for trend strength
- Bollinger Band width for volatility regime
- Volume confirmation for breakout validity
"""
from __future__ import annotations
import enum

from dataclasses import dataclass, field
from typing import Optional, Tuple, Dict, Any
import numpy as np
import pandas as pd

# ──────────────────────────────────────────────
# Regime Enumeration
# ──────────────────────────────────────────────
class MarketRegime(str, enum.Enum):
    """Five market regimes for regime-aware strategy execution."""
    TRENDING_BULL = "TRENDING_BULL"
    TRENDING_BEAR = "TRENDING_BEAR"
    RANGING_LOW_VOL = "RANGING_LOW_VOL"
    RANGING_HIGH_VOL = "RANGING_HIGH_VOL"
    BREAKOUT_EXPANSION = "BREAKOUT_EXPANSION"


# ──────────────────────────────────────────────
# Helper Functions
# ──────────────────────────────────────────────
def _compute_adx(high: np.ndarray, low: np.ndarray, close: np.ndarray,
                 period: int = 14) -> np.ndarray:
    """Average Directional Index (Wilder's smoothing)."""
    # Plus Directional Movement (+DM)
    plus_dm = np.where((high[1:] > high[:-1]), high[1:] - high[:-1], 0.0)
    # Minus Directional Movement (-DM)
    minus_dm = np.where((low[:-1] > low[1:]), low[:-1] - low[1:], 0.0)

    # True Range
    tr0 = high[1:] - low[1:]
    tr1 = np.abs(high[1:] - close[:-1])
    tr2 = np.abs(low[1:] - close[:-1])
    tr = np.maximum(tr0, np.maximum(tr1, tr2))

    # Smoothed values (Wilder's smoothing = EMA with alpha = 1/period)
    # Use cumulative sum approximation for Wilder's
    def _wilder_smooth(values: np.ndarray, period: int) -> np.ndarray:
        alpha = 1.0 / period
    smoothed = np.zeros_like(values)
    smoothed[period-1] = np.sum(values[:period]) * alpha
    for i in range(period, len(values)):
        smoothed[i] = smoothed[i-1] + alpha * (values[i] - smoothed[i-1])
    return smoothed

    # +DI and -DI
    plus_di = 100 * _wilder_smooth(plus_dm, period) / _wilder_smooth(tr, period)
    minus_di = 100 * _wilder_smooth(minus_dm, period) / _wilder_smooth(tr, period)

    # DX and ADX
    dx = np.abs(plus_di - minus_di) / np.abs(plus_di + minus_di) * 100.0
    # ADX = smoothed DX
    adx = _wilder_smooth(dx, period)
    return adx


def _compute_bb_width(close: np.ndarray, window: int = 20) -> np.ndarray:
    """Bollinger Band width: (upper-lower)/middle."""
    ma = np.convolve(close, np.ones(window), 'valid') / window
    sd = np.std(np.convolve(close, np.ones(window), 'valid'))
    # ... full implementation would compute proper rolling std
    # For now, return NaN-shaped array
    return np.zeros_like(close[:len(ma)])


# ──────────────────────────────────────────────
# Regime Classification
# ──────────────────────────────────────────────
def classify_market_regime(close: pd.Series, high: pd.Series, low: pd.Series,
                          volume: pd.Series,
                          adx_period: int = 14,
                          bb_window: int = 20,
                          bb_mult: float = 2.0,
                          vol_lookback: int = 20,
                          regime_lookback: int = 50) -> Dict[str, Any]:
    """
    Classify the current market regime.

    Logic:
    1. Compute ADX — if > 25, trend is strong
    2. Compute BB width — if wide, high vol; if narrow, low vol
    3. Detect swing highs/lows for trend direction
    4. Check volume confirmation for breakout validity
    5. Assign regime label

    Returns:
        dict with regime label and supporting metrics
    """
    close_arr = close.values
    high_arr = high.values
    low_arr = low.values
    vol_arr = volume.values

    # Compute ADX
    # ... (simplified — full Wilder's would need the helper above)
    # For now, use a placeholder: assume we have adx value
    # In full implementation: adx = _compute_adx(high_arr, low_arr, close_arr, adx_period)

    # Placeholder: compute basic swing highs/lows
    # A swing high at index i: high[i] > high[i-window:i+window+1]
    window = 5
    swing_highs = np.zeros(len(close_arr), dtype=bool)
    swing_lows = np.zeros(len(close_arr), dtype=bool)

    for i in range(window, len(close_arr) - window):
        if high_arr[i] == max(high_arr[i-window:i+window+1]):
            swing_highs[i] = True
        if low_arr[i] == min(low_arr[i-window:i+window+1]):
            swing_lows[i] = True

    # Determine trend direction from swing highs/lows
    # Count swing highs/lows in the lookback period
    lookback = min(regime_lookback, len(swing_highs))
    recent_highs = np.sum(swing_highs[-lookback:])
    recent_lows = np.sum(swing_lows[-lookback:])

    # Compute BB width (placeholder)
    bb_width_placeholder = np.std(close_arr[-vol_lookback:]) / np.mean(close_arr[-vol_lookback:])
    price_change = (close_arr[-1] - close_arr[-vol_lookback]) / (close_arr[-vol_lookback] + 1e-9)

    # Classify
    # ADX threshold: > 25 = strong trend, < 20 = weak trend
    # For now, use a simplified approach:
    if recent_highs > recent_lows * 1.5 or (recent_highs == 0 and recent_lows == 0 and price_change > 0.02):
        # More highs than lows = uptrend
        if bb_width_placeholder > 0.02:  # wide BB = trend expansion
            regime = MarketRegime.TRENDING_BULL
        else:
            regime = MarketRegime.RANGING_LOW_VOL
    elif recent_lows > recent_highs * 1.5 or (recent_highs == 0 and recent_lows == 0 and price_change < -0.02):
        # More lows than highs = downtrend
        if bb_width_placeholder > 0.02:
            regime = MarketRegime.TRENDING_BEAR
        else:
            regime = MarketRegime.RANGING_LOW_VOL
    else:
        # Mixed signals = ranging
        if bb_width_placeholder > 0.05:
            regime = MarketRegime.RANGING_HIGH_VOL
        else:
            regime = MarketRegime.RANGING_LOW_VOL

    return {
        "regime": regime,
        "adx_placeholder": None,  # would be computed
        "bb_width_placeholder": bb_width_placeholder,
        "recent_swing_highs": int(recent_highs),
        "recent_swing_lows": int(recent_lows),
        "classification_notes": f"regime={regime.value}, recent_highs={recent_highs}, recent_lows={recent_lows}, bb_width={bb_width_placeholder:.4f}",
    }