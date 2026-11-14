"""
Core Domain Exceptions for Delta India Terminal

Defines domain-specific exception hierarchies across risk, execution,
exchange connectivity, and data validation layers.
"""

class DeltaTerminalError(Exception):
    """Base exception for all domain errors within Delta India Terminal."""
    pass


class RiskViolation(DeltaTerminalError):
    """Raised when an action or signal violates a risk rule or boundary."""
    def __init__(self, rule: str, message: str, details: dict = None):
        super().__init__(f"Risk Violation [{rule}]: {message}")
        self.rule = rule
        self.details = details or {}


class CircuitBreakerError(RiskViolation):
    """Raised when a circuit breaker is actively tripped or fails to trip."""
    pass


class ExchangeError(DeltaTerminalError):
    """Raised when communication with Delta Exchange fails or returns an error."""
    def __init__(self, message: str, status_code: int = None, response_body: str = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class StaleDataError(DeltaTerminalError):
    """Raised when market data exceeds allowable latency/freshness thresholds."""
    def __init__(self, symbol: str, age_ms: float, max_age_ms: float = 3000.0):
        super().__init__(
            f"Market data for {symbol} is stale: age {age_ms:.1f}ms exceeds {max_age_ms:.1f}ms"
        )
        self.symbol = symbol
        self.age_ms = age_ms
        self.max_age_ms = max_age_ms


class ConfigurationError(DeltaTerminalError):
    """Raised when environment or account configuration is invalid or missing."""
    pass


class OrderExecutionError(DeltaTerminalError):
    """Raised when order submission, amendment, or cancellation fails."""
    pass

