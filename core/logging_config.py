"""
Structured JSON Logging with Secret Redaction

Provides production-ready logging that:
- Outputs structured JSON for log aggregation
- Redacts API keys, secrets, tokens, and other sensitive data
- Includes correlation IDs for request tracing
"""
import logging
import re
import json
import sys
import os
import threading
from typing import Any, Dict, Optional
from datetime import datetime
from pythonjsonlogger import jsonlogger


# Thread-local storage for correlation IDs
_request_context = threading.local()


def set_correlation_id(correlation_id: str) -> None:
    """Set correlation ID for current request/thread."""
    _request_context.correlation_id = correlation_id


def get_correlation_id() -> Optional[str]:
    """Get correlation ID for current request/thread."""
    return getattr(_request_context, 'correlation_id', None)


def clear_correlation_id() -> None:
    """Clear correlation ID for current request/thread."""
    if hasattr(_request_context, 'correlation_id'):
        del _request_context.correlation_id


# Patterns to redact from log messages
# ORDER MATTERS: More specific patterns should come before generic ones
SECRET_PATTERNS = [
    # Private keys (PEM format) - most specific
    (re.compile(r'-----BEGIN (RSA |EC |)PRIVATE KEY-----[\s\S]*?-----END (RSA |EC |)PRIVATE KEY-----'), '[REDACTED_PRIVATE_KEY]'),
    # JWT tokens (three base64 parts separated by dots)
    (re.compile(r'eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+'), '[REDACTED_JWT]'),
    # Credit card numbers (basic pattern)
    (re.compile(r'\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b'), '[REDACTED_CC]'),
    # Secret: <64-hex> - specific pattern for secret followed by hex (BEFORE standalone hex)
    (re.compile(r'(?i)secret\s*[:=]\s*[a-fA-F0-9]{64,}'), 'Secret: [REDACTED_SECRET]'),
    # HMAC secrets (typically 64+ hex chars) - standalone pattern
    (re.compile(r'\b[a-fA-F0-9]{64,}\b'), '[REDACTED_SECRET]'),
    # Bearer tokens
    (re.compile(r'Bearer\s+[a-zA-Z0-9_-]+'), 'Bearer [REDACTED]'),
    # Authorization headers
    (re.compile(r'(?i)authorization\s*[:=]\s*[^\s]+'), 'Authorization: [REDACTED]'),
    # API key headers
    (re.compile(r'(?i)api-key\s*[:=]\s*[^\s]+'), 'Api-Key: [REDACTED]'),
    # Signature headers
    (re.compile(r'(?i)signature\s*[:=]\s*[^\s]+'), 'Signature: [REDACTED]'),
    # Password fields
    (re.compile(r'(?i)password\s*[:=]\s*[^\s]+'), 'Password: [REDACTED]'),
    # Token fields (generic key=value)
    (re.compile(r'(?i)token\s*[:=]\s*[^\s]+'), 'Token: [REDACTED]'),
    # Secret fields (generic key=value) - after specific 64+ hex pattern
    (re.compile(r'(?i)secret\s*[:=]\s*[^\s]+'), 'Secret: [REDACTED]'),
    # API keys (32+ alphanumeric chars) - generic catch-all, last
    (re.compile(r'\b[a-zA-Z0-9]{32,}\b'), '[REDACTED_API_KEY]'),
]


class SecretRedactionFilter(logging.Filter):
    """
    Logging filter that redacts sensitive information from log records.

    Applies to both the log message and any structured data in the record.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Redact the main message
        if isinstance(record.msg, str):
            record.msg = self._redact_string(record.msg)

        # Redact args if they're strings
        if record.args:
            new_args = []
            for arg in record.args:
                if isinstance(arg, str):
                    new_args.append(self._redact_string(arg))
                elif isinstance(arg, dict):
                    new_args.append(self._redact_dict(arg))
                elif isinstance(arg, (list, tuple)):
                    new_args.append(self._redact_iterable(arg))
                else:
                    new_args.append(arg)
            record.args = tuple(new_args)

        # Add correlation ID if available
        corr_id = get_correlation_id()
        if corr_id:
            record.correlation_id = corr_id

        return True

    def _redact_string(self, text: str) -> str:
        """Apply all redaction patterns to a string."""
        for pattern, replacement in SECRET_PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    def _redact_dict(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Recursively redact sensitive fields in a dictionary."""
        redacted = {}
        sensitive_keys = {
            'api_key', 'apikey', 'api_secret', 'apisecret', 'secret', 'password',
            'token', 'access_token', 'refresh_token', 'auth_token', 'authorization',
            'signature', 'hmac', 'private_key', 'secret_key', 'client_secret',
            'aws_secret_access_key', 'aws_access_key_id', 'stripe_secret_key',
        }

        for key, value in data.items():
            key_lower = key.lower()
            if key_lower in sensitive_keys:
                redacted[key] = '[REDACTED]'
            elif isinstance(value, str):
                redacted[key] = self._redact_string(value)
            elif isinstance(value, dict):
                redacted[key] = self._redact_dict(value)
            elif isinstance(value, (list, tuple)):
                redacted[key] = self._redact_iterable(value)
            else:
                redacted[key] = value
        return redacted

    def _redact_iterable(self, data) -> list:
        """Recursively redact items in a list/tuple."""
        result = []
        for item in data:
            if isinstance(item, str):
                result.append(self._redact_string(item))
            elif isinstance(item, dict):
                result.append(self._redact_dict(item))
            elif isinstance(item, (list, tuple)):
                result.append(self._redact_iterable(item))
            else:
                result.append(item)
        return result


class JsonFormatter(jsonlogger.JsonFormatter):
    """
    Custom JSON formatter that includes standard fields and handles redaction.
    """

    def add_fields(self, log_record: Dict[str, Any], record: logging.LogRecord, message_dict: Dict[str, Any]) -> None:
        super().add_fields(log_record, record, message_dict)

        # Standard fields
        log_record['timestamp'] = datetime.utcnow().isoformat() + 'Z'
        log_record['level'] = record.levelname
        log_record['logger'] = record.name
        log_record['module'] = record.module
        log_record['function'] = record.funcName
        log_record['line'] = record.lineno

        # Correlation ID for request tracing
        corr_id = getattr(record, 'correlation_id', None) or get_correlation_id()
        if corr_id:
            log_record['correlation_id'] = corr_id

        # Process/thread info
        log_record['process_id'] = os.getpid()
        log_record['thread_id'] = threading.get_ident()

        # Exception info
        if record.exc_info:
            log_record['exception'] = self.formatException(record.exc_info)


class PrettyFormatter(logging.Formatter):
    """Human-readable formatter for development/console output."""

    COLORS = {
        'DEBUG': '\033[36m',      # Cyan
        'INFO': '\033[32m',       # Green
        'WARNING': '\033[33m',    # Yellow
        'ERROR': '\033[31m',      # Red
        'CRITICAL': '\033[35m',   # Magenta
        'RESET': '\033[0m',
    }

    def format(self, record: logging.LogRecord) -> str:
        # Apply secret redaction
        filter_instance = SecretRedactionFilter()
        filter_instance.filter(record)

        # Format timestamp
        timestamp = datetime.fromtimestamp(record.created).strftime('%H:%M:%S.%f')[:-3]

        # Level with color
        level_color = self.COLORS.get(record.levelname, '')
        reset = self.COLORS['RESET']
        level = f"{level_color}{record.levelname:<8}{reset}"

        # Correlation ID
        corr_id = getattr(record, 'correlation_id', None)
        corr_str = f" [{corr_id[:8]}]" if corr_id else ""

        # Message
        msg = record.getMessage()

        # Module:function:line
        location = f"{record.module}:{record.funcName}:{record.lineno}"

        return f"{timestamp} {level}{corr_str} {location} - {msg}"


def setup_logging(
    level: str = "INFO",
    json_output: bool = False,
    log_file: Optional[str] = None
) -> logging.Logger:
    """
    Configure application logging.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        json_output: If True, output structured JSON; else pretty console format
        log_file: Optional file path to also write logs to

    Returns:
        Root logger instance
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper()))

    # Clear existing handlers
    root_logger.handlers.clear()

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    if json_output:
        console_handler.setFormatter(JsonFormatter())
    else:
        console_handler.setFormatter(PrettyFormatter())
    console_handler.addFilter(SecretRedactionFilter())
    root_logger.addHandler(console_handler)

    # File handler (optional)
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setFormatter(JsonFormatter())
        file_handler.addFilter(SecretRedactionFilter())
        root_logger.addHandler(file_handler)

    # Reduce noise from third-party libraries
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('requests').setLevel(logging.WARNING)
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
    logging.getLogger('flask').setLevel(logging.INFO)

    return root_logger


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance with the given name."""
    return logging.getLogger(name)


# Context manager for correlation ID
class CorrelationContext:
    """Context manager for setting correlation ID for a block of code."""

    def __init__(self, correlation_id: Optional[str] = None):
        self.correlation_id = correlation_id or f"req-{os.getpid()}-{threading.get_ident()}-{int(datetime.utcnow().timestamp() * 1000)}"
        self.previous = None

    def __enter__(self):
        self.previous = get_correlation_id()
        set_correlation_id(self.correlation_id)
        return self.correlation_id

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.previous:
            set_correlation_id(self.previous)
        else:
            clear_correlation_id()