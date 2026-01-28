"""
Tests for the Paper Trading Module (State Manager and Paper Executor).

Run with: pytest tests/test_paper_trading.py -v
"""

import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict

import pytest

from src.data.models import OrderIntent, OrderStatus, OrderType, Side
from src.data.orderbook import OrderBookTracker
from src.execution.paper_executor import (
    ExecutionResult,
    InsufficientBalanceError,
    MarketNotFoundError,
    PaperExecutor,
    PaperOrderRequest,
    PerformanceMetrics,
    TradeRecord,
)
from src.state.state_manager import (
    MarketState,
    OrderState,
    PositionState,
    StateManager,
)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def state_manager() -> StateManager:
    """Create a StateManager for testing."""
    return StateManager(initial_balance=Decimal("1000"))


@pytest.fixture
def orderbook_tracker() -> OrderBookTracker:
    """Create an OrderBookTracker for testing."""
    return OrderBookTracker()


@pytest.fixture
def paper_executor(state_manager, orderbook_tracker) -> PaperExecutor:
    """Create a PaperExecutor for testing."""
    return PaperExecutor(state_manager, orderbook_tracker)


@pytest.fixture
def sample_orderbook_data() -> Dict:
    """Sample order book data."""
    return {
        "yes": {
            "bids": [["0.47", "500"], ["0.46", "1000"]],
            "asks": [["0.49", "300"], ["0.50", "800"]],
        },
        "no": {
            "bids": [["0.51", "400"], ["0.50", "600"]],
            "asks": [["0.53", "350"], ["0.54", "700"]],
        },
    }


@pytest.fixture
def market_with_book(orderbook_tracker, sample_orderbook_data) -> str:
    """Create a market with order book data."""
    market_slug = "nba-test-game-2025"
    orderbook_tracker.update(market_slug, sample_orderbook_data)
    return market_slug


# =============================================================================
# StateManager Tests
# =============================================================================

class TestStateManagerInit:
    """Tests for StateManager initialization."""
    
    def test_init_default_balance(self):
        """Test initialization with default balance."""
        state = StateManager()
        assert state.get_balance() == Decimal("0")
    
    def test_init_custom_balance(self, state_manager):
        """Test initialization with custom balance."""
        assert state_manager.get_balance() == Decimal("1000")
    
    def test_init_empty_state(self, state_manager):
        """Test that initial state is empty."""
        assert state_manager.get_all_markets() == []
        assert state_manager.get_all_positions() == []
        assert state_manager.get_all_orders() == []


class TestStateManagerMarkets:
    """Tests for market state management."""
    
    def test_update_market_new(self, state_manager):
        """Test creating a new market state."""
        state_manager.update_market(
            "test-market",
            yes_bid=Decimal("0.47"),
            yes_ask=Decimal("0.49"),
        )
        
        market = state_manager.get_market("test-market")
        assert market is not None
        assert market.market_slug == "test-market"
        assert market.yes_bid == Decimal("0.47")
        assert market.yes_ask == Decimal("0.49")
    
    def test_update_market_partial(self, state_manager):
        """Test partial updates preserve existing values."""
        state_manager.update_market(
            "test-market",
            yes_bid=Decimal("0.47"),
            yes_ask=Decimal("0.49"),
        )
        
        # Update only yes_bid
        state_manager.update_market(
            "test-market",
            yes_bid=Decimal("0.48"),
        )
        
        market = state_manager.get_market("test-market")
        assert market.yes_bid == Decimal("0.48")
        assert market.yes_ask == Decimal("0.49")  # Preserved
    
    def test_market_mid_price(self, state_manager):
        """Test mid-price calculation."""
        state_manager.update_market(
            "test-market",
            yes_bid=Decimal("0.47"),
            yes_ask=Decimal("0.49"),
        )
        
        market = state_manager.get_market("test-market")
        assert market.yes_mid_price == Decimal("0.48")
    
    def test_market_spread(self, state_manager):
        """Test spread calculation."""
        state_manager.update_market(
            "test-market",
            yes_bid=Decimal("0.47"),
            yes_ask=Decimal("0.49"),
        )
        
        market = state_manager.get_market("test-market")
        assert market.yes_spread == Decimal("0.02")
    
    def test_get_nonexistent_market(self, state_manager):
        """Test getting a market that doesn't exist."""
        assert state_manager.get_market("nonexistent") is None
    
    def test_get_all_markets(self, state_manager):
        """Test getting all markets."""
        state_manager.update_market("market-1", yes_bid=Decimal("0.5"))
        state_manager.update_market("market-2", yes_bid=Decimal("0.6"))
        
        markets = state_manager.get_all_markets()
        assert len(markets) == 2
    
    def test_remove_market(self, state_manager):
        """Test removing a market."""
        state_manager.update_market("test-market", yes_bid=Decimal("0.5"))
        assert state_manager.get_market("test-market") is not None
        
        state_manager.remove_market("test-market")
        assert state_manager.get_market("test-market") is None


class TestStateManagerPositions:
    """Tests for position management."""
    
    def test_update_position_new(self, state_manager):
        """Test creating a new position."""
        state_manager.update_position(
            market_slug="test-market",
            side=Side.YES,
            quantity=100,
            avg_price=Decimal("0.50"),
        )
        
        position = state_manager.get_position("test-market")
        assert position is not None
        assert position.side == Side.YES
        assert position.quantity == 100
        assert position.avg_price == Decimal("0.50")
    
    def test_update_position_existing(self, state_manager):
        """Test updating an existing position."""
        state_manager.update_position(
            market_slug="test-market",
            side=Side.YES,
            quantity=100,
            avg_price=Decimal("0.50"),
        )
        
        state_manager.update_position(
            market_slug="test-market",
            side=Side.YES,
            quantity=200,
            avg_price=Decimal("0.55"),
        )
        
        position = state_manager.get_position("test-market")
        assert position.quantity == 200
        assert position.avg_price == Decimal("0.55")
    
    def test_close_position_zero_quantity(self, state_manager):
        """Test that zero quantity closes position."""
        state_manager.update_position(
            market_slug="test-market",
            side=Side.YES,
            quantity=100,
            avg_price=Decimal("0.50"),
        )
        
        state_manager.update_position(
            market_slug="test-market",
            side=Side.YES,
            quantity=0,
            avg_price=Decimal("0.50"),
        )
        
        assert state_manager.get_position("test-market") is None
    
    def test_position_cost_basis(self, state_manager):
        """Test cost basis calculation."""
        state_manager.update_position(
            market_slug="test-market",
            side=Side.YES,
            quantity=100,
            avg_price=Decimal("0.50"),
        )
        
        position = state_manager.get_position("test-market")
        assert position.cost_basis == Decimal("50")
    
    def test_get_all_positions(self, state_manager):
        """Test getting all positions."""
        state_manager.update_position("market-1", Side.YES, 100, Decimal("0.5"))
        state_manager.update_position("market-2", Side.NO, 50, Decimal("0.6"))
        
        positions = state_manager.get_all_positions()
        assert len(positions) == 2
    
    def test_close_position(self, state_manager):
        """Test explicitly closing a position."""
        state_manager.update_position("test-market", Side.YES, 100, Decimal("0.5"))
        
        closed = state_manager.close_position("test-market")
        assert closed is not None
        assert closed.quantity == 100
        assert state_manager.get_position("test-market") is None


class TestStateManagerOrders:
    """Tests for order management."""
    
    def test_add_order(self, state_manager):
        """Test adding an order."""
        order = OrderState(
            order_id="order-123",
            market_slug="test-market",
            intent=OrderIntent.BUY_LONG,
            price=Decimal("0.50"),
            quantity=100,
        )
        
        state_manager.add_order(order)
        
        retrieved = state_manager.get_order("order-123")
        assert retrieved is not None
        assert retrieved.order_id == "order-123"
        assert retrieved.intent == OrderIntent.BUY_LONG
    
    def test_update_order(self, state_manager):
        """Test updating an order."""
        order = OrderState(
            order_id="order-123",
            market_slug="test-market",
            intent=OrderIntent.BUY_LONG,
            price=Decimal("0.50"),
            quantity=100,
        )
        state_manager.add_order(order)
        
        state_manager.update_order(
            "order-123",
            status=OrderStatus.PARTIALLY_FILLED,
            filled_quantity=50,
        )
        
        updated = state_manager.get_order("order-123")
        assert updated.status == OrderStatus.PARTIALLY_FILLED
        assert updated.filled_quantity == 50
    
    def test_remove_order(self, state_manager):
        """Test removing an order."""
        order = OrderState(
            order_id="order-123",
            market_slug="test-market",
            intent=OrderIntent.BUY_LONG,
            price=Decimal("0.50"),
            quantity=100,
        )
        state_manager.add_order(order)
        
        removed = state_manager.remove_order("order-123")
        assert removed is not None
        assert state_manager.get_order("order-123") is None
    
    def test_get_open_orders(self, state_manager):
        """Test getting open orders."""
        # Open order
        state_manager.add_order(OrderState(
            order_id="order-1",
            market_slug="market-1",
            intent=OrderIntent.BUY_LONG,
            price=Decimal("0.50"),
            quantity=100,
            status=OrderStatus.OPEN,
        ))
        
        # Filled order
        state_manager.add_order(OrderState(
            order_id="order-2",
            market_slug="market-1",
            intent=OrderIntent.BUY_LONG,
            price=Decimal("0.50"),
            quantity=100,
            status=OrderStatus.FILLED,
        ))
        
        open_orders = state_manager.get_open_orders()
        assert len(open_orders) == 1
        assert open_orders[0].order_id == "order-1"
    
    def test_get_open_orders_by_market(self, state_manager):
        """Test filtering open orders by market."""
        state_manager.add_order(OrderState(
            order_id="order-1",
            market_slug="market-1",
            intent=OrderIntent.BUY_LONG,
            price=Decimal("0.50"),
            quantity=100,
            status=OrderStatus.OPEN,
        ))
        
        state_manager.add_order(OrderState(
            order_id="order-2",
            market_slug="market-2",
            intent=OrderIntent.BUY_LONG,
            price=Decimal("0.50"),
            quantity=100,
            status=OrderStatus.OPEN,
        ))
        
        orders = state_manager.get_open_orders("market-1")
        assert len(orders) == 1
        assert orders[0].market_slug == "market-1"


class TestStateManagerBalance:
    """Tests for balance management."""
    
    def test_update_balance(self, state_manager):
        """Test setting balance."""
        state_manager.update_balance(Decimal("500"))
        assert state_manager.get_balance() == Decimal("500")
    
    def test_adjust_balance_positive(self, state_manager):
        """Test adding to balance."""
        new_balance = state_manager.adjust_balance(Decimal("100"))
        assert new_balance == Decimal("1100")
        assert state_manager.get_balance() == Decimal("1100")
    
    def test_adjust_balance_negative(self, state_manager):
        """Test subtracting from balance."""
        new_balance = state_manager.adjust_balance(Decimal("-200"))
        assert new_balance == Decimal("800")
        assert state_manager.get_balance() == Decimal("800")


class TestStateManagerEquity:
    """Tests for equity calculations."""
    
    def test_total_equity_no_positions(self, state_manager):
        """Test equity with only balance."""
        assert state_manager.get_total_equity() == Decimal("1000")
    
    def test_total_equity_with_positions(self, state_manager):
        """Test equity with positions."""
        # Add market data
        state_manager.update_market(
            "test-market",
            yes_bid=Decimal("0.55"),
            yes_ask=Decimal("0.57"),
        )
        
        # Add position
        state_manager.update_position(
            "test-market",
            Side.YES,
            100,
            Decimal("0.50"),
        )
        
        # Equity = 1000 + (0.55 * 100) = 1055
        assert state_manager.get_total_equity() == Decimal("1055")
    
    def test_exposure(self, state_manager):
        """Test exposure calculation."""
        state_manager.update_position("market-1", Side.YES, 100, Decimal("0.50"))
        state_manager.update_position("market-2", Side.NO, 50, Decimal("0.40"))
        
        # Total exposure = 50 + 20 = 70
        assert state_manager.get_exposure() == Decimal("70")
        
        # Market-specific exposure
        assert state_manager.get_exposure("market-1") == Decimal("50")


class TestStateManagerThreadSafety:
    """Tests for thread safety."""
    
    def test_concurrent_market_updates(self, state_manager):
        """Test concurrent market updates."""
        errors = []
        
        def update_market(market_id: int):
            try:
                for i in range(100):
                    state_manager.update_market(
                        f"market-{market_id}",
                        yes_bid=Decimal(f"0.{i:02d}"),
                    )
            except Exception as e:
                errors.append(e)
        
        threads = [
            threading.Thread(target=update_market, args=(i,))
            for i in range(5)
        ]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert len(errors) == 0
        assert len(state_manager.get_all_markets()) == 5
    
    def test_concurrent_position_updates(self, state_manager):
        """Test concurrent position updates."""
        errors = []
        
        def update_position(market_id: int):
            try:
                for i in range(100):
                    state_manager.update_position(
                        f"market-{market_id}",
                        Side.YES,
                        i,
                        Decimal("0.50"),
                    )
            except Exception as e:
                errors.append(e)
        
        threads = [
            threading.Thread(target=update_position, args=(i,))
            for i in range(5)
        ]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert len(errors) == 0


class TestStateManagerWebSocketHandlers:
    """Tests for WebSocket handler integration."""
    
    @pytest.mark.asyncio
    async def test_market_handler(self, state_manager):
        """Test market data handler."""
        handler = state_manager.create_market_handler()
        
        message = {
            "type": "MARKET_DATA",
            "marketSlug": "nba-test-game",
            "yes": {
                "bids": [["0.47", "500"]],
                "asks": [["0.49", "300"]],
            },
            "no": {
                "bids": [["0.51", "400"]],
                "asks": [["0.53", "350"]],
            },
        }
        
        await handler(message)
        
        market = state_manager.get_market("nba-test-game")
        assert market is not None
        assert market.yes_bid == Decimal("0.47")
        assert market.yes_ask == Decimal("0.49")
        assert market.no_bid == Decimal("0.51")
        assert market.no_ask == Decimal("0.53")
    
    @pytest.mark.asyncio
    async def test_market_handler_ignores_other_types(self, state_manager):
        """Test that handler ignores non-market-data messages."""
        handler = state_manager.create_market_handler()
        
        await handler({"type": "ORDER_UPDATE", "orderId": "123"})
        
        assert len(state_manager.get_all_markets()) == 0


# =============================================================================
# PaperExecutor Tests
# =============================================================================

class TestPaperExecutorInit:
    """Tests for PaperExecutor initialization."""
    
    def test_init(self, paper_executor):
        """Test basic initialization."""
        assert paper_executor._initial_balance == Decimal("1000")
    
    def test_init_custom_balance(self, state_manager, orderbook_tracker):
        """Test initialization with custom balance."""
        executor = PaperExecutor(
            state_manager,
            orderbook_tracker,
            initial_balance=Decimal("5000"),
        )
        assert executor.state.get_balance() == Decimal("5000")


class TestPaperExecutorOrderExecution:
    """Tests for order execution."""
    
    def test_buy_yes_immediate_fill(self, paper_executor, market_with_book):
        """Test buying YES shares with immediate fill."""
        order = PaperOrderRequest(
            market_slug=market_with_book,
            intent=OrderIntent.BUY_LONG,
            quantity=100,
            price=Decimal("0.50"),  # Above best ask of 0.49
        )
        
        result = paper_executor.execute_order(order)
        
        assert result.is_success
        assert result.is_filled
        assert result.filled_quantity == 100
        assert result.avg_fill_price == Decimal("0.49")  # Filled at best ask
        assert result.fee > Decimal("0")
    
    def test_buy_no_immediate_fill(self, paper_executor, market_with_book):
        """Test buying NO shares with immediate fill."""
        order = PaperOrderRequest(
            market_slug=market_with_book,
            intent=OrderIntent.BUY_SHORT,
            quantity=100,
            price=Decimal("0.55"),  # Above best ask of 0.53
        )
        
        result = paper_executor.execute_order(order)
        
        assert result.is_filled
        assert result.avg_fill_price == Decimal("0.53")  # NO best ask
    
    def test_sell_yes_immediate_fill(self, paper_executor, market_with_book):
        """Test selling YES shares."""
        # First buy some shares
        buy_order = PaperOrderRequest(
            market_slug=market_with_book,
            intent=OrderIntent.BUY_LONG,
            quantity=100,
            price=Decimal("0.50"),
        )
        paper_executor.execute_order(buy_order)
        
        # Now sell
        sell_order = PaperOrderRequest(
            market_slug=market_with_book,
            intent=OrderIntent.SELL_LONG,
            quantity=50,
            price=Decimal("0.45"),  # Below best bid of 0.47
        )
        
        result = paper_executor.execute_order(sell_order)
        
        assert result.is_filled
        assert result.avg_fill_price == Decimal("0.47")  # YES best bid
    
    def test_resting_order(self, paper_executor, market_with_book):
        """Test that non-marketable limit order rests on book."""
        order = PaperOrderRequest(
            market_slug=market_with_book,
            intent=OrderIntent.BUY_LONG,
            quantity=100,
            price=Decimal("0.45"),  # Below best ask of 0.49
        )
        
        result = paper_executor.execute_order(order)
        
        assert result.status == OrderStatus.OPEN
        assert result.filled_quantity == 0
        
        # Order should be in state
        open_orders = paper_executor.state.get_open_orders()
        assert len(open_orders) == 1
    
    def test_insufficient_balance(self, paper_executor, market_with_book):
        """Test rejection when balance is insufficient."""
        order = PaperOrderRequest(
            market_slug=market_with_book,
            intent=OrderIntent.BUY_LONG,
            quantity=10000,  # Way more than balance can cover
            price=Decimal("0.50"),
        )
        
        result = paper_executor.execute_order(order)
        
        assert not result.is_success
        assert result.status == OrderStatus.REJECTED
        assert "Insufficient balance" in result.error
    
    def test_market_not_found(self, paper_executor):
        """Test rejection when market is not found."""
        order = PaperOrderRequest(
            market_slug="nonexistent-market",
            intent=OrderIntent.BUY_LONG,
            quantity=100,
            price=Decimal("0.50"),
        )
        
        result = paper_executor.execute_order(order)
        
        assert not result.is_success
        assert "Market not found" in result.error


class TestPaperExecutorFees:
    """Tests for fee calculation."""
    
    def test_taker_fee_calculation(self, paper_executor, market_with_book):
        """Test that taker fee is 0.1%."""
        order = PaperOrderRequest(
            market_slug=market_with_book,
            intent=OrderIntent.BUY_LONG,
            quantity=100,
            price=Decimal("0.50"),
        )
        
        result = paper_executor.execute_order(order)
        
        # Cost = 0.49 * 100 = 49, Fee = 49 * 0.001 = 0.049
        expected_fee = Decimal("0.49") * 100 * Decimal("0.001")
        assert result.fee == expected_fee
    
    def test_fees_tracked_in_performance(self, paper_executor, market_with_book):
        """Test that fees are tracked in performance metrics."""
        order = PaperOrderRequest(
            market_slug=market_with_book,
            intent=OrderIntent.BUY_LONG,
            quantity=100,
            price=Decimal("0.50"),
        )
        
        result = paper_executor.execute_order(order)
        
        performance = paper_executor.get_performance()
        assert performance.total_fees == result.fee


class TestPaperExecutorPositionManagement:
    """Tests for position management."""
    
    def test_position_created_on_buy(self, paper_executor, market_with_book):
        """Test that position is created on buy."""
        order = PaperOrderRequest(
            market_slug=market_with_book,
            intent=OrderIntent.BUY_LONG,
            quantity=100,
            price=Decimal("0.50"),
        )
        
        paper_executor.execute_order(order)
        
        position = paper_executor.state.get_position(market_with_book)
        assert position is not None
        assert position.side == Side.YES
        assert position.quantity == 100
        assert position.avg_price == Decimal("0.49")  # Fill price
    
    def test_position_averaging(self, paper_executor, market_with_book):
        """Test average price calculation on multiple buys."""
        # First buy at 0.49
        order1 = PaperOrderRequest(
            market_slug=market_with_book,
            intent=OrderIntent.BUY_LONG,
            quantity=100,
            price=Decimal("0.50"),
        )
        paper_executor.execute_order(order1)
        
        # Update order book with different price
        paper_executor.orderbook.update(market_with_book, {
            "yes": {
                "bids": [["0.50", "500"]],
                "asks": [["0.52", "300"]],
            },
            "no": {
                "bids": [["0.48", "400"]],
                "asks": [["0.50", "350"]],
            },
        })
        
        # Second buy at 0.52
        order2 = PaperOrderRequest(
            market_slug=market_with_book,
            intent=OrderIntent.BUY_LONG,
            quantity=100,
            price=Decimal("0.55"),
        )
        paper_executor.execute_order(order2)
        
        position = paper_executor.state.get_position(market_with_book)
        assert position.quantity == 200
        
        # Average price = (49 + 52) / 2 = 50.5
        expected_avg = (Decimal("0.49") * 100 + Decimal("0.52") * 100) / 200
        assert position.avg_price == expected_avg
    
    def test_partial_position_close(self, paper_executor, market_with_book):
        """Test selling part of a position."""
        # Buy 100 shares
        buy_order = PaperOrderRequest(
            market_slug=market_with_book,
            intent=OrderIntent.BUY_LONG,
            quantity=100,
            price=Decimal("0.50"),
        )
        paper_executor.execute_order(buy_order)
        
        # Sell 50 shares
        sell_order = PaperOrderRequest(
            market_slug=market_with_book,
            intent=OrderIntent.SELL_LONG,
            quantity=50,
            price=Decimal("0.45"),
        )
        paper_executor.execute_order(sell_order)
        
        position = paper_executor.state.get_position(market_with_book)
        assert position is not None
        assert position.quantity == 50
    
    def test_full_position_close(self, paper_executor, market_with_book):
        """Test closing entire position."""
        # Buy 100 shares
        buy_order = PaperOrderRequest(
            market_slug=market_with_book,
            intent=OrderIntent.BUY_LONG,
            quantity=100,
            price=Decimal("0.50"),
        )
        paper_executor.execute_order(buy_order)
        
        # Sell all 100 shares
        sell_order = PaperOrderRequest(
            market_slug=market_with_book,
            intent=OrderIntent.SELL_LONG,
            quantity=100,
            price=Decimal("0.45"),
        )
        paper_executor.execute_order(sell_order)
        
        position = paper_executor.state.get_position(market_with_book)
        assert position is None

    def test_side_flip_realized_pnl_uses_same_price_basis(self, paper_executor):
        """
        When closing via the opposite side (YES <-> NO), realized P&L must compare
        prices in the existing position's basis. This happens in practice when
        sells are normalized into opposite-side buys and a later trade flips sides.
        """
        market_slug = "test-flip-market"

        # Start with a YES position.
        paper_executor.state.update_position(
            market_slug=market_slug,
            side=Side.YES,
            quantity=10,
            avg_price=Decimal("0.30"),
        )

        # Buying the opposite side closes the YES position. The close price is
        # expressed in NO basis, so the effective YES close price is (1 - NO_price).
        start_balance = paper_executor.state.get_balance()
        realized = paper_executor._update_position(
            market_slug=market_slug,
            side=Side.NO,
            quantity=10,
            price=Decimal("0.60"),
            is_buy=True,
            fee=Decimal("0"),
        )

        assert realized == Decimal("1.0")  # (1 - 0.60 - 0.30) * 10
        # Cashflow: + (0.40 * 10) - (0.60 * 10) = -2.0
        assert paper_executor.state.get_balance() == start_balance - Decimal("2.0")
        pos = paper_executor.state.get_position(market_slug)
        assert pos is not None
        assert pos.side == Side.NO
        assert pos.quantity == 10
        assert pos.avg_price == Decimal("0.60")
        assert paper_executor._realized_pnl_total == Decimal("1.0")

    def test_side_flip_partial_close_opens_remaining_new_side(self, paper_executor):
        """Under Option A, side flip closes existing and opens new side at order qty."""
        market_slug = "test-flip-partial-market"

        paper_executor.state.update_position(
            market_slug=market_slug,
            side=Side.YES,
            quantity=10,
            avg_price=Decimal("0.30"),
        )

        realized = paper_executor._update_position(
            market_slug=market_slug,
            side=Side.NO,
            quantity=15,
            price=Decimal("0.60"),
            is_buy=True,
            fee=Decimal("0"),
        )

        assert realized == Decimal("1.0")  # Close 10 YES at effective 0.40
        pos = paper_executor.state.get_position(market_slug)
        assert pos is not None
        assert pos.side == Side.NO
        assert pos.quantity == 15
        assert pos.avg_price == Decimal("0.60")


class TestPaperExecutorBalanceManagement:
    """Tests for balance management during execution."""
    
    def test_balance_decreases_on_buy(self, paper_executor, market_with_book):
        """Test that balance decreases on buy."""
        initial = paper_executor.state.get_balance()
        
        order = PaperOrderRequest(
            market_slug=market_with_book,
            intent=OrderIntent.BUY_LONG,
            quantity=100,
            price=Decimal("0.50"),
        )
        result = paper_executor.execute_order(order)
        
        expected_cost = Decimal("0.49") * 100
        expected_fee = expected_cost * Decimal("0.001")
        expected_total = expected_cost + expected_fee
        
        assert paper_executor.state.get_balance() == initial - expected_total
    
    def test_balance_increases_on_sell(self, paper_executor, market_with_book):
        """Test that balance increases on sell (minus fees)."""
        # Buy first
        buy_order = PaperOrderRequest(
            market_slug=market_with_book,
            intent=OrderIntent.BUY_LONG,
            quantity=100,
            price=Decimal("0.50"),
        )
        paper_executor.execute_order(buy_order)
        
        balance_after_buy = paper_executor.state.get_balance()
        
        # Sell
        sell_order = PaperOrderRequest(
            market_slug=market_with_book,
            intent=OrderIntent.SELL_LONG,
            quantity=100,
            price=Decimal("0.45"),
        )
        paper_executor.execute_order(sell_order)
        
        proceeds = Decimal("0.47") * 100  # Fill at bid
        fee = proceeds * Decimal("0.001")
        expected_balance = balance_after_buy + proceeds - fee
        
        assert paper_executor.state.get_balance() == expected_balance


class TestPaperExecutorOrderManagement:
    """Tests for order management."""
    
    def test_cancel_order(self, paper_executor, market_with_book):
        """Test cancelling a resting order."""
        order = PaperOrderRequest(
            market_slug=market_with_book,
            intent=OrderIntent.BUY_LONG,
            quantity=100,
            price=Decimal("0.45"),  # Will rest
        )
        
        result = paper_executor.execute_order(order)
        assert result.status == OrderStatus.OPEN
        
        cancelled = paper_executor.cancel_order(result.order_id)
        assert cancelled is True
        
        assert len(paper_executor.state.get_open_orders()) == 0
    
    def test_cancel_all_orders(self, paper_executor, market_with_book):
        """Test cancelling all orders."""
        # Create multiple resting orders
        for i in range(3):
            order = PaperOrderRequest(
                market_slug=market_with_book,
                intent=OrderIntent.BUY_LONG,
                quantity=100,
                price=Decimal("0.40") + Decimal(str(i)) * Decimal("0.01"),
            )
            paper_executor.execute_order(order)
        
        assert len(paper_executor.state.get_open_orders()) == 3
        
        cancelled = paper_executor.cancel_all_orders()
        assert cancelled == 3
        assert len(paper_executor.state.get_open_orders()) == 0
    
    def test_check_resting_orders(self, paper_executor, market_with_book):
        """Test checking if resting orders can fill."""
        # Create a resting buy order
        order = PaperOrderRequest(
            market_slug=market_with_book,
            intent=OrderIntent.BUY_LONG,
            quantity=100,
            price=Decimal("0.48"),  # Below current ask of 0.49
        )
        result = paper_executor.execute_order(order)
        assert result.status == OrderStatus.OPEN
        
        # Update order book with lower ask
        paper_executor.orderbook.update(market_with_book, {
            "yes": {
                "bids": [["0.45", "500"]],
                "asks": [["0.47", "300"]],  # Now below our limit
            },
            "no": {
                "bids": [["0.53", "400"]],
                "asks": [["0.55", "350"]],
            },
        })
        
        # Check resting orders
        fills = paper_executor.check_resting_orders()
        
        assert len(fills) == 1
        assert fills[0].is_filled


class TestPaperExecutorPerformance:
    """Tests for performance metrics."""
    
    def test_initial_performance(self, paper_executor):
        """Test performance metrics with no trades."""
        perf = paper_executor.get_performance()
        
        assert perf.initial_balance == Decimal("1000")
        assert perf.current_balance == Decimal("1000")
        assert perf.total_trades == 0
        assert perf.total_fees == Decimal("0")
        assert perf.total_pnl == Decimal("0")
    
    def test_performance_after_trades(self, paper_executor, market_with_book):
        """Test performance after executing trades."""
        order = PaperOrderRequest(
            market_slug=market_with_book,
            intent=OrderIntent.BUY_LONG,
            quantity=100,
            price=Decimal("0.50"),
        )
        paper_executor.execute_order(order)
        
        perf = paper_executor.get_performance()
        
        assert perf.total_trades == 1
        assert perf.total_fees > Decimal("0")
        assert perf.open_positions == 1
    
    def test_performance_pnl_calculation(self, paper_executor, market_with_book):
        """Test P&L calculation."""
        # Buy at 0.49
        buy_order = PaperOrderRequest(
            market_slug=market_with_book,
            intent=OrderIntent.BUY_LONG,
            quantity=100,
            price=Decimal("0.50"),
        )
        paper_executor.execute_order(buy_order)
        
        # Update price higher
        paper_executor.orderbook.update(market_with_book, {
            "yes": {
                "bids": [["0.55", "500"]],
                "asks": [["0.57", "300"]],
            },
            "no": {
                "bids": [["0.43", "400"]],
                "asks": [["0.45", "350"]],
            },
        })
        
        perf = paper_executor.get_performance()
        
        # Unrealized P&L = (0.55 - 0.49) * 100 = 6
        assert perf.unrealized_pnl == Decimal("6")

    def test_performance_no_unrealized_pnl(self, paper_executor, market_with_book):
        """Test unrealized P&L for NO positions."""
        paper_executor.orderbook.update(market_with_book, {
            "yes": {
                "bids": [["0.60", "500"]],
                "asks": [["0.62", "300"]],
            },
            "no": {
                "bids": [["0.39", "400"]],
                "asks": [["0.40", "350"]],
            },
        })

        buy_no = PaperOrderRequest(
            market_slug=market_with_book,
            intent=OrderIntent.BUY_SHORT,
            quantity=100,
            price=Decimal("0.41"),
        )
        paper_executor.execute_order(buy_no)

        paper_executor.orderbook.update(market_with_book, {
            "yes": {
                "bids": [["0.38", "500"]],
                "asks": [["0.40", "300"]],
            },
            "no": {
                "bids": [["0.60", "400"]],
                "asks": [["0.62", "350"]],
            },
        })

        perf = paper_executor.get_performance()
        assert perf.unrealized_pnl == Decimal("20")

    def test_performance_no_realized_pnl(self, paper_executor, market_with_book):
        """Test realized P&L for NO positions."""
        paper_executor.orderbook.update(market_with_book, {
            "yes": {
                "bids": [["0.60", "500"]],
                "asks": [["0.62", "300"]],
            },
            "no": {
                "bids": [["0.39", "400"]],
                "asks": [["0.40", "350"]],
            },
        })

        buy_no = PaperOrderRequest(
            market_slug=market_with_book,
            intent=OrderIntent.BUY_SHORT,
            quantity=100,
            price=Decimal("0.41"),
        )
        paper_executor.execute_order(buy_no)

        paper_executor.orderbook.update(market_with_book, {
            "yes": {
                "bids": [["0.38", "500"]],
                "asks": [["0.40", "300"]],
            },
            "no": {
                "bids": [["0.60", "400"]],
                "asks": [["0.62", "350"]],
            },
        })

        sell_no = PaperOrderRequest(
            market_slug=market_with_book,
            intent=OrderIntent.SELL_SHORT,
            quantity=100,
            price=Decimal("0.59"),
        )
        paper_executor.execute_order(sell_no)

        perf = paper_executor.get_performance()
        assert perf.realized_pnl == Decimal("20")
        assert perf.unrealized_pnl == Decimal("0")

    def test_performance_pnl_reconciliation(self, paper_executor, market_with_book):
        """Test that P&L reconciles with fees for NO positions."""
        paper_executor.orderbook.update(market_with_book, {
            "yes": {
                "bids": [["0.60", "500"]],
                "asks": [["0.62", "300"]],
            },
            "no": {
                "bids": [["0.39", "400"]],
                "asks": [["0.40", "350"]],
            },
        })

        buy_no = PaperOrderRequest(
            market_slug=market_with_book,
            intent=OrderIntent.BUY_SHORT,
            quantity=100,
            price=Decimal("0.41"),
        )
        paper_executor.execute_order(buy_no)

        paper_executor.orderbook.update(market_with_book, {
            "yes": {
                "bids": [["0.38", "500"]],
                "asks": [["0.40", "300"]],
            },
            "no": {
                "bids": [["0.60", "400"]],
                "asks": [["0.62", "350"]],
            },
        })

        sell_no = PaperOrderRequest(
            market_slug=market_with_book,
            intent=OrderIntent.SELL_SHORT,
            quantity=100,
            price=Decimal("0.59"),
        )
        paper_executor.execute_order(sell_no)

        perf = paper_executor.get_performance()
        expected_total_pnl = perf.realized_pnl + perf.unrealized_pnl - perf.total_fees
        assert perf.total_pnl == expected_total_pnl

    def test_performance_pnl_reconciliation_after_side_flip(self, paper_executor):
        """P&L should reconcile even after a buy-side side flip (YES<->NO)."""
        market_slug = "flip-reconcile-market"

        # Set up a book where both YES and NO have liquidity.
        paper_executor.orderbook.update(market_slug, {
            "yes": {"bids": [["0.39", "500"]], "asks": [["0.40", "500"]]},
            "no": {"bids": [["0.59", "500"]], "asks": [["0.60", "500"]]},
        })

        # Buy YES (fills at 0.40)
        paper_executor.execute_order(PaperOrderRequest(
            market_slug=market_slug,
            intent=OrderIntent.BUY_LONG,
            quantity=10,
            price=Decimal("0.41"),
        ))

        # Flip by buying NO (fills at 0.60); Option A: synthetic close YES at 0.40,
        # then open NO at 0.60.
        paper_executor.execute_order(PaperOrderRequest(
            market_slug=market_slug,
            intent=OrderIntent.BUY_SHORT,
            quantity=10,
            price=Decimal("0.61"),
        ))

        perf = paper_executor.get_performance()
        expected_total_pnl = perf.realized_pnl + perf.unrealized_pnl - perf.total_fees
        assert perf.total_pnl == expected_total_pnl
    
    def test_trade_history(self, paper_executor, market_with_book):
        """Test trade history retrieval."""
        order = PaperOrderRequest(
            market_slug=market_with_book,
            intent=OrderIntent.BUY_LONG,
            quantity=100,
            price=Decimal("0.50"),
        )
        paper_executor.execute_order(order)
        
        trades = paper_executor.get_trades()
        assert len(trades) == 1
        
        history = paper_executor.get_trade_history()
        assert len(history) == 1
        assert history[0]["market_slug"] == market_with_book


class TestPaperExecutorReset:
    """Tests for reset functionality."""
    
    def test_reset(self, paper_executor, market_with_book):
        """Test resetting paper executor."""
        # Execute some trades
        order = PaperOrderRequest(
            market_slug=market_with_book,
            intent=OrderIntent.BUY_LONG,
            quantity=100,
            price=Decimal("0.50"),
        )
        paper_executor.execute_order(order)
        
        assert len(paper_executor.get_trades()) == 1
        
        # Reset
        paper_executor.reset()
        
        assert paper_executor.state.get_balance() == Decimal("1000")
        assert len(paper_executor.get_trades()) == 0
        assert len(paper_executor.state.get_all_positions()) == 0
    
    def test_reset_with_new_balance(self, paper_executor, market_with_book):
        """Test resetting with a new initial balance."""
        paper_executor.reset(initial_balance=Decimal("5000"))
        
        assert paper_executor.state.get_balance() == Decimal("5000")
        assert paper_executor._initial_balance == Decimal("5000")


class TestTradeRecord:
    """Tests for TradeRecord dataclass."""
    
    def test_trade_record_total_cost(self):
        """Test total cost calculation."""
        trade = TradeRecord(
            trade_id="trade-1",
            order_id="order-1",
            market_slug="test-market",
            side=Side.YES,
            intent=OrderIntent.BUY_LONG,
            quantity=100,
            price=Decimal("0.50"),
            cost=Decimal("50"),
            fee=Decimal("0.05"),
        )
        
        assert trade.total_cost == Decimal("50.05")


class TestExecutionResult:
    """Tests for ExecutionResult dataclass."""
    
    def test_execution_result_properties(self):
        """Test result properties."""
        result = ExecutionResult(
            order_id="order-1",
            status=OrderStatus.FILLED,
            filled_quantity=100,
            avg_fill_price=Decimal("0.50"),
            fee=Decimal("0.05"),
        )
        
        assert result.is_success
        assert result.is_filled
    
    def test_execution_result_to_dict(self):
        """Test dictionary conversion."""
        result = ExecutionResult(
            order_id="order-1",
            status=OrderStatus.FILLED,
            filled_quantity=100,
            avg_fill_price=Decimal("0.50"),
            fee=Decimal("0.05"),
        )
        
        d = result.to_dict()
        assert d["orderId"] == "order-1"
        assert d["status"] == "FILLED"
        assert d["filledQuantity"] == 100


class TestPerformanceMetrics:
    """Tests for PerformanceMetrics dataclass."""
    
    def test_win_rate(self):
        """Test win rate calculation."""
        metrics = PerformanceMetrics(
            initial_balance=Decimal("1000"),
            current_balance=Decimal("1100"),
            position_value=Decimal("0"),
            position_value_best_bid=Decimal("0"),
            position_value_liquidation=Decimal("0"),
            total_equity=Decimal("1100"),
            total_equity_best_bid=Decimal("1100"),
            total_pnl=Decimal("100"),
            total_pnl_best_bid=Decimal("100"),
            realized_pnl=Decimal("100"),
            unrealized_pnl=Decimal("0"),
            unrealized_pnl_best_bid=Decimal("0"),
            unrealized_pnl_liquidation=Decimal("0"),
            total_fees=Decimal("5"),
            total_trades=10,
            winning_trades=7,
            losing_trades=3,
            open_positions=0,
        )
        
        assert metrics.win_rate == 70.0
    
    def test_pnl_percent(self):
        """Test P&L percentage calculation."""
        metrics = PerformanceMetrics(
            initial_balance=Decimal("1000"),
            current_balance=Decimal("1100"),
            position_value=Decimal("0"),
            position_value_best_bid=Decimal("0"),
            position_value_liquidation=Decimal("0"),
            total_equity=Decimal("1100"),
            total_equity_best_bid=Decimal("1100"),
            total_pnl=Decimal("100"),
            total_pnl_best_bid=Decimal("100"),
            realized_pnl=Decimal("100"),
            unrealized_pnl=Decimal("0"),
            unrealized_pnl_best_bid=Decimal("0"),
            unrealized_pnl_liquidation=Decimal("0"),
            total_fees=Decimal("5"),
            total_trades=10,
            winning_trades=7,
            losing_trades=3,
            open_positions=0,
        )
        
        assert metrics.pnl_percent == 10.0


# =============================================================================
# Integration Tests
# =============================================================================

class TestPaperTradingIntegration:
    """Integration tests for the paper trading module."""
    
    def test_full_trading_session(self, paper_executor, market_with_book):
        """Test a complete trading session."""
        # 1. Buy YES shares
        buy_result = paper_executor.execute_order(PaperOrderRequest(
            market_slug=market_with_book,
            intent=OrderIntent.BUY_LONG,
            quantity=100,
            price=Decimal("0.50"),
        ))
        assert buy_result.is_filled
        
        # 2. Check position
        position = paper_executor.state.get_position(market_with_book)
        assert position is not None
        assert position.quantity == 100
        
        # 3. Price moves up
        paper_executor.orderbook.update(market_with_book, {
            "yes": {
                "bids": [["0.55", "500"]],
                "asks": [["0.57", "300"]],
            },
            "no": {
                "bids": [["0.43", "400"]],
                "asks": [["0.45", "350"]],
            },
        })
        
        # 4. Check unrealized profit
        perf = paper_executor.get_performance()
        assert perf.unrealized_pnl > Decimal("0")
        
        # 5. Sell for profit
        sell_result = paper_executor.execute_order(PaperOrderRequest(
            market_slug=market_with_book,
            intent=OrderIntent.SELL_LONG,
            quantity=100,
            price=Decimal("0.50"),
        ))
        assert sell_result.is_filled
        
        # 6. Position should be closed
        assert paper_executor.state.get_position(market_with_book) is None
        
        # 7. Should have profit (minus fees)
        final_perf = paper_executor.get_performance()
        assert final_perf.total_trades == 2
        # Note: P&L might be positive or negative depending on fees
    
    def test_multiple_markets(self, paper_executor, orderbook_tracker):
        """Test trading across multiple markets."""
        # Setup two markets
        orderbook_tracker.update("market-1", {
            "yes": {"bids": [["0.50", "500"]], "asks": [["0.52", "300"]]},
            "no": {"bids": [["0.48", "400"]], "asks": [["0.50", "350"]]},
        })
        orderbook_tracker.update("market-2", {
            "yes": {"bids": [["0.60", "500"]], "asks": [["0.62", "300"]]},
            "no": {"bids": [["0.38", "400"]], "asks": [["0.40", "350"]]},
        })
        
        # Buy in both markets
        paper_executor.execute_order(PaperOrderRequest(
            market_slug="market-1",
            intent=OrderIntent.BUY_LONG,
            quantity=50,
            price=Decimal("0.55"),
        ))
        
        paper_executor.execute_order(PaperOrderRequest(
            market_slug="market-2",
            intent=OrderIntent.BUY_LONG,
            quantity=50,
            price=Decimal("0.65"),
        ))
        
        # Should have two positions
        positions = paper_executor.state.get_all_positions()
        assert len(positions) == 2
        
        perf = paper_executor.get_performance()
        assert perf.open_positions == 2
        assert perf.total_trades == 2
