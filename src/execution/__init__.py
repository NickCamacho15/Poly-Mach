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
from .async_paper_executor import AsyncPaperExecutor
from .executor_protocol import ExecutorProtocol

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
    "AsyncPaperExecutor",
    "ExecutorProtocol",
]
