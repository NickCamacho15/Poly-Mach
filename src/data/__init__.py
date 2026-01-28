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
from .market_discovery import (
    League,
    MarketProduct,
    SportsMarketType,
    SportsMarket,
    GameMarkets,
    MarketDiscovery,
    get_basketball_slugs,
    get_nba_slugs,
    get_cbb_slugs,
)
from .event_bus import EventBus, EVENT_GAME_STATE, EVENT_ODDS_SNAPSHOT
from .sports_feed import GameState, GameStatus, SportsFeed, MockSportsFeed
from .odds_feed import OddsFeed, OddsSnapshot, MockOddsFeed

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
    # Market discovery
    "League",
    "MarketProduct",
    "SportsMarketType",
    "SportsMarket",
    "GameMarkets",
    "MarketDiscovery",
    "get_basketball_slugs",
    "get_nba_slugs",
    "get_cbb_slugs",
    # Event bus
    "EventBus",
    "EVENT_GAME_STATE",
    "EVENT_ODDS_SNAPSHOT",
    # Sports feed
    "GameState",
    "GameStatus",
    "SportsFeed",
    "MockSportsFeed",
    # Odds feed
    "OddsFeed",
    "OddsSnapshot",
    "MockOddsFeed",
]
