"""
Async executor protocol used by StrategyEngine.

Paper and live executors must implement the same async contract so the engine can
operate identically across modes.
"""

from __future__ import annotations

from typing import Callable, Optional, Protocol, List, runtime_checkable

from .paper_executor import ExecutionResult, PaperOrderRequest


FillListener = Callable[[str], None]


@runtime_checkable
class ExecutorProtocol(Protocol):
    async def execute_order(self, order: PaperOrderRequest) -> ExecutionResult: ...

    async def cancel_order(self, order_id: str) -> bool: ...

    async def cancel_all_orders(self, market_slug: Optional[str] = None) -> int: ...

    async def check_resting_orders(self) -> List[ExecutionResult]: ...

    # Optional hooks used by StrategyEngine for cache invalidation after fills.
    def add_fill_listener(self, listener: FillListener) -> None: ...

    def remove_fill_listener(self, listener: FillListener) -> None: ...

    # Health endpoint reads performance synchronously.
    def get_performance(self): ...

