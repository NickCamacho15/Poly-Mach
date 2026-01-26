"""
Polymarket US API client package.

This package provides authentication, REST client, and WebSocket client
for the Polymarket US API.
"""

from .auth import PolymarketAuth, AuthenticationError
from .client import (
    PolymarketClient,
    APIError,
    RateLimitError,
    InsufficientBalanceError,
    MarketClosedError,
    InvalidOrderError,
)
from .websocket import (
    PolymarketWebSocket,
    SubscriptionType,
    ConnectionState,
    Endpoint,
    Subscription,
    WebSocketError,
    ConnectionError,
    SubscriptionError,
)

__all__ = [
    # Auth
    "PolymarketAuth",
    "AuthenticationError",
    # Client
    "PolymarketClient",
    "APIError",
    "RateLimitError",
    "InsufficientBalanceError",
    "MarketClosedError",
    "InvalidOrderError",
    # WebSocket
    "PolymarketWebSocket",
    "SubscriptionType",
    "ConnectionState",
    "Endpoint",
    "Subscription",
    "WebSocketError",
    "ConnectionError",
    "SubscriptionError",
]
