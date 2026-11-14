"""
Phase 5 Tests: Analytics, Greeks, Implied Volatility, Indicators, and Market Regime
"""
import pytest
from decimal import Decimal
import pandas as pd
import numpy as np

from analytics.greeks import calculate_greeks, GreeksInput, quick_greeks, Greeks
from analytics.implied_volatility import solve_implied_volatility, quick_iv
from analytics.indicators import ema, rsi, atr, bollinger_bands, vwap
from analytics.market_regime import classify_market_regime, MarketRegime


class TestGreeks:
    def test_call_delta_atm(self):
        inp = GreeksInput(
            S=Decimal("50000"),
            K=Decimal("50000"),
            T=Decimal("0.1"),
            r=Decimal("0.05"),
            q=Decimal("0"),
            sigma=Decimal("0.5"),
        )
        greeks = calculate_greeks(inp)
        assert isinstance(greeks, Greeks)
        # ATM call delta should be around 0.5 - 0.65
        assert Decimal("0.45") <= greeks.delta <= Decimal("0.65")
        assert greeks.gamma > Decimal("0")
        assert greeks.vega > Decimal("0")
        assert greeks.theta < Decimal("0")

    def test_quick_greeks_convenience(self):
        g = quick_greeks(S=50000.0, K=50000.0, T=0.1, r=0.05, sigma=0.5)
        assert g is not None
        assert 0.4 < float(g.delta) < 0.7


class TestImpliedVolatility:
    def test_solve_implied_volatility_known(self):
        inp = GreeksInput(
            S=Decimal("50000"),
            K=Decimal("50000"),
            T=Decimal("0.1"),
            r=Decimal("0.05"),
            q=Decimal("0"),
            sigma=Decimal("0.3"),
        )
        iv = solve_implied_volatility(
            market_price=Decimal("2500"),
            input_=inp,
            option_type="call",
        )
        assert iv is not None
        assert Decimal("0.2") <= iv <= Decimal("0.6")

    def test_quick_iv_wrapper(self):
        iv = quick_iv(
            market_price=Decimal("2500"),
            S=Decimal("50000"),
            K=Decimal("50000"),
            T=Decimal("0.1"),
            r=Decimal("0.05"),
        )
        assert iv is not None
        assert Decimal("0.2") <= iv <= Decimal("0.6")


class TestIndicators:
    def test_ema(self):
        series = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0, 20.0])
        result = ema(series, span=5)
        assert len(result) == len(series)
        assert result.iloc[-1] > result.iloc[0]
        assert 17.0 < result.iloc[-1] < 20.0

    def test_rsi(self):
        # Monotonically increasing prices -> RSI should be high (>70)
        up_series = pd.Series([100.0 + i for i in range(30)])
        res_up = rsi(up_series, period=14)
        assert len(res_up) == len(up_series)
        assert res_up.iloc[-1] > 70.0

        # Monotonically decreasing prices -> RSI should be low (<30)
        down_series = pd.Series([100.0 - i for i in range(30)])
        res_down = rsi(down_series, period=14)
        assert len(res_down) == len(down_series)
        assert res_down.iloc[-1] < 30.0

    def test_atr(self):
        highs = pd.Series([105.0] * 30)
        lows = pd.Series([95.0] * 30)
        closes = pd.Series([100.0] * 30)
        res = atr(highs, lows, closes, period=14)
        assert len(res) == 30
        assert round(res.iloc[-1], 1) == 10.0

    def test_bollinger_bands(self):
        series = pd.Series([100.0 + (i % 5) for i in range(30)])
        mid, upper, lower = bollinger_bands(series, window=20, num_std=2.0)
        assert len(upper) == len(series)
        assert upper.iloc[-1] > mid.iloc[-1] > lower.iloc[-1]

    def test_vwap(self):
        highs = pd.Series([102.0, 104.0, 103.0])
        lows = pd.Series([98.0, 100.0, 99.0])
        closes = pd.Series([100.0, 102.0, 101.0])
        volumes = pd.Series([1000.0, 2000.0, 1500.0])
        res = vwap(price=(highs + lows + closes) / 3, volume=volumes)
        assert len(res) == 3
        assert 98.0 <= res.iloc[-1] <= 104.0


class TestMarketRegime:
    def test_regime_classification_trending(self):
        closes = pd.Series([float(100 + i * 2) for i in range(60)])
        highs = closes + 1.0
        lows = closes - 1.0
        volumes = pd.Series([1000.0] * 60)
        res = classify_market_regime(close=closes, high=highs, low=lows, volume=volumes)
        assert isinstance(res, dict)
        assert "regime" in res
        regime = res["regime"]
        assert isinstance(regime, MarketRegime)
        assert regime in (MarketRegime.TRENDING_BULL, MarketRegime.BREAKOUT_EXPANSION)

    def test_regime_classification_ranging(self):
        closes = pd.Series([100.0 + (1.0 if i % 2 == 0 else -1.0) for i in range(60)])
        highs = closes + 0.5
        lows = closes - 0.5
        volumes = pd.Series([500.0] * 60)
        res = classify_market_regime(close=closes, high=highs, low=lows, volume=volumes)
        assert isinstance(res, dict)
        assert "regime" in res
        regime = res["regime"]
        assert isinstance(regime, MarketRegime)
        assert regime in (MarketRegime.RANGING_LOW_VOL, MarketRegime.RANGING_HIGH_VOL)
