"""
Pydantic data models for Polymarket US API.

These models provide type safety and validation for API responses
and internal data structures.
"""

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


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
    
    # Positions response may vary; accept common aliases.
    market_slug: str = Field(validation_alias=AliasChoices("marketSlug", "market_slug", "slug", "market"))
    side: Side = Field(validation_alias=AliasChoices("side", "outcome"))
    quantity: int = Field(validation_alias=AliasChoices("quantity", "size", "qty"))
    avg_price: Decimal = Field(validation_alias=AliasChoices("avgPrice", "avg_price", "averagePrice"))
    current_price: Optional[Decimal] = Field(default=None, validation_alias=AliasChoices("currentPrice", "curPrice"))
    current_value: Optional[Decimal] = Field(default=None, validation_alias=AliasChoices("currentValue", "assetNotional"))
    unrealized_pnl: Optional[Decimal] = Field(default=None, validation_alias=AliasChoices("unrealizedPnl", "cashPnl"))
    unrealized_pnl_percent: Optional[Decimal] = Field(
        default=None, validation_alias=AliasChoices("unrealizedPnlPercent", "percentPnl")
    )

    @model_validator(mode="before")
    @classmethod
    def _normalize_portfolio_positions_schema(cls, data: Any) -> Any:
        """
        Normalize Polymarket's GET /v1/portfolio/positions schema into our simpler Position shape.

        Docs indicate:
        - positions is a map {marketSlug -> positionObj}
        - fields include netPosition, qtyBought, qtySold, cost{value}, cashValue{value}, marketMetadata{slug,outcome}
        """
        if not isinstance(data, dict):
            return data

        # If this already looks like our internal shape, don't touch it.
        if any(k in data for k in ("avgPrice", "avg_price", "averagePrice")) and any(k in data for k in ("quantity", "size", "qty")):
            return data

        if "netPosition" not in data and "marketMetadata" not in data:
            return data

        md = data.get("marketMetadata") if isinstance(data.get("marketMetadata"), dict) else {}
        slug = (
            md.get("slug")
            or data.get("marketSlug")
            or data.get("market_slug")
            or data.get("slug")
            or data.get("market")
        )

        outcome = (md.get("outcome") or data.get("outcome") or data.get("side") or "").strip()
        outcome_upper = outcome.upper()

        def _dec(v: Any) -> Optional[Decimal]:
            if v is None:
                return None
            try:
                return Decimal(str(v))
            except Exception:
                return None

        def _int(v: Any) -> Optional[int]:
            if v is None:
                return None
            try:
                return int(Decimal(str(v)))
            except Exception:
                try:
                    return int(v)
                except Exception:
                    return None

        net = _int(data.get("netPosition")) or 0
        qty_bought = _int(data.get("qtyBought"))
        qty_sold = _int(data.get("qtySold"))

        cost_obj = data.get("cost") if isinstance(data.get("cost"), dict) else {}
        cash_obj = data.get("cashValue") if isinstance(data.get("cashValue"), dict) else {}
        cost_value = _dec(cost_obj.get("value")) or Decimal("0")
        cash_value = _dec(cash_obj.get("value"))

        qty_for_avg = (qty_bought or 0) if (qty_bought or 0) > 0 else abs(net)
        avg_price = (cost_value / Decimal(qty_for_avg)) if qty_for_avg > 0 else Decimal("0")

        # Determine side:
        # - For classic YES/NO markets, outcome may literally be "Yes"/"No".
        # - For sports AEC markets, outcome may be a team name (e.g., "DUCKS").
        #   In that case, interpret netPosition sign as long-side vs short-side.
        side: str
        if outcome_upper in ("YES", "Y"):
            side = "YES"
        elif outcome_upper in ("NO", "N"):
            side = "NO"
        else:
            side = "YES" if net >= 0 else "NO"

        quantity = abs(net)
        current_value = cash_value
        unrealized_pnl = (cash_value - cost_value) if cash_value is not None else None
        unrealized_pnl_percent = (
            (unrealized_pnl / cost_value) if (unrealized_pnl is not None and cost_value != 0) else None
        )
        current_price = (cash_value / Decimal(quantity)) if (cash_value is not None and quantity > 0) else None

        normalized: Dict[str, Any] = {
            "marketSlug": slug,
            "side": side,
            "quantity": quantity,
            "avgPrice": str(avg_price),
            "currentValue": str(current_value) if current_value is not None else None,
            "unrealizedPnl": str(unrealized_pnl) if unrealized_pnl is not None else None,
            "unrealizedPnlPercent": str(unrealized_pnl_percent) if unrealized_pnl_percent is not None else None,
            "currentPrice": str(current_price) if current_price is not None else None,
        }

        # Keep original fields around (helpful for debugging, ignored by model_config extra=ignore elsewhere).
        # But since this model doesn't set extra=ignore, we avoid attaching raw fields directly.
        # Callers that need raw can log the raw payload.
        return normalized

    @field_validator("quantity", mode="before")
    @classmethod
    def _coerce_quantity(cls, v: Any) -> int:
        if v is None:
            return 0
        try:
            return int(Decimal(str(v)))
        except Exception:
            return int(v)

    @field_validator("avg_price", "current_price", "current_value", "unrealized_pnl", "unrealized_pnl_percent", mode="before")
    @classmethod
    def _coerce_decimal(cls, v: Any) -> Any:
        if v is None:
            return None
        try:
            return Decimal(str(v))
        except Exception:
            return v
    
    @property
    def cost_basis(self) -> Decimal:
        """Calculate total cost basis."""
        return self.avg_price * self.quantity


class Balance(BaseModel):
    """Account balance information."""
    model_config = ConfigDict(populate_by_name=True)
    
    # Polymarket has used multiple schemas:
    # - legacy: {"availableBalance": "...", "totalBalance": "...", "currency": "USD"}
    # - current: {"currentBalance": 1000.0, "buyingPower": 850.0, "currency": "USD", ...}
    #
    # We treat buyingPower as the cash available for placing new orders.
    available_balance: Decimal = Field(
        validation_alias=AliasChoices("availableBalance", "buyingPower")
    )
    total_balance: Optional[Decimal] = Field(
        default=None,
        validation_alias=AliasChoices("totalBalance", "currentBalance"),
    )
    currency: str = Field(default="USD", validation_alias=AliasChoices("currency",))


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
