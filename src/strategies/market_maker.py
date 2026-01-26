"""
Market making strategy for Polymarket US trading bot.

This module implements a two-sided market making strategy that provides
liquidity by posting bid and ask orders around the mid-price.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

import structlog
from pydantic import BaseModel, Field

from ..state.state_manager import MarketState, PositionState
from .base_strategy import BaseStrategy, Signal, SignalAction, Urgency

logger = structlog.get_logger()


# =============================================================================
# Configuration
# =============================================================================

class MarketMakerConfig(BaseModel):
    """
    Configuration for market making strategy.
    
    Attributes:
        spread: Bid-ask spread to maintain (e.g., 0.02 = 2 cents)
        order_size: USD amount per order side
        max_inventory: Maximum position value per market
        refresh_interval: Seconds between quote refreshes
        min_spread: Minimum spread to maintain (won't quote tighter)
        max_spread: Maximum spread (won't quote wider)
        price_tolerance: Price movement threshold to trigger refresh
        enabled_markets: List of market patterns to trade (empty = all)
        inventory_skew_factor: How much to skew quotes based on inventory
    """
    spread: Decimal = Field(default=Decimal("0.02"), ge=Decimal("0.01"), le=Decimal("0.20"))
    order_size: Decimal = Field(default=Decimal("10.00"), ge=Decimal("1.00"))
    max_inventory: Decimal = Field(default=Decimal("50.00"), ge=Decimal("0"))
    refresh_interval: float = Field(default=5.0, ge=1.0, le=60.0)
    min_spread: Decimal = Field(default=Decimal("0.01"))
    max_spread: Decimal = Field(default=Decimal("0.10"))
    price_tolerance: Decimal = Field(default=Decimal("0.005"))
    enabled_markets: List[str] = Field(default_factory=list)
    inventory_skew_factor: Decimal = Field(default=Decimal("0.5"))
    
    class Config:
        """Pydantic config."""
        frozen = True


# =============================================================================
# Quote State
# =============================================================================

@dataclass
class QuoteState:
    """
    Tracks the current quote state for a market.
    
    Attributes:
        market_slug: Market identifier
        bid_price: Current bid price we're quoting
        ask_price: Current ask price we're quoting
        bid_quantity: Current bid quantity
        ask_quantity: Current ask quantity
        last_refresh: When quotes were last updated
        last_mid_price: Mid-price at last refresh
    """
    market_slug: str
    bid_price: Optional[Decimal] = None
    ask_price: Optional[Decimal] = None
    bid_quantity: int = 0
    ask_quantity: int = 0
    last_refresh: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_mid_price: Optional[Decimal] = None
    
    @property
    def is_active(self) -> bool:
        """Check if we have active quotes."""
        return self.bid_price is not None or self.ask_price is not None


# =============================================================================
# Market Maker Strategy
# =============================================================================

class MarketMakerStrategy(BaseStrategy):
    """
    Two-sided market making strategy.
    
    Posts limit orders on both YES and NO sides, capturing the spread
    when both sides fill. Includes inventory management to stay balanced.
    
    Features:
    - Configurable spread and order size
    - Automatic quote refreshing based on time and price movement
    - Inventory management to reduce directional exposure
    - Skews quotes based on current position
    
    Example:
        >>> config = MarketMakerConfig(spread=Decimal("0.02"), order_size=Decimal("10"))
        >>> strategy = MarketMakerStrategy(config)
        >>> 
        >>> # Generate signals on market update
        >>> signals = strategy.on_market_update(market_state)
    """
    
    def __init__(
        self,
        config: Optional[MarketMakerConfig] = None,
        enabled: bool = True,
    ):
        """
        Initialize market maker strategy.
        
        Args:
            config: Strategy configuration
            enabled: Whether strategy is active
        """
        super().__init__(enabled=enabled)
        
        self.config = config or MarketMakerConfig()
        self._quotes: Dict[str, QuoteState] = {}
        
        logger.info(
            "MarketMakerStrategy initialized",
            spread=float(self.config.spread),
            order_size=float(self.config.order_size),
            max_inventory=float(self.config.max_inventory),
            refresh_interval=self.config.refresh_interval,
        )
    
    # =========================================================================
    # BaseStrategy Implementation
    # =========================================================================
    
    @property
    def name(self) -> str:
        """Get strategy name."""
        return "market_maker"
    
    def on_market_update(self, market: MarketState) -> List[Signal]:
        """
        Generate market making signals on market update.
        
        Args:
            market: Current market state
            
        Returns:
            List of signals (cancel + new quotes if refresh needed)
        """
        if not self.enabled:
            return []
        
        # Check if market is enabled for trading
        if not self._is_market_enabled(market.market_slug):
            return []
        
        # Update cached market state
        self.update_market_state(market)
        
        # Check if we have valid prices
        if not self._has_valid_prices(market):
            logger.debug(
                "Market missing prices, skipping",
                market_slug=market.market_slug,
            )
            return []
        
        # Get or create quote state
        quote_state = self._get_quote_state(market.market_slug)
        
        # Check if we need to refresh quotes
        if self._should_refresh_quotes(market, quote_state):
            return self._generate_quote_signals(market, quote_state)
        
        return []
    
    def on_tick(self) -> List[Signal]:
        """
        Check for time-based quote refreshes.
        
        Returns:
            List of signals for markets needing refresh
        """
        if not self.enabled:
            return []
        
        signals = []
        now = datetime.now(timezone.utc)
        
        for market_slug, quote_state in list(self._quotes.items()):
            # Check if refresh interval has elapsed
            elapsed = (now - quote_state.last_refresh).total_seconds()
            
            if elapsed >= self.config.refresh_interval:
                market = self.get_market(market_slug)
                if market and self._has_valid_prices(market):
                    signals.extend(self._generate_quote_signals(market, quote_state))
        
        return signals
    
    def on_position_update(self, position: PositionState) -> List[Signal]:
        """
        React to position changes for inventory management.
        
        Args:
            position: Updated position state
            
        Returns:
            Inventory reduction signals if needed
        """
        if not self.enabled:
            return []
        
        # Update cached position
        self.update_position_state(position)
        
        # Check if we need to reduce inventory
        return self._check_inventory(position)
    
    # =========================================================================
    # Quote Calculation
    # =========================================================================
    
    def calculate_quotes(
        self,
        market: MarketState,
        position: Optional[PositionState] = None,
    ) -> Tuple[Decimal, Decimal]:
        """
        Calculate bid and ask prices based on mid-price and spread.
        
        Args:
            market: Current market state
            position: Optional current position for skewing
            
        Returns:
            Tuple of (bid_price, ask_price)
        """
        # Calculate mid-price from YES side
        mid_price = market.yes_mid_price
        
        if mid_price is None:
            # Fallback: use average of bid and ask if available
            if market.yes_bid is not None and market.yes_ask is not None:
                mid_price = (market.yes_bid + market.yes_ask) / 2
            else:
                # Cannot calculate quotes without prices
                raise ValueError("Cannot calculate mid-price")
        
        # Base spread
        half_spread = self.config.spread / 2
        
        # Calculate inventory skew if we have a position
        skew = Decimal("0")
        if position is not None and position.quantity > 0:
            # Calculate position value
            position_value = position.avg_price * position.quantity
            
            # Skew based on inventory level relative to max
            if self.config.max_inventory > 0:
                inventory_ratio = position_value / self.config.max_inventory
                skew = inventory_ratio * self.config.inventory_skew_factor * half_spread
                
                # If long YES, skew to sell YES (lower ask) and discourage more buying (lower bid)
                if position.side.value == "YES":
                    skew = -skew  # Move both prices down
                # If long NO, skew to sell NO (need to adjust opposite)
        
        # Calculate prices with skew
        our_bid = mid_price - half_spread + skew
        our_ask = mid_price + half_spread + skew
        
        # Clamp to valid range
        our_bid = self.clamp_price(our_bid)
        our_ask = self.clamp_price(our_ask)
        
        # Ensure bid < ask
        if our_bid >= our_ask:
            # Widen spread to maintain order
            our_bid = self.clamp_price(mid_price - half_spread)
            our_ask = self.clamp_price(mid_price + half_spread)
        
        return (our_bid, our_ask)
    
    def calculate_quantity(self, price: Decimal) -> int:
        """
        Calculate order quantity based on order size and price.
        
        Args:
            price: Order price
            
        Returns:
            Number of contracts to order
        """
        if price <= 0:
            return 0
        
        # Convert USD order size to contracts
        # quantity = order_size / price (rounded down)
        quantity = int(self.config.order_size / price)
        
        return max(1, quantity)  # At least 1 contract
    
    # =========================================================================
    # Quote Refresh Logic
    # =========================================================================
    
    def _should_refresh_quotes(
        self,
        market: MarketState,
        quote_state: QuoteState,
    ) -> bool:
        """
        Determine if quotes need to be refreshed.
        
        Refresh triggers:
        1. No active quotes
        2. Time interval elapsed
        3. Mid-price moved beyond tolerance
        
        Args:
            market: Current market state
            quote_state: Current quote state
            
        Returns:
            True if quotes should be refreshed
        """
        # No active quotes
        if not quote_state.is_active:
            return True
        
        # Time-based refresh
        now = datetime.now(timezone.utc)
        elapsed = (now - quote_state.last_refresh).total_seconds()
        if elapsed >= self.config.refresh_interval:
            return True
        
        # Price-based refresh
        current_mid = market.yes_mid_price
        if current_mid is not None and quote_state.last_mid_price is not None:
            price_change = abs(current_mid - quote_state.last_mid_price)
            if price_change >= self.config.price_tolerance:
                logger.debug(
                    "Price moved beyond tolerance",
                    market_slug=market.market_slug,
                    old_mid=float(quote_state.last_mid_price),
                    new_mid=float(current_mid),
                    change=float(price_change),
                )
                return True
        
        return False
    
    def _generate_quote_signals(
        self,
        market: MarketState,
        quote_state: QuoteState,
    ) -> List[Signal]:
        """
        Generate signals to refresh quotes.
        
        Args:
            market: Current market state
            quote_state: Current quote state
            
        Returns:
            List of signals (cancel + new orders)
        """
        signals = []
        
        try:
            # Get current position for skewing
            position = self.get_position(market.market_slug)
            
            # Calculate new quotes
            bid_price, ask_price = self.calculate_quotes(market, position)
            bid_quantity = self.calculate_quantity(bid_price)
            ask_quantity = self.calculate_quantity(ask_price)
            
            # Check inventory limits before quoting
            if position is not None:
                position_value = position.avg_price * position.quantity
                if position_value >= self.config.max_inventory:
                    # At max inventory, only quote to reduce position
                    if position.side.value == "YES":
                        # Only quote ask to sell YES
                        bid_quantity = 0
                    else:
                        # Only quote bid to buy YES (which closes NO)
                        ask_quantity = 0
            
            # Cancel existing orders first
            if quote_state.is_active:
                signals.append(self.create_cancel_signal(
                    market_slug=market.market_slug,
                    reason="Refreshing market maker quotes",
                ))
            
            # Post new bid (buy YES)
            if bid_quantity > 0:
                signals.append(self.create_signal(
                    market_slug=market.market_slug,
                    action=SignalAction.BUY_YES,
                    price=bid_price,
                    quantity=bid_quantity,
                    urgency=Urgency.LOW,
                    confidence=0.8,
                    reason=f"Market making bid at {bid_price:.4f}",
                    metadata={
                        "mid_price": float(market.yes_mid_price) if market.yes_mid_price else None,
                        "spread": float(self.config.spread),
                    },
                ))
            
            # Post new ask (sell YES)
            if ask_quantity > 0:
                signals.append(self.create_signal(
                    market_slug=market.market_slug,
                    action=SignalAction.SELL_YES,
                    price=ask_price,
                    quantity=ask_quantity,
                    urgency=Urgency.LOW,
                    confidence=0.8,
                    reason=f"Market making ask at {ask_price:.4f}",
                    metadata={
                        "mid_price": float(market.yes_mid_price) if market.yes_mid_price else None,
                        "spread": float(self.config.spread),
                    },
                ))
            
            # Update quote state
            self._update_quote_state(
                market_slug=market.market_slug,
                bid_price=bid_price if bid_quantity > 0 else None,
                ask_price=ask_price if ask_quantity > 0 else None,
                bid_quantity=bid_quantity,
                ask_quantity=ask_quantity,
                mid_price=market.yes_mid_price,
            )
            
            logger.debug(
                "Generated market maker quotes",
                market_slug=market.market_slug,
                bid_price=float(bid_price),
                ask_price=float(ask_price),
                bid_quantity=bid_quantity,
                ask_quantity=ask_quantity,
            )
            
        except ValueError as e:
            logger.warning(
                "Cannot generate quotes",
                market_slug=market.market_slug,
                error=str(e),
            )
        
        return signals
    
    # =========================================================================
    # Inventory Management
    # =========================================================================
    
    def _check_inventory(self, position: PositionState) -> List[Signal]:
        """
        Check if inventory needs to be reduced.
        
        Args:
            position: Current position
            
        Returns:
            Inventory reduction signals if needed
        """
        signals = []
        
        # Calculate position value
        position_value = position.avg_price * position.quantity
        
        # Check if over max inventory
        if position_value > self.config.max_inventory:
            excess = position_value - self.config.max_inventory
            
            # Get current market for price
            market = self.get_market(position.market_slug)
            if not market:
                return signals
            
            # Calculate reduction quantity
            # Reduce by half the excess (gradual reduction)
            reduce_value = excess / 2
            
            if position.side.value == "YES":
                # Sell YES to reduce long YES position
                price = market.yes_bid
                if price is not None and price > 0:
                    reduce_qty = int(reduce_value / price)
                    reduce_qty = min(reduce_qty, position.quantity // 2)  # Max half position
                    
                    if reduce_qty > 0:
                        signals.append(self.create_signal(
                            market_slug=position.market_slug,
                            action=SignalAction.SELL_YES,
                            price=self.clamp_price(price * Decimal("0.98")),  # 2% discount for urgency
                            quantity=reduce_qty,
                            urgency=Urgency.HIGH,
                            confidence=0.9,
                            reason=f"Inventory reduction: excess=${float(excess):.2f}",
                        ))
            else:
                # Buy YES to close NO position
                price = market.yes_ask
                if price is not None and price > 0:
                    reduce_qty = int(reduce_value / price)
                    reduce_qty = min(reduce_qty, position.quantity // 2)
                    
                    if reduce_qty > 0:
                        signals.append(self.create_signal(
                            market_slug=position.market_slug,
                            action=SignalAction.BUY_YES,
                            price=self.clamp_price(price * Decimal("1.02")),  # 2% premium for urgency
                            quantity=reduce_qty,
                            urgency=Urgency.HIGH,
                            confidence=0.9,
                            reason=f"Inventory reduction: excess=${float(excess):.2f}",
                        ))
            
            if signals:
                logger.info(
                    "Inventory reduction triggered",
                    market_slug=position.market_slug,
                    position_value=float(position_value),
                    max_inventory=float(self.config.max_inventory),
                    excess=float(excess),
                )
        
        return signals
    
    # =========================================================================
    # Helper Methods
    # =========================================================================
    
    def _is_market_enabled(self, market_slug: str) -> bool:
        """
        Check if market is enabled for trading.
        
        Args:
            market_slug: Market identifier
            
        Returns:
            True if market should be traded
        """
        if not self.config.enabled_markets:
            return True  # Trade all markets if no filter
        
        # Check for pattern matches
        for pattern in self.config.enabled_markets:
            if pattern.endswith("*"):
                if market_slug.startswith(pattern[:-1]):
                    return True
            elif pattern == market_slug:
                return True
        
        return False
    
    def _has_valid_prices(self, market: MarketState) -> bool:
        """
        Check if market has valid bid/ask prices.
        
        Args:
            market: Market state to check
            
        Returns:
            True if market has valid prices for quoting
        """
        return (
            market.yes_bid is not None
            and market.yes_ask is not None
            and market.yes_bid > 0
            and market.yes_ask > 0
            and market.yes_bid < market.yes_ask
        )
    
    def _get_quote_state(self, market_slug: str) -> QuoteState:
        """
        Get or create quote state for a market.
        
        Args:
            market_slug: Market identifier
            
        Returns:
            QuoteState for the market
        """
        if market_slug not in self._quotes:
            self._quotes[market_slug] = QuoteState(market_slug=market_slug)
        return self._quotes[market_slug]
    
    def _update_quote_state(
        self,
        market_slug: str,
        bid_price: Optional[Decimal],
        ask_price: Optional[Decimal],
        bid_quantity: int,
        ask_quantity: int,
        mid_price: Optional[Decimal],
    ) -> None:
        """
        Update quote state after generating signals.
        
        Args:
            market_slug: Market identifier
            bid_price: New bid price
            ask_price: New ask price
            bid_quantity: New bid quantity
            ask_quantity: New ask quantity
            mid_price: Current mid-price
        """
        quote_state = self._get_quote_state(market_slug)
        quote_state.bid_price = bid_price
        quote_state.ask_price = ask_price
        quote_state.bid_quantity = bid_quantity
        quote_state.ask_quantity = ask_quantity
        quote_state.last_refresh = datetime.now(timezone.utc)
        quote_state.last_mid_price = mid_price
    
    def clear_quotes(self, market_slug: Optional[str] = None) -> None:
        """
        Clear quote state.
        
        Args:
            market_slug: Specific market to clear (all if None)
        """
        if market_slug:
            self._quotes.pop(market_slug, None)
        else:
            self._quotes.clear()
    
    def get_quote_state(self, market_slug: str) -> Optional[QuoteState]:
        """
        Get current quote state for a market (public accessor).
        
        Args:
            market_slug: Market identifier
            
        Returns:
            QuoteState if exists, None otherwise
        """
        return self._quotes.get(market_slug)
