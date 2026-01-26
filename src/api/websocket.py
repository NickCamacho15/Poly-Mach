"""
WebSocket client for Polymarket US real-time data.

This module provides an async WebSocket client with auto-reconnection,
handler dispatch, and subscription management for market data and private updates.
"""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set

import structlog
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatusCode

from .auth import PolymarketAuth

logger = structlog.get_logger()


# =============================================================================
# Enums
# =============================================================================

class SubscriptionType(str, Enum):
    """WebSocket subscription types."""
    # Market subscriptions (markets endpoint)
    MARKET_DATA = "SUBSCRIPTION_TYPE_MARKET_DATA"
    MARKET_DATA_LITE = "SUBSCRIPTION_TYPE_MARKET_DATA_LITE"
    TRADE = "SUBSCRIPTION_TYPE_TRADE"
    
    # Private subscriptions (private endpoint)
    ORDER = "SUBSCRIPTION_TYPE_ORDER"
    POSITION = "SUBSCRIPTION_TYPE_POSITION"
    ACCOUNT_BALANCE = "SUBSCRIPTION_TYPE_ACCOUNT_BALANCE"


class ConnectionState(str, Enum):
    """WebSocket connection states."""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"


class Endpoint(str, Enum):
    """WebSocket endpoint types."""
    MARKETS = "markets"
    PRIVATE = "private"


# =============================================================================
# Exceptions
# =============================================================================

class WebSocketError(Exception):
    """Base exception for WebSocket errors."""
    pass


class ConnectionError(WebSocketError):
    """Raised when connection fails."""
    pass


class SubscriptionError(WebSocketError):
    """Raised when subscription fails."""
    pass


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class Subscription:
    """Tracks an active subscription."""
    request_id: str
    subscription_type: SubscriptionType
    market_slugs: List[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# Type alias for message handlers
MessageHandler = Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]


# =============================================================================
# WebSocket Client
# =============================================================================

class PolymarketWebSocket:
    """
    Async WebSocket client for Polymarket US real-time data.
    
    Features:
    - Connects to markets and private WebSocket endpoints
    - Event handler registration with `on()` pattern
    - Auto-reconnect with exponential backoff
    - Subscription management with automatic resubscription
    
    Example:
        >>> auth = PolymarketAuth(api_key_id, private_key)
        >>> ws = PolymarketWebSocket(auth)
        >>> 
        >>> async def handle_market_data(data: dict):
        ...     print(f"Market update: {data}")
        >>> 
        >>> ws.on("MARKET_DATA", handle_market_data)
        >>> 
        >>> async with ws:
        ...     await ws.connect(Endpoint.MARKETS)
        ...     await ws.subscribe(
        ...         SubscriptionType.MARKET_DATA,
        ...         ["nba-lakers-vs-celtics-2025-01-25"]
        ...     )
        ...     await ws.run()
    """
    
    DEFAULT_BASE_URL = "wss://api.polymarket.us/v1/ws"
    PING_INTERVAL = 30
    PING_TIMEOUT = 10
    MAX_RECONNECT_DELAY = 60.0
    INITIAL_RECONNECT_DELAY = 1.0
    
    def __init__(
        self,
        auth: PolymarketAuth,
        base_url: str = DEFAULT_BASE_URL,
    ):
        """
        Initialize WebSocket client.
        
        Args:
            auth: PolymarketAuth instance for request signing
            base_url: WebSocket base URL
        """
        self.auth = auth
        self.base_url = base_url.rstrip("/")
        
        # Connection state
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._state = ConnectionState.DISCONNECTED
        self._endpoint: Optional[Endpoint] = None
        self._running = False
        
        # Reconnection
        self._reconnect_delay = self.INITIAL_RECONNECT_DELAY
        self._reconnect_task: Optional[asyncio.Task] = None
        
        # Subscriptions
        self._subscriptions: Dict[str, Subscription] = {}
        self._subscription_counter = 0
        
        # Event handlers
        self._handlers: Dict[str, List[MessageHandler]] = {}
        
        # Message processing
        self._message_queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
    
    # =========================================================================
    # Context Manager
    # =========================================================================
    
    async def __aenter__(self) -> "PolymarketWebSocket":
        """Async context manager entry."""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        await self.disconnect()
    
    # =========================================================================
    # Properties
    # =========================================================================
    
    @property
    def state(self) -> ConnectionState:
        """Current connection state."""
        return self._state
    
    @property
    def is_connected(self) -> bool:
        """Check if WebSocket is connected."""
        return self._state == ConnectionState.CONNECTED and self._ws is not None
    
    @property
    def subscriptions(self) -> Dict[str, Subscription]:
        """Get active subscriptions (copy)."""
        return dict(self._subscriptions)
    
    # =========================================================================
    # Connection Management
    # =========================================================================
    
    async def connect(self, endpoint: Endpoint = Endpoint.MARKETS) -> None:
        """
        Connect to WebSocket endpoint.
        
        Args:
            endpoint: Which endpoint to connect to (markets or private)
            
        Raises:
            ConnectionError: If connection fails
        """
        if self._state in (ConnectionState.CONNECTED, ConnectionState.CONNECTING):
            logger.warning("Already connected or connecting")
            return
        
        self._state = ConnectionState.CONNECTING
        self._endpoint = endpoint
        
        path = f"/v1/ws/{endpoint.value}"
        url = f"{self.base_url.replace('/v1/ws', '')}{path}"
        
        try:
            # Get auth headers
            headers = self.auth.get_ws_headers(path)
            
            logger.info(
                "Connecting to WebSocket",
                url=url,
                endpoint=endpoint.value,
            )
            
            self._ws = await websockets.connect(
                url,
                extra_headers=headers,
                ping_interval=self.PING_INTERVAL,
                ping_timeout=self.PING_TIMEOUT,
            )
            
            self._state = ConnectionState.CONNECTED
            self._reconnect_delay = self.INITIAL_RECONNECT_DELAY
            
            logger.info(
                "WebSocket connected",
                endpoint=endpoint.value,
            )
            
        except InvalidStatusCode as e:
            self._state = ConnectionState.DISCONNECTED
            logger.error(
                "WebSocket connection rejected",
                status_code=e.status_code,
            )
            raise ConnectionError(f"Connection rejected with status {e.status_code}")
        except Exception as e:
            self._state = ConnectionState.DISCONNECTED
            logger.error(
                "WebSocket connection failed",
                error=str(e),
            )
            raise ConnectionError(f"Connection failed: {e}")
    
    async def disconnect(self) -> None:
        """
        Disconnect from WebSocket.
        """
        self._running = False
        
        # Cancel reconnect task if running
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
            self._reconnect_task = None
        
        # Close WebSocket
        if self._ws:
            try:
                await self._ws.close()
            except Exception as e:
                logger.debug("Error closing WebSocket", error=str(e))
            self._ws = None
        
        self._state = ConnectionState.DISCONNECTED
        self._subscriptions.clear()
        
        logger.info("WebSocket disconnected")
    
    async def _reconnect(self) -> None:
        """
        Attempt to reconnect with exponential backoff.
        """
        if not self._endpoint:
            logger.error("Cannot reconnect: no endpoint set")
            return
        
        self._state = ConnectionState.RECONNECTING
        
        while self._running:
            logger.info(
                "Attempting reconnect",
                delay=self._reconnect_delay,
                endpoint=self._endpoint.value,
            )
            
            await asyncio.sleep(self._reconnect_delay)
            
            try:
                await self.connect(self._endpoint)
                
                # Resubscribe to all previous subscriptions
                await self._resubscribe()
                
                logger.info("Reconnection successful")
                return
                
            except ConnectionError as e:
                logger.warning(
                    "Reconnection failed",
                    error=str(e),
                    next_delay=min(self._reconnect_delay * 2, self.MAX_RECONNECT_DELAY),
                )
                
                # Exponential backoff
                self._reconnect_delay = min(
                    self._reconnect_delay * 2,
                    self.MAX_RECONNECT_DELAY,
                )
    
    async def _resubscribe(self) -> None:
        """
        Resubscribe to all previous subscriptions after reconnect.
        """
        # Copy subscriptions to avoid modification during iteration
        subs_to_restore = list(self._subscriptions.values())
        self._subscriptions.clear()
        
        for sub in subs_to_restore:
            try:
                await self.subscribe(
                    sub.subscription_type,
                    sub.market_slugs,
                    request_id=sub.request_id,
                )
                logger.debug(
                    "Resubscribed",
                    request_id=sub.request_id,
                    type=sub.subscription_type.value,
                )
            except Exception as e:
                logger.error(
                    "Failed to resubscribe",
                    request_id=sub.request_id,
                    error=str(e),
                )
    
    # =========================================================================
    # Subscriptions
    # =========================================================================
    
    async def subscribe(
        self,
        subscription_type: SubscriptionType,
        market_slugs: Optional[List[str]] = None,
        request_id: Optional[str] = None,
    ) -> str:
        """
        Subscribe to a data feed.
        
        Args:
            subscription_type: Type of subscription
            market_slugs: List of market slugs (for market subscriptions)
            request_id: Optional custom request ID
            
        Returns:
            The request ID for this subscription
            
        Raises:
            SubscriptionError: If not connected or send fails
        """
        if not self.is_connected:
            raise SubscriptionError("Not connected")
        
        if request_id is None:
            self._subscription_counter += 1
            request_id = f"sub_{subscription_type.name.lower()}_{self._subscription_counter}"
        
        message = {
            "subscribe": {
                "requestId": request_id,
                "subscriptionType": subscription_type.value,
            }
        }
        
        if market_slugs:
            message["subscribe"]["marketSlugs"] = market_slugs
        
        try:
            await self._ws.send(json.dumps(message))
            
            # Track subscription
            self._subscriptions[request_id] = Subscription(
                request_id=request_id,
                subscription_type=subscription_type,
                market_slugs=market_slugs or [],
            )
            
            logger.info(
                "Subscribed",
                request_id=request_id,
                type=subscription_type.value,
                markets=market_slugs,
            )
            
            return request_id
            
        except Exception as e:
            logger.error(
                "Subscribe failed",
                error=str(e),
                type=subscription_type.value,
            )
            raise SubscriptionError(f"Subscribe failed: {e}")
    
    async def unsubscribe(self, request_id: str) -> None:
        """
        Unsubscribe from a data feed.
        
        Args:
            request_id: The subscription request ID to cancel
        """
        if not self.is_connected:
            logger.warning("Cannot unsubscribe: not connected")
            return
        
        if request_id not in self._subscriptions:
            logger.warning("Unknown subscription", request_id=request_id)
            return
        
        message = {
            "unsubscribe": {
                "requestId": request_id,
            }
        }
        
        try:
            await self._ws.send(json.dumps(message))
            del self._subscriptions[request_id]
            
            logger.info("Unsubscribed", request_id=request_id)
            
        except Exception as e:
            logger.error(
                "Unsubscribe failed",
                request_id=request_id,
                error=str(e),
            )
    
    # =========================================================================
    # Event Handlers
    # =========================================================================
    
    def on(self, event_type: str, handler: MessageHandler) -> None:
        """
        Register a handler for an event type.
        
        Args:
            event_type: Event type to handle (e.g., "MARKET_DATA", "ORDER_UPDATE")
                       Use "*" for a wildcard handler that receives all messages
            handler: Async function to call when event is received
        """
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        
        if handler not in self._handlers[event_type]:
            self._handlers[event_type].append(handler)
            logger.debug("Handler registered", event_type=event_type)
    
    def off(self, event_type: str, handler: MessageHandler) -> None:
        """
        Remove a handler for an event type.
        
        Args:
            event_type: Event type
            handler: Handler function to remove
        """
        if event_type in self._handlers:
            try:
                self._handlers[event_type].remove(handler)
                logger.debug("Handler removed", event_type=event_type)
            except ValueError:
                pass
    
    def clear_handlers(self, event_type: Optional[str] = None) -> None:
        """
        Clear handlers for an event type or all handlers.
        
        Args:
            event_type: Specific event type to clear, or None for all
        """
        if event_type:
            self._handlers.pop(event_type, None)
        else:
            self._handlers.clear()
    
    # =========================================================================
    # Message Processing
    # =========================================================================
    
    async def run(self) -> None:
        """
        Main loop - process incoming messages.
        
        This method blocks until disconnect() is called or connection is lost.
        On connection loss, it will attempt to reconnect automatically.
        """
        if not self.is_connected:
            raise WebSocketError("Not connected")
        
        self._running = True
        
        while self._running:
            try:
                async for raw_message in self._ws:
                    if not self._running:
                        break
                    await self._handle_message(raw_message)
                    
            except ConnectionClosed as e:
                logger.warning(
                    "WebSocket connection closed",
                    code=e.code,
                    reason=e.reason,
                )
                
                if self._running:
                    # Start reconnection
                    self._reconnect_task = asyncio.create_task(self._reconnect())
                    await self._reconnect_task
                    
            except Exception as e:
                logger.error("WebSocket error", error=str(e))
                
                if self._running:
                    self._reconnect_task = asyncio.create_task(self._reconnect())
                    await self._reconnect_task
    
    async def _handle_message(self, raw_message: str) -> None:
        """
        Parse and dispatch a message to handlers.
        
        Args:
            raw_message: Raw JSON message string
        """
        try:
            data = json.loads(raw_message)
        except json.JSONDecodeError:
            logger.error("Invalid JSON received", message=raw_message[:100])
            return
        
        event_type = data.get("type", "UNKNOWN")
        
        # Get handlers for this event type
        handlers = list(self._handlers.get(event_type, []))
        
        # Add wildcard handlers
        handlers.extend(self._handlers.get("*", []))
        
        if not handlers:
            logger.debug("No handlers for event", event_type=event_type)
            return
        
        # Dispatch to all handlers
        for handler in handlers:
            try:
                await handler(data)
            except Exception as e:
                logger.error(
                    "Handler error",
                    event_type=event_type,
                    handler=handler.__name__,
                    error=str(e),
                )
    
    # =========================================================================
    # Utilities
    # =========================================================================
    
    async def send_raw(self, message: Dict[str, Any]) -> None:
        """
        Send a raw message (for advanced use).
        
        Args:
            message: Message dict to send as JSON
        """
        if not self.is_connected:
            raise WebSocketError("Not connected")
        
        await self._ws.send(json.dumps(message))
    
    def get_subscription(self, request_id: str) -> Optional[Subscription]:
        """
        Get subscription details by request ID.
        
        Args:
            request_id: The subscription request ID
            
        Returns:
            Subscription if found, None otherwise
        """
        return self._subscriptions.get(request_id)
