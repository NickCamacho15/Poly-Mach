"""
Async wrapper around PaperExecutor.

StrategyEngine now awaits executor operations so we provide an adapter that keeps
paper-mode behavior identical while satisfying the async executor contract.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, List

from .paper_executor import ExecutionResult, PaperExecutor, PaperOrderRequest


class AsyncPaperExecutor:
    def __init__(self, inner: PaperExecutor):
        self._inner = inner

    # ---------------------------------------------------------------------
    # Fill listeners (delegate)
    # ---------------------------------------------------------------------
    def add_fill_listener(self, listener: Callable[[str], None]) -> None:
        self._inner.add_fill_listener(listener)

    def remove_fill_listener(self, listener: Callable[[str], None]) -> None:
        self._inner.remove_fill_listener(listener)

    # ---------------------------------------------------------------------
    # Async contract (delegate)
    # ---------------------------------------------------------------------
    async def execute_order(self, order: PaperOrderRequest) -> ExecutionResult:
        return self._inner.execute_order(order)

    async def cancel_order(self, order_id: str) -> bool:
        return self._inner.cancel_order(order_id)

    async def cancel_all_orders(self, market_slug: Optional[str] = None) -> int:
        return self._inner.cancel_all_orders(market_slug)

    async def check_resting_orders(self) -> List[ExecutionResult]:
        return self._inner.check_resting_orders()

    # ---------------------------------------------------------------------
    # Health/metrics passthroughs
    # ---------------------------------------------------------------------
    def get_performance(self) -> Any:
        return self._inner.get_performance()

    def get_positions_report(self, limit: int = 50):
        return self._inner.get_positions_report(limit=limit)

    def __getattr__(self, name: str) -> Any:
        # Best-effort passthrough for optional paper-only helpers.
        return getattr(self._inner, name)

