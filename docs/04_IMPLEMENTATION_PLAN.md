# Implementation Plan

## Overview

This document provides step-by-step instructions for building the Polymarket US trading bot in Cursor. Follow these phases in order.

**Estimated Timeline:** 4-6 weeks to paper trading, +2 weeks to live

---

## Phase 1: Project Setup & Authentication (Days 1-2)

### Step 1.1: Initialize Project

```bash
# Create project directory
mkdir polymarket-bot
cd polymarket-bot

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Initialize git
git init
echo "venv/\n.env\n__pycache__/\n*.pyc\nlogs/\n.credentials" > .gitignore
```

### Step 1.2: Install Dependencies

Create `requirements.txt`:

```
# Core
python-dotenv==1.0.0
pydantic==2.5.0
pydantic-settings==2.1.0

# HTTP & WebSocket
requests==2.31.0
aiohttp==3.9.0
websockets==12.0

# Cryptography (for Ed25519)
cryptography==41.0.0

# Data handling
pandas==2.1.0
numpy==1.26.0

# Async
asyncio-throttle==1.0.2

# Logging
structlog==24.1.0

# Testing
pytest==7.4.0
pytest-asyncio==0.21.0

# Development
black==24.1.0
mypy==1.8.0
```

Install:
```bash
pip install -r requirements.txt
```

### Step 1.3: Create Configuration

Create `config/config.yaml`:

```yaml
api:
  base_url: "https://api.polymarket.us"
  ws_url: "wss://api.polymarket.us/v1/ws"

trading:
  mode: "paper"  # Start with paper trading
  markets:
    - "nba-*"

risk:
  max_position_per_market: 50.00
  max_portfolio_exposure: 250.00
  max_daily_loss: 25.00
  kelly_fraction: 0.25
  min_trade_size: 1.00

strategies:
  market_maker:
    enabled: true
    spread: 0.02
    order_size: 10.00
    
  live_arbitrage:
    enabled: false  # Enable later
    min_edge: 0.03
    
  statistical_edge:
    enabled: false  # Enable later
    min_edge: 0.05

logging:
  level: "INFO"
  file: "logs/bot.log"
```

Create `.env`:

```bash
PM_API_KEY_ID=your-api-key-uuid-here
PM_PRIVATE_KEY=your-base64-private-key-here
```

### Step 1.4: Implement Authentication Module

Create `src/api/auth.py`:

```python
"""
Ed25519 authentication for Polymarket US API.
"""

import time
import base64
from typing import Dict
from cryptography.hazmat.primitives.asymmetric import ed25519


class PolymarketAuth:
    """
    Handles authentication for Polymarket US API requests.
    
    Uses Ed25519 signatures as required by the API.
    """
    
    def __init__(self, api_key_id: str, private_key_base64: str):
        """
        Initialize authentication handler.
        
        Args:
            api_key_id: Your API key UUID from polymarket.us/developer
            private_key_base64: Base64-encoded Ed25519 private key
        """
        self.api_key_id = api_key_id
        
        # Decode and create private key
        private_key_bytes = base64.b64decode(private_key_base64)[:32]
        self.private_key = ed25519.Ed25519PrivateKey.from_private_bytes(
            private_key_bytes
        )
    
    def sign_request(self, method: str, path: str) -> Dict[str, str]:
        """
        Generate authentication headers for an API request.
        
        Args:
            method: HTTP method (GET, POST, etc.)
            path: Request path starting with / (e.g., "/v1/orders")
            
        Returns:
            Dictionary of headers to include in the request
        """
        # Generate timestamp (milliseconds)
        timestamp = str(int(time.time() * 1000))
        
        # Construct message: timestamp + method + path
        message = f"{timestamp}{method}{path}"
        
        # Sign the message
        signature_bytes = self.private_key.sign(message.encode())
        signature = base64.b64encode(signature_bytes).decode()
        
        return {
            "X-PM-Access-Key": self.api_key_id,
            "X-PM-Timestamp": timestamp,
            "X-PM-Signature": signature,
            "Content-Type": "application/json"
        }
    
    def get_ws_headers(self, path: str) -> Dict[str, str]:
        """
        Generate headers for WebSocket connection.
        
        Args:
            path: WebSocket path (e.g., "/v1/ws/markets")
            
        Returns:
            Dictionary of headers for WebSocket handshake
        """
        return self.sign_request("GET", path)
```

### Step 1.5: Create Basic API Client

Create `src/api/client.py`:

```python
"""
REST API client for Polymarket US.
"""

import json
from decimal import Decimal
from typing import Optional, Dict, Any, List
import requests
from pydantic import BaseModel

from .auth import PolymarketAuth


class OrderRequest(BaseModel):
    """Order creation request."""
    market_slug: str
    order_type: str = "ORDER_TYPE_LIMIT"
    price: Optional[Decimal] = None
    quantity: int
    tif: str = "TIME_IN_FORCE_GOOD_TILL_CANCEL"
    intent: str  # ORDER_INTENT_BUY_LONG, etc.


class PolymarketClient:
    """
    REST API client for Polymarket US.
    """
    
    def __init__(self, auth: PolymarketAuth, base_url: str):
        self.auth = auth
        self.base_url = base_url.rstrip("/")
        
    def _request(
        self,
        method: str,
        path: str,
        data: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Make an authenticated API request."""
        url = f"{self.base_url}{path}"
        headers = self.auth.sign_request(method, path)
        
        if method == "GET":
            response = requests.get(url, headers=headers)
        elif method == "POST":
            response = requests.post(url, headers=headers, json=data)
        else:
            raise ValueError(f"Unsupported method: {method}")
        
        response.raise_for_status()
        return response.json()
    
    # === Account ===
    
    def get_balance(self) -> Dict[str, Any]:
        """Get account balance."""
        return self._request("GET", "/v1/account/balance")
    
    # === Portfolio ===
    
    def get_positions(self) -> List[Dict[str, Any]]:
        """Get all positions."""
        result = self._request("GET", "/v1/portfolio/positions")
        return result.get("positions", [])
    
    def get_activity(self) -> List[Dict[str, Any]]:
        """Get account activity."""
        result = self._request("GET", "/v1/portfolio/activity")
        return result.get("activity", [])
    
    # === Orders ===
    
    def create_order(self, order: OrderRequest) -> Dict[str, Any]:
        """
        Create a new order.
        
        Args:
            order: OrderRequest with order details
            
        Returns:
            Order response with orderId and status
        """
        payload = {
            "marketSlug": order.market_slug,
            "type": order.order_type,
            "quantity": order.quantity,
            "tif": order.tif,
            "intent": order.intent,
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC"
        }
        
        if order.price is not None:
            payload["price"] = {
                "value": str(order.price),
                "currency": "USD"
            }
        
        return self._request("POST", "/v1/orders", payload)
    
    def preview_order(self, order: OrderRequest) -> Dict[str, Any]:
        """Preview order before submitting."""
        payload = {
            "marketSlug": order.market_slug,
            "type": order.order_type,
            "quantity": order.quantity,
            "tif": order.tif,
            "intent": order.intent,
        }
        
        if order.price is not None:
            payload["price"] = {
                "value": str(order.price),
                "currency": "USD"
            }
        
        return self._request("POST", "/v1/order/preview", payload)
    
    def get_open_orders(
        self,
        market_slug: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get all open orders."""
        path = "/v1/orders/open"
        if market_slug:
            path += f"?marketSlug={market_slug}"
        result = self._request("GET", path)
        return result.get("orders", [])
    
    def get_order(self, order_id: str) -> Dict[str, Any]:
        """Get order by ID."""
        return self._request("GET", f"/v1/order/{order_id}")
    
    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """Cancel a specific order."""
        return self._request("POST", f"/v1/order/{order_id}/cancel")
    
    def cancel_all_orders(
        self,
        market_slug: Optional[str] = None
    ) -> Dict[str, Any]:
        """Cancel all open orders."""
        path = "/v1/orders/open/cancel"
        if market_slug:
            path += f"?marketSlug={market_slug}"
        return self._request("POST", path)
    
    def close_position(self, market_slug: str) -> Dict[str, Any]:
        """Close entire position in a market."""
        return self._request(
            "POST",
            "/v1/order/close-position",
            {"marketSlug": market_slug}
        )
    
    # === Markets ===
    
    def get_markets(
        self,
        status: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Get list of markets."""
        path = f"/v1/markets?limit={limit}"
        if status:
            path += f"&status={status}"
        if category:
            path += f"&category={category}"
        result = self._request("GET", path)
        return result.get("markets", [])
    
    def get_market(self, market_slug: str) -> Dict[str, Any]:
        """Get market details."""
        return self._request("GET", f"/v1/market/{market_slug}")
    
    def get_market_sides(self, market_slug: str) -> Dict[str, Any]:
        """Get order book for a market."""
        return self._request("GET", f"/v1/market/{market_slug}/sides")
```

### Step 1.6: Test Authentication

Create `tests/test_auth.py`:

```python
"""Test authentication module."""

import os
import pytest
from dotenv import load_dotenv

from src.api.auth import PolymarketAuth
from src.api.client import PolymarketClient

load_dotenv()


@pytest.fixture
def auth():
    return PolymarketAuth(
        api_key_id=os.getenv("PM_API_KEY_ID"),
        private_key_base64=os.getenv("PM_PRIVATE_KEY")
    )


@pytest.fixture
def client(auth):
    return PolymarketClient(
        auth=auth,
        base_url="https://api.polymarket.us"
    )


def test_auth_headers(auth):
    """Test that auth headers are generated correctly."""
    headers = auth.sign_request("GET", "/v1/account/balance")
    
    assert "X-PM-Access-Key" in headers
    assert "X-PM-Timestamp" in headers
    assert "X-PM-Signature" in headers
    assert headers["Content-Type"] == "application/json"


def test_get_balance(client):
    """Test getting account balance."""
    balance = client.get_balance()
    
    assert "availableBalance" in balance
    print(f"Balance: ${balance['availableBalance']}")


def test_get_markets(client):
    """Test getting markets list."""
    markets = client.get_markets(category="NBA", limit=5)
    
    assert isinstance(markets, list)
    print(f"Found {len(markets)} NBA markets")
    for market in markets:
        print(f"  - {market['slug']}: {market.get('title', 'N/A')}")
```

Run tests:
```bash
pytest tests/test_auth.py -v
```

**Checkpoint:** If tests pass, authentication is working. Move to Phase 2.

---

## Phase 2: WebSocket & Data Layer (Days 3-5)

### Step 2.1: Implement WebSocket Client

Create `src/api/websocket.py`:

```python
"""
WebSocket client for Polymarket US real-time data.
"""

import asyncio
import json
from typing import Callable, Optional, List, Dict, Any
import websockets
from websockets.exceptions import ConnectionClosed
import structlog

from .auth import PolymarketAuth

logger = structlog.get_logger()


class PolymarketWebSocket:
    """
    WebSocket client for real-time market data and order updates.
    """
    
    def __init__(
        self,
        auth: PolymarketAuth,
        base_url: str = "wss://api.polymarket.us/v1/ws"
    ):
        self.auth = auth
        self.base_url = base_url
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.handlers: Dict[str, List[Callable]] = {}
        self._running = False
        self._reconnect_delay = 1.0
        
    def on(self, event_type: str, handler: Callable):
        """
        Register a handler for an event type.
        
        Args:
            event_type: Event type (e.g., "MARKET_DATA", "ORDER_UPDATE")
            handler: Async function to call when event received
        """
        if event_type not in self.handlers:
            self.handlers[event_type] = []
        self.handlers[event_type].append(handler)
        
    async def connect(self, endpoint: str = "markets"):
        """
        Connect to WebSocket endpoint.
        
        Args:
            endpoint: "markets" or "private"
        """
        path = f"/v1/ws/{endpoint}"
        url = f"{self.base_url.replace('/v1/ws', '')}{path}"
        
        headers = self.auth.get_ws_headers(path)
        
        logger.info("Connecting to WebSocket", url=url)
        
        self.ws = await websockets.connect(
            url,
            extra_headers=headers,
            ping_interval=30,
            ping_timeout=10
        )
        
        self._running = True
        logger.info("WebSocket connected", endpoint=endpoint)
        
    async def subscribe(
        self,
        subscription_type: str,
        market_slugs: Optional[List[str]] = None,
        request_id: Optional[str] = None
    ):
        """
        Subscribe to a data feed.
        
        Args:
            subscription_type: e.g., "SUBSCRIPTION_TYPE_MARKET_DATA"
            market_slugs: List of markets (for market subscriptions)
            request_id: Optional custom request ID
        """
        if not self.ws:
            raise RuntimeError("Not connected")
        
        if request_id is None:
            request_id = f"sub_{subscription_type}_{len(self.handlers)}"
        
        message = {
            "subscribe": {
                "requestId": request_id,
                "subscriptionType": subscription_type,
            }
        }
        
        if market_slugs:
            message["subscribe"]["marketSlugs"] = market_slugs
        
        await self.ws.send(json.dumps(message))
        logger.info(
            "Subscribed",
            type=subscription_type,
            markets=market_slugs
        )
        
    async def run(self):
        """
        Main loop - process messages and handle reconnection.
        """
        while self._running:
            try:
                async for message in self.ws:
                    await self._handle_message(message)
            except ConnectionClosed as e:
                logger.warning(
                    "WebSocket disconnected",
                    code=e.code,
                    reason=e.reason
                )
                if self._running:
                    await self._reconnect()
            except Exception as e:
                logger.error("WebSocket error", error=str(e))
                if self._running:
                    await self._reconnect()
                    
    async def _handle_message(self, raw_message: str):
        """Process incoming message and dispatch to handlers."""
        try:
            data = json.loads(raw_message)
            event_type = data.get("type", "UNKNOWN")
            
            handlers = self.handlers.get(event_type, [])
            handlers += self.handlers.get("*", [])  # Wildcard handlers
            
            for handler in handlers:
                try:
                    await handler(data)
                except Exception as e:
                    logger.error(
                        "Handler error",
                        event_type=event_type,
                        error=str(e)
                    )
                    
        except json.JSONDecodeError:
            logger.error("Invalid JSON received", message=raw_message[:100])
            
    async def _reconnect(self):
        """Attempt to reconnect with exponential backoff."""
        logger.info(
            "Attempting reconnect",
            delay=self._reconnect_delay
        )
        await asyncio.sleep(self._reconnect_delay)
        
        # Exponential backoff, max 60 seconds
        self._reconnect_delay = min(self._reconnect_delay * 2, 60)
        
        try:
            # Re-establish connection
            # Note: Need to track endpoint and resubscribe
            # This is simplified - real impl should restore state
            pass
        except Exception as e:
            logger.error("Reconnect failed", error=str(e))
            
    async def close(self):
        """Close the WebSocket connection."""
        self._running = False
        if self.ws:
            await self.ws.close()
            logger.info("WebSocket closed")
```

### Step 2.2: Implement State Manager

Create `src/data/state_manager.py`:

```python
"""
Manages current state of markets, positions, and orders.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from datetime import datetime
from typing import Dict, Optional, List
from threading import Lock
import structlog

logger = structlog.get_logger()


@dataclass
class MarketState:
    """Current state of a market."""
    market_slug: str
    yes_bid: Decimal = Decimal("0")
    yes_ask: Decimal = Decimal("1")
    no_bid: Decimal = Decimal("0")
    no_ask: Decimal = Decimal("1")
    yes_bid_size: Decimal = Decimal("0")
    yes_ask_size: Decimal = Decimal("0")
    last_trade_price: Optional[Decimal] = None
    last_trade_time: Optional[datetime] = None
    last_update: datetime = field(default_factory=datetime.utcnow)
    
    @property
    def mid_price(self) -> Decimal:
        """Calculate mid-price."""
        return (self.yes_bid + self.yes_ask) / 2
    
    @property
    def spread(self) -> Decimal:
        """Calculate bid-ask spread."""
        return self.yes_ask - self.yes_bid


@dataclass
class PositionState:
    """Current position in a market."""
    market_slug: str
    side: str  # "YES" or "NO"
    quantity: Decimal
    avg_price: Decimal
    current_price: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    
    @property
    def current_value(self) -> Decimal:
        return self.quantity * self.current_price


@dataclass
class OrderState:
    """Current state of an order."""
    order_id: str
    market_slug: str
    intent: str
    price: Decimal
    quantity: Decimal
    filled_quantity: Decimal = Decimal("0")
    status: str = "PENDING"
    created_at: datetime = field(default_factory=datetime.utcnow)


class StateManager:
    """
    Thread-safe state manager for the trading bot.
    """
    
    def __init__(self):
        self._markets: Dict[str, MarketState] = {}
        self._positions: Dict[str, PositionState] = {}
        self._orders: Dict[str, OrderState] = {}
        self._balance: Decimal = Decimal("0")
        self._lock = Lock()
        
    # === Markets ===
    
    def update_market(
        self,
        market_slug: str,
        yes_bid: Optional[Decimal] = None,
        yes_ask: Optional[Decimal] = None,
        no_bid: Optional[Decimal] = None,
        no_ask: Optional[Decimal] = None,
        yes_bid_size: Optional[Decimal] = None,
        yes_ask_size: Optional[Decimal] = None,
    ):
        """Update market state."""
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
                
            market.last_update = datetime.utcnow()
            
    def get_market(self, market_slug: str) -> Optional[MarketState]:
        """Get market state."""
        with self._lock:
            return self._markets.get(market_slug)
    
    def get_all_markets(self) -> List[MarketState]:
        """Get all market states."""
        with self._lock:
            return list(self._markets.values())
    
    # === Positions ===
    
    def update_position(
        self,
        market_slug: str,
        side: str,
        quantity: Decimal,
        avg_price: Decimal
    ):
        """Update position state."""
        with self._lock:
            self._positions[market_slug] = PositionState(
                market_slug=market_slug,
                side=side,
                quantity=quantity,
                avg_price=avg_price
            )
            
    def get_position(self, market_slug: str) -> Optional[PositionState]:
        """Get position state."""
        with self._lock:
            return self._positions.get(market_slug)
    
    def get_all_positions(self) -> List[PositionState]:
        """Get all positions."""
        with self._lock:
            return list(self._positions.values())
    
    # === Orders ===
    
    def add_order(self, order: OrderState):
        """Add a new order."""
        with self._lock:
            self._orders[order.order_id] = order
            
    def update_order(
        self,
        order_id: str,
        status: Optional[str] = None,
        filled_quantity: Optional[Decimal] = None
    ):
        """Update order state."""
        with self._lock:
            if order_id in self._orders:
                order = self._orders[order_id]
                if status:
                    order.status = status
                if filled_quantity is not None:
                    order.filled_quantity = filled_quantity
                    
    def remove_order(self, order_id: str):
        """Remove an order."""
        with self._lock:
            self._orders.pop(order_id, None)
            
    def get_order(self, order_id: str) -> Optional[OrderState]:
        """Get order state."""
        with self._lock:
            return self._orders.get(order_id)
    
    def get_open_orders(
        self,
        market_slug: Optional[str] = None
    ) -> List[OrderState]:
        """Get all open orders."""
        with self._lock:
            orders = [
                o for o in self._orders.values()
                if o.status in ("PENDING", "OPEN", "PARTIALLY_FILLED")
            ]
            if market_slug:
                orders = [o for o in orders if o.market_slug == market_slug]
            return orders
    
    # === Balance ===
    
    def update_balance(self, balance: Decimal):
        """Update account balance."""
        with self._lock:
            self._balance = balance
            
    def get_balance(self) -> Decimal:
        """Get account balance."""
        with self._lock:
            return self._balance
```

### Step 2.3: Create Event Bus

Create `src/data/event_bus.py`:

```python
"""
Simple async event bus for internal communication.
"""

import asyncio
from typing import Callable, Dict, List, Any
import structlog

logger = structlog.get_logger()


class EventBus:
    """
    Async event bus for publishing and subscribing to events.
    """
    
    def __init__(self):
        self._subscribers: Dict[str, List[Callable]] = {}
        self._queue: asyncio.Queue = asyncio.Queue()
        self._running = False
        
    def subscribe(self, event_type: str, handler: Callable):
        """
        Subscribe to an event type.
        
        Args:
            event_type: Event type to subscribe to
            handler: Async function to call when event published
        """
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(handler)
        logger.debug("Subscribed to event", event_type=event_type)
        
    async def publish(self, event_type: str, data: Any):
        """
        Publish an event.
        
        Args:
            event_type: Event type
            data: Event data
        """
        await self._queue.put((event_type, data))
        
    async def run(self):
        """
        Main loop - process events from queue.
        """
        self._running = True
        
        while self._running:
            try:
                event_type, data = await asyncio.wait_for(
                    self._queue.get(),
                    timeout=1.0
                )
                
                handlers = self._subscribers.get(event_type, [])
                
                for handler in handlers:
                    try:
                        await handler(data)
                    except Exception as e:
                        logger.error(
                            "Event handler error",
                            event_type=event_type,
                            error=str(e)
                        )
                        
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error("Event bus error", error=str(e))
                
    def stop(self):
        """Stop the event bus."""
        self._running = False
```

**Checkpoint:** Phase 2 complete. Run integration tests to verify WebSocket connectivity.

---

## Phase 3: Paper Trading Module (Days 6-8)

### Step 3.1: Implement Paper Executor

Create `src/paper/paper_executor.py`:

```python
"""
Paper trading executor - simulates order execution without real money.
"""

from decimal import Decimal
from datetime import datetime
from typing import Optional, Dict, Any
import uuid
import structlog

from src.data.state_manager import StateManager, OrderState, PositionState
from src.api.client import OrderRequest

logger = structlog.get_logger()


class PaperExecutor:
    """
    Simulates order execution for paper trading.
    """
    
    def __init__(self, state: StateManager, initial_balance: Decimal):
        self.state = state
        self.state.update_balance(initial_balance)
        self.trades: list = []
        self.fees_paid = Decimal("0")
        
    def execute_order(self, order: OrderRequest) -> Dict[str, Any]:
        """
        Simulate order execution.
        
        Args:
            order: Order to execute
            
        Returns:
            Simulated order response
        """
        order_id = str(uuid.uuid4())
        market = self.state.get_market(order.market_slug)
        
        if not market:
            return {"error": "Market not found"}
        
        # Determine fill price based on intent
        if order.intent == "ORDER_INTENT_BUY_LONG":
            fill_price = market.yes_ask
            side = "YES"
        elif order.intent == "ORDER_INTENT_BUY_SHORT":
            fill_price = market.no_ask
            side = "NO"
        elif order.intent == "ORDER_INTENT_SELL_LONG":
            fill_price = market.yes_bid
            side = "YES"
        elif order.intent == "ORDER_INTENT_SELL_SHORT":
            fill_price = market.no_bid
            side = "NO"
        else:
            return {"error": f"Unknown intent: {order.intent}"}
        
        # Check if limit order would fill
        if order.order_type == "ORDER_TYPE_LIMIT" and order.price:
            if "BUY" in order.intent:
                if order.price < fill_price:
                    # Order rests on book
                    return self._create_resting_order(order, order_id, side)
            else:
                if order.price > fill_price:
                    return self._create_resting_order(order, order_id, side)
        
        # Calculate cost and fees
        quantity = Decimal(str(order.quantity))
        cost = fill_price * quantity
        fee = cost * Decimal("0.001")  # 0.1% taker fee
        
        # Check balance for buys
        if "BUY" in order.intent:
            if cost + fee > self.state.get_balance():
                return {"error": "Insufficient balance"}
            self.state.update_balance(self.state.get_balance() - cost - fee)
        else:
            # Sells: add proceeds minus fee
            self.state.update_balance(self.state.get_balance() + cost - fee)
        
        self.fees_paid += fee
        
        # Update position
        self._update_position(order.market_slug, side, quantity, fill_price, order.intent)
        
        # Record trade
        trade = {
            "order_id": order_id,
            "market_slug": order.market_slug,
            "intent": order.intent,
            "side": side,
            "quantity": quantity,
            "price": fill_price,
            "cost": cost,
            "fee": fee,
            "timestamp": datetime.utcnow().isoformat()
        }
        self.trades.append(trade)
        
        logger.info(
            "Paper trade executed",
            **trade
        )
        
        return {
            "orderId": order_id,
            "status": "FILLED",
            "filledQuantity": int(quantity),
            "avgFillPrice": str(fill_price),
            "fee": str(fee)
        }
    
    def _create_resting_order(
        self,
        order: OrderRequest,
        order_id: str,
        side: str
    ) -> Dict[str, Any]:
        """Create a resting limit order."""
        order_state = OrderState(
            order_id=order_id,
            market_slug=order.market_slug,
            intent=order.intent,
            price=order.price,
            quantity=Decimal(str(order.quantity)),
            status="OPEN"
        )
        self.state.add_order(order_state)
        
        return {
            "orderId": order_id,
            "status": "OPEN",
            "filledQuantity": 0
        }
    
    def _update_position(
        self,
        market_slug: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        intent: str
    ):
        """Update position after trade."""
        current = self.state.get_position(market_slug)
        
        if "BUY" in intent:
            if current and current.side == side:
                # Adding to position
                new_qty = current.quantity + quantity
                new_avg = (
                    (current.avg_price * current.quantity + price * quantity)
                    / new_qty
                )
                self.state.update_position(market_slug, side, new_qty, new_avg)
            else:
                # New position
                self.state.update_position(market_slug, side, quantity, price)
        else:
            # Selling
            if current:
                new_qty = current.quantity - quantity
                if new_qty <= 0:
                    # Position closed
                    self.state._positions.pop(market_slug, None)
                else:
                    self.state.update_position(
                        market_slug, side, new_qty, current.avg_price
                    )
    
    def get_performance(self) -> Dict[str, Any]:
        """Get paper trading performance summary."""
        balance = self.state.get_balance()
        positions = self.state.get_all_positions()
        
        # Calculate position values
        position_value = Decimal("0")
        for pos in positions:
            market = self.state.get_market(pos.market_slug)
            if market:
                price = market.yes_bid if pos.side == "YES" else market.no_bid
                position_value += pos.quantity * price
        
        total_value = balance + position_value
        
        return {
            "cash_balance": float(balance),
            "position_value": float(position_value),
            "total_value": float(total_value),
            "fees_paid": float(self.fees_paid),
            "total_trades": len(self.trades),
            "open_positions": len(positions)
        }
```

Continue implementation in later phases...

---

## Phase 4: Strategy Implementation (Days 9-14)

See `02_STRATEGY.md` for detailed strategy code. Implement in this order:

1. **Market Maker** (simplest, test infrastructure)
2. **Statistical Edge** (next simplest)
3. **Live Arbitrage** (requires sports data feed)

---

## Phase 5: Risk Management (Days 15-17)

See `06_RISK_MANAGEMENT.md` for implementation details.

---

## Phase 6: Integration & Testing (Days 18-21)

1. Integration tests for all components
2. End-to-end paper trading test (24+ hours)
3. Performance validation against targets
4. Bug fixes and optimization

---

## Phase 7: Production Deployment (Days 22-28)

See `05_INFRASTRUCTURE.md` for AWS deployment instructions.

---

## Cursor Tips

### Useful Prompts

**For implementing a new component:**
```
Implement the [component name] according to the specification in 
[relevant doc]. Follow the patterns established in [existing file].
Include comprehensive error handling and logging.
```

**For debugging:**
```
This error occurs when [describe situation]. The relevant code is in 
[file]. Help me understand why and fix it.
```

**For optimization:**
```
This function [paste code] is a hot path. Profile it and suggest 
performance improvements while maintaining readability.
```

### Code Style

- Use type hints everywhere
- Use dataclasses for data structures
- Use Decimal for all money/price values
- Async/await for all I/O operations
- Structured logging with structlog
- Tests for all business logic
