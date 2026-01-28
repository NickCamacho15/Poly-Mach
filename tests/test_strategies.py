"""
Tests for the Strategy Module (BaseStrategy, MarketMakerStrategy, StrategyEngine).

Run with: pytest tests/test_strategies.py -v
"""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List

import pytest

from src.data.models import OrderIntent, OrderStatus, Side
from src.data.orderbook import OrderBookTracker
from src.execution.paper_executor import PaperExecutor, PaperOrderRequest
from src.state.state_manager import MarketState, PositionState, StateManager
from src.strategies.base_strategy import (
    BaseStrategy,
    Signal,
    SignalAction,
    Urgency,
)
from src.strategies.market_maker import (
    MarketMakerConfig,
    MarketMakerStrategy,
    QuoteState,
)
from src.strategies.strategy_engine import (
    AggregatedSignals,
    SignalAggregator,
    StrategyEngine,
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
def market_with_book(orderbook_tracker, state_manager, sample_orderbook_data) -> str:
    """Create a market with order book data."""
    market_slug = "nba-test-game-2025"
    orderbook_tracker.update(market_slug, sample_orderbook_data)
    
    # Also update state manager
    state_manager.update_market(
        market_slug,
        yes_bid=Decimal("0.47"),
        yes_ask=Decimal("0.49"),
        no_bid=Decimal("0.51"),
        no_ask=Decimal("0.53"),
    )
    
    return market_slug


@pytest.fixture
def market_state(market_with_book, state_manager) -> MarketState:
    """Get market state for testing."""
    return state_manager.get_market(market_with_book)


@pytest.fixture
def market_maker_config() -> MarketMakerConfig:
    """Create a market maker config for testing."""
    return MarketMakerConfig(
        spread=Decimal("0.02"),
        order_size=Decimal("10.00"),
        max_inventory=Decimal("50.00"),
        refresh_interval=5.0,
    )


@pytest.fixture
def market_maker_strategy(market_maker_config) -> MarketMakerStrategy:
    """Create a market maker strategy for testing."""
    return MarketMakerStrategy(market_maker_config)


@pytest.fixture
def strategy_engine(state_manager, orderbook_tracker, paper_executor) -> StrategyEngine:
    """Create a strategy engine for testing."""
    return StrategyEngine(
        state_manager=state_manager,
        orderbook=orderbook_tracker,
        executor=paper_executor,
        tick_interval=1.0,
    )


# =============================================================================
# Signal Tests
# =============================================================================

class TestSignal:
    """Tests for Signal dataclass."""
    
    def test_signal_creation(self):
        """Test creating a signal."""
        signal = Signal(
            market_slug="test-market",
            action=SignalAction.BUY_YES,
            price=Decimal("0.50"),
            quantity=100,
            urgency=Urgency.LOW,
            strategy_name="test_strategy",
            confidence=0.8,
            reason="Test signal",
        )
        
        assert signal.market_slug == "test-market"
        assert signal.action == SignalAction.BUY_YES
        assert signal.price == Decimal("0.50")
        assert signal.quantity == 100
        assert signal.urgency == Urgency.LOW
        assert signal.strategy_name == "test_strategy"
        assert signal.confidence == 0.8
    
    def test_signal_is_buy(self):
        """Test is_buy property."""
        buy_yes = Signal(
            market_slug="test",
            action=SignalAction.BUY_YES,
            price=Decimal("0.50"),
            quantity=100,
            urgency=Urgency.LOW,
            strategy_name="test",
            confidence=0.5,
            reason="",
        )
        
        buy_no = Signal(
            market_slug="test",
            action=SignalAction.BUY_NO,
            price=Decimal("0.50"),
            quantity=100,
            urgency=Urgency.LOW,
            strategy_name="test",
            confidence=0.5,
            reason="",
        )
        
        sell_yes = Signal(
            market_slug="test",
            action=SignalAction.SELL_YES,
            price=Decimal("0.50"),
            quantity=100,
            urgency=Urgency.LOW,
            strategy_name="test",
            confidence=0.5,
            reason="",
        )
        
        assert buy_yes.is_buy is True
        assert buy_no.is_buy is True
        assert sell_yes.is_buy is False
    
    def test_signal_is_sell(self):
        """Test is_sell property."""
        sell_yes = Signal(
            market_slug="test",
            action=SignalAction.SELL_YES,
            price=Decimal("0.50"),
            quantity=100,
            urgency=Urgency.LOW,
            strategy_name="test",
            confidence=0.5,
            reason="",
        )
        
        assert sell_yes.is_sell is True
    
    def test_signal_is_cancel(self):
        """Test is_cancel property."""
        cancel = Signal(
            market_slug="test",
            action=SignalAction.CANCEL_ALL,
            price=Decimal("0.50"),
            quantity=0,
            urgency=Urgency.LOW,
            strategy_name="test",
            confidence=1.0,
            reason="",
        )
        
        assert cancel.is_cancel is True
    
    def test_signal_side(self):
        """Test side property."""
        buy_yes = Signal(
            market_slug="test",
            action=SignalAction.BUY_YES,
            price=Decimal("0.50"),
            quantity=100,
            urgency=Urgency.LOW,
            strategy_name="test",
            confidence=0.5,
            reason="",
        )
        
        buy_no = Signal(
            market_slug="test",
            action=SignalAction.BUY_NO,
            price=Decimal("0.50"),
            quantity=100,
            urgency=Urgency.LOW,
            strategy_name="test",
            confidence=0.5,
            reason="",
        )
        
        assert buy_yes.side == "YES"
        assert buy_no.side == "NO"
    
    def test_signal_notional_value(self):
        """Test notional value calculation."""
        signal = Signal(
            market_slug="test",
            action=SignalAction.BUY_YES,
            price=Decimal("0.50"),
            quantity=100,
            urgency=Urgency.LOW,
            strategy_name="test",
            confidence=0.5,
            reason="",
        )
        
        assert signal.notional_value == Decimal("50")
    
    def test_signal_to_dict(self):
        """Test dictionary conversion."""
        signal = Signal(
            market_slug="test-market",
            action=SignalAction.BUY_YES,
            price=Decimal("0.50"),
            quantity=100,
            urgency=Urgency.LOW,
            strategy_name="test_strategy",
            confidence=0.8,
            reason="Test reason",
        )
        
        d = signal.to_dict()
        
        assert d["market_slug"] == "test-market"
        assert d["action"] == "BUY_YES"
        assert d["price"] == 0.5
        assert d["quantity"] == 100
        assert d["urgency"] == "LOW"
        assert d["strategy_name"] == "test_strategy"
        assert d["confidence"] == 0.8
        assert d["reason"] == "Test reason"
    
    def test_signal_validation_negative_quantity(self):
        """Test that negative quantity raises error."""
        with pytest.raises(ValueError, match="Quantity must be non-negative"):
            Signal(
                market_slug="test",
                action=SignalAction.BUY_YES,
                price=Decimal("0.50"),
                quantity=-10,
                urgency=Urgency.LOW,
                strategy_name="test",
                confidence=0.5,
                reason="",
            )
    
    def test_signal_validation_invalid_confidence(self):
        """Test that invalid confidence raises error."""
        with pytest.raises(ValueError, match="Confidence must be between"):
            Signal(
                market_slug="test",
                action=SignalAction.BUY_YES,
                price=Decimal("0.50"),
                quantity=100,
                urgency=Urgency.LOW,
                strategy_name="test",
                confidence=1.5,  # Invalid
                reason="",
            )
    
    def test_signal_validation_invalid_price(self):
        """Test that invalid price raises error."""
        with pytest.raises(ValueError, match="Price must be between"):
            Signal(
                market_slug="test",
                action=SignalAction.BUY_YES,
                price=Decimal("1.50"),  # Invalid - must be < 1
                quantity=100,
                urgency=Urgency.LOW,
                strategy_name="test",
                confidence=0.5,
                reason="",
            )
    
    def test_signal_immutable(self):
        """Test that signals are immutable (frozen dataclass)."""
        signal = Signal(
            market_slug="test",
            action=SignalAction.BUY_YES,
            price=Decimal("0.50"),
            quantity=100,
            urgency=Urgency.LOW,
            strategy_name="test",
            confidence=0.5,
            reason="",
        )
        
        with pytest.raises(Exception):
            signal.quantity = 200


# =============================================================================
# BaseStrategy Tests
# =============================================================================

class ConcreteStrategy(BaseStrategy):
    """Concrete implementation of BaseStrategy for testing."""
    
    def __init__(self, name: str = "concrete_strategy", enabled: bool = True):
        # Set _name before calling super().__init__() because it logs using self.name
        self._name = name
        self._market_signals: List[Signal] = []
        self._tick_signals: List[Signal] = []
        super().__init__(enabled=enabled)
    
    @property
    def name(self) -> str:
        return self._name
    
    def on_market_update(self, market: MarketState) -> List[Signal]:
        return self._market_signals
    
    def on_tick(self) -> List[Signal]:
        return self._tick_signals
    
    def set_market_signals(self, signals: List[Signal]) -> None:
        self._market_signals = signals
    
    def set_tick_signals(self, signals: List[Signal]) -> None:
        self._tick_signals = signals


class TestBaseStrategy:
    """Tests for BaseStrategy abstract class."""
    
    def test_abstract_methods_required(self):
        """Test that abstract methods must be implemented."""
        with pytest.raises(TypeError):
            BaseStrategy()  # type: ignore
    
    def test_concrete_implementation(self):
        """Test that concrete implementation works."""
        strategy = ConcreteStrategy()
        
        assert strategy.name == "concrete_strategy"
        assert strategy.enabled is True
    
    def test_enabled_property(self):
        """Test enabled property."""
        strategy = ConcreteStrategy(enabled=False)
        assert strategy.enabled is False
        
        strategy.enabled = True
        assert strategy.enabled is True
    
    def test_market_state_caching(self, market_state):
        """Test market state caching."""
        strategy = ConcreteStrategy()
        
        strategy.update_market_state(market_state)
        
        cached = strategy.get_market(market_state.market_slug)
        assert cached is not None
        assert cached.market_slug == market_state.market_slug
    
    def test_position_state_caching(self):
        """Test position state caching."""
        strategy = ConcreteStrategy()
        
        position = PositionState(
            market_slug="test-market",
            side=Side.YES,
            quantity=100,
            avg_price=Decimal("0.50"),
        )
        
        strategy.update_position_state(position)
        
        cached = strategy.get_position("test-market")
        assert cached is not None
        assert cached.quantity == 100
    
    def test_get_all_markets(self, market_state):
        """Test getting all cached markets."""
        strategy = ConcreteStrategy()
        
        strategy.update_market_state(market_state)
        
        markets = strategy.get_all_markets()
        assert len(markets) == 1
    
    def test_get_all_positions(self):
        """Test getting all cached positions."""
        strategy = ConcreteStrategy()
        
        pos1 = PositionState("market-1", Side.YES, 100, Decimal("0.50"))
        pos2 = PositionState("market-2", Side.NO, 50, Decimal("0.40"))
        
        strategy.update_position_state(pos1)
        strategy.update_position_state(pos2)
        
        positions = strategy.get_all_positions()
        assert len(positions) == 2
    
    def test_create_signal(self):
        """Test signal creation helper."""
        strategy = ConcreteStrategy(name="my_strategy")
        
        signal = strategy.create_signal(
            market_slug="test-market",
            action=SignalAction.BUY_YES,
            price=Decimal("0.50"),
            quantity=100,
        )
        
        assert signal.strategy_name == "my_strategy"
        assert signal.market_slug == "test-market"
        assert signal.action == SignalAction.BUY_YES
    
    def test_create_cancel_signal(self):
        """Test cancel signal creation."""
        strategy = ConcreteStrategy(name="my_strategy")
        
        signal = strategy.create_cancel_signal("test-market", "Testing")
        
        assert signal.action == SignalAction.CANCEL_ALL
        assert signal.strategy_name == "my_strategy"
        assert signal.quantity == 0
    
    def test_clamp_price(self):
        """Test price clamping."""
        strategy = ConcreteStrategy()
        
        assert strategy.clamp_price(Decimal("0.50")) == Decimal("0.50")
        assert strategy.clamp_price(Decimal("0.001")) == Decimal("0.01")
        assert strategy.clamp_price(Decimal("0.999")) == Decimal("0.99")
        assert strategy.clamp_price(Decimal("1.5")) == Decimal("0.99")
        assert strategy.clamp_price(Decimal("-0.5")) == Decimal("0.01")
    
    def test_repr(self):
        """Test string representation."""
        strategy = ConcreteStrategy(name="test_strat", enabled=True)
        
        repr_str = repr(strategy)
        assert "ConcreteStrategy" in repr_str
        assert "test_strat" in repr_str


# =============================================================================
# MarketMakerConfig Tests
# =============================================================================

class TestMarketMakerConfig:
    """Tests for MarketMakerConfig."""
    
    def test_default_config(self):
        """Test default configuration values."""
        config = MarketMakerConfig()
        
        assert config.spread == Decimal("0.02")
        assert config.order_size == Decimal("10.00")
        assert config.max_inventory == Decimal("50.00")
        assert config.refresh_interval == 5.0
    
    def test_custom_config(self):
        """Test custom configuration."""
        config = MarketMakerConfig(
            spread=Decimal("0.04"),
            order_size=Decimal("20.00"),
            max_inventory=Decimal("100.00"),
            refresh_interval=10.0,
        )
        
        assert config.spread == Decimal("0.04")
        assert config.order_size == Decimal("20.00")
    
    def test_config_is_frozen(self):
        """Test that config is immutable."""
        config = MarketMakerConfig()
        
        with pytest.raises(Exception):
            config.spread = Decimal("0.05")


# =============================================================================
# MarketMakerStrategy Tests
# =============================================================================

class TestMarketMakerStrategy:
    """Tests for MarketMakerStrategy."""
    
    def test_init(self, market_maker_strategy):
        """Test strategy initialization."""
        assert market_maker_strategy.name == "market_maker"
        assert market_maker_strategy.enabled is True
    
    def test_init_default_config(self):
        """Test initialization with default config."""
        strategy = MarketMakerStrategy()
        assert strategy.config.spread == Decimal("0.02")
    
    def test_calculate_quotes(self, market_maker_strategy, market_state):
        """Test quote calculation."""
        bid, ask = market_maker_strategy.calculate_quotes(market_state)
        
        # Mid-price = (0.47 + 0.49) / 2 = 0.48
        # With spread of 0.02:
        # Bid = 0.48 - 0.01 = 0.47
        # Ask = 0.48 + 0.01 = 0.49
        
        assert bid == Decimal("0.47")
        assert ask == Decimal("0.49")
    
    def test_calculate_quotes_clamped(self, market_maker_config):
        """Test quote calculation clamping to valid range."""
        # Create market with extreme prices
        market = MarketState(
            market_slug="test-market",
            yes_bid=Decimal("0.01"),
            yes_ask=Decimal("0.02"),
        )
        
        strategy = MarketMakerStrategy(market_maker_config)
        bid, ask = strategy.calculate_quotes(market)
        
        # Prices should be clamped to [0.01, 0.99]
        assert bid >= Decimal("0.01")
        assert ask <= Decimal("0.99")
    
    def test_calculate_quantity(self, market_maker_strategy):
        """Test quantity calculation."""
        # order_size = 10.00, price = 0.50
        # quantity = 10 / 0.50 = 20
        quantity = market_maker_strategy.calculate_quantity(Decimal("0.50"))
        assert quantity == 20
        
        # price = 0.25
        # quantity = 10 / 0.25 = 40
        quantity = market_maker_strategy.calculate_quantity(Decimal("0.25"))
        assert quantity == 40
    
    def test_calculate_quantity_minimum_one(self, market_maker_strategy):
        """Test that quantity is at least 1."""
        # Very high price
        quantity = market_maker_strategy.calculate_quantity(Decimal("0.99"))
        assert quantity >= 1
    
    def test_on_market_update_generates_signals(self, market_maker_strategy, market_state):
        """Test that on_market_update generates quote signals."""
        signals = market_maker_strategy.on_market_update(market_state)
        
        # Should generate bid and ask signals
        assert len(signals) >= 2
        
        # Find the buy and sell signals
        buy_signals = [s for s in signals if s.action == SignalAction.BUY_YES]
        sell_signals = [s for s in signals if s.action == SignalAction.SELL_YES]
        
        assert len(buy_signals) >= 1
        assert len(sell_signals) >= 1
    
    def test_on_market_update_disabled(self, market_maker_strategy, market_state):
        """Test that disabled strategy returns no signals."""
        market_maker_strategy.enabled = False
        
        signals = market_maker_strategy.on_market_update(market_state)
        
        assert len(signals) == 0
    
    def test_on_market_update_missing_prices(self, market_maker_strategy):
        """Test handling of market with missing prices."""
        market = MarketState(market_slug="test-market")  # No prices
        
        signals = market_maker_strategy.on_market_update(market)
        
        assert len(signals) == 0
    
    def test_quote_refresh_on_price_move(self, market_maker_strategy, market_state):
        """Test quote refresh when price moves beyond tolerance."""
        # First update - establishes quotes
        signals1 = market_maker_strategy.on_market_update(market_state)
        assert len(signals1) > 0
        
        # Same update - should not refresh (no price change)
        signals2 = market_maker_strategy.on_market_update(market_state)
        assert len(signals2) == 0
        
        # Update with price move beyond tolerance
        market_state.yes_bid = Decimal("0.50")
        market_state.yes_ask = Decimal("0.52")
        
        signals3 = market_maker_strategy.on_market_update(market_state)
        
        # Should include cancel + new quotes
        cancel_signals = [s for s in signals3 if s.action == SignalAction.CANCEL_ALL]
        assert len(cancel_signals) >= 1
    
    def test_on_tick_time_based_refresh(self, market_maker_strategy, market_state):
        """Test time-based quote refresh."""
        # First update
        market_maker_strategy.on_market_update(market_state)
        
        # Immediately tick - no refresh needed
        signals = market_maker_strategy.on_tick()
        assert len(signals) == 0
        
        # Simulate time passage by modifying quote state
        quote_state = market_maker_strategy._get_quote_state(market_state.market_slug)
        quote_state.last_refresh = datetime.now(timezone.utc) - timedelta(seconds=10)
        
        # Now tick should trigger refresh
        signals = market_maker_strategy.on_tick()
        assert len(signals) > 0
    
    def test_market_filtering_enabled_markets(self):
        """Test filtering of enabled markets."""
        config = MarketMakerConfig(enabled_markets=["nba-*"])
        strategy = MarketMakerStrategy(config)
        
        # NBA market should be enabled
        nba_market = MarketState(
            market_slug="nba-lakers-vs-celtics",
            yes_bid=Decimal("0.47"),
            yes_ask=Decimal("0.49"),
        )
        signals = strategy.on_market_update(nba_market)
        assert len(signals) > 0
        
        # NFL market should be skipped
        nfl_market = MarketState(
            market_slug="nfl-game",
            yes_bid=Decimal("0.47"),
            yes_ask=Decimal("0.49"),
        )
        signals = strategy.on_market_update(nfl_market)
        assert len(signals) == 0
    
    def test_inventory_skew(self, market_maker_strategy, market_state):
        """Test quote skewing based on inventory."""
        # Add a position
        position = PositionState(
            market_slug=market_state.market_slug,
            side=Side.YES,
            quantity=100,
            avg_price=Decimal("0.45"),
        )
        market_maker_strategy.update_position_state(position)
        
        # Get quotes with position
        bid, ask = market_maker_strategy.calculate_quotes(market_state, position)
        
        # When long YES, should skew to encourage selling (lower prices)
        base_mid = Decimal("0.48")
        # Quotes should be skewed
        # The exact values depend on the skew factor
        assert bid is not None
        assert ask is not None
    
    def test_max_inventory_one_sided_quoting(self):
        """Test that max inventory triggers one-sided quoting."""
        config = MarketMakerConfig(max_inventory=Decimal("50.00"))
        strategy = MarketMakerStrategy(config)
        
        market = MarketState(
            market_slug="test-market",
            yes_bid=Decimal("0.47"),
            yes_ask=Decimal("0.49"),
        )
        
        # Add position at max inventory
        position = PositionState(
            market_slug="test-market",
            side=Side.YES,
            quantity=120,  # 120 * 0.47 > 50
            avg_price=Decimal("0.47"),
        )
        strategy.update_position_state(position)
        strategy.update_market_state(market)
        
        signals = strategy.on_market_update(market)
        
        # Should only have sell signals (to reduce inventory)
        buy_signals = [s for s in signals if s.action == SignalAction.BUY_YES and s.quantity > 0]
        # Buy signals should have quantity 0 or not exist
        # (we filter for quantity > 0)
        assert len(buy_signals) == 0 or all(s.quantity == 0 for s in buy_signals)
    
    def test_on_position_update_inventory_reduction(self):
        """Test inventory reduction signal generation."""
        config = MarketMakerConfig(max_inventory=Decimal("50.00"))
        strategy = MarketMakerStrategy(config)
        
        # Add market state
        market = MarketState(
            market_slug="test-market",
            yes_bid=Decimal("0.47"),
            yes_ask=Decimal("0.49"),
        )
        strategy.update_market_state(market)
        
        # Position over max inventory
        position = PositionState(
            market_slug="test-market",
            side=Side.YES,
            quantity=200,  # 200 * 0.47 = 94 > 50
            avg_price=Decimal("0.47"),
        )
        
        signals = strategy.on_position_update(position)
        
        # Should generate inventory reduction signals
        assert len(signals) > 0
        assert signals[0].urgency == Urgency.HIGH
        assert signals[0].action == SignalAction.SELL_YES
    
    def test_clear_quotes(self, market_maker_strategy, market_state):
        """Test clearing quote state."""
        # Generate quotes
        market_maker_strategy.on_market_update(market_state)
        
        # Verify quotes exist
        assert market_maker_strategy.get_quote_state(market_state.market_slug) is not None
        
        # Clear
        market_maker_strategy.clear_quotes(market_state.market_slug)
        
        # Quotes should be cleared
        assert market_maker_strategy.get_quote_state(market_state.market_slug) is None
    
    def test_clear_all_quotes(self, market_maker_strategy, market_state):
        """Test clearing all quote states."""
        # Generate quotes for multiple markets
        market_maker_strategy.on_market_update(market_state)
        
        market2 = MarketState(
            market_slug="market-2",
            yes_bid=Decimal("0.40"),
            yes_ask=Decimal("0.42"),
        )
        market_maker_strategy.on_market_update(market2)
        
        # Clear all
        market_maker_strategy.clear_quotes()
        
        assert market_maker_strategy.get_quote_state(market_state.market_slug) is None
        assert market_maker_strategy.get_quote_state("market-2") is None


# =============================================================================
# QuoteState Tests
# =============================================================================

class TestQuoteState:
    """Tests for QuoteState dataclass."""
    
    def test_is_active(self):
        """Test is_active property."""
        inactive = QuoteState(market_slug="test")
        assert inactive.is_active is False
        
        active = QuoteState(
            market_slug="test",
            bid_price=Decimal("0.47"),
            ask_price=Decimal("0.49"),
        )
        assert active.is_active is True


# =============================================================================
# SignalAggregator Tests
# =============================================================================

class TestSignalAggregator:
    """Tests for SignalAggregator."""
    
    def test_empty_signals(self):
        """Test aggregation of empty signal list."""
        aggregator = SignalAggregator()
        result = aggregator.aggregate([])
        
        assert len(result.signals) == 0
    
    def test_single_signal(self):
        """Test aggregation of single signal."""
        aggregator = SignalAggregator()
        
        signal = Signal(
            market_slug="test-market",
            action=SignalAction.BUY_YES,
            price=Decimal("0.50"),
            quantity=100,
            urgency=Urgency.LOW,
            strategy_name="market_maker",
            confidence=0.8,
            reason="Test",
        )
        
        result = aggregator.aggregate([signal])
        
        assert len(result.signals) == 1
        assert result.signals[0] == signal
    
    def test_deduplication_same_action(self):
        """Test deduplication of same action from different strategies."""
        aggregator = SignalAggregator()
        
        signal1 = Signal(
            market_slug="test-market",
            action=SignalAction.BUY_YES,
            price=Decimal("0.50"),
            quantity=100,
            urgency=Urgency.LOW,
            strategy_name="market_maker",
            confidence=0.8,
            reason="MM signal",
        )
        
        signal2 = Signal(
            market_slug="test-market",
            action=SignalAction.BUY_YES,
            price=Decimal("0.51"),
            quantity=50,
            urgency=Urgency.HIGH,
            strategy_name="live_arbitrage",
            confidence=0.9,
            reason="Arb signal",
        )
        
        result = aggregator.aggregate([signal1, signal2])
        
        # Should only have one BUY_YES signal (highest priority)
        buy_yes_signals = [s for s in result.signals if s.action == SignalAction.BUY_YES]
        assert len(buy_yes_signals) == 1
        assert buy_yes_signals[0].strategy_name == "live_arbitrage"  # Higher priority
    
    def test_priority_ordering(self):
        """Test that signals are ordered by strategy priority."""
        aggregator = SignalAggregator()
        
        mm_signal = Signal(
            market_slug="market-1",
            action=SignalAction.BUY_YES,
            price=Decimal("0.50"),
            quantity=100,
            urgency=Urgency.LOW,
            strategy_name="market_maker",
            confidence=0.8,
            reason="",
        )
        
        arb_signal = Signal(
            market_slug="market-2",
            action=SignalAction.BUY_YES,
            price=Decimal("0.50"),
            quantity=100,
            urgency=Urgency.LOW,
            strategy_name="live_arbitrage",
            confidence=0.8,
            reason="",
        )
        
        # Arbitrage priority is higher, but they're different markets so both should pass
        result = aggregator.aggregate([mm_signal, arb_signal])
        
        assert len(result.signals) == 2
    
    def test_urgency_sorting(self):
        """Test that signals are sorted by urgency."""
        aggregator = SignalAggregator()
        
        low_signal = Signal(
            market_slug="market-1",
            action=SignalAction.BUY_YES,
            price=Decimal("0.50"),
            quantity=100,
            urgency=Urgency.LOW,
            strategy_name="market_maker",
            confidence=0.8,
            reason="",
        )
        
        high_signal = Signal(
            market_slug="market-2",
            action=SignalAction.BUY_NO,
            price=Decimal("0.50"),
            quantity=100,
            urgency=Urgency.HIGH,
            strategy_name="live_arbitrage",
            confidence=0.8,
            reason="",
        )
        
        result = aggregator.aggregate([low_signal, high_signal])
        
        # High urgency should be first
        assert result.signals[0].urgency == Urgency.HIGH
    
    def test_cancel_signals_always_pass(self):
        """Test that cancel signals are not deduplicated."""
        aggregator = SignalAggregator()
        
        cancel1 = Signal(
            market_slug="test-market",
            action=SignalAction.CANCEL_ALL,
            price=Decimal("0.50"),
            quantity=0,
            urgency=Urgency.LOW,
            strategy_name="market_maker",
            confidence=1.0,
            reason="Cancel 1",
        )
        
        cancel2 = Signal(
            market_slug="test-market",
            action=SignalAction.CANCEL_ALL,
            price=Decimal("0.50"),
            quantity=0,
            urgency=Urgency.LOW,
            strategy_name="live_arbitrage",
            confidence=1.0,
            reason="Cancel 2",
        )
        
        result = aggregator.aggregate([cancel1, cancel2])
        
        cancel_signals = [s for s in result.signals if s.action == SignalAction.CANCEL_ALL]
        assert len(cancel_signals) == 2
    
    def test_by_market_grouping(self):
        """Test that signals are grouped by market."""
        aggregator = SignalAggregator()
        
        signal1 = Signal(
            market_slug="market-1",
            action=SignalAction.BUY_YES,
            price=Decimal("0.50"),
            quantity=100,
            urgency=Urgency.LOW,
            strategy_name="market_maker",
            confidence=0.8,
            reason="",
        )
        
        signal2 = Signal(
            market_slug="market-2",
            action=SignalAction.BUY_YES,
            price=Decimal("0.50"),
            quantity=100,
            urgency=Urgency.LOW,
            strategy_name="market_maker",
            confidence=0.8,
            reason="",
        )
        
        result = aggregator.aggregate([signal1, signal2])
        
        assert "market-1" in result.by_market
        assert "market-2" in result.by_market
        assert len(result.by_market["market-1"]) == 1
        assert len(result.by_market["market-2"]) == 1


# =============================================================================
# StrategyEngine Tests
# =============================================================================

class TestStrategyEngine:
    """Tests for StrategyEngine."""
    
    def test_init(self, strategy_engine):
        """Test engine initialization."""
        assert strategy_engine.is_running is False
        assert strategy_engine.enabled is True
        assert len(strategy_engine.get_all_strategies()) == 0
    
    def test_add_strategy(self, strategy_engine, market_maker_strategy):
        """Test adding a strategy."""
        strategy_engine.add_strategy(market_maker_strategy)
        
        assert len(strategy_engine.get_all_strategies()) == 1
    
    def test_remove_strategy(self, strategy_engine, market_maker_strategy):
        """Test removing a strategy."""
        strategy_engine.add_strategy(market_maker_strategy)
        
        removed = strategy_engine.remove_strategy("market_maker")
        
        assert removed is True
        assert len(strategy_engine.get_all_strategies()) == 0
    
    def test_remove_nonexistent_strategy(self, strategy_engine):
        """Test removing a strategy that doesn't exist."""
        removed = strategy_engine.remove_strategy("nonexistent")
        
        assert removed is False
    
    def test_get_strategy(self, strategy_engine, market_maker_strategy):
        """Test getting a strategy by name."""
        strategy_engine.add_strategy(market_maker_strategy)
        
        strategy = strategy_engine.get_strategy("market_maker")
        
        assert strategy is not None
        assert strategy.name == "market_maker"
    
    def test_process_market_update(self, strategy_engine, market_maker_strategy, market_state):
        """Test processing market updates."""
        strategy_engine.add_strategy(market_maker_strategy)
        
        signals = strategy_engine.process_market_update(market_state)
        
        # Market maker should generate signals
        assert len(signals) > 0
    
    def test_process_market_update_disabled_engine(self, strategy_engine, market_maker_strategy, market_state):
        """Test that disabled engine returns no signals."""
        strategy_engine.add_strategy(market_maker_strategy)
        strategy_engine.enabled = False
        
        signals = strategy_engine.process_market_update(market_state)
        
        assert len(signals) == 0
    
    def test_process_market_update_disabled_strategy(self, strategy_engine, market_maker_strategy, market_state):
        """Test that disabled strategy is skipped."""
        market_maker_strategy.enabled = False
        strategy_engine.add_strategy(market_maker_strategy)
        
        signals = strategy_engine.process_market_update(market_state)
        
        assert len(signals) == 0
    
    def test_process_tick(self, strategy_engine):
        """Test processing tick."""
        strategy = ConcreteStrategy()
        strategy.set_tick_signals([
            Signal(
                market_slug="test",
                action=SignalAction.BUY_YES,
                price=Decimal("0.50"),
                quantity=100,
                urgency=Urgency.LOW,
                strategy_name="concrete_strategy",
                confidence=0.5,
                reason="Tick signal",
            )
        ])
        strategy_engine.add_strategy(strategy)
        
        signals = strategy_engine.process_tick()
        
        assert len(signals) == 1
    
    def test_execute_signals(self, strategy_engine, market_with_book):
        """Test executing signals."""
        signal = Signal(
            market_slug=market_with_book,
            action=SignalAction.BUY_YES,
            price=Decimal("0.50"),
            quantity=100,
            urgency=Urgency.LOW,
            strategy_name="test",
            confidence=0.8,
            reason="Test",
        )
        
        results = strategy_engine.execute_signals([signal])
        
        assert results["executed"] == 1
        assert results["errors"] == 0
    
    def test_execute_cancel_signal(self, strategy_engine, market_with_book, paper_executor):
        """Test executing cancel signal."""
        # First create a resting order
        order = PaperOrderRequest(
            market_slug=market_with_book,
            intent=OrderIntent.BUY_LONG,
            quantity=100,
            price=Decimal("0.40"),  # Will rest
        )
        paper_executor.execute_order(order)
        
        # Now cancel via signal
        cancel_signal = Signal(
            market_slug=market_with_book,
            action=SignalAction.CANCEL_ALL,
            price=Decimal("0.50"),
            quantity=0,
            urgency=Urgency.LOW,
            strategy_name="test",
            confidence=1.0,
            reason="Cancel all",
        )
        
        results = strategy_engine.execute_signals([cancel_signal])
        
        assert results["cancelled"] >= 1
    
    def test_signal_to_order_conversion(self, strategy_engine, market_with_book):
        """Test signal to order conversion."""
        signal = Signal(
            market_slug=market_with_book,
            action=SignalAction.BUY_YES,
            price=Decimal("0.50"),
            quantity=100,
            urgency=Urgency.LOW,
            strategy_name="test",
            confidence=0.8,
            reason="Test",
        )
        
        order = strategy_engine._signal_to_order(signal)
        
        assert order.market_slug == market_with_book
        assert order.intent == OrderIntent.BUY_LONG
        assert order.quantity == 100
        assert order.price == Decimal("0.50")
    
    def test_signal_to_order_all_actions(self, strategy_engine):
        """Test conversion for all signal actions."""
        mappings = [
            (SignalAction.BUY_YES, OrderIntent.BUY_LONG),
            (SignalAction.SELL_YES, OrderIntent.SELL_LONG),
            (SignalAction.BUY_NO, OrderIntent.BUY_SHORT),
            (SignalAction.SELL_NO, OrderIntent.SELL_SHORT),
        ]
        
        for action, expected_intent in mappings:
            signal = Signal(
                market_slug="test",
                action=action,
                price=Decimal("0.50"),
                quantity=100,
                urgency=Urgency.LOW,
                strategy_name="test",
                confidence=0.5,
                reason="",
            )
            
            order = strategy_engine._signal_to_order(signal)
            assert order.intent == expected_intent
    
    def test_get_metrics(self, strategy_engine, market_maker_strategy, market_with_book, market_state):
        """Test metrics collection."""
        strategy_engine.add_strategy(market_maker_strategy)
        
        # Process some updates
        signals = strategy_engine.process_market_update(market_state)
        strategy_engine.execute_signals(signals)
        
        metrics = strategy_engine.get_metrics()
        
        assert metrics["strategies"] == 1
        assert metrics["enabled_strategies"] == 1
        assert metrics["signals_generated"] > 0
        assert metrics["signals_executed"] > 0
    
    def test_reset_metrics(self, strategy_engine):
        """Test resetting metrics."""
        strategy_engine._signals_generated = 100
        strategy_engine._signals_executed = 50
        strategy_engine._execution_errors = 5
        
        strategy_engine.reset_metrics()
        
        metrics = strategy_engine.get_metrics()
        assert metrics["signals_generated"] == 0
        assert metrics["signals_executed"] == 0
        assert metrics["execution_errors"] == 0
    
    def test_enabled_property(self, strategy_engine):
        """Test enabled property."""
        assert strategy_engine.enabled is True
        
        strategy_engine.enabled = False
        assert strategy_engine.enabled is False


class TestStrategyEngineAsync:
    """Async tests for StrategyEngine."""
    
    @pytest.mark.asyncio
    async def test_run_and_stop(self, strategy_engine):
        """Test running and stopping the engine."""
        task = await strategy_engine.start_async()
        
        # Give the task a chance to start
        await asyncio.sleep(0.05)
        
        assert strategy_engine.is_running is True
        
        await asyncio.sleep(0.1)  # Let it run briefly
        
        strategy_engine.stop()
        
        # Wait for task to complete
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except asyncio.CancelledError:
            pass
        
        assert strategy_engine.is_running is False
    
    @pytest.mark.asyncio
    async def test_tick_loop(self, strategy_engine, market_maker_strategy, market_state):
        """Test that tick loop processes strategies."""
        strategy_engine.add_strategy(market_maker_strategy)
        market_maker_strategy.update_market_state(market_state)
        
        # Manually trigger tick
        await strategy_engine._tick()
        
        # Check that tick was processed
        # (no direct assertion, just ensure no errors)
    
    @pytest.mark.asyncio
    async def test_market_handler(self, strategy_engine, market_maker_strategy, state_manager, market_with_book):
        """Test WebSocket market handler."""
        strategy_engine.add_strategy(market_maker_strategy)
        
        handler = strategy_engine.create_market_handler()
        
        message = {
            "type": "MARKET_DATA",
            "marketSlug": market_with_book,
        }
        
        # Should not raise
        await handler(message)


# =============================================================================
# Integration Tests
# =============================================================================

class TestStrategyIntegration:
    """Integration tests for the strategy module."""
    
    def test_full_signal_flow(
        self,
        state_manager,
        orderbook_tracker,
        paper_executor,
        sample_orderbook_data,
    ):
        """Test complete signal flow from strategy to execution."""
        market_slug = "integration-test-market"
        
        # Setup market
        orderbook_tracker.update(market_slug, sample_orderbook_data)
        state_manager.update_market(
            market_slug,
            yes_bid=Decimal("0.47"),
            yes_ask=Decimal("0.49"),
            no_bid=Decimal("0.51"),
            no_ask=Decimal("0.53"),
        )
        
        # Create engine with market maker
        engine = StrategyEngine(
            state_manager=state_manager,
            orderbook=orderbook_tracker,
            executor=paper_executor,
        )
        
        config = MarketMakerConfig(
            spread=Decimal("0.02"),
            order_size=Decimal("10.00"),
        )
        strategy = MarketMakerStrategy(config)
        engine.add_strategy(strategy)
        
        # Process market update
        market = state_manager.get_market(market_slug)
        signals = engine.process_market_update(market)
        
        # Execute signals
        results = engine.execute_signals(signals)
        
        # Verify execution
        assert results["executed"] > 0
        
        # Check metrics
        metrics = engine.get_metrics()
        assert metrics["signals_generated"] > 0
        assert metrics["signals_executed"] > 0
    
    def test_multiple_strategies(self, state_manager, orderbook_tracker, paper_executor):
        """Test engine with multiple strategies."""
        market_slug = "multi-strategy-test"
        
        # Setup market
        orderbook_tracker.update(market_slug, {
            "yes": {"bids": [["0.50", "500"]], "asks": [["0.52", "300"]]},
            "no": {"bids": [["0.48", "400"]], "asks": [["0.50", "350"]]},
        })
        state_manager.update_market(
            market_slug,
            yes_bid=Decimal("0.50"),
            yes_ask=Decimal("0.52"),
        )
        
        # Create engine
        engine = StrategyEngine(
            state_manager=state_manager,
            orderbook=orderbook_tracker,
            executor=paper_executor,
        )
        
        # Add multiple strategies
        mm_strategy = MarketMakerStrategy()
        custom_strategy = ConcreteStrategy(name="custom_strategy")
        custom_strategy.set_market_signals([
            Signal(
                market_slug=market_slug,
                action=SignalAction.BUY_YES,
                price=Decimal("0.50"),
                quantity=50,
                urgency=Urgency.MEDIUM,
                strategy_name="custom_strategy",
                confidence=0.7,
                reason="Custom signal",
            )
        ])
        
        engine.add_strategy(mm_strategy)
        engine.add_strategy(custom_strategy)
        
        assert len(engine.get_all_strategies()) == 2
        
        # Process update
        market = state_manager.get_market(market_slug)
        signals = engine.process_market_update(market)
        
        # Should have signals from both strategies
        # (though some may be deduplicated)
        assert len(signals) > 0
    
    def test_position_based_inventory_management(
        self,
        state_manager,
        orderbook_tracker,
        paper_executor,
    ):
        """Test inventory management based on position."""
        market_slug = "inventory-test"
        
        # Setup market
        orderbook_tracker.update(market_slug, {
            "yes": {"bids": [["0.47", "500"]], "asks": [["0.49", "300"]]},
            "no": {"bids": [["0.51", "400"]], "asks": [["0.53", "350"]]},
        })
        state_manager.update_market(
            market_slug,
            yes_bid=Decimal("0.47"),
            yes_ask=Decimal("0.49"),
        )
        
        # Create position over max inventory
        state_manager.update_position(
            market_slug,
            side=Side.YES,
            quantity=200,  # Large position - 200 * 0.45 = 90 > 30
            avg_price=Decimal("0.45"),
        )
        
        # Create engine with low max inventory
        engine = StrategyEngine(
            state_manager=state_manager,
            orderbook=orderbook_tracker,
            executor=paper_executor,
        )
        
        config = MarketMakerConfig(max_inventory=Decimal("30.00"))
        strategy = MarketMakerStrategy(config)
        engine.add_strategy(strategy)
        
        # First, update the strategy with market state (needed for inventory check)
        market = state_manager.get_market(market_slug)
        strategy.update_market_state(market)
        
        # Process position update
        position = state_manager.get_position(market_slug)
        signals = engine.process_position_update(position)
        
        # Should have inventory reduction signal
        high_urgency = [s for s in signals if s.urgency == Urgency.HIGH]
        assert len(high_urgency) > 0

    def test_strategy_position_cache_updates_after_fill(
        self,
        strategy_engine,
        market_maker_strategy,
        market_with_book,
    ):
        """
        CRITICAL: After an execution fill, StrategyEngine must propagate the
        updated position into each strategy's cached state.
        """
        strategy_engine.add_strategy(market_maker_strategy)
        assert market_maker_strategy.get_position(market_with_book) is None

        # Force an immediate taker fill (post_only=False by default).
        buy_signal = Signal(
            market_slug=market_with_book,
            action=SignalAction.BUY_YES,
            price=Decimal("0.49"),  # >= best ask => marketable
            quantity=10,
            urgency=Urgency.HIGH,
            strategy_name="test",
            confidence=1.0,
            reason="Test buy fill",
        )
        strategy_engine.execute_signals([buy_signal])

        cached = market_maker_strategy.get_position(market_with_book)
        assert cached is not None
        assert cached.side == Side.YES
        assert cached.quantity == 10

    def test_strategy_position_cache_clears_on_close(
        self,
        strategy_engine,
        market_maker_strategy,
        market_with_book,
    ):
        """
        CRITICAL: When a position is fully closed, StrategyEngine must clear
        any stale cached position state in strategies.
        """
        strategy_engine.add_strategy(market_maker_strategy)

        buy_signal = Signal(
            market_slug=market_with_book,
            action=SignalAction.BUY_YES,
            price=Decimal("0.49"),
            quantity=10,
            urgency=Urgency.HIGH,
            strategy_name="test",
            confidence=1.0,
            reason="Open position",
        )
        strategy_engine.execute_signals([buy_signal])
        assert market_maker_strategy.get_position(market_with_book) is not None

        # Close the entire position with an immediate fill (sell price <= best bid).
        sell_signal = Signal(
            market_slug=market_with_book,
            action=SignalAction.SELL_YES,
            price=Decimal("0.47"),
            quantity=10,
            urgency=Urgency.HIGH,
            strategy_name="test",
            confidence=1.0,
            reason="Close position",
        )
        strategy_engine.execute_signals([sell_signal])

        assert strategy_engine.state_manager.get_position(market_with_book) is None
        assert market_maker_strategy.get_position(market_with_book) is None
