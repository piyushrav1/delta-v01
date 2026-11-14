"""
Application Settings & Global Parameter Validation

Provides structured configuration for timeouts, rate limits, risk thresholds,
and exchange endpoints.
"""
from dataclasses import dataclass, field
import os
from typing import Optional
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class RiskSettings:
    """Risk management parameters and boundaries."""
    max_daily_loss_usd: float = float(os.getenv("MAX_DAILY_LOSS_USD", "500.0"))
    max_daily_loss_pct: float = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.02"))  # 2% max drawdown per day
    max_risk_per_trade_pct: float = float(os.getenv("MAX_RISK_PER_TRADE_PCT", "0.01"))  # 1% equity per trade
    max_open_positions: int = int(os.getenv("MAX_OPEN_POSITIONS", "5"))
    max_leverage: float = float(os.getenv("MAX_LEVERAGE", "10.0"))
    max_slippage_pct: float = float(os.getenv("MAX_SLIPPAGE_PCT", "0.005"))  # 0.5% max slippage
    quote_freshness_max_ms: float = float(os.getenv("QUOTE_FRESHNESS_MAX_MS", "3000.0"))
    heartbeat_timeout_sec: float = float(os.getenv("HEARTBEAT_TIMEOUT_SEC", "10.0"))
    consecutive_rejection_limit: int = int(os.getenv("CONSECUTIVE_REJECTION_LIMIT", "3"))


@dataclass(frozen=True)
class SystemSettings:
    """System-level operational settings."""
    app_env: str = os.getenv("APP_ENV", "development")
    db_path: str = os.getenv("DB_PATH", "delta_terminal.db")
    request_timeout_sec: float = float(os.getenv("REQUEST_TIMEOUT_SEC", "10.0"))
    reconnect_interval_sec: float = float(os.getenv("RECONNECT_INTERVAL_SEC", "5.0"))
    risk: RiskSettings = field(default_factory=RiskSettings)


_settings: Optional[SystemSettings] = None


def get_settings() -> SystemSettings:
    """Retrieve global system settings singleton."""
    global _settings
    if _settings is None:
        _settings = SystemSettings()
    return _settings

