"""
Data models and state management package.

This package provides Pydantic models for API data, internal state management,
and real-time order book tracking.
"""

from .models import (
    # Enums
    MarketStatus,
    OrderType,
    OrderIntent,
    OrderStatus,
    TimeInForce,
    Side,
    # Market models
    PriceLevel,
    OrderBookSide,
    OrderBook,
    Market,
    # Order models
    Price,
    OrderRequest,
    Order,
    OrderPreview,
    # Portfolio models
    Position,
    Balance,
    Trade,
    # Response wrappers
    MarketsResponse,
    OrdersResponse,
    PositionsResponse,
    # WebSocket messages
    MarketDataMessage,
    OrderUpdateMessage,
    PositionUpdateMessage,
)
from .orderbook import (
    OrderBookState,
    OrderBookTracker,
    create_orderbook_handler,
)

__all__ = [
    # Enums
    "MarketStatus",
    "OrderType",
    "OrderIntent",
    "OrderStatus",
    "TimeInForce",
    "Side",
    # Market models
    "PriceLevel",
    "OrderBookSide",
    "OrderBook",
    "Market",
    # Order models
    "Price",
    "OrderRequest",
    "Order",
    "OrderPreview",
    # Portfolio models
    "Position",
    "Balance",
    "Trade",
    # Response wrappers
    "MarketsResponse",
    "OrdersResponse",
    "PositionsResponse",
    # WebSocket messages
    "MarketDataMessage",
    "OrderUpdateMessage",
    "PositionUpdateMessage",
    # Order book tracking
    "OrderBookState",
    "OrderBookTracker",
    "create_orderbook_handler",
]
