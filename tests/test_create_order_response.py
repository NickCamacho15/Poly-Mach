"""
Tests for create-order response parsing.

Polymarket's create-order endpoint may return only:
  {"id": "...", "executions": []}
We must accept this shape and propagate the order_id back to the caller.
"""

from decimal import Decimal

import pytest

from src.data.models import CreateOrderResponse, OrderIntent, OrderStatus, OrderType
from src.data.orderbook import OrderBookTracker
from src.execution.live_executor import LiveExecutor
from src.execution.paper_executor import PaperOrderRequest
from src.state.state_manager import StateManager


def test_create_order_response_parses_id_to_order_id():
    resp = CreateOrderResponse.model_validate({"id": "7ZF8PHZ06H6Z", "executions": []})
    assert resp.order_id == "7ZF8PHZ06H6Z"
    assert resp.executions == []


@pytest.mark.asyncio
async def test_live_executor_returns_execution_result_with_order_id_from_create_response():
    class FakeClient:
        async def preview_order(self, order_req):
            # Best-effort path; LiveExecutor tolerates preview failures too.
            return type("Preview", (), {"estimated_fee": None})()

        async def create_order(self, order_req):
            return CreateOrderResponse.model_validate({"id": "7ZF8PHZ06H6Z", "executions": []})

    state = StateManager(initial_balance=Decimal("1000"))
    orderbook = OrderBookTracker()
    executor = LiveExecutor(client=FakeClient(), state=state, orderbook=orderbook)

    result = await executor.execute_order(
        PaperOrderRequest(
            market_slug="test-market",
            intent=OrderIntent.BUY_LONG,
            quantity=10,
            price=Decimal("0.50"),
            order_type=OrderType.LIMIT,
            post_only=False,
        )
    )

    assert result.error is None
    assert result.order_id == "7ZF8PHZ06H6Z"
    assert result.status == OrderStatus.OPEN

