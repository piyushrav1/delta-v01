"""
Trading Environment Definitions

Strictly defines the five mutually exclusive operating modes.
This is the SINGLE SOURCE OF TRUTH for environment configuration.
"""
from enum import Enum
from typing import Literal
from dataclasses import dataclass


class TradingEnvironment(str, Enum):
    """Five mutually exclusive operating modes - FAIL CLOSED by default."""
    BACKTEST = "BACKTEST"      # Historical simulation, no live data, no orders
    PAPER = "PAPER"            # Live market data, simulated execution, no exchange orders
    TESTNET = "TESTNET"        # Live testnet exchange (cdn-ind.testnet.deltaex.org), real orders
    LIVE_MANUAL = "LIVE_MANUAL"  # Live exchange, human-initiated orders only
    LIVE_AUTO = "LIVE_AUTO"    # Live exchange, automated strategies allowed (REQUIRES explicit auth)

    @property
    def is_live(self) -> bool:
        """True if environment can reach real exchange with real money."""
        return self in (TradingEnvironment.LIVE_MANUAL, TradingEnvironment.LIVE_AUTO)

    @property
    def is_testnet(self) -> bool:
        """True if environment uses testnet exchange."""
        return self == TradingEnvironment.TESTNET

    @property
    def allows_auto_trading(self) -> bool:
        """True if automated strategy execution is permitted."""
        return self in (TradingEnvironment.PAPER, TradingEnvironment.TESTNET, TradingEnvironment.LIVE_AUTO)

    @property
    def allows_manual_trading(self) -> bool:
        """True if human-initiated orders are permitted."""
        return self in (TradingEnvironment.TESTNET, TradingEnvironment.LIVE_MANUAL, TradingEnvironment.LIVE_AUTO)

    @property
    def base_url(self) -> str:
        """Default Delta Exchange base URL for this environment."""
        if self == TradingEnvironment.TESTNET:
            return "https://cdn-ind.testnet.deltaex.org"
        if self.is_live:
            return "https://api.india.delta.exchange"
        # BACKTEST and PAPER don't need a real base URL
        return "https://api.india.delta.exchange"

    @property
    def color(self) -> str:
        """UI color coding for this environment."""
        colors = {
            TradingEnvironment.BACKTEST: "#6366f1",      # Indigo
            TradingEnvironment.PAPER: "#06b6d4",         # Cyan
            TradingEnvironment.TESTNET: "#f59e0b",       # Amber
            TradingEnvironment.LIVE_MANUAL: "#ef4444",   # Red
            TradingEnvironment.LIVE_AUTO: "#dc2626",     # Darker red
        }
        return colors[self]


@dataclass(frozen=True)
class EnvironmentConfig:
    """Complete environment configuration."""
    environment: TradingEnvironment
    live_auto_authorized: bool = False  # Explicit opt-in for LIVE_AUTO

    def __post_init__(self):
        if self.environment == TradingEnvironment.LIVE_AUTO and not self.live_auto_authorized:
            raise ValueError(
                "LIVE_AUTO requires LIVE_AUTO_AUTHORIZED=true. "
                "Set environment variable LIVE_AUTO_AUTHORIZED=true to enable."
            )

    @property
    def is_production(self) -> bool:
        """True if real money is at risk."""
        return self.environment.is_live

    def validate_account_environment(self, account_env: str) -> bool:
        """Check if an account's configured environment matches this config."""
        try:
            return TradingEnvironment(account_env.upper()) == self.environment
        except ValueError:
            return False


def get_environment_from_env() -> EnvironmentConfig:
    """
    Load environment configuration from environment variables.

    Required:
        TRADING_ENVIRONMENT: One of BACKTEST, PAPER, TESTNET, LIVE_MANUAL, LIVE_AUTO

    Optional:
        LIVE_AUTO_AUTHORIZED: Must be "true" to enable LIVE_AUTO mode

    Returns:
        EnvironmentConfig with validated settings

    Raises:
        ValueError: If configuration is invalid
    """
    import os

    env_str = os.getenv("TRADING_ENVIRONMENT", "PAPER").upper()

    try:
        env = TradingEnvironment(env_str)
    except ValueError:
        valid = [e.value for e in TradingEnvironment]
        raise ValueError(
            f"Invalid TRADING_ENVIRONMENT='{env_str}'. Must be one of: {valid}"
        )

    live_auto_auth = os.getenv("LIVE_AUTO_AUTHORIZED", "false").lower() == "true"

    return EnvironmentConfig(
        environment=env,
        live_auto_authorized=live_auto_auth
    )


def require_environment(*allowed: TradingEnvironment):
    """
    Decorator/guard to restrict function execution to specific environments.

    Usage:
        @require_environment(TradingEnvironment.TESTNET, TradingEnvironment.PAPER)
        def place_test_order(...):
            ...

    Raises:
        RuntimeError: If current environment not in allowed list
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            current = get_environment_from_env()
            if current.environment not in allowed:
                raise RuntimeError(
                    f"Function {func.__name__} not allowed in {current.environment.value} mode. "
                    f"Allowed: {[e.value for e in allowed]}"
                )
            return func(*args, **kwargs)
        return wrapper
    return decorator