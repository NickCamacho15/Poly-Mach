"""
Paper trading executor for Polymarket US trading bot.

This module simulates order execution for paper trading mode,
providing realistic fill simulation, fee calculation, and position management.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

import structlog

from ..data.models import OrderIntent, OrderStatus, OrderType, Price, Side
from ..data.orderbook import OrderBookTracker
from ..state.state_manager import OrderState, PositionState, StateManager

logger = structlog.get_logger()


# =============================================================================
# Constants
# =============================================================================

TAKER_FEE_RATE = Decimal("0.001")  # 0.1% taker fee
MAKER_FEE_RATE = Decimal("0")  # 0% maker fee (simplified)


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class TradeRecord:
    """
    Record of an executed trade.
    
    Attributes:
        trade_id: Unique trade identifier
        order_id: Associated order ID
        market_slug: Market identifier
        side: Position side (YES or NO)
        intent: Order intent
        quantity: Number of contracts traded
        price: Execution price
        cost: Total cost (price * quantity)
        fee: Fee charged
        is_taker: Whether this was a taker trade
        timestamp: Execution timestamp
    """
    trade_id: str
    order_id: str
    market_slug: str
    side: Side
    intent: OrderIntent
    quantity: int
    price: Decimal
    cost: Decimal
    fee: Decimal
    is_taker: bool = True
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    @property
    def total_cost(self) -> Decimal:
        """Total cost including fees."""
        return self.cost + self.fee


@dataclass
class ExecutionResult:
    """
    Result of an order execution attempt.
    
    Attributes:
        order_id: Order identifier
        status: Resulting order status
        filled_quantity: Quantity filled
        avg_fill_price: Average fill price (if any fill)
        fee: Fee charged
        error: Error message if failed
        trade: Trade record if executed
    """
    order_id: str
    status: OrderStatus
    filled_quantity: int = 0
    avg_fill_price: Optional[Decimal] = None
    fee: Decimal = Decimal("0")
    error: Optional[str] = None
    trade: Optional[TradeRecord] = None
    
    @property
    def is_success(self) -> bool:
        """Check if execution was successful."""
        return self.error is None
    
    @property
    def is_filled(self) -> bool:
        """Check if order was filled."""
        return self.status == OrderStatus.FILLED
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API-like response."""
        return {
            "orderId": self.order_id,
            "status": self.status.value,
            "filledQuantity": self.filled_quantity,
            "avgFillPrice": str(self.avg_fill_price) if self.avg_fill_price else None,
            "fee": str(self.fee),
            "error": self.error,
        }


@dataclass
class PerformanceMetrics:
    """
    Performance metrics for paper trading.
    
    Attributes:
        initial_balance: Starting balance
        current_balance: Current cash balance
        position_value: Total value of open positions
        total_equity: Balance + position value
        total_pnl: Total profit/loss
        realized_pnl: P&L from closed positions
        unrealized_pnl: P&L from open positions
        total_fees: Total fees paid
        total_trades: Number of trades executed
        winning_trades: Number of profitable trades
        losing_trades: Number of losing trades
        open_positions: Number of open positions
    """
    initial_balance: Decimal
    current_balance: Decimal
    position_value: Decimal
    total_equity: Decimal
    total_pnl: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_fees: Decimal
    total_trades: int
    winning_trades: int
    losing_trades: int
    open_positions: int
    
    @property
    def win_rate(self) -> Optional[float]:
        """Calculate win rate as percentage."""
        total = self.winning_trades + self.losing_trades
        if total == 0:
            return None
        return (self.winning_trades / total) * 100
    
    @property
    def pnl_percent(self) -> float:
        """Calculate P&L as percentage of initial balance."""
        if self.initial_balance == 0:
            return 0.0
        return float((self.total_pnl / self.initial_balance) * 100)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "initial_balance": float(self.initial_balance),
            "current_balance": float(self.current_balance),
            "position_value": float(self.position_value),
            "total_equity": float(self.total_equity),
            "total_pnl": float(self.total_pnl),
            "pnl_percent": self.pnl_percent,
            "realized_pnl": float(self.realized_pnl),
            "unrealized_pnl": float(self.unrealized_pnl),
            "total_fees": float(self.total_fees),
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate": self.win_rate,
            "open_positions": self.open_positions,
        }


# =============================================================================
# Exceptions
# =============================================================================

class ExecutionError(Exception):
    """Base exception for execution errors."""
    pass


class InsufficientBalanceError(ExecutionError):
    """Raised when account has insufficient funds."""
    pass


class MarketNotFoundError(ExecutionError):
    """Raised when market is not found in order book."""
    pass


class InvalidOrderError(ExecutionError):
    """Raised when order parameters are invalid."""
    pass


# =============================================================================
# Paper Order Request
# =============================================================================

@dataclass
class PaperOrderRequest:
    """
    Order request for paper trading.
    
    Simplified version of OrderRequest for paper trading.
    """
    market_slug: str
    intent: OrderIntent
    quantity: int
    price: Optional[Decimal] = None
    order_type: OrderType = OrderType.LIMIT
    
    @classmethod
    def from_order_request(cls, order) -> "PaperOrderRequest":
        """Create from an OrderRequest model."""
        price = None
        if order.price:
            if isinstance(order.price, Price):
                price = Decimal(order.price.value)
            elif isinstance(order.price, dict):
                price = Decimal(str(order.price.get("value", "0")))
            else:
                price = Decimal(str(order.price))
        
        intent = order.intent
        if isinstance(intent, str):
            intent = OrderIntent(intent)
        
        order_type = order.order_type if hasattr(order, 'order_type') else OrderType.LIMIT
        if isinstance(order_type, str):
            order_type = OrderType(order_type)
        
        return cls(
            market_slug=order.market_slug,
            intent=intent,
            quantity=order.quantity,
            price=price,
            order_type=order_type,
        )


# =============================================================================
# Paper Executor
# =============================================================================

class PaperExecutor:
    """
    Paper trading executor that simulates order execution.
    
    Features:
    - Executes against OrderBookTracker state
    - Handles limit orders (immediate fill vs resting)
    - Calculates taker fees (0.1%)
    - Tracks positions with proper average price
    - Provides performance metrics
    
    Example:
        >>> state = StateManager(initial_balance=Decimal("1000"))
        >>> orderbook = OrderBookTracker()
        >>> executor = PaperExecutor(state, orderbook)
        >>> 
        >>> # Execute an order
        >>> order = PaperOrderRequest(
        ...     market_slug="nba-game",
        ...     intent=OrderIntent.BUY_LONG,
        ...     quantity=100,
        ...     price=Decimal("0.50"),
        ... )
        >>> result = executor.execute_order(order)
        >>> print(result.status)  # FILLED or OPEN
    """
    
    def __init__(
        self,
        state: StateManager,
        orderbook: OrderBookTracker,
        initial_balance: Optional[Decimal] = None,
    ):
        """
        Initialize paper executor.
        
        Args:
            state: StateManager for tracking positions, orders, balance
            orderbook: OrderBookTracker for price data
            initial_balance: Optional initial balance (overrides state balance)
        """
        self.state = state
        self.orderbook = orderbook
        
        # Set initial balance
        if initial_balance is not None:
            self.state.update_balance(initial_balance)
        
        self._initial_balance = self.state.get_balance()
        
        # Trade history
        self._trades: List[TradeRecord] = []
        self._total_fees = Decimal("0")
        self._winning_trades = 0
        self._losing_trades = 0
        
        logger.info(
            "PaperExecutor initialized",
            initial_balance=float(self._initial_balance),
        )
    
    # =========================================================================
    # Order Execution
    # =========================================================================
    
    def execute_order(self, order: PaperOrderRequest) -> ExecutionResult:
        """
        Execute an order in paper trading mode.
        
        Args:
            order: PaperOrderRequest with order details
            
        Returns:
            ExecutionResult with execution status and details
        """
        order_id = str(uuid.uuid4())
        
        logger.info(
            "Executing paper order",
            order_id=order_id,
            market_slug=order.market_slug,
            intent=order.intent.value,
            quantity=order.quantity,
            price=float(order.price) if order.price else None,
        )
        
        try:
            # Validate order
            self._validate_order(order)
            
            # Get fill price from order book
            fill_price = self._get_fill_price(order)
            
            if fill_price is None:
                return ExecutionResult(
                    order_id=order_id,
                    status=OrderStatus.REJECTED,
                    error="No liquidity available",
                )
            
            # Check if limit order is marketable
            if order.order_type == OrderType.LIMIT and order.price is not None:
                if not self._is_marketable(order, fill_price):
                    # Order rests on book
                    return self._create_resting_order(order, order_id)
            
            # Execute immediately
            return self._execute_fill(order, order_id, fill_price)
            
        except InsufficientBalanceError as e:
            logger.warning("Insufficient balance", order_id=order_id, error=str(e))
            return ExecutionResult(
                order_id=order_id,
                status=OrderStatus.REJECTED,
                error=str(e),
            )
        except MarketNotFoundError as e:
            logger.warning("Market not found", order_id=order_id, error=str(e))
            return ExecutionResult(
                order_id=order_id,
                status=OrderStatus.REJECTED,
                error=str(e),
            )
        except Exception as e:
            logger.error("Execution error", order_id=order_id, error=str(e))
            return ExecutionResult(
                order_id=order_id,
                status=OrderStatus.REJECTED,
                error=f"Execution error: {e}",
            )
    
    def _validate_order(self, order: PaperOrderRequest) -> None:
        """Validate order parameters."""
        if order.quantity <= 0:
            raise InvalidOrderError("Quantity must be positive")
        
        if order.price is not None:
            if order.price <= 0 or order.price >= 1:
                raise InvalidOrderError("Price must be between 0 and 1 (exclusive)")
    
    def _get_fill_price(self, order: PaperOrderRequest) -> Optional[Decimal]:
        """
        Get the fill price from order book.
        
        Args:
            order: Order request
            
        Returns:
            Fill price or None if no liquidity
        """
        book = self.orderbook.get(order.market_slug)
        
        if book is None:
            # Try to get from state manager as fallback
            market = self.state.get_market(order.market_slug)
            if market is None:
                raise MarketNotFoundError(f"Market not found: {order.market_slug}")
            
            # Use state manager prices
            if order.intent == OrderIntent.BUY_LONG:
                return market.yes_ask
            elif order.intent == OrderIntent.BUY_SHORT:
                return market.no_ask
            elif order.intent == OrderIntent.SELL_LONG:
                return market.yes_bid
            elif order.intent == OrderIntent.SELL_SHORT:
                return market.no_bid
            
            return None
        
        # Use order book prices
        if order.intent == OrderIntent.BUY_LONG:
            return book.yes_best_ask
        elif order.intent == OrderIntent.BUY_SHORT:
            return book.no_best_ask
        elif order.intent == OrderIntent.SELL_LONG:
            return book.yes_best_bid
        elif order.intent == OrderIntent.SELL_SHORT:
            return book.no_best_bid
        
        return None
    
    def _is_marketable(self, order: PaperOrderRequest, fill_price: Decimal) -> bool:
        """
        Check if limit order is marketable (crosses the spread).
        
        Args:
            order: Order request
            fill_price: Current fill price from order book
            
        Returns:
            True if order would fill immediately
        """
        if order.price is None:
            return True  # Market orders always marketable
        
        # Buy orders: marketable if price >= ask
        if order.intent in (OrderIntent.BUY_LONG, OrderIntent.BUY_SHORT):
            return order.price >= fill_price
        
        # Sell orders: marketable if price <= bid
        return order.price <= fill_price
    
    def _create_resting_order(
        self,
        order: PaperOrderRequest,
        order_id: str,
    ) -> ExecutionResult:
        """
        Create a resting limit order.
        
        Args:
            order: Order request
            order_id: Order identifier
            
        Returns:
            ExecutionResult with OPEN status
        """
        order_state = OrderState(
            order_id=order_id,
            market_slug=order.market_slug,
            intent=order.intent,
            price=order.price,
            quantity=order.quantity,
            status=OrderStatus.OPEN,
        )
        
        self.state.add_order(order_state)
        
        logger.info(
            "Resting order created",
            order_id=order_id,
            market_slug=order.market_slug,
            price=float(order.price),
        )
        
        return ExecutionResult(
            order_id=order_id,
            status=OrderStatus.OPEN,
        )
    
    def _execute_fill(
        self,
        order: PaperOrderRequest,
        order_id: str,
        fill_price: Decimal,
    ) -> ExecutionResult:
        """
        Execute an immediate fill.
        
        Args:
            order: Order request
            order_id: Order identifier
            fill_price: Execution price
            
        Returns:
            ExecutionResult with fill details
        """
        quantity = order.quantity
        cost = fill_price * quantity
        fee = cost * TAKER_FEE_RATE
        
        # Determine side
        side = Side.YES if order.intent in (
            OrderIntent.BUY_LONG, OrderIntent.SELL_LONG
        ) else Side.NO
        
        is_buy = order.intent in (OrderIntent.BUY_LONG, OrderIntent.BUY_SHORT)
        
        # Check and update balance
        if is_buy:
            total_cost = cost + fee
            current_balance = self.state.get_balance()
            
            if total_cost > current_balance:
                raise InsufficientBalanceError(
                    f"Insufficient balance: need ${total_cost:.2f}, have ${current_balance:.2f}"
                )
            
            self.state.adjust_balance(-total_cost)
        else:
            # Selling: add proceeds minus fee
            proceeds = cost - fee
            self.state.adjust_balance(proceeds)
        
        # Update position
        realized_pnl = self._update_position(
            market_slug=order.market_slug,
            side=side,
            quantity=quantity,
            price=fill_price,
            is_buy=is_buy,
        )
        
        # Track fees
        self._total_fees += fee
        
        # Record trade
        trade = TradeRecord(
            trade_id=str(uuid.uuid4()),
            order_id=order_id,
            market_slug=order.market_slug,
            side=side,
            intent=order.intent,
            quantity=quantity,
            price=fill_price,
            cost=cost,
            fee=fee,
        )
        self._trades.append(trade)
        
        # Track win/loss for sells
        if not is_buy and realized_pnl is not None:
            if realized_pnl > 0:
                self._winning_trades += 1
            elif realized_pnl < 0:
                self._losing_trades += 1
        
        logger.info(
            "Order filled",
            order_id=order_id,
            market_slug=order.market_slug,
            side=side.value,
            quantity=quantity,
            price=float(fill_price),
            cost=float(cost),
            fee=float(fee),
        )
        
        return ExecutionResult(
            order_id=order_id,
            status=OrderStatus.FILLED,
            filled_quantity=quantity,
            avg_fill_price=fill_price,
            fee=fee,
            trade=trade,
        )
    
    def _update_position(
        self,
        market_slug: str,
        side: Side,
        quantity: int,
        price: Decimal,
        is_buy: bool,
    ) -> Optional[Decimal]:
        """
        Update position after a trade.
        
        Args:
            market_slug: Market identifier
            side: Position side
            quantity: Trade quantity
            price: Trade price
            is_buy: Whether this is a buy
            
        Returns:
            Realized P&L if closing position, None otherwise
        """
        current = self.state.get_position(market_slug)
        realized_pnl = None
        
        if is_buy:
            if current and current.side == side:
                # Adding to existing position - calculate new average price
                total_qty = current.quantity + quantity
                total_cost = (current.avg_price * current.quantity) + (price * quantity)
                new_avg = total_cost / total_qty
                
                self.state.update_position(
                    market_slug=market_slug,
                    side=side,
                    quantity=total_qty,
                    avg_price=new_avg,
                )
            elif current and current.side != side:
                # Opposite side - this would be closing and opening opposite
                # For simplicity, close existing and open new
                realized_pnl = (price - current.avg_price) * current.quantity
                if current.side == Side.NO:
                    realized_pnl = -realized_pnl  # Invert for NO side
                
                self.state.update_position(
                    market_slug=market_slug,
                    side=side,
                    quantity=quantity,
                    avg_price=price,
                    realized_pnl=realized_pnl,
                )
            else:
                # New position
                self.state.update_position(
                    market_slug=market_slug,
                    side=side,
                    quantity=quantity,
                    avg_price=price,
                )
        else:
            # Selling
            if current:
                # Calculate realized P&L
                realized_pnl = (price - current.avg_price) * quantity
                if current.side == Side.NO:
                    realized_pnl = -realized_pnl  # Invert for NO side
                
                new_qty = current.quantity - quantity
                
                if new_qty <= 0:
                    # Position fully closed
                    self.state.close_position(market_slug)
                else:
                    # Partial close
                    self.state.update_position(
                        market_slug=market_slug,
                        side=current.side,
                        quantity=new_qty,
                        avg_price=current.avg_price,
                        realized_pnl=realized_pnl,
                    )
            else:
                # Short selling (opening short position) - not typical for binary markets
                # but handle it anyway
                self.state.update_position(
                    market_slug=market_slug,
                    side=side,
                    quantity=quantity,
                    avg_price=price,
                )
        
        return realized_pnl
    
    # =========================================================================
    # Order Management
    # =========================================================================
    
    def cancel_order(self, order_id: str) -> bool:
        """
        Cancel a resting order.
        
        Args:
            order_id: Order identifier
            
        Returns:
            True if order was cancelled, False if not found
        """
        order = self.state.get_order(order_id)
        
        if not order:
            return False
        
        if not order.is_open:
            return False
        
        self.state.update_order(order_id, status=OrderStatus.CANCELLED)
        self.state.remove_order(order_id)
        
        logger.info("Order cancelled", order_id=order_id)
        return True
    
    def cancel_all_orders(self, market_slug: Optional[str] = None) -> int:
        """
        Cancel all open orders.
        
        Args:
            market_slug: Optional filter by market
            
        Returns:
            Number of orders cancelled
        """
        open_orders = self.state.get_open_orders(market_slug)
        cancelled = 0
        
        for order in open_orders:
            if self.cancel_order(order.order_id):
                cancelled += 1
        
        logger.info(
            "Orders cancelled",
            count=cancelled,
            market_slug=market_slug,
        )
        return cancelled
    
    def check_resting_orders(self) -> List[ExecutionResult]:
        """
        Check if any resting orders can now be filled.
        
        Returns:
            List of ExecutionResults for orders that filled
        """
        results = []
        open_orders = self.state.get_open_orders()
        
        for order_state in open_orders:
            fill_price = self._get_fill_price_for_order(order_state)
            
            if fill_price is None:
                continue
            
            # Check if now marketable
            if self._is_order_marketable(order_state, fill_price):
                # Remove from open orders and execute
                self.state.remove_order(order_state.order_id)
                
                paper_order = PaperOrderRequest(
                    market_slug=order_state.market_slug,
                    intent=order_state.intent,
                    quantity=order_state.remaining_quantity,
                    price=order_state.price,
                )
                
                try:
                    result = self._execute_fill(
                        paper_order,
                        order_state.order_id,
                        order_state.price,  # Fill at limit price (price improvement)
                    )
                    results.append(result)
                except Exception as e:
                    logger.error(
                        "Failed to fill resting order",
                        order_id=order_state.order_id,
                        error=str(e),
                    )
        
        return results
    
    def _get_fill_price_for_order(self, order: OrderState) -> Optional[Decimal]:
        """Get fill price for an existing order."""
        book = self.orderbook.get(order.market_slug)
        
        if book is None:
            return None
        
        if order.intent == OrderIntent.BUY_LONG:
            return book.yes_best_ask
        elif order.intent == OrderIntent.BUY_SHORT:
            return book.no_best_ask
        elif order.intent == OrderIntent.SELL_LONG:
            return book.yes_best_bid
        elif order.intent == OrderIntent.SELL_SHORT:
            return book.no_best_bid
        
        return None
    
    def _is_order_marketable(self, order: OrderState, fill_price: Decimal) -> bool:
        """Check if existing order is now marketable."""
        if order.is_buy:
            return order.price >= fill_price
        return order.price <= fill_price
    
    # =========================================================================
    # Performance Metrics
    # =========================================================================
    
    def get_performance(self) -> PerformanceMetrics:
        """
        Get paper trading performance metrics.
        
        Returns:
            PerformanceMetrics with all trading stats
        """
        current_balance = self.state.get_balance()
        positions = self.state.get_all_positions()
        
        # Calculate position value and unrealized P&L
        position_value = Decimal("0")
        unrealized_pnl = Decimal("0")
        
        for position in positions:
            book = self.orderbook.get(position.market_slug)
            
            if book:
                # Mark to market using bid price
                if position.side == Side.YES:
                    mark_price = book.yes_best_bid or position.avg_price
                else:
                    mark_price = book.no_best_bid or position.avg_price
            else:
                # Fallback to state manager
                market = self.state.get_market(position.market_slug)
                if market:
                    if position.side == Side.YES:
                        mark_price = market.yes_bid or position.avg_price
                    else:
                        mark_price = market.no_bid or position.avg_price
                else:
                    mark_price = position.avg_price
            
            current_value = mark_price * position.quantity
            position_value += current_value
            
            # Unrealized P&L
            entry_value = position.avg_price * position.quantity
            pnl = current_value - entry_value
            if position.side == Side.NO:
                pnl = -pnl
            unrealized_pnl += pnl
        
        # Calculate realized P&L from positions
        realized_pnl = sum(p.realized_pnl for p in positions)
        
        total_equity = current_balance + position_value
        total_pnl = total_equity - self._initial_balance
        
        return PerformanceMetrics(
            initial_balance=self._initial_balance,
            current_balance=current_balance,
            position_value=position_value,
            total_equity=total_equity,
            total_pnl=total_pnl,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            total_fees=self._total_fees,
            total_trades=len(self._trades),
            winning_trades=self._winning_trades,
            losing_trades=self._losing_trades,
            open_positions=len(positions),
        )
    
    def get_trades(self) -> List[TradeRecord]:
        """
        Get all trade records.
        
        Returns:
            List of TradeRecord objects
        """
        return list(self._trades)
    
    def get_trade_history(
        self,
        market_slug: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Get trade history as list of dicts.
        
        Args:
            market_slug: Optional filter by market
            limit: Maximum number of trades to return
            
        Returns:
            List of trade dictionaries
        """
        trades = self._trades
        
        if market_slug:
            trades = [t for t in trades if t.market_slug == market_slug]
        
        # Most recent first
        trades = sorted(trades, key=lambda t: t.timestamp, reverse=True)
        trades = trades[:limit]
        
        return [
            {
                "trade_id": t.trade_id,
                "order_id": t.order_id,
                "market_slug": t.market_slug,
                "side": t.side.value,
                "intent": t.intent.value,
                "quantity": t.quantity,
                "price": float(t.price),
                "cost": float(t.cost),
                "fee": float(t.fee),
                "timestamp": t.timestamp.isoformat(),
            }
            for t in trades
        ]
    
    # =========================================================================
    # Utility Methods
    # =========================================================================
    
    def reset(self, initial_balance: Optional[Decimal] = None) -> None:
        """
        Reset paper trading state.
        
        Args:
            initial_balance: New initial balance (uses original if not provided)
        """
        if initial_balance is not None:
            self._initial_balance = initial_balance
        
        self.state.clear()
        self.state.update_balance(self._initial_balance)
        
        self._trades.clear()
        self._total_fees = Decimal("0")
        self._winning_trades = 0
        self._losing_trades = 0
        
        logger.info(
            "PaperExecutor reset",
            initial_balance=float(self._initial_balance),
        )
