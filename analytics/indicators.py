"""
Technical Indicators Engine

Vectorized implementations of EMA, RSI, ATR, Bollinger Bands, VWAP
with multi-timeframe aggregation.

All functions accept `pd.Series` or `pd.DataFrame` and return same type,
preserving index/column structure. No NaN padding — callers handle leading
NaNs from warmup periods.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Union, List, Tuple, Optional
from decimal import Decimal
import numpy as np
import pandas as pd

Price = Union[float, Decimal, np.floating]
Volume = Union[float, int, np.floating]


# ──────────────────────────────────────────────
# EMA (Exponential Moving Average)
# ──────────────────────────────────────────────
def ema(series: pd.Series, span: int, adjust: bool = True) -> pd.Series:
    """
    Exponential Moving Average.

    Args:
        series: Price series
        span: EMA span (e.g., 20 for EMA20)
        adjust: True = exponential adjustment, False = simple

    Returns:
        EMA series (same index as input)
    """
    return series.ewm(span=span, adjust=adjust).mean()


# ──────────────────────────────────────────────
# RSI (Relative Strength Index)
# ──────────────────────────────────────────────
def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Relative Strength Index (Wilder's method).

    Args:
        series: Price series (typically close)
        period: Lookback period (default 14)

    Returns:
        RSI series (range 0-100, same index as input)
    """
    delta = series.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)

    # Smoothed EMA of gains/losses
    roll = period
    ma_up = up.ewm(span=roll, adjust=False).mean()
    ma_down = down.abs().ewm(span=roll, adjust=False).mean()

    rs = ma_up / ma_down
    rsi_val = 100 - (100 / (1 + rs))
    return rsi_val


# ──────────────────────────────────────────────
# ATR (Average True Range)
# ──────────────────────────────────────────────
def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """
    Average True Range (Wilder's smoothing).

    Args:
        high: High series
        low: Low series
        close: Close series
        period: Lookback period (default 14)

    Returns:
        ATR series (same units as price, same index)
    """
    tr = pd.concat([
        (high - low).abs(),
        (high - shift).abs(),
        (low - shift).abs(),
    ], axis=1).max(axis=1)
    # Wilder's smoothing = EMA with span=period, adjust=False
    return tr.ewm(span=period, adjust=False).mean()


# ──────────────────────────────────────────────
# Bollinger Bands
# ──────────────────────────────────────────────
def bollinger_bands(
    series: pd.Series,
    window: int = 20,
    num_std: float = 2.0,
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """
    Bollinger Bands (SMA +/- k*stddev).

    Args:
        series: Price series
        window: Lookback window (default 20)
        num_std: Number of standard deviations (default 2.0)

    Returns:
        (middle_band, upper_band, lower_band) tuples
    """
    middle = series.rolling(window=window).mean()
    std = series.rolling(window=window).std()
    upper = middle + (num_std * std)
    lower = middle - (num_std * std)
    return middle, upper, lower


# ──────────────────────────────────────────────
# VWAP (Volume Weighted Average Price)
# ──────────────────────────────────────────────
def vwap(price: pd.Series, volume: pd.Series) -> pd.Series:
    """
    Volume Weighted Average Price.

    Args:
        price: Price series (typically typical price = (high+low+close)/3)
        volume: Volume series

    Returns:
        VWAP series (cumsum of price*volume / cumsum of volume)
    """
    pv = price * volume  # price * volume
    return (pv.cumsum() / volume.cumsum())


# ──────────────────────────────────────────────
# Multi-timeframe aggregation
# ──────────────────────────────────────────────
def aggregate_timeframe(
    df: pd.DataFrame,
    rule: str,
    price_cols: List[str] = None,
    volume_col: str = "volume",
) -> pd.DataFrame:
    """
    Aggregate OHLCV data to a new timeframe.

    Args:
        df: DataFrame with OHLCV data
        rule: Pandas resample rule (e.g., '1H', '4H', '1D')
        price_cols: List of price column names (['open', 'high', 'low', 'close'])
        volume_col: Volume column name

    Returns:
        Resampled DataFrame with same column structure
    """
    if price_cols is None:
        price_cols = ["open", "high", "low", "close"]

    def _ohlc(x):
        return pd.Series({
            "open": x.iloc[0]["open"],
            "high": x["high"].max(),
            "low": x["low"].min(),
            "close": x.iloc[-1]["close"],
        })

    def _volume(x):
        return x[volume_col].sum()

    result = df.groupby(pd.Grouper(key="timestamp", freq=rule)).agg({
        **{c: "first" for c in price_cols},
        "high": "max",
        "low": "last",
        volume_col: "sum",
    })
    result.columns = price_cols + ["high", "low", volume_col]
    return result


# ──────────────────────────────────────────────
# Indicator Result helper
# ──────────────────────────────────────────────
@dataclass
class IndicatorResult:
    """Container for indicator output with metadata."""
    values: dict  # name -> pd.Series
    timestamps: List[str]  # warmup notes
    notes: str  # any caveats (e.g., "EMA warmup period = span-1")

    def get(self, name: str) -> pd.Series:
        """Get a specific indicator series by name."""
        return self.values.get(name)


# Module convenience
__all__ = [
    "ema", "rsi", "atr", "bollinger_bands", "vwap",
    "aggregate_timeframe", "IndicatorResult",
]