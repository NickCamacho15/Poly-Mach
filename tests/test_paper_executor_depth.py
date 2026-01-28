"""
Depth-aware paper trading tests.

These focus on:
- book-walking VWAP taker fills
- partial fills when depth is insufficient at the limit
- depth-aware liquidation valuation
- partial/inventory-safe maker fills
"""

from decimal import Decimal

import pytest

from src.data.models import OrderIntent, OrderStatus, Side
from src.data.orderbook import OrderBookTracker
from src.execution.paper_executor import PaperExecutor, PaperOrderRequest
from src.state.state_manager import StateManager


@pytest.fixture
def state_manager() -> StateManager:
    return StateManager(initial_balance=Decimal("1000"))


@pytest.fixture
def orderbook_tracker() -> OrderBookTracker:
    return OrderBookTracker()


@pytest.fixture
def paper_executor(state_manager: StateManager, orderbook_tracker: OrderBookTracker) -> PaperExecutor:
    return PaperExecutor(state_manager, orderbook_tracker)


def test_taker_fill_walks_book_vwap_yes_buy(paper_executor: PaperExecutor, orderbook_tracker: OrderBookTracker):
    market_slug = "depth-buy-market"
    orderbook_tracker.update(
        market_slug,
        {
            "yes": {
                "bids": [["0.48", "100"]],
                "asks": [["0.49", "10"], ["0.50", "10"]],
            },
            "no": {
                "bids": [["0.50", "100"]],
                "asks": [["0.51", "100"]],
            },
        },
    )

    order = PaperOrderRequest(
        market_slug=market_slug,
        intent=OrderIntent.BUY_LONG,
        quantity=15,
        price=Decimal("0.50"),  # marketable through 0.50
    )
    result = paper_executor.execute_order(order)

    assert result.is_success
    assert result.status == OrderStatus.FILLED
    assert result.filled_quantity == 15

    expected_vwap = (Decimal("0.49") * 10 + Decimal("0.50") * 5) / 15
    assert result.avg_fill_price == expected_vwap

    position = paper_executor.state.get_position(market_slug)
    assert position is not None
    assert position.quantity == 15
    assert position.avg_price == expected_vwap


def test_taker_limit_partial_fill_when_depth_insufficient_at_limit(
    paper_executor: PaperExecutor,
    orderbook_tracker: OrderBookTracker,
):
    market_slug = "depth-partial-limit"
    orderbook_tracker.update(
        market_slug,
        {
            "yes": {
                "bids": [["0.48", "100"]],
                "asks": [["0.49", "10"], ["0.51", "10"]],
            },
            "no": {"bids": [["0.50", "100"]], "asks": [["0.52", "100"]]},
        },
    )

    order = PaperOrderRequest(
        market_slug=market_slug,
        intent=OrderIntent.BUY_LONG,
        quantity=15,
        price=Decimal("0.50"),  # only the 0.49 level is eligible
    )
    result = paper_executor.execute_order(order)

    assert result.is_success
    assert result.status == OrderStatus.PARTIALLY_FILLED
    assert result.filled_quantity == 10
    assert result.avg_fill_price == Decimal("0.49")

    # Remainder should be resting with the same order_id.
    open_orders = paper_executor.state.get_open_orders(market_slug)
    assert len(open_orders) == 1
    assert open_orders[0].order_id == result.order_id
    assert open_orders[0].status == OrderStatus.PARTIALLY_FILLED
    assert open_orders[0].filled_quantity == 10
    assert open_orders[0].remaining_quantity == 5


def test_liquidation_valuation_uses_depth_and_conservative_remainder(
    paper_executor: PaperExecutor,
    orderbook_tracker: OrderBookTracker,
):
    market_slug = "depth-liquidation"
    orderbook_tracker.update(
        market_slug,
        {
            "yes": {
                "bids": [["0.47", "5"], ["0.46", "5"]],
                "asks": [["0.49", "5"]],
            },
            "no": {
                "bids": [["0.53", "5"]],
                "asks": [["0.55", "5"]],
            },
        },
    )

    # Create a large YES position (20 contracts) with only 10 contracts of bid depth.
    paper_executor.state.update_position(
        market_slug=market_slug,
        side=Side.YES,
        quantity=20,
        avg_price=Decimal("0.40"),
    )
    # Reflect the cash spent so total PnL reconciles.
    paper_executor.state.adjust_balance(-(Decimal("0.40") * 20))

    perf = paper_executor.get_performance()

    # Best bid value assumes full liquidation at 0.47.
    assert perf.position_value_best_bid == Decimal("9.40")
    assert perf.unrealized_pnl_best_bid == Decimal("1.40")

    # Liquidation value can only fill 10 contracts across the two bid levels.
    liquidation_value = Decimal("0.47") * 5 + Decimal("0.46") * 5  # 4.65
    assert perf.position_value_liquidation == liquidation_value
    assert perf.position_value == liquidation_value  # legacy field mapped to liquidation

    entry_value = Decimal("0.40") * 20  # 8.00
    assert perf.unrealized_pnl_liquidation == liquidation_value - entry_value
    assert perf.unrealized_pnl == perf.unrealized_pnl_liquidation


def test_maker_fills_are_partial_and_inventory_safe(
    paper_executor: PaperExecutor,
    orderbook_tracker: OrderBookTracker,
):
    market_slug = "maker-partial"

    # Start with a book where a buy at 0.48 rests (ask=0.49).
    orderbook_tracker.update(
        market_slug,
        {
            "yes": {"bids": [["0.47", "500"]], "asks": [["0.49", "300"]]},
            "no": {"bids": [["0.51", "400"]], "asks": [["0.53", "350"]]},
        },
    )

    rest = paper_executor.execute_order(
        PaperOrderRequest(
            market_slug=market_slug,
            intent=OrderIntent.BUY_LONG,
            quantity=100,
            price=Decimal("0.48"),
        )
    )
    assert rest.status == OrderStatus.OPEN

    # Cross the order (ask drops below our limit). Crossed fills are deterministic in sim.
    orderbook_tracker.update(
        market_slug,
        {
            "yes": {"bids": [["0.45", "500"]], "asks": [["0.47", "300"]]},
            "no": {"bids": [["0.53", "400"]], "asks": [["0.55", "350"]]},
        },
    )

    fills = paper_executor.check_resting_orders()
    assert len(fills) == 1
    assert fills[0].order_id == rest.order_id
    assert fills[0].filled_quantity == 2  # 2% of 100 per tick (min 1), capped by max

    still_open = paper_executor.state.get_order(rest.order_id)
    assert still_open is not None
    assert still_open.status == OrderStatus.PARTIALLY_FILLED
    assert still_open.filled_quantity == 2
    assert still_open.remaining_quantity == 98

    # Inventory-safe sells: create a position of 1, but place a sell order of 5.
    sell_market = "maker-inventory-safe"
    orderbook_tracker.update(
        sell_market,
        {
            "yes": {"bids": [["0.47", "10"]], "asks": [["0.50", "100"]]},
            "no": {"bids": [["0.51", "10"]], "asks": [["0.53", "10"]]},
        },
    )

    paper_executor.state.update_position(
        market_slug=sell_market,
        side=Side.YES,
        quantity=1,
        avg_price=Decimal("0.40"),
    )
    paper_executor.state.adjust_balance(-(Decimal("0.40") * 1))

    sell_rest = paper_executor.execute_order(
        PaperOrderRequest(
            market_slug=sell_market,
            intent=OrderIntent.SELL_LONG,
            quantity=5,
            price=Decimal("0.50"),  # rests above bid
        )
    )
    assert sell_rest.status == OrderStatus.OPEN

    # Cross it with a higher bid (0.55), which would otherwise attempt to fill > inventory.
    orderbook_tracker.update(
        sell_market,
        {
            "yes": {"bids": [["0.55", "10"]], "asks": [["0.56", "100"]]},
            "no": {"bids": [["0.45", "10"]], "asks": [["0.46", "10"]]},
        },
    )

    sell_fills = paper_executor.check_resting_orders()
    # We may get multiple fills in a single call if multiple orders are open; find ours.
    ours = [f for f in sell_fills if f.order_id == sell_rest.order_id]
    assert len(ours) == 1
    assert ours[0].filled_quantity == 1  # capped by available inventory

    # Position should now be closed.
    assert paper_executor.state.get_position(sell_market) is None
