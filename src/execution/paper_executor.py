"""
Paper trading executor for Polymarket US trading bot.

This module simulates order execution for paper trading mode,
providing realistic fill simulation, fee calculation, and position management.
"""

import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

import structlog

from ..data.models import OrderIntent, OrderStatus, OrderType, Price, PriceLevel, Side
from ..data.orderbook import OrderBookTracker
from ..state.state_manager import OrderState, PositionState, StateManager

logger = structlog.get_logger()


# =============================================================================
# Constants
# =============================================================================

TAKER_FEE_RATE = Decimal("0.001")  # 0.1% taker fee
MAKER_FEE_RATE = Decimal("0")  # 0% maker fee (simplified)
MAKER_FILL_BASE_PROB = 0.02
MAKER_FILL_QUEUE_WEIGHT = 0.2
MAKER_FILL_AGE_WEIGHT = 0.1
MAKER_FILL_MAX_PROB = 0.35
MAKER_FILL_MAX_AGE_SECONDS = 30.0
MAKER_FILL_MAX_QTY_PER_TICK = 100
MAKER_FILL_MAX_FRACTION_PER_TICK = 0.02


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
        position_value: Total value of open positions (depth-aware liquidation value)
        position_value_best_bid: Total value using best-bid mark (optimistic)
        position_value_liquidation: Total liquidation value using order book depth (conservative)
        total_equity: Balance + position value
        total_equity_best_bid: Balance + position value (best bid)
        total_pnl: Total profit/loss
        total_pnl_best_bid: Total P&L using best-bid valuation
        realized_pnl: P&L from closed positions
        unrealized_pnl: Unrealized P&L using liquidation valuation
        unrealized_pnl_best_bid: Unrealized P&L using best-bid valuation
        unrealized_pnl_liquidation: Unrealized P&L using liquidation valuation (same as unrealized_pnl)
        total_fees: Total fees paid
        total_trades: Number of trades executed
        winning_trades: Number of profitable trades
        losing_trades: Number of losing trades
        open_positions: Number of open positions
    """
    initial_balance: Decimal
    current_balance: Decimal
    position_value: Decimal
    position_value_best_bid: Decimal
    position_value_liquidation: Decimal
    total_equity: Decimal
    total_equity_best_bid: Decimal
    total_pnl: Decimal
    total_pnl_best_bid: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    unrealized_pnl_best_bid: Decimal
    unrealized_pnl_liquidation: Decimal
    total_fees: Decimal
    total_trades: int
    winning_trades: int
    losing_trades: int
    open_positions: int
    maker_fills: int = 0
    taker_fills: int = 0
    
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
            "position_value_best_bid": float(self.position_value_best_bid),
            "position_value_liquidation": float(self.position_value_liquidation),
            "total_equity": float(self.total_equity),
            "total_equity_best_bid": float(self.total_equity_best_bid),
            "total_pnl": float(self.total_pnl),
            "total_pnl_best_bid": float(self.total_pnl_best_bid),
            "pnl_percent": self.pnl_percent,
            "realized_pnl": float(self.realized_pnl),
            "unrealized_pnl": float(self.unrealized_pnl),
            "unrealized_pnl_best_bid": float(self.unrealized_pnl_best_bid),
            "unrealized_pnl_liquidation": float(self.unrealized_pnl_liquidation),
            "total_fees": float(self.total_fees),
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate": self.win_rate,
            "open_positions": self.open_positions,
            "maker_fills": self.maker_fills,
            "taker_fills": self.taker_fills,
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
    post_only: bool = False
    
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
        
        post_only = False
        if hasattr(order, "post_only"):
            post_only = bool(getattr(order, "post_only"))

        return cls(
            market_slug=order.market_slug,
            intent=intent,
            quantity=order.quantity,
            price=price,
            order_type=order_type,
            post_only=post_only,
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
        self._realized_pnl_total = Decimal("0")
        self._taker_fills = 0
        self._maker_fills = 0

        # Fill listeners (used to notify StrategyEngine about position changes).
        # Signature: listener(market_slug) -> None
        self._fill_listeners: List[Callable[[str], None]] = []
        
        logger.info(
            "PaperExecutor initialized",
            initial_balance=float(self._initial_balance),
        )

    # =========================================================================
    # Fill Listeners
    # =========================================================================

    def add_fill_listener(self, listener: Callable[[str], None]) -> None:
        """
        Register a callback invoked after an order fill updates state.

        Args:
            listener: Callable that accepts a market_slug.
        """
        if listener not in self._fill_listeners:
            self._fill_listeners.append(listener)

    def remove_fill_listener(self, listener: Callable[[str], None]) -> None:
        """Remove a previously registered fill listener."""
        try:
            self._fill_listeners.remove(listener)
        except ValueError:
            return

    def _notify_fill_listeners(self, market_slug: str) -> None:
        """
        Notify listeners that a fill occurred for market_slug.

        Listener errors are swallowed so execution cannot be disrupted.
        """
        for listener in list(self._fill_listeners):
            try:
                listener(market_slug)
            except Exception as exc:
                logger.warning(
                    "Fill listener error",
                    market_slug=market_slug,
                    listener=getattr(listener, "__name__", str(listener)),
                    error=str(exc),
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
        order = self._normalize_order(order)
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

            is_buy = order.intent in (OrderIntent.BUY_LONG, OrderIntent.BUY_SHORT)
            side = (
                Side.YES
                if order.intent in (OrderIntent.BUY_LONG, OrderIntent.SELL_LONG)
                else Side.NO
            )
            current_position = self.state.get_position(order.market_slug)
            is_buy_side_flip = (
                is_buy and current_position is not None and current_position.side != side
            )
            
            # Check if limit order is marketable
            if order.order_type == OrderType.LIMIT and order.price is not None:
                is_marketable = self._is_marketable(order, fill_price)
                if not is_marketable:
                    # Order rests on book
                    if is_buy and not is_buy_side_flip:
                        required = order.price * order.quantity
                        if required > self.state.get_balance():
                            raise InsufficientBalanceError(
                                f"Insufficient balance: need ${required:.2f}, have ${self.state.get_balance():.2f}"
                            )
                    return self._create_resting_order(order, order_id)
                if order.post_only:
                    post_price = self._get_post_only_price(order)
                    if is_buy and not is_buy_side_flip:
                        required = post_price * order.quantity
                        if required > self.state.get_balance():
                            raise InsufficientBalanceError(
                                f"Insufficient balance: need ${required:.2f}, have ${self.state.get_balance():.2f}"
                            )
                    post_order = PaperOrderRequest(
                        market_slug=order.market_slug,
                        intent=order.intent,
                        quantity=order.quantity,
                        price=post_price,
                        order_type=order.order_type,
                        post_only=order.post_only,
                    )
                    return self._create_resting_order(
                        post_order,
                        order_id,
                        reason="Post-only: resting instead of taking",
                    )
            
            # Execute immediately (depth-aware when possible)
            if is_buy and not is_buy_side_flip:
                # Require sufficient balance for the full notional (exchange-like behavior).
                check_price = order.price if order.price is not None else fill_price
                required = check_price * order.quantity
                # If we're taking (taker fill), include taker fee.
                if order.order_type == OrderType.MARKET or (
                    order.order_type == OrderType.LIMIT and order.price is None
                ) or (
                    order.order_type == OrderType.LIMIT and order.price is not None
                ):
                    # Market orders always take; marketable limit orders take (post_only handled above).
                    required = required + (required * TAKER_FEE_RATE)
                if required > self.state.get_balance():
                    raise InsufficientBalanceError(
                        f"Insufficient balance: need ${required:.2f}, have ${self.state.get_balance():.2f}"
                    )
            book = self.orderbook.get(order.market_slug)
            if book is None:
                # Fallback: top-of-book fill price only.
                return self._execute_fill(order, order_id, fill_price, is_taker=True)

            sort_desc = not is_buy  # buys walk asks low->high, sells walk bids high->low

            levels: List[PriceLevel] = []
            if order.intent == OrderIntent.BUY_LONG:
                levels = list(book.yes.asks)
            elif order.intent == OrderIntent.BUY_SHORT:
                levels = list(book.no.asks)
            elif order.intent == OrderIntent.SELL_LONG:
                levels = list(book.yes.bids)
            elif order.intent == OrderIntent.SELL_SHORT:
                levels = list(book.no.bids)

            # Apply limit-price constraints for marketable LIMIT orders.
            if order.order_type == OrderType.LIMIT and order.price is not None:
                if is_buy:
                    levels = [lvl for lvl in levels if lvl.price <= order.price]
                else:
                    levels = [lvl for lvl in levels if lvl.price >= order.price]

            filled_qty, vwap, _total, levels_used = self._walk_price_levels(
                levels,
                order.quantity,
                sort_desc=sort_desc,
            )

            if filled_qty <= 0:
                return ExecutionResult(
                    order_id=order_id,
                    status=OrderStatus.REJECTED,
                    error="No liquidity available",
                )

            if filled_qty < order.quantity:
                fill_order = PaperOrderRequest(
                    market_slug=order.market_slug,
                    intent=order.intent,
                    quantity=filled_qty,
                    price=order.price,
                    order_type=order.order_type,
                    post_only=order.post_only,
                )
                fill_result = self._execute_fill(fill_order, order_id, vwap, is_taker=True)

                # Rest the remainder for LIMIT orders.
                if order.order_type == OrderType.LIMIT and order.price is not None:
                    order_state = OrderState(
                        order_id=order_id,
                        market_slug=order.market_slug,
                        intent=order.intent,
                        price=order.price,
                        quantity=order.quantity,
                        filled_quantity=filled_qty,
                        status=OrderStatus.PARTIALLY_FILLED,
                    )
                    self.state.add_order(order_state)
                    logger.info(
                        "Order partially filled; resting remainder",
                        order_id=order_id,
                        market_slug=order.market_slug,
                        filled_quantity=filled_qty,
                        remaining_quantity=order.quantity - filled_qty,
                        limit_price=float(order.price),
                        levels_used=levels_used,
                    )

                return ExecutionResult(
                    order_id=order_id,
                    status=OrderStatus.PARTIALLY_FILLED,
                    filled_quantity=filled_qty,
                    avg_fill_price=vwap,
                    fee=fill_result.fee,
                    trade=fill_result.trade,
                )

            # Fully filled across available depth (possibly multiple levels)
            return self._execute_fill(order, order_id, vwap, is_taker=True)
            
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

    def _normalize_order(self, order: PaperOrderRequest) -> PaperOrderRequest:
        """
        Normalize sell orders into economically equivalent opposite-side buys
        when no matching inventory exists.
        """
        if order.intent not in (OrderIntent.SELL_LONG, OrderIntent.SELL_SHORT):
            return order

        current = self.state.get_position(order.market_slug)

        if order.intent == OrderIntent.SELL_LONG:
            if current and current.side == Side.YES:
                return order
            return self._convert_sell_to_buy(order, OrderIntent.BUY_SHORT)

        if order.intent == OrderIntent.SELL_SHORT:
            if current and current.side == Side.NO:
                return order
            return self._convert_sell_to_buy(order, OrderIntent.BUY_LONG)

        return order

    def _convert_sell_to_buy(
        self,
        order: PaperOrderRequest,
        target_intent: OrderIntent,
    ) -> PaperOrderRequest:
        converted_price = None
        if order.price is not None:
            converted_price = Decimal("1") - order.price

        logger.debug(
            "Normalized sell to opposite buy",
            market_slug=order.market_slug,
            original_intent=order.intent.value,
            normalized_intent=target_intent.value,
            original_price=float(order.price) if order.price is not None else None,
            normalized_price=float(converted_price) if converted_price is not None else None,
        )

        return PaperOrderRequest(
            market_slug=order.market_slug,
            intent=target_intent,
            quantity=order.quantity,
            price=converted_price,
            order_type=order.order_type,
            post_only=order.post_only,
        )
    
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
        *,
        reason: Optional[str] = None,
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
            post_only=order.post_only,
            reason=reason,
        )
        
        return ExecutionResult(
            order_id=order_id,
            status=OrderStatus.OPEN,
        )

    def _get_post_only_price(self, order: PaperOrderRequest) -> Decimal:
        """
        Adjust price to avoid crossing the spread for post-only orders.
        """
        price = order.price
        if price is None:
            return price

        book = self.orderbook.get(order.market_slug)
        if book is not None:
            if order.intent == OrderIntent.BUY_LONG and book.yes_best_bid is not None:
                return min(price, book.yes_best_bid)
            if order.intent == OrderIntent.BUY_SHORT and book.no_best_bid is not None:
                return min(price, book.no_best_bid)
            if order.intent == OrderIntent.SELL_LONG and book.yes_best_ask is not None:
                return max(price, book.yes_best_ask)
            if order.intent == OrderIntent.SELL_SHORT and book.no_best_ask is not None:
                return max(price, book.no_best_ask)

        market = self.state.get_market(order.market_slug)
        if market is None:
            return price

        if order.intent == OrderIntent.BUY_LONG and market.yes_bid is not None:
            return min(price, market.yes_bid)
        if order.intent == OrderIntent.BUY_SHORT and market.no_bid is not None:
            return min(price, market.no_bid)
        if order.intent == OrderIntent.SELL_LONG and market.yes_ask is not None:
            return max(price, market.yes_ask)
        if order.intent == OrderIntent.SELL_SHORT and market.no_ask is not None:
            return max(price, market.no_ask)

        return price
    
    def _execute_fill(
        self,
        order: PaperOrderRequest,
        order_id: str,
        fill_price: Decimal,
        *,
        is_taker: bool,
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
        fee_rate = TAKER_FEE_RATE if is_taker else MAKER_FEE_RATE
        fee = cost * fee_rate
        
        # Determine side
        side = Side.YES if order.intent in (
            OrderIntent.BUY_LONG, OrderIntent.SELL_LONG
        ) else Side.NO
        
        is_buy = order.intent in (OrderIntent.BUY_LONG, OrderIntent.BUY_SHORT)

        current_position = self.state.get_position(order.market_slug)
        is_buy_side_flip = (
            is_buy and current_position is not None and current_position.side != side
        )

        # Check and update balance
        if is_buy and not is_buy_side_flip:
            total_cost = cost + fee
            current_balance = self.state.get_balance()

            if total_cost > current_balance:
                raise InsufficientBalanceError(
                    f"Insufficient balance: need ${total_cost:.2f}, have ${current_balance:.2f}"
                )

            self.state.adjust_balance(-total_cost)
        elif not is_buy:
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
            fee=fee,
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
            is_taker=is_taker,
        )
        self._trades.append(trade)

        if is_taker:
            self._taker_fills += 1
        else:
            self._maker_fills += 1
        
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
            is_taker=is_taker,
        )

        # Notify listeners after state has been updated for this fill.
        self._notify_fill_listeners(order.market_slug)
        
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
        fee: Decimal = Decimal("0"),
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
                # Side flip on a buy is economically two trades:
                # 1) close the existing position (a synthetic sell in current side basis)
                # 2) open the new position (the actual buy in the new side basis)
                #
                # This ensures cashflows and P&L reconciliation remain consistent.
                effective_close_price = Decimal("1") - price

                # 1) Synthetic close: credit proceeds for the entire existing position.
                close_proceeds = effective_close_price * current.quantity
                self.state.adjust_balance(close_proceeds)

                realized_pnl = (effective_close_price - current.avg_price) * current.quantity
                self.state.close_position(market_slug)

                # 2) Open new position: debit cost + fee for the new buy.
                total_cost = (price * quantity) + fee
                current_balance = self.state.get_balance()
                if total_cost > current_balance:
                    raise InsufficientBalanceError(
                        f"Insufficient balance: need ${total_cost:.2f}, have ${current_balance:.2f}"
                    )
                self.state.adjust_balance(-total_cost)

                self.state.update_position(
                    market_slug=market_slug,
                    side=side,
                    quantity=quantity,
                    avg_price=price,
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
            if not current:
                # Disallow naked sells in paper mode; otherwise we can "print cash"
                # without modeling margin/collateral.
                raise InvalidOrderError("Cannot sell without an open position")

            if current.side != side:
                raise InvalidOrderError(
                    f"Cannot sell {side.value} when holding {current.side.value}"
                )

            if quantity > current.quantity:
                raise InvalidOrderError(
                    f"Cannot sell {quantity}; only {current.quantity} available"
                )

            # Calculate realized P&L for the closed quantity
            realized_pnl = (price - current.avg_price) * quantity

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
        
        if realized_pnl is not None:
            self._realized_pnl_total += realized_pnl

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
            if order_state.remaining_quantity <= 0:
                # Defensive: clear fully-filled orders that may still be present.
                self.state.remove_order(order_state.order_id)
                continue

            if order_state.price is None:
                continue

            book = self.orderbook.get(order_state.market_slug)
            if book is None:
                continue

            fill_price = self._get_fill_price_for_order(order_state)
            
            if fill_price is None:
                continue
            
            is_crossed = self._is_order_marketable(order_state, fill_price)

            # Decide whether to fill this tick.
            should_fill = is_crossed or self._should_fill_as_maker(order_state)
            if not should_fill:
                continue

            # Cap the amount we can fill per tick so the simulation isn't too optimistic.
            max_by_fraction = max(
                1,
                int(order_state.remaining_quantity * MAKER_FILL_MAX_FRACTION_PER_TICK),
            )
            fill_qty = min(
                order_state.remaining_quantity,
                MAKER_FILL_MAX_QTY_PER_TICK,
                max_by_fraction,
            )

            # Bound by observed top-of-book size (simple queue proxy).
            top_qty: Optional[int] = None
            if order_state.intent == OrderIntent.BUY_LONG and book.yes.bids:
                top_qty = int(book.yes.bids[0].quantity)
            elif order_state.intent == OrderIntent.BUY_SHORT and book.no.bids:
                top_qty = int(book.no.bids[0].quantity)
            elif order_state.intent == OrderIntent.SELL_LONG and book.yes.asks:
                top_qty = int(book.yes.asks[0].quantity)
            elif order_state.intent == OrderIntent.SELL_SHORT and book.no.asks:
                top_qty = int(book.no.asks[0].quantity)

            if top_qty is not None and top_qty > 0:
                fill_qty = min(fill_qty, top_qty)

            # Inventory-safe sells (avoid exceptions like "Cannot sell X; only Y available").
            if not order_state.is_buy:
                current = self.state.get_position(order_state.market_slug)
                available = (
                    current.quantity
                    if current is not None and current.side == order_state.side
                    else 0
                )
                if available <= 0:
                    continue
                fill_qty = min(fill_qty, available)

            if fill_qty <= 0:
                continue

            # If crossed, bound fill by current opposite-side liquidity at/through our limit.
            if is_crossed:
                if order_state.intent == OrderIntent.BUY_LONG:
                    opp_levels = book.yes.asks
                elif order_state.intent == OrderIntent.BUY_SHORT:
                    opp_levels = book.no.asks
                elif order_state.intent == OrderIntent.SELL_LONG:
                    opp_levels = book.yes.bids
                else:
                    opp_levels = book.no.bids

                if order_state.is_buy:
                    available_liq = sum(
                        int(lvl.quantity)
                        for lvl in opp_levels
                        if lvl.price <= order_state.price
                    )
                else:
                    available_liq = sum(
                        int(lvl.quantity)
                        for lvl in opp_levels
                        if lvl.price >= order_state.price
                    )

                if available_liq <= 0:
                    continue
                fill_qty = min(fill_qty, available_liq)

            if fill_qty <= 0:
                continue

            paper_order = PaperOrderRequest(
                market_slug=order_state.market_slug,
                intent=order_state.intent,
                quantity=fill_qty,
                price=order_state.price,
            )

            try:
                # Resting orders are maker fills (0% fee) at the limit price.
                result = self._execute_fill(
                    paper_order,
                    order_state.order_id,
                    order_state.price,
                    is_taker=False,
                )
                results.append(result)

                new_filled = order_state.filled_quantity + fill_qty
                if new_filled >= order_state.quantity:
                    # Fully filled: update status and remove from the book.
                    self.state.update_order(
                        order_state.order_id,
                        status=OrderStatus.FILLED,
                        filled_quantity=order_state.quantity,
                    )
                    self.state.remove_order(order_state.order_id)
                else:
                    self.state.update_order(
                        order_state.order_id,
                        status=OrderStatus.PARTIALLY_FILLED,
                        filled_quantity=new_filled,
                    )

            except Exception as e:
                logger.error(
                    "Failed to simulate maker fill",
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

    def _should_fill_as_maker(self, order: OrderState) -> bool:
        """
        Probabilistically fill resting orders at/near top-of-book.
        """
        if order.price is None:
            return False

        book = self.orderbook.get(order.market_slug)
        if not book:
            return False

        top_price = None
        top_qty = None
        if order.intent == OrderIntent.BUY_LONG:
            if book.yes.bids:
                top_price = book.yes.bids[0].price
                top_qty = book.yes.bids[0].quantity
        elif order.intent == OrderIntent.BUY_SHORT:
            if book.no.bids:
                top_price = book.no.bids[0].price
                top_qty = book.no.bids[0].quantity
        elif order.intent == OrderIntent.SELL_LONG:
            if book.yes.asks:
                top_price = book.yes.asks[0].price
                top_qty = book.yes.asks[0].quantity
        elif order.intent == OrderIntent.SELL_SHORT:
            if book.no.asks:
                top_price = book.no.asks[0].price
                top_qty = book.no.asks[0].quantity

        if top_price is None or top_qty is None:
            return False

        if order.is_buy and order.price < top_price:
            return False
        if not order.is_buy and order.price > top_price:
            return False

        queue_qty = top_qty if top_qty > 0 else order.remaining_quantity
        queue_ratio = order.remaining_quantity / (order.remaining_quantity + queue_qty)
        age_seconds = (datetime.now(timezone.utc) - order.created_at).total_seconds()
        age_factor = min(age_seconds / MAKER_FILL_MAX_AGE_SECONDS, 1.0)

        probability = MAKER_FILL_BASE_PROB
        probability += queue_ratio * MAKER_FILL_QUEUE_WEIGHT
        probability += age_factor * MAKER_FILL_AGE_WEIGHT
        probability = min(probability, MAKER_FILL_MAX_PROB)

        return random.random() < probability
    
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
        
        # Calculate position value and unrealized P&L using two marking modes:
        # - best-bid (optimistic)
        # - depth-aware liquidation (conservative, assumes unfillable remainder is worth 0)
        position_value_best_bid = Decimal("0")
        unrealized_pnl_best_bid = Decimal("0")
        position_value_liquidation = Decimal("0")
        unrealized_pnl_liquidation = Decimal("0")
        
        for position in positions:
            entry_value = position.avg_price * position.quantity
            book = self.orderbook.get(position.market_slug)
            
            # ----------------------------
            # Best-bid mark
            # ----------------------------
            if book is not None:
                if position.side == Side.YES:
                    best_bid = book.yes_best_bid
                else:
                    best_bid = book.no_best_bid
            else:
                market = self.state.get_market(position.market_slug)
                if market is not None:
                    best_bid = market.yes_bid if position.side == Side.YES else market.no_bid
                else:
                    best_bid = None

            mark_best_bid = best_bid or position.avg_price
            value_best_bid = mark_best_bid * position.quantity
            position_value_best_bid += value_best_bid
            unrealized_pnl_best_bid += value_best_bid - entry_value

            # ----------------------------
            # Depth-aware liquidation mark
            # ----------------------------
            bid_levels: List[PriceLevel] = []
            if book is not None:
                side_book = book.yes if position.side == Side.YES else book.no
                bid_levels = list(side_book.bids)
            else:
                # Best-effort fallback for YES side only (state has bid size for YES).
                market = self.state.get_market(position.market_slug)
                if (
                    market is not None
                    and position.side == Side.YES
                    and market.yes_bid is not None
                    and market.yes_bid_size > 0
                ):
                    bid_levels = [
                        PriceLevel(price=market.yes_bid, quantity=int(market.yes_bid_size))
                    ]

            _filled, _vwap, liquidation_value, _levels_used = self._walk_price_levels(
                bid_levels,
                position.quantity,
                sort_desc=True,
            )
            position_value_liquidation += liquidation_value
            unrealized_pnl_liquidation += liquidation_value - entry_value
        
        # Realized P&L should persist even after positions close.
        realized_pnl = self._realized_pnl_total
        
        # By default, expose conservative liquidation-based valuation in the legacy
        # fields (position_value/unrealized_pnl/total_equity/total_pnl).
        position_value = position_value_liquidation
        unrealized_pnl = unrealized_pnl_liquidation

        total_equity = current_balance + position_value
        total_pnl = total_equity - self._initial_balance

        total_equity_best_bid = current_balance + position_value_best_bid
        total_pnl_best_bid = total_equity_best_bid - self._initial_balance
        
        return PerformanceMetrics(
            initial_balance=self._initial_balance,
            current_balance=current_balance,
            position_value=position_value,
            position_value_best_bid=position_value_best_bid,
            position_value_liquidation=position_value_liquidation,
            total_equity=total_equity,
            total_equity_best_bid=total_equity_best_bid,
            total_pnl=total_pnl,
            total_pnl_best_bid=total_pnl_best_bid,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            unrealized_pnl_best_bid=unrealized_pnl_best_bid,
            unrealized_pnl_liquidation=unrealized_pnl_liquidation,
            total_fees=self._total_fees,
            total_trades=len(self._trades),
            winning_trades=self._winning_trades,
            losing_trades=self._losing_trades,
            open_positions=len(positions),
            maker_fills=self._maker_fills,
            taker_fills=self._taker_fills,
        )

    def _walk_price_levels(
        self,
        levels: List[PriceLevel],
        quantity: int,
        *,
        sort_desc: bool,
    ) -> Tuple[int, Decimal, Decimal, int]:
        """
        Walk order book levels to simulate an immediate fill.

        Returns:
            filled_qty, vwap_price (0 if none), total_value, levels_used
        """
        if quantity <= 0:
            return 0, Decimal("0"), Decimal("0"), 0

        sorted_levels = sorted(levels, key=lambda l: l.price, reverse=sort_desc)

        remaining = int(quantity)
        filled = 0
        total = Decimal("0")
        levels_used = 0

        for level in sorted_levels:
            if remaining <= 0:
                break

            level_qty = int(level.quantity)
            if level_qty <= 0:
                continue

            take = min(remaining, level_qty)
            total += level.price * take
            filled += take
            remaining -= take
            levels_used += 1

        vwap = (total / filled) if filled > 0 else Decimal("0")
        return filled, vwap, total, levels_used

    def get_positions_report(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Return a detailed view of open positions with best-bid marks and
        depth-aware liquidation estimates.
        """
        positions = self.state.get_all_positions()
        report: List[Dict[str, Any]] = []

        for position in positions:
            book = self.orderbook.get(position.market_slug)

            best_bid: Optional[Decimal] = None
            best_ask: Optional[Decimal] = None
            best_bid_size: int = 0
            best_ask_size: int = 0

            if book is not None:
                side_book = book.yes if position.side == Side.YES else book.no
                if side_book.bids:
                    best_bid = side_book.bids[0].price
                    best_bid_size = int(side_book.bids[0].quantity)
                if side_book.asks:
                    best_ask = side_book.asks[0].price
                    best_ask_size = int(side_book.asks[0].quantity)
            else:
                market = self.state.get_market(position.market_slug)
                if market is not None:
                    if position.side == Side.YES:
                        best_bid = market.yes_bid
                        best_ask = market.yes_ask
                        best_bid_size = int(market.yes_bid_size)
                        best_ask_size = int(market.yes_ask_size)
                    else:
                        best_bid = market.no_bid
                        best_ask = market.no_ask

            mark_best_bid = best_bid or position.avg_price
            entry_value = position.avg_price * position.quantity
            value_best_bid = mark_best_bid * position.quantity
            unrealized_best_bid = value_best_bid - entry_value

            # Depth-aware liquidation (sell into bids).
            bid_levels: List[PriceLevel] = []
            if book is not None:
                side_book = book.yes if position.side == Side.YES else book.no
                bid_levels = list(side_book.bids)
            else:
                # Best-effort fallback for YES side only (state has bid size for YES).
                if position.side == Side.YES and best_bid is not None and best_bid_size > 0:
                    bid_levels = [PriceLevel(price=best_bid, quantity=best_bid_size)]

            filled_qty, vwap, liquidation_value, levels_used = self._walk_price_levels(
                bid_levels,
                position.quantity,
                sort_desc=True,
            )

            # If the book cannot fill the whole position immediately, we assume the
            # remainder is not liquidatable at any price right now (conservative).
            liquidation_mark = (
                (liquidation_value / position.quantity)
                if position.quantity > 0
                else Decimal("0")
            )
            unrealized_liquidation = liquidation_value - entry_value

            report.append(
                {
                    "market_slug": position.market_slug,
                    "side": position.side.value,
                    "quantity": position.quantity,
                    "avg_price": float(position.avg_price),
                    "best_bid": float(best_bid) if best_bid is not None else None,
                    "best_bid_size": best_bid_size,
                    "best_ask": float(best_ask) if best_ask is not None else None,
                    "best_ask_size": best_ask_size,
                    "best_bid_mark": float(mark_best_bid),
                    "unrealized_pnl_best_bid": float(unrealized_best_bid),
                    "liquidation_mark": float(liquidation_mark),
                    "liquidation_fillable_qty": int(filled_qty),
                    "liquidation_unfilled_qty": int(position.quantity - filled_qty),
                    "liquidation_levels_used": int(levels_used),
                    "unrealized_pnl_liquidation": float(unrealized_liquidation),
                }
            )

        # Sort most impactful first (best-bid mark), then cap output size.
        report.sort(key=lambda r: abs(r.get("unrealized_pnl_best_bid", 0.0)), reverse=True)
        return report[: max(0, int(limit))]
    
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
                "is_taker": t.is_taker,
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
        self._realized_pnl_total = Decimal("0")
        self._maker_fills = 0
        self._taker_fills = 0
        
        logger.info(
            "PaperExecutor reset",
            initial_balance=float(self._initial_balance),
        )
