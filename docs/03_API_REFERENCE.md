# Polymarket US API Reference

## Overview

| Property | Value |
|----------|-------|
| Base URL | `https://api.polymarket.us` |
| WebSocket URL | `wss://api.polymarket.us/v1/ws` |
| Authentication | Ed25519 signatures |
| Rate Limits | 5 WS connections, 100 markets/subscription |

---

## Authentication

### Required Headers

All authenticated requests must include these headers:

| Header | Description |
|--------|-------------|
| `X-PM-Access-Key` | Your API key ID (UUID format) |
| `X-PM-Timestamp` | Unix timestamp in milliseconds |
| `X-PM-Signature` | Base64-encoded Ed25519 signature |
| `Content-Type` | `application/json` (for POST/PUT requests) |

### Signature Construction

```
message = timestamp + method + path
signature = Ed25519.sign(private_key, message)
```

**Example:**
```
timestamp = "1705420800000"
method = "GET"
path = "/v1/portfolio/positions"
message = "1705420800000GET/v1/portfolio/positions"
```

**Important:** Timestamps must be within 30 seconds of server time.

### Python Authentication Implementation

```python
import time
import base64
import requests
from cryptography.hazmat.primitives.asymmetric import ed25519

class PolymarketAuth:
    """
    Handles Ed25519 authentication for Polymarket US API.
    """
    
    def __init__(self, api_key_id: str, private_key_base64: str):
        self.api_key_id = api_key_id
        self.private_key = ed25519.Ed25519PrivateKey.from_private_bytes(
            base64.b64decode(private_key_base64)[:32]
        )
    
    def sign_request(self, method: str, path: str) -> dict:
        """
        Generate authentication headers for a request.
        
        Args:
            method: HTTP method (GET, POST, etc.)
            path: Request path starting with / (e.g., "/v1/orders")
            
        Returns:
            Dictionary of headers to include in request
        """
        timestamp = str(int(time.time() * 1000))
        message = f"{timestamp}{method}{path}"
        
        signature = base64.b64encode(
            self.private_key.sign(message.encode())
        ).decode()
        
        return {
            "X-PM-Access-Key": self.api_key_id,
            "X-PM-Timestamp": timestamp,
            "X-PM-Signature": signature,
            "Content-Type": "application/json"
        }


# Usage example
auth = PolymarketAuth(
    api_key_id="your-api-key-uuid",
    private_key_base64="your-base64-private-key"
)

headers = auth.sign_request("GET", "/v1/portfolio/positions")
response = requests.get(
    "https://api.polymarket.us/v1/portfolio/positions",
    headers=headers
)
```

---

## Orders API

### Create Order

**POST** `/v1/orders`

Creates a new order.

**Request Body:**

```json
{
  "marketSlug": "super-bowl-lix-chiefs-vs-eagles",
  "type": "ORDER_TYPE_LIMIT",
  "price": {
    "value": "0.55",
    "currency": "USD"
  },
  "quantity": 100,
  "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
  "intent": "ORDER_INTENT_BUY_LONG",
  "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC"
}
```

**Fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `marketSlug` | string | Yes | Market identifier |
| `type` | enum | Yes | `ORDER_TYPE_LIMIT` or `ORDER_TYPE_MARKET` |
| `price` | object | Limit only | Price object with value and currency |
| `price.value` | string | Limit only | Price as decimal string (e.g., "0.55") |
| `price.currency` | string | Limit only | Always "USD" |
| `quantity` | integer | Yes | Number of contracts |
| `tif` | enum | Yes | Time in force |
| `intent` | enum | Yes | Order intent (direction) |
| `manualOrderIndicator` | enum | Yes | Always `MANUAL_ORDER_INDICATOR_AUTOMATIC` for bots |

**Order Intents:**

| Intent | Description |
|--------|-------------|
| `ORDER_INTENT_BUY_LONG` | Buy YES shares |
| `ORDER_INTENT_SELL_LONG` | Sell YES shares |
| `ORDER_INTENT_BUY_SHORT` | Buy NO shares |
| `ORDER_INTENT_SELL_SHORT` | Sell NO shares |

**Time in Force:**

| TIF | Description |
|-----|-------------|
| `TIME_IN_FORCE_GOOD_TILL_CANCEL` | Order remains until filled or cancelled |
| `TIME_IN_FORCE_GOOD_TILL_DATE` | Order expires at specified date |
| `TIME_IN_FORCE_IMMEDIATE_OR_CANCEL` | Fill immediately or cancel |
| `TIME_IN_FORCE_FILL_OR_KILL` | Fill entirely or cancel (no partial) |

**Response:**

```json
{
  "orderId": "order-uuid-here",
  "status": "PENDING",
  "marketSlug": "super-bowl-lix-chiefs-vs-eagles",
  "intent": "ORDER_INTENT_BUY_LONG",
  "price": "0.55",
  "quantity": 100,
  "filledQuantity": 0,
  "remainingQuantity": 100,
  "createdAt": "2025-01-25T12:00:00Z"
}
```

---

### Preview Order

**POST** `/v1/order/preview`

Preview an order before submitting (shows estimated fill, fees, etc.)

**Request Body:** Same as Create Order

**Response:**

```json
{
  "estimatedFill": {
    "price": "0.55",
    "quantity": 100,
    "cost": "55.00"
  },
  "estimatedFee": "0.055",
  "estimatedTotal": "55.055"
}
```

---

### Get Open Orders

**GET** `/v1/orders/open`

Returns all open orders for the account.

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `marketSlug` | string | No | Filter by market |

**Response:**

```json
{
  "orders": [
    {
      "orderId": "order-uuid-1",
      "marketSlug": "nba-lakers-vs-celtics-2025-01-25",
      "intent": "ORDER_INTENT_BUY_LONG",
      "price": "0.48",
      "quantity": 50,
      "filledQuantity": 0,
      "status": "OPEN",
      "createdAt": "2025-01-25T10:00:00Z"
    }
  ]
}
```

---

### Get Order by ID

**GET** `/v1/order/{orderId}`

Returns details of a specific order.

**Response:**

```json
{
  "orderId": "order-uuid-here",
  "marketSlug": "nba-lakers-vs-celtics-2025-01-25",
  "intent": "ORDER_INTENT_BUY_LONG",
  "type": "ORDER_TYPE_LIMIT",
  "price": "0.48",
  "quantity": 50,
  "filledQuantity": 25,
  "remainingQuantity": 25,
  "avgFillPrice": "0.47",
  "status": "PARTIALLY_FILLED",
  "createdAt": "2025-01-25T10:00:00Z",
  "updatedAt": "2025-01-25T10:05:00Z"
}
```

---

### Modify Order

**POST** `/v1/order/{orderId}/modify`

Modify an existing order's price or quantity.

**Request Body:**

```json
{
  "price": {
    "value": "0.50",
    "currency": "USD"
  },
  "quantity": 75
}
```

---

### Cancel Order

**POST** `/v1/order/{orderId}/cancel`

Cancel a specific order.

**Response:**

```json
{
  "orderId": "order-uuid-here",
  "status": "CANCELLED"
}
```

---

### Cancel All Orders

**POST** `/v1/orders/open/cancel`

Cancel all open orders.

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `marketSlug` | string | No | Cancel only orders in this market |

---

### Close Position

**POST** `/v1/order/close-position`

Close an entire position in a market.

**Request Body:**

```json
{
  "marketSlug": "nba-lakers-vs-celtics-2025-01-25"
}
```

---

## Portfolio API

### Get Positions

**GET** `/v1/portfolio/positions`

Returns all current positions.

**Response:**

```json
{
  "positions": [
    {
      "marketSlug": "nba-lakers-vs-celtics-2025-01-25",
      "side": "YES",
      "quantity": 100,
      "avgPrice": "0.45",
      "currentPrice": "0.52",
      "currentValue": "52.00",
      "unrealizedPnl": "7.00",
      "unrealizedPnlPercent": "15.56"
    }
  ]
}
```

---

### Get Activity

**GET** `/v1/portfolio/activity`

Returns recent account activity (trades, deposits, etc.)

---

## Account API

### Get Balance

**GET** `/v1/account/balance`

Returns current account balance.

**Response:**

```json
{
  "availableBalance": "1000.00",
  "totalBalance": "1245.00",
  "currency": "USD"
}
```

---

## Markets API

### Get Markets

**GET** `/v1/markets`

Returns list of available markets.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `status` | string | Filter by status: `OPEN`, `CLOSED`, `RESOLVED` |
| `category` | string | Filter by category: `NBA`, `NFL`, etc. |
| `limit` | integer | Max results (default 100) |
| `offset` | integer | Pagination offset |

**Response:**

```json
{
  "markets": [
    {
      "slug": "nba-lakers-vs-celtics-2025-01-25",
      "title": "Lakers vs Celtics - January 25, 2025",
      "description": "Will the Lakers beat the Celtics?",
      "status": "OPEN",
      "category": "NBA",
      "resolutionDate": "2025-01-26T05:00:00Z",
      "yesBid": "0.47",
      "yesAsk": "0.49",
      "noBid": "0.51",
      "noAsk": "0.53",
      "volume24h": "15000.00"
    }
  ]
}
```

---

### Get Market Details

**GET** `/v1/market/{marketSlug}`

Returns detailed information about a specific market.

---

### Get Market Sides

**GET** `/v1/market/{marketSlug}/sides`

Returns order book data for a market.

**Response:**

```json
{
  "marketSlug": "nba-lakers-vs-celtics-2025-01-25",
  "yes": {
    "bids": [
      {"price": "0.47", "quantity": 500},
      {"price": "0.46", "quantity": 1000}
    ],
    "asks": [
      {"price": "0.49", "quantity": 300},
      {"price": "0.50", "quantity": 800}
    ]
  },
  "no": {
    "bids": [
      {"price": "0.51", "quantity": 400},
      {"price": "0.50", "quantity": 600}
    ],
    "asks": [
      {"price": "0.53", "quantity": 350},
      {"price": "0.54", "quantity": 700}
    ]
  }
}
```

---

## WebSocket API

### Connection

**Endpoints:**
- Private (orders, positions, balance): `wss://api.polymarket.us/v1/ws/private`
- Markets (order book, trades): `wss://api.polymarket.us/v1/ws/markets`

### Authentication

Include the same authentication headers in the WebSocket handshake.

**Signature for WebSocket:**
```
message = timestamp + "GET" + path
```

Where `path` is `/v1/ws/private` or `/v1/ws/markets`

### Subscribe Message Format

```json
{
  "subscribe": {
    "requestId": "unique-request-id",
    "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
    "marketSlugs": ["nba-lakers-vs-celtics-2025-01-25"]
  }
}
```

### Private Subscription Types

| Type | Description |
|------|-------------|
| `SUBSCRIPTION_TYPE_ORDER` | Order status updates |
| `SUBSCRIPTION_TYPE_POSITION` | Position changes |
| `SUBSCRIPTION_TYPE_ACCOUNT_BALANCE` | Balance updates |

### Market Subscription Types

| Type | Description |
|------|-------------|
| `SUBSCRIPTION_TYPE_MARKET_DATA` | Full order book |
| `SUBSCRIPTION_TYPE_MARKET_DATA_LITE` | Best bid/offer only |
| `SUBSCRIPTION_TYPE_TRADE` | Trade notifications |

### Market Data Message

```json
{
  "type": "MARKET_DATA",
  "marketSlug": "nba-lakers-vs-celtics-2025-01-25",
  "timestamp": "2025-01-25T12:00:00.123Z",
  "yes": {
    "bids": [["0.47", "500"], ["0.46", "1000"]],
    "asks": [["0.49", "300"], ["0.50", "800"]]
  },
  "no": {
    "bids": [["0.51", "400"]],
    "asks": [["0.53", "350"]]
  }
}
```

### Order Update Message

```json
{
  "type": "ORDER_UPDATE",
  "orderId": "order-uuid",
  "status": "FILLED",
  "filledQuantity": 100,
  "avgFillPrice": "0.48",
  "timestamp": "2025-01-25T12:00:05.456Z"
}
```

### Position Update Message

```json
{
  "type": "POSITION_UPDATE",
  "marketSlug": "nba-lakers-vs-celtics-2025-01-25",
  "side": "YES",
  "quantity": 100,
  "avgPrice": "0.48",
  "timestamp": "2025-01-25T12:00:05.456Z"
}
```

---

## Python WebSocket Implementation

```python
import asyncio
import json
import websockets
from typing import Callable

class PolymarketWebSocket:
    """
    WebSocket client for Polymarket US.
    """
    
    def __init__(self, auth: PolymarketAuth, on_message: Callable):
        self.auth = auth
        self.on_message = on_message
        self.ws = None
        self.subscriptions = []
        
    async def connect(self, endpoint: str = "markets"):
        """
        Connect to WebSocket endpoint.
        
        Args:
            endpoint: "markets" or "private"
        """
        path = f"/v1/ws/{endpoint}"
        url = f"wss://api.polymarket.us{path}"
        
        # Generate auth headers
        headers = self.auth.sign_request("GET", path)
        
        self.ws = await websockets.connect(url, extra_headers=headers)
        
        # Start message handler
        asyncio.create_task(self._message_loop())
        
    async def _message_loop(self):
        """Process incoming messages."""
        async for message in self.ws:
            data = json.loads(message)
            await self.on_message(data)
            
    async def subscribe(
        self,
        subscription_type: str,
        market_slugs: list = None
    ):
        """
        Subscribe to a data feed.
        
        Args:
            subscription_type: e.g., "SUBSCRIPTION_TYPE_MARKET_DATA"
            market_slugs: List of markets to subscribe to
        """
        request_id = f"sub_{len(self.subscriptions)}"
        
        message = {
            "subscribe": {
                "requestId": request_id,
                "subscriptionType": subscription_type,
            }
        }
        
        if market_slugs:
            message["subscribe"]["marketSlugs"] = market_slugs
            
        await self.ws.send(json.dumps(message))
        self.subscriptions.append(request_id)
        
    async def close(self):
        """Close the WebSocket connection."""
        if self.ws:
            await self.ws.close()


# Usage example
async def handle_message(data: dict):
    print(f"Received: {data}")

async def main():
    auth = PolymarketAuth("api-key", "private-key")
    ws = PolymarketWebSocket(auth, handle_message)
    
    await ws.connect("markets")
    await ws.subscribe(
        "SUBSCRIPTION_TYPE_MARKET_DATA",
        ["nba-lakers-vs-celtics-2025-01-25"]
    )
    
    # Keep running
    await asyncio.sleep(3600)
    await ws.close()

asyncio.run(main())
```

---

## Fee Schedule

| Type | Fee |
|------|-----|
| Taker (immediate fill) | 0.10% (10 basis points) |
| Maker (posts to book) | 0% |
| Deposits | Free |
| Withdrawals | Free |

**Fee Calculation:**
```
fee = fill_price × quantity × 0.001  (for taker orders)
fee = 0  (for maker orders)
```

---

## Rate Limits

| Resource | Limit |
|----------|-------|
| WebSocket connections | 5 per API key |
| Markets per subscription | 100 |
| Reconnection attempts | 1 per second |

**Note:** REST API rate limits not specified in documentation. Recommend conservative approach (1-2 requests/second) until confirmed.

---

## Error Handling

Common error responses:

```json
{
  "error": {
    "code": "INVALID_SIGNATURE",
    "message": "Request signature is invalid or expired"
  }
}
```

| Error Code | Meaning | Solution |
|------------|---------|----------|
| `INVALID_SIGNATURE` | Auth failure | Check timestamp sync, signature construction |
| `INSUFFICIENT_BALANCE` | Not enough funds | Check balance before ordering |
| `MARKET_CLOSED` | Market not accepting orders | Check market status |
| `INVALID_PRICE` | Price out of range | Ensure 0.01 ≤ price ≤ 0.99 |
| `RATE_LIMITED` | Too many requests | Implement backoff |
