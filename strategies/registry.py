"""
Strategy Registry Module

Provides access to strategy registration, discovery, activation states,
and operational metadata.
"""
from typing import Dict, Type, List, Any, Optional
from strategies.base import (
    BaseStrategy,
    register_strategy,
    get_strategy,
    list_strategies,
    _strategies,
)

# In-memory activation registry (defaults to True)
_strategy_enabled_state: Dict[str, bool] = {
    "trend_following": True,
    "momentum_breakout": True,
    "mean_reversion": True,
    "options_spreads": True,
    "anomaly_scanner": True,
    "opening_range_breakout": True,
    "daily_setup_scanner": True,
    "atr_exit_manager": True,
}

# Human-readable metadata for registered strategies
_strategy_catalog: Dict[str, Dict[str, Any]] = {
    "trend_following": {
        "name": "Trend Following (EMA 20/50 + ATR)",
        "version": "1.0.0",
        "description": "Multi-timeframe EMA crossover with dynamic ATR stop-loss & take-profit",
        "category": "Directional Futures",
        "warmup_period": 50,
        "default_allocation_pct": 25.0,
        "parameters": {"fast_ema": 20, "slow_ema": 50, "atr_period": 14, "risk_multiplier": 2.0},
    },
    "momentum_breakout": {
        "name": "Momentum Breakout (RSI + Volume Surge)",
        "version": "1.0.0",
        "description": "20-period volume SMA acceleration with RSI 14 momentum confirmation",
        "category": "Breakout Momentum",
        "warmup_period": 20,
        "default_allocation_pct": 20.0,
        "parameters": {"rsi_period": 14, "volume_multiplier": 2.0, "rsi_threshold": 55.0},
    },
    "mean_reversion": {
        "name": "Mean Reversion (Bollinger Bands 2.5σ)",
        "version": "1.0.0",
        "description": "Statistical volatility overshoot detection returning to 20 SMA center line",
        "category": "Mean Reversion",
        "warmup_period": 20,
        "default_allocation_pct": 20.0,
        "parameters": {"bb_period": 20, "std_dev": 2.5, "atr_sl_mult": 1.5},
    },
    "options_spreads": {
        "name": "Vertical Debit Spreads (Bull Call / Bear Put)",
        "version": "1.0.0",
        "description": "Multi-leg defined-risk directional option spreads with R:R ≥ 1.5",
        "category": "Options Multi-Leg",
        "warmup_period": 10,
        "default_allocation_pct": 20.0,
        "parameters": {"min_rr_ratio": 1.5, "dte_min": 1, "dte_max": 14},
    },
    "anomaly_scanner": {
        "name": "Arbitrage & Volatility Anomaly Scanner",
        "version": "2.0.0",
        "description": "Strike monotonicity arbitrage, illiquidity gaps, and mispriced butterfly spreads",
        "category": "Arbitrage & Scanner",
        "warmup_period": 5,
        "default_allocation_pct": 15.0,
        "parameters": {"min_violation_usd": 20.0, "fat_finger_pct": 10.0},
    },
    "opening_range_breakout": {
        "name": "Opening Range Breakout (ORB)",
        "version": "1.0.0",
        "description": "Adapted from day-trader-skill: Breakout bar close outside range with volume surge & midpoint stop",
        "category": "Breakout Momentum",
        "warmup_period": 3,
        "default_allocation_pct": 15.0,
        "parameters": {"range_bars": 3, "min_volume_x": 1.0, "rr_ratio": 2.0},
    },
    "daily_setup_scanner": {
        "name": "Daily Setup Scanner",
        "version": "1.0.0",
        "description": "Adapted from day-trader-skill: 5 setups (20d breakout, bull flag, MA20 reclaim, oversold bounce/breakdown)",
        "category": "Multi-Setup Scanner",
        "warmup_period": 25,
        "default_allocation_pct": 15.0,
        "parameters": {"breakout_lookback": 20, "flag_min_gain_pct": 10.0, "atr_sl_mult": 1.5},
    },
    "atr_exit_manager": {
        "name": "ATR Dynamic Exit Manager",
        "version": "1.0.0",
        "description": "Adapted from day-trader-skill: Dynamic ATR stop-loss, take-profit bracket planning and time exits",
        "category": "Execution & Risk",
        "warmup_period": 15,
        "default_allocation_pct": 0.0,
        "parameters": {"atr_period": 14, "default_atr_sl_mult": 1.5, "default_rr_ratio": 2.0},
    },
}


def is_strategy_enabled(strategy_id: str) -> bool:
    """Check if strategy is currently enabled for trading."""
    return _strategy_enabled_state.get(strategy_id, True)


def set_strategy_enabled(strategy_id: str, enabled: bool) -> bool:
    """Enable or disable strategy execution."""
    _strategy_enabled_state[strategy_id] = bool(enabled)
    return _strategy_enabled_state[strategy_id]


def get_strategy_metadata(strategy_id: str) -> Dict[str, Any]:
    """Retrieve full metadata for a registered strategy."""
    catalog_info = _strategy_catalog.get(strategy_id, {
        "name": strategy_id.replace("_", " ").title(),
        "version": "1.0.0",
        "description": "Algorithmic trading strategy",
        "category": "General",
        "warmup_period": 20,
        "default_allocation_pct": 10.0,
        "parameters": {},
    })

    cls = get_strategy(strategy_id)
    if cls:
        catalog_info["version"] = getattr(cls, "strategy_version", catalog_info["version"])
        catalog_info["warmup_period"] = getattr(cls, "warmup_period", catalog_info["warmup_period"])

    return {
        "strategy_id": strategy_id,
        "enabled": is_strategy_enabled(strategy_id),
        **catalog_info,
    }


def get_all_strategies_metadata() -> List[Dict[str, Any]]:
    """Retrieve metadata for all known strategies."""
    # Ensure all registered strategies in _strategies and _strategy_catalog are included
    all_ids = list(dict.fromkeys(list(_strategy_catalog.keys()) + list_strategies()))
    return [get_strategy_metadata(sid) for sid in all_ids]


__all__ = [
    "BaseStrategy",
    "register_strategy",
    "get_strategy",
    "list_strategies",
    "_strategies",
    "is_strategy_enabled",
    "set_strategy_enabled",
    "get_strategy_metadata",
    "get_all_strategies_metadata",
]
