"""
Strategy Registry Module

Provides access to strategy registration and discovery interfaces.
"""
from typing import Dict, Type, List
from strategies.base import (
    BaseStrategy,
    register_strategy,
    get_strategy,
    list_strategies,
    _strategies,
)

__all__ = [
    "BaseStrategy",
    "register_strategy",
    "get_strategy",
    "list_strategies",
    "_strategies",
]
