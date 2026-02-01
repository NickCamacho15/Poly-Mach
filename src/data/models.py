"""
Pydantic data models for Polymarket US API.

These models provide type safety and validation for API responses
and internal data structures.
"""

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


# =============================================================================
# Enums
# =============================================================================

class MarketStatus(str, Enum):
    """Market status values."""
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    RESOLVED = "RESOLVED"


class OrderType(str, Enum):
    """Order type values."""
    LIMIT = "ORDER_TYPE_LIMIT"
    MARKET = "ORDER_TYPE_MARKET"


class OrderIntent(str, Enum):
    """Order intent (direction) values."""
    BUY_LONG = "ORDER_INTENT_BUY_LONG"      # Buy YES shares
    SELL_LONG = "ORDER_INTENT_SELL_LONG"    # Sell YES shares
    BUY_SHORT = "ORDER_INTENT_BUY_SHORT"    # Buy NO shares
    SELL_SHORT = "ORDER_INTENT_SELL_SHORT"  # Sell NO shares


class OrderStatus(str, Enum):
    """Order status values."""
    PENDING = "PENDING"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class TimeInForce(str, Enum):
    """Time in force values for orders."""
    GOOD_TILL_CANCEL = "TIME_IN_FORCE_GOOD_TILL_CANCEL"
    GOOD_TILL_DATE = "TIME_IN_FORCE_GOOD_TILL_DATE"
    IMMEDIATE_OR_CANCEL = "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"
    FILL_OR_KILL = "TIME_IN_FORCE_FILL_OR_KILL"


class Side(str, Enum):
    """Position side."""
    YES = "YES"
    NO = "NO"


# =============================================================================
# Market Models
# =============================================================================

class PriceLevel(BaseModel):
    """A single price level in the order book."""
    price: Decimal
    quantity: int


class OrderBookSide(BaseModel):
    """One side (YES or NO) of the order book."""
    bids: List[PriceLevel] = Field(default_factory=list)
    asks: List[PriceLevel] = Field(default_factory=list)
    
    @property
    def best_bid(self) -> Optional[Decimal]:
        """Get best bid price."""
        if self.bids:
            return max(level.price for level in self.bids)
        return None
    
    @property
    def best_ask(self) -> Optional[Decimal]:
        """Get best ask price."""
        if self.asks:
            return min(level.price for level in self.asks)
        return None
    
    @property
    def spread(self) -> Optional[Decimal]:
        """Get bid-ask spread."""
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None


class OrderBook(BaseModel):
    """Full order book for a market."""
    model_config = ConfigDict(populate_by_name=True)
    
    market_slug: str = Field(alias="marketSlug")
    yes: OrderBookSide
    no: OrderBookSide


class Market(BaseModel):
    """Market information."""
    model_config = ConfigDict(populate_by_name=True)
    
    slug: str
    title: str
    description: Optional[str] = None
    status: MarketStatus
    category: Optional[str] = None
    resolution_date: Optional[datetime] = Field(default=None, alias="resolutionDate")
    yes_bid: Optional[Decimal] = Field(default=None, alias="yesBid")
    yes_ask: Optional[Decimal] = Field(default=None, alias="yesAsk")
    no_bid: Optional[Decimal] = Field(default=None, alias="noBid")
    no_ask: Optional[Decimal] = Field(default=None, alias="noAsk")
    volume_24h: Optional[Decimal] = Field(default=None, alias="volume24h")
    
    @property
    def mid_price(self) -> Optional[Decimal]:
        """Calculate mid-price for YES side."""
        if self.yes_bid is not None and self.yes_ask is not None:
            return (self.yes_bid + self.yes_ask) / 2
        return None
    
    @property
    def spread(self) -> Optional[Decimal]:
        """Calculate bid-ask spread for YES side."""
        if self.yes_bid is not None and self.yes_ask is not None:
            return self.yes_ask - self.yes_bid
        return None


# =============================================================================
# Order Models
# =============================================================================

class Price(BaseModel):
    """Price object for order requests."""
    value: str
    currency: str = "USD"


class OrderRequest(BaseModel):
    """Request body for creating an order."""
    model_config = ConfigDict(populate_by_name=True, use_enum_values=True)
    
    market_slug: str = Field(alias="marketSlug")
    order_type: OrderType = Field(default=OrderType.LIMIT, alias="type")
    price: Optional[Price] = None
    quantity: int
    tif: TimeInForce = TimeInForce.GOOD_TILL_CANCEL
    intent: OrderIntent
    manual_order_indicator: str = Field(
        default="MANUAL_ORDER_INDICATOR_AUTOMATIC",
        alias="manualOrderIndicator"
    )
    
    def to_api_payload(self) -> dict:
        """Convert to API request payload."""
        payload = {
            "marketSlug": self.market_slug,
            "type": self.order_type.value if isinstance(self.order_type, OrderType) else self.order_type,
            "quantity": self.quantity,
            "tif": self.tif.value if isinstance(self.tif, TimeInForce) else self.tif,
            "intent": self.intent.value if isinstance(self.intent, OrderIntent) else self.intent,
            "manualOrderIndicator": self.manual_order_indicator,
        }
        
        if self.price is not None:
            payload["price"] = {
                "value": self.price.value,
                "currency": self.price.currency,
            }
        
        return payload


class Order(BaseModel):
    """Order response from API."""
    model_config = ConfigDict(populate_by_name=True)
    
    order_id: str = Field(alias="orderId")
    market_slug: str = Field(alias="marketSlug")
    intent: str
    order_type: Optional[str] = Field(default=None, alias="type")
    price: Optional[Decimal] = None
    quantity: int
    filled_quantity: int = Field(default=0, alias="filledQuantity")
    remaining_quantity: Optional[int] = Field(default=None, alias="remainingQuantity")
    avg_fill_price: Optional[Decimal] = Field(default=None, alias="avgFillPrice")
    status: str
    created_at: Optional[datetime] = Field(default=None, alias="createdAt")
    updated_at: Optional[datetime] = Field(default=None, alias="updatedAt")
    
    @property
    def is_open(self) -> bool:
        """Check if order is still open."""
        return self.status in ("PENDING", "OPEN", "PARTIALLY_FILLED")
    
    @property
    def is_filled(self) -> bool:
        """Check if order is fully filled."""
        return self.status == "FILLED"


class CreateOrderResponse(BaseModel):
    """
    Minimal create-order response from API.

    Polymarket's create-order endpoint may return only an ID and executions, e.g.
    {"id": "...", "executions": []}.
    """

    model_config = ConfigDict(populate_by_name=True)

    order_id: str = Field(validation_alias=AliasChoices("id", "orderId", "order_id"))
    executions: List[Dict[str, Any]] = Field(default_factory=list)


class OrderPreview(BaseModel):
    """Order preview response."""
    model_config = ConfigDict(populate_by_name=True)
    
    estimated_fill_price: Optional[Decimal] = Field(default=None, alias="price")
    estimated_fill_quantity: Optional[int] = Field(default=None, alias="quantity")
    estimated_cost: Optional[Decimal] = Field(default=None, alias="cost")
    estimated_fee: Optional[Decimal] = Field(default=None, alias="estimatedFee")
    estimated_total: Optional[Decimal] = Field(default=None, alias="estimatedTotal")


# =============================================================================
# Portfolio Models
# =============================================================================

class Position(BaseModel):
    """Position in a market."""
    model_config = ConfigDict(populate_by_name=True)
    
    market_slug: str = Field(alias="marketSlug")
    side: Side
    quantity: int
    avg_price: Decimal = Field(alias="avgPrice")
    current_price: Optional[Decimal] = Field(default=None, alias="currentPrice")
    current_value: Optional[Decimal] = Field(default=None, alias="currentValue")
    unrealized_pnl: Optional[Decimal] = Field(default=None, alias="unrealizedPnl")
    unrealized_pnl_percent: Optional[Decimal] = Field(
        default=None, alias="unrealizedPnlPercent"
    )
    
    @property
    def cost_basis(self) -> Decimal:
        """Calculate total cost basis."""
        return self.avg_price * self.quantity


class Balance(BaseModel):
    """Account balance information."""
    model_config = ConfigDict(populate_by_name=True)
    
    available_balance: Decimal = Field(alias="availableBalance")
    total_balance: Optional[Decimal] = Field(default=None, alias="totalBalance")
    currency: str = "USD"


# =============================================================================
# Trade Models
# =============================================================================

class Trade(BaseModel):
    """Record of an executed trade."""
    model_config = ConfigDict(populate_by_name=True)
    
    trade_id: Optional[str] = Field(default=None, alias="tradeId")
    order_id: str = Field(alias="orderId")
    market_slug: str = Field(alias="marketSlug")
    side: Side
    price: Decimal
    quantity: int
    fee: Decimal = Decimal("0")
    timestamp: datetime
    
    @property
    def notional_value(self) -> Decimal:
        """Calculate notional value of trade."""
        return self.price * self.quantity
    
    @property
    def total_cost(self) -> Decimal:
        """Calculate total cost including fees."""
        return self.notional_value + self.fee


# =============================================================================
# API Response Wrappers
# =============================================================================

class MarketsResponse(BaseModel):
    """Response from GET /v1/markets."""
    markets: List[Market]


class OrdersResponse(BaseModel):
    """Response from GET /v1/orders/open."""
    orders: List[Order]


class PositionsResponse(BaseModel):
    """Response from GET /v1/portfolio/positions."""
    positions: List[Position]


# =============================================================================
# WebSocket Message Models
# =============================================================================

class MarketDataMessage(BaseModel):
    """WebSocket market data update."""
    model_config = ConfigDict(populate_by_name=True)
    
    type: str
    market_slug: str = Field(alias="marketSlug")
    timestamp: datetime
    yes: OrderBookSide
    no: OrderBookSide


class OrderUpdateMessage(BaseModel):
    """WebSocket order update."""
    model_config = ConfigDict(populate_by_name=True)
    
    type: str
    order_id: str = Field(alias="orderId")
    status: str
    filled_quantity: int = Field(alias="filledQuantity")
    avg_fill_price: Optional[Decimal] = Field(default=None, alias="avgFillPrice")
    timestamp: datetime


class PositionUpdateMessage(BaseModel):
    """WebSocket position update."""
    model_config = ConfigDict(populate_by_name=True)
    
    type: str
    market_slug: str = Field(alias="marketSlug")
    side: Side
    quantity: int
    avg_price: Decimal = Field(alias="avgPrice")
    timestamp: datetime
