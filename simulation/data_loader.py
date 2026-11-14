"""
Historical Data Ingestion & Storage

Handles downloading, normalizing, validating, and generating historical OHLCV
market data for futures and options backtesting.

Guarantees:
1. Strict column normalization: ['timestamp', 'open', 'high', 'low', 'close', 'volume']
2. Monotonic timestamp sorting & duplicate removal
3. Resampling across standard timeframes (1m, 5m, 15m, 1h, 1d)
4. Deterministic synthetic data generator for reproducible tests
"""
from __future__ import annotations

import os
import math
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any, Union
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


class DataLoader:
    """
    Loads and normalizes historical candle data.
    """

    REQUIRED_COLUMNS = ['timestamp', 'open', 'high', 'low', 'close', 'volume']

    @staticmethod
    def normalize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
        """
        Standardize DataFrame column names, datatypes, and sorting.
        """
        df = df.copy()
        # Lowercase all column headers
        df.columns = [c.lower().strip() for c in df.columns]

        # Map common timestamp column names
        for col in ['time', 'date', 'datetime']:
            if col in df.columns and 'timestamp' not in df.columns:
                df.rename(columns={col: 'timestamp'}, inplace=True)

        missing = set(DataLoader.REQUIRED_COLUMNS) - set(df.columns)
        if missing:
            raise ValueError(f"DataFrame missing required OHLCV columns: {missing}")

        # Ensure timestamp is datetime
        if not pd.api.types.is_datetime64_any_dtype(df['timestamp']):
            df['timestamp'] = pd.to_datetime(df['timestamp'])

        # Numeric conversions
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        # Drop NaNs, sort by timestamp ascending, drop duplicate timestamps
        df.dropna(subset=['open', 'high', 'low', 'close'], inplace=True)
        df.sort_values(by='timestamp', ascending=True, inplace=True)
        df.drop_duplicates(subset=['timestamp'], keep='last', inplace=True)
        df.reset_index(drop=True, inplace=True)

        return df[DataLoader.REQUIRED_COLUMNS]

    @staticmethod
    def load_from_csv(filepath: str) -> pd.DataFrame:
        """Load and normalize historical data from CSV file."""
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"Historical data file not found: {filepath}")
        df = pd.read_csv(filepath)
        return DataLoader.normalize_dataframe(df)

    @staticmethod
    def resample_timeframe(df: pd.DataFrame, rule: str = '5min') -> pd.DataFrame:
        """
        Resample 1m OHLCV bars into a higher timeframe (e.g., '5min', '15min', '1h', '1D').
        """
        df = DataLoader.normalize_dataframe(df)
        df = df.set_index('timestamp')

        resampled = df.resample(rule).agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum',
        }).dropna()

        resampled = resampled.reset_index()
        return resampled

    @staticmethod
    def generate_synthetic_ohlcv(
        symbol: str = "BTCUSD",
        start_date: Optional[datetime] = None,
        num_bars: int = 500,
        interval_minutes: int = 15,
        base_price: float = 60000.0,
        volatility: float = 0.003,
        trend_drift: float = 0.0001,
        seed: int = 42,
    ) -> pd.DataFrame:
        """
        Generate deterministic geometric Brownian motion OHLCV candle data
        for offline testing and simulation.
        """
        np.random.seed(seed)
        start = start_date or datetime(2026, 1, 1, 0, 0)
        timestamps = [start + timedelta(minutes=i * interval_minutes) for i in range(num_bars)]

        # Simulate log returns with drift
        returns = np.random.normal(loc=trend_drift, scale=volatility, size=num_bars)
        price_series = base_price * np.exp(np.cumsum(returns))

        data = []
        for i in range(num_bars):
            close_p = price_series[i]
            open_p = price_series[i - 1] if i > 0 else base_price
            high_noise = abs(np.random.normal(0, volatility * 0.5))
            low_noise = abs(np.random.normal(0, volatility * 0.5))

            high_p = max(open_p, close_p) * (1.0 + high_noise)
            low_p = min(open_p, close_p) * (1.0 - low_noise)
            vol = int(np.random.lognormal(mean=7.0, sigma=0.8))

            data.append({
                'timestamp': timestamps[i],
                'open': round(float(open_p), 2),
                'high': round(float(high_p), 2),
                'low': round(float(low_p), 2),
                'close': round(float(close_p), 2),
                'volume': vol,
            })

        return pd.DataFrame(data)
