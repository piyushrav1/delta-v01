"""
Simulation Package

Provides historical data ingestion, event-driven backtesting,
and institutional performance analytics.
"""
from simulation.data_loader import DataLoader
from simulation.performance import PerformanceAnalytics, BacktestReport
from simulation.backtester import EventDrivenBacktester

__all__ = [
    "DataLoader",
    "PerformanceAnalytics",
    "BacktestReport",
    "EventDrivenBacktester",
]
