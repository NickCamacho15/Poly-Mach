"""
Centralized state manager for Polymarket US trading bot.

This module provides a thread-safe state container for tracking markets,
positions, orders, and account balance. It integrates with WebSocket handlers
for real-time updates.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from threading import Lock
from typing import Any, Callable, Coroutine, Dict, List, Optional

import structlog

from ..data.models import OrderIntent, OrderStatus, Side

logger = structlog.get_logger()


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class MarketState:
    """
    Current state of a market's prices.
    
    Attributes:
        market_slug: Market identifier
        yes_bid: Best bid price for YES side
        yes_ask: Best ask price for YES side
        no_bid: Best bid price for NO side
        no_ask: Best ask price for NO side
        yes_bid_size: Quantity at best YES bid
        yes_ask_size: Quantity at best YES ask
        last_trade_price: Price of last trade (if available)
        last_trade_time: Timestamp of last trade
        last_update: Timestamp of last state update
    """
    market_slug: str
    yes_bid: Optional[Decimal] = None
    yes_ask: Optional[Decimal] = None
    no_bid: Optional[Decimal] = None
    no_ask: Optional[Decimal] = None
    yes_bid_size: int = 0
    yes_ask_size: int = 0
    last_trade_price: Optional[Decimal] = None
    last_trade_time: Optional[datetime] = None
    last_update: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    @property
    def yes_mid_price(self) -> Optional[Decimal]:
        """Calculate mid-price for YES side."""
        if self.yes_bid is not None and self.yes_ask is not None:
            return (self.yes_bid + self.yes_ask) / 2
        return None
    
    @property
    def no_mid_price(self) -> Optional[Decimal]:
        """Calculate mid-price for NO side."""
        if self.no_bid is not None and self.no_ask is not None:
            return (self.no_bid + self.no_ask) / 2
        return None
    
    @property
    def yes_spread(self) -> Optional[Decimal]:
        """Calculate bid-ask spread for YES side."""
        if self.yes_bid is not None and self.yes_ask is not None:
            return self.yes_ask - self.yes_bid
        return None
    
    @property
    def no_spread(self) -> Optional[Decimal]:
        """Calculate bid-ask spread for NO side."""
        if self.no_bid is not None and self.no_ask is not None:
            return self.no_ask - self.no_bid
        return None


@dataclass
class PositionState:
    """
    Current position in a market.
    
    Attributes:
        market_slug: Market identifier
        side: Position side (YES or NO)
        quantity: Number of contracts held
        avg_price: Average entry price
        realized_pnl: Realized profit/loss from closed portions
        unrealized_pnl: Current unrealized P&L (updated by mark-to-market)
        created_at: When position was opened
        updated_at: Last update timestamp
    """
    market_slug: str
    side: Side
    quantity: int
    avg_price: Decimal
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    @property
    def cost_basis(self) -> Decimal:
        """Calculate total cost basis."""
        return self.avg_price * self.quantity
    
    @property
    def is_long(self) -> bool:
        """Check if position is long (YES side)."""
        return self.side == Side.YES


@dataclass
class OrderState:
    """
    Current state of an order.
    
    Attributes:
        order_id: Unique order identifier
        market_slug: Market identifier
        intent: Order intent (BUY_LONG, SELL_LONG, etc.)
        price: Limit price
        quantity: Total order quantity
        filled_quantity: Quantity already filled
        status: Current order status
        created_at: Order creation timestamp
        updated_at: Last update timestamp
    """
    order_id: str
    market_slug: str
    intent: OrderIntent
    price: Decimal
    quantity: int
    filled_quantity: int = 0
    status: OrderStatus = OrderStatus.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    @property
    def remaining_quantity(self) -> int:
        """Get remaining unfilled quantity."""
        return self.quantity - self.filled_quantity
    
    @property
    def is_open(self) -> bool:
        """Check if order is still open."""
        return self.status in (
            OrderStatus.PENDING,
            OrderStatus.OPEN,
            OrderStatus.PARTIALLY_FILLED,
        )
    
    @property
    def is_buy(self) -> bool:
        """Check if order is a buy order."""
        return self.intent in (OrderIntent.BUY_LONG, OrderIntent.BUY_SHORT)
    
    @property
    def side(self) -> Side:
        """Get the side (YES or NO) this order affects."""
        if self.intent in (OrderIntent.BUY_LONG, OrderIntent.SELL_LONG):
            return Side.YES
        return Side.NO


# =============================================================================
# Type Aliases
# =============================================================================

MessageHandler = Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]


# =============================================================================
# State Manager
# =============================================================================

class StateManager:
    """
    Thread-safe centralized state manager for the trading bot.
    
    Manages:
    - Market price states
    - Position tracking
    - Order tracking
    - Account balance
    
    Example:
        >>> state = StateManager(initial_balance=Decimal("1000"))
        >>> 
        >>> # Update market state
        >>> state.update_market(
        ...     "nba-lakers-vs-celtics",
        ...     yes_bid=Decimal("0.47"),
        ...     yes_ask=Decimal("0.49"),
        ... )
        >>> 
        >>> # Get market state
        >>> market = state.get_market("nba-lakers-vs-celtics")
        >>> print(market.yes_mid_price)  # Decimal("0.48")
    """
    
    def __init__(self, initial_balance: Decimal = Decimal("0")):
        """
        Initialize state manager.
        
        Args:
            initial_balance: Starting account balance
        """
        self._markets: Dict[str, MarketState] = {}
        self._positions: Dict[str, PositionState] = {}
        self._orders: Dict[str, OrderState] = {}
        self._balance: Decimal = initial_balance
        
        # Thread safety
        self._lock = Lock()
        self._async_lock = asyncio.Lock()
        
        logger.info(
            "StateManager initialized",
            initial_balance=float(initial_balance),
        )
    
    # =========================================================================
    # Market State Management
    # =========================================================================
    
    def update_market(
        self,
        market_slug: str,
        yes_bid: Optional[Decimal] = None,
        yes_ask: Optional[Decimal] = None,
        no_bid: Optional[Decimal] = None,
        no_ask: Optional[Decimal] = None,
        yes_bid_size: Optional[int] = None,
        yes_ask_size: Optional[int] = None,
        last_trade_price: Optional[Decimal] = None,
        last_trade_time: Optional[datetime] = None,
    ) -> None:
        """
        Update market state.
        
        Only non-None values are updated. Creates market if it doesn't exist.
        
        Args:
            market_slug: Market identifier
            yes_bid: Best bid price for YES side
            yes_ask: Best ask price for YES side
            no_bid: Best bid price for NO side
            no_ask: Best ask price for NO side
            yes_bid_size: Quantity at best YES bid
            yes_ask_size: Quantity at best YES ask
            last_trade_price: Price of last trade
            last_trade_time: Time of last trade
        """
        with self._lock:
            if market_slug not in self._markets:
                self._markets[market_slug] = MarketState(market_slug=market_slug)
            
            market = self._markets[market_slug]
            
            if yes_bid is not None:
                market.yes_bid = yes_bid
            if yes_ask is not None:
                market.yes_ask = yes_ask
            if no_bid is not None:
                market.no_bid = no_bid
            if no_ask is not None:
                market.no_ask = no_ask
            if yes_bid_size is not None:
                market.yes_bid_size = yes_bid_size
            if yes_ask_size is not None:
                market.yes_ask_size = yes_ask_size
            if last_trade_price is not None:
                market.last_trade_price = last_trade_price
            if last_trade_time is not None:
                market.last_trade_time = last_trade_time
            
            market.last_update = datetime.now(timezone.utc)
    
    async def update_market_async(
        self,
        market_slug: str,
        **kwargs,
    ) -> None:
        """
        Async version of update_market.
        
        Args:
            market_slug: Market identifier
            **kwargs: Same as update_market
        """
        async with self._async_lock:
            # Call sync version within async lock
            # Note: We need to release _async_lock before acquiring _lock
            pass
        
        # Actually do the update (sync lock is acquired inside)
        self.update_market(market_slug, **kwargs)
    
    def get_market(self, market_slug: str) -> Optional[MarketState]:
        """
        Get market state.
        
        Args:
            market_slug: Market identifier
            
        Returns:
            MarketState if exists, None otherwise
        """
        with self._lock:
            return self._markets.get(market_slug)
    
    def get_all_markets(self) -> List[MarketState]:
        """
        Get all market states.
        
        Returns:
            List of all MarketState objects
        """
        with self._lock:
            return list(self._markets.values())
    
    def remove_market(self, market_slug: str) -> None:
        """
        Remove a market from state.
        
        Args:
            market_slug: Market identifier
        """
        with self._lock:
            self._markets.pop(market_slug, None)
    
    # =========================================================================
    # Position Management
    # =========================================================================
    
    def update_position(
        self,
        market_slug: str,
        side: Side,
        quantity: int,
        avg_price: Decimal,
        realized_pnl: Optional[Decimal] = None,
    ) -> None:
        """
        Update or create a position.
        
        Args:
            market_slug: Market identifier
            side: Position side (YES or NO)
            quantity: Number of contracts
            avg_price: Average entry price
            realized_pnl: Realized P&L (optional, added to existing)
        """
        with self._lock:
            if quantity <= 0:
                # Remove position if quantity is zero or negative
                self._positions.pop(market_slug, None)
                logger.debug("Position closed", market_slug=market_slug)
                return
            
            existing = self._positions.get(market_slug)
            
            if existing:
                # Update existing position
                existing.side = side
                existing.quantity = quantity
                existing.avg_price = avg_price
                if realized_pnl is not None:
                    existing.realized_pnl += realized_pnl
                existing.updated_at = datetime.now(timezone.utc)
            else:
                # Create new position
                self._positions[market_slug] = PositionState(
                    market_slug=market_slug,
                    side=side,
                    quantity=quantity,
                    avg_price=avg_price,
                    realized_pnl=realized_pnl or Decimal("0"),
                )
            
            logger.debug(
                "Position updated",
                market_slug=market_slug,
                side=side.value,
                quantity=quantity,
                avg_price=float(avg_price),
            )
    
    def get_position(self, market_slug: str) -> Optional[PositionState]:
        """
        Get position for a market.
        
        Args:
            market_slug: Market identifier
            
        Returns:
            PositionState if exists, None otherwise
        """
        with self._lock:
            return self._positions.get(market_slug)
    
    def get_all_positions(self) -> List[PositionState]:
        """
        Get all positions.
        
        Returns:
            List of all PositionState objects
        """
        with self._lock:
            return list(self._positions.values())
    
    def close_position(self, market_slug: str) -> Optional[PositionState]:
        """
        Close and remove a position.
        
        Args:
            market_slug: Market identifier
            
        Returns:
            The closed PositionState, or None if not found
        """
        with self._lock:
            return self._positions.pop(market_slug, None)
    
    def update_unrealized_pnl(self, market_slug: str, unrealized_pnl: Decimal) -> None:
        """
        Update unrealized P&L for a position (mark-to-market).
        
        Args:
            market_slug: Market identifier
            unrealized_pnl: Current unrealized P&L value
        """
        with self._lock:
            position = self._positions.get(market_slug)
            if position:
                position.unrealized_pnl = unrealized_pnl
                position.updated_at = datetime.now(timezone.utc)
    
    # =========================================================================
    # Order Management
    # =========================================================================
    
    def add_order(self, order: OrderState) -> None:
        """
        Add a new order to state.
        
        Args:
            order: OrderState to add
        """
        with self._lock:
            self._orders[order.order_id] = order
            logger.debug(
                "Order added",
                order_id=order.order_id,
                market_slug=order.market_slug,
                intent=order.intent.value,
                price=float(order.price),
                quantity=order.quantity,
            )
    
    def update_order(
        self,
        order_id: str,
        status: Optional[OrderStatus] = None,
        filled_quantity: Optional[int] = None,
    ) -> Optional[OrderState]:
        """
        Update an existing order.
        
        Args:
            order_id: Order identifier
            status: New status (optional)
            filled_quantity: New filled quantity (optional)
            
        Returns:
            Updated OrderState, or None if not found
        """
        with self._lock:
            order = self._orders.get(order_id)
            if not order:
                return None
            
            if status is not None:
                order.status = status
            if filled_quantity is not None:
                order.filled_quantity = filled_quantity
            
            order.updated_at = datetime.now(timezone.utc)
            
            logger.debug(
                "Order updated",
                order_id=order_id,
                status=order.status.value,
                filled_quantity=order.filled_quantity,
            )
            
            return order
    
    def remove_order(self, order_id: str) -> Optional[OrderState]:
        """
        Remove an order from state.
        
        Args:
            order_id: Order identifier
            
        Returns:
            Removed OrderState, or None if not found
        """
        with self._lock:
            order = self._orders.pop(order_id, None)
            if order:
                logger.debug("Order removed", order_id=order_id)
            return order
    
    def get_order(self, order_id: str) -> Optional[OrderState]:
        """
        Get an order by ID.
        
        Args:
            order_id: Order identifier
            
        Returns:
            OrderState if exists, None otherwise
        """
        with self._lock:
            return self._orders.get(order_id)
    
    def get_open_orders(
        self,
        market_slug: Optional[str] = None,
    ) -> List[OrderState]:
        """
        Get all open orders, optionally filtered by market.
        
        Args:
            market_slug: Optional market filter
            
        Returns:
            List of open OrderState objects
        """
        with self._lock:
            orders = [o for o in self._orders.values() if o.is_open]
            if market_slug:
                orders = [o for o in orders if o.market_slug == market_slug]
            return orders
    
    def get_all_orders(self) -> List[OrderState]:
        """
        Get all orders (including closed).
        
        Returns:
            List of all OrderState objects
        """
        with self._lock:
            return list(self._orders.values())
    
    # =========================================================================
    # Balance Management
    # =========================================================================
    
    def update_balance(self, balance: Decimal) -> None:
        """
        Set account balance.
        
        Args:
            balance: New balance value
        """
        with self._lock:
            old_balance = self._balance
            self._balance = balance
            logger.debug(
                "Balance updated",
                old_balance=float(old_balance),
                new_balance=float(balance),
            )
    
    def adjust_balance(self, amount: Decimal) -> Decimal:
        """
        Adjust balance by an amount (positive or negative).
        
        Args:
            amount: Amount to add (positive) or subtract (negative)
            
        Returns:
            New balance after adjustment
        """
        with self._lock:
            self._balance += amount
            return self._balance
    
    def get_balance(self) -> Decimal:
        """
        Get current account balance.
        
        Returns:
            Current balance
        """
        with self._lock:
            return self._balance
    
    # =========================================================================
    # WebSocket Integration
    # =========================================================================
    
    def create_market_handler(self) -> MessageHandler:
        """
        Create a WebSocket message handler for market data updates.
        
        Returns:
            Async handler function for MARKET_DATA messages
            
        Example:
            >>> state = StateManager()
            >>> handler = state.create_market_handler()
            >>> ws.on("MARKET_DATA", handler)
        """
        async def handler(data: Dict[str, Any]) -> None:
            if data.get("type") != "MARKET_DATA":
                return
            
            market_slug = data.get("marketSlug")
            if not market_slug:
                return
            
            def _parse_price_levels(levels: Any) -> List[tuple[Decimal, int]]:
                """
                Normalize various orderbook level shapes into [(price, qty), ...].

                Supports:
                - [price, qty]
                - {"price": ..., "quantity": ...}
                - {"price": ..., "size": ...}  (some feeds use size)
                """
                if not isinstance(levels, list):
                    return []

                def _parse_qty(raw: Any) -> Optional[int]:
                    try:
                        return int(Decimal(str(raw)))
                    except Exception:
                        return None

                parsed: List[tuple[Decimal, int]] = []
                for level in levels:
                    try:
                        if isinstance(level, (list, tuple)) and len(level) >= 2:
                            price = Decimal(str(level[0]))
                            qty = _parse_qty(level[1])
                            if qty is not None:
                                parsed.append((price, qty))
                        elif isinstance(level, dict):
                            if "px" in level:
                                px = level.get("px")
                                if isinstance(px, dict):
                                    price_raw = px.get("value", "0")
                                else:
                                    price_raw = px
                                qty_raw = level.get("qty", 0)
                            else:
                                price_raw = level.get("price", "0")
                                qty_raw = level.get("quantity", level.get("size", 0))
                            price = Decimal(str(price_raw))
                            qty = _parse_qty(qty_raw)
                            if qty is not None:
                                parsed.append((price, qty))
                    except Exception:
                        # Ignore malformed levels
                        continue
                return parsed

            # Extract prices from order book data.
            #
            # The documented format is {"yes": {"bids": ..., "asks": ...}, "no": ...}
            # but live sports markets may emit top-level {"bids": ..., "offers": ...}.
            yes_data = data.get("yes")
            no_data = data.get("no")
            if not yes_data and ("bids" in data or "offers" in data or "asks" in data):
                yes_data = {
                    "bids": data.get("bids", []),
                    "asks": data.get("offers", data.get("asks", [])),
                }
                no_data = no_data or {}
            else:
                yes_data = yes_data or {}
                no_data = no_data or {}
            
            # Parse YES side
            yes_bids = yes_data.get("bids", [])
            yes_asks = yes_data.get("asks", [])
            
            yes_bid = None
            yes_ask = None
            yes_bid_size = 0
            yes_ask_size = 0
            
            parsed_yes_bids = _parse_price_levels(yes_bids)
            parsed_yes_asks = _parse_price_levels(yes_asks)

            if parsed_yes_bids:
                best_bid_price, best_bid_qty = max(parsed_yes_bids, key=lambda x: x[0])
                yes_bid = best_bid_price
                yes_bid_size = best_bid_qty
            
            if parsed_yes_asks:
                best_ask_price, best_ask_qty = min(parsed_yes_asks, key=lambda x: x[0])
                yes_ask = best_ask_price
                yes_ask_size = best_ask_qty
            
            # Parse NO side
            no_bids = no_data.get("bids", [])
            no_asks = no_data.get("asks", [])
            
            no_bid = None
            no_ask = None
            
            parsed_no_bids = _parse_price_levels(no_bids)
            parsed_no_asks = _parse_price_levels(no_asks)

            if parsed_no_bids:
                no_bid, _ = max(parsed_no_bids, key=lambda x: x[0])
            
            if parsed_no_asks:
                no_ask, _ = min(parsed_no_asks, key=lambda x: x[0])
            
            # Update state
            self.update_market(
                market_slug=market_slug,
                yes_bid=yes_bid,
                yes_ask=yes_ask,
                no_bid=no_bid,
                no_ask=no_ask,
                yes_bid_size=yes_bid_size,
                yes_ask_size=yes_ask_size,
            )
            
            logger.debug(
                "Market state updated from WebSocket",
                market_slug=market_slug,
                yes_bid=float(yes_bid) if yes_bid else None,
                yes_ask=float(yes_ask) if yes_ask else None,
            )
        
        return handler
    
    def create_order_handler(self) -> MessageHandler:
        """
        Create a WebSocket message handler for order updates.
        
        Returns:
            Async handler function for ORDER_UPDATE messages
        """
        async def handler(data: Dict[str, Any]) -> None:
            if data.get("type") != "ORDER_UPDATE":
                return
            
            order_id = data.get("orderId")
            if not order_id:
                return
            
            status_str = data.get("status")
            filled_qty = data.get("filledQuantity")
            
            status = None
            if status_str:
                try:
                    status = OrderStatus(status_str)
                except ValueError:
                    logger.warning("Unknown order status", status=status_str)
            
            self.update_order(
                order_id=order_id,
                status=status,
                filled_quantity=filled_qty,
            )
        
        return handler
    
    # =========================================================================
    # Utility Methods
    # =========================================================================
    
    def get_total_position_value(self) -> Decimal:
        """
        Calculate total value of all positions at current prices.
        
        Returns:
            Total position value
        """
        with self._lock:
            total = Decimal("0")
            
            for position in self._positions.values():
                market = self._markets.get(position.market_slug)
                if not market:
                    # Use average price if no market data
                    total += position.avg_price * position.quantity
                    continue
                
                # Use appropriate bid price for mark-to-market
                if position.side == Side.YES:
                    price = market.yes_bid or position.avg_price
                else:
                    price = market.no_bid or position.avg_price
                
                total += price * position.quantity
            
            return total
    
    def get_total_equity(self) -> Decimal:
        """
        Calculate total equity (balance + position value).
        
        Returns:
            Total equity value
        """
        return self.get_balance() + self.get_total_position_value()
    
    def get_exposure(self, market_slug: Optional[str] = None) -> Decimal:
        """
        Calculate current exposure.
        
        Args:
            market_slug: Optional filter for specific market
            
        Returns:
            Total exposure (sum of position cost bases)
        """
        with self._lock:
            if market_slug:
                position = self._positions.get(market_slug)
                return position.cost_basis if position else Decimal("0")
            
            return sum(
                p.cost_basis for p in self._positions.values()
            )
    
    def clear(self) -> None:
        """Clear all state (for testing/reset)."""
        with self._lock:
            self._markets.clear()
            self._positions.clear()
            self._orders.clear()
            self._balance = Decimal("0")
            logger.info("State cleared")
    
    def snapshot(self) -> Dict[str, Any]:
        """
        Get a snapshot of current state.
        
        Returns:
            Dictionary with all state data
        """
        with self._lock:
            return {
                "balance": float(self._balance),
                "markets": {
                    slug: {
                        "yes_bid": float(m.yes_bid) if m.yes_bid else None,
                        "yes_ask": float(m.yes_ask) if m.yes_ask else None,
                        "no_bid": float(m.no_bid) if m.no_bid else None,
                        "no_ask": float(m.no_ask) if m.no_ask else None,
                    }
                    for slug, m in self._markets.items()
                },
                "positions": {
                    slug: {
                        "side": p.side.value,
                        "quantity": p.quantity,
                        "avg_price": float(p.avg_price),
                        "realized_pnl": float(p.realized_pnl),
                    }
                    for slug, p in self._positions.items()
                },
                "open_orders": len([o for o in self._orders.values() if o.is_open]),
                "total_orders": len(self._orders),
            }
