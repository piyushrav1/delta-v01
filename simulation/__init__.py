"""
Simulation Package

Provides historical data ingestion, event-driven backtesting,
institutional performance analytics, and a paper trading simulator.
"""
from simulation.data_loader import DataLoader
from simulation.performance import PerformanceAnalytics, BacktestReport
from simulation.backtester import EventDrivenBacktester
from simulation.paper_ledger import (
    PaperLedger,
    PaperOrderModel,
    PaperPositionModel,
    PaperLedgerModel,
)
from simulation.paper_broker import PaperBroker

__all__ = [
    "DataLoader",
    "PerformanceAnalytics",
    "BacktestReport",
    "EventDrivenBacktester",
    "PaperLedger",
    "PaperOrderModel",
    "PaperPositionModel",
    "PaperLedgerModel",
    "PaperBroker",
]
