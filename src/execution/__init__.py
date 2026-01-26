"""
Execution module for Polymarket US trading bot.

Provides order execution functionality for both paper and live trading.
"""

from .paper_executor import (
    ExecutionError,
    ExecutionResult,
    InsufficientBalanceError,
    InvalidOrderError,
    MarketNotFoundError,
    PaperExecutor,
    PaperOrderRequest,
    PerformanceMetrics,
    TradeRecord,
)

__all__ = [
    "ExecutionError",
    "ExecutionResult",
    "InsufficientBalanceError",
    "InvalidOrderError",
    "MarketNotFoundError",
    "PaperExecutor",
    "PaperOrderRequest",
    "PerformanceMetrics",
    "TradeRecord",
]
