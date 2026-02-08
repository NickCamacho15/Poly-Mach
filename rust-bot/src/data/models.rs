//! Core data models for Polymarket US API.
//!
//! These models provide type safety and serialization for API responses
//! and internal data structures. Faithfully ported from Python `src/data/models.py`.

use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use std::fmt;

// =============================================================================
// Enums
// =============================================================================

#[derive(Debug, Default, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum MarketStatus {
    #[default]
    Open,
    Closed,
    Resolved,
    #[serde(other)]
    Unknown,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum OrderType {
    #[serde(rename = "ORDER_TYPE_LIMIT")]
    Limit,
    #[serde(rename = "ORDER_TYPE_MARKET")]
    Market,
}

impl Default for OrderType {
    fn default() -> Self {
        Self::Limit
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum OrderIntent {
    #[serde(rename = "ORDER_INTENT_BUY_LONG")]
    BuyLong,
    #[serde(rename = "ORDER_INTENT_SELL_LONG")]
    SellLong,
    #[serde(rename = "ORDER_INTENT_BUY_SHORT")]
    BuyShort,
    #[serde(rename = "ORDER_INTENT_SELL_SHORT")]
    SellShort,
}

impl OrderIntent {
    pub fn is_buy(&self) -> bool {
        matches!(self, Self::BuyLong | Self::BuyShort)
    }

    pub fn is_sell(&self) -> bool {
        matches!(self, Self::SellLong | Self::SellShort)
    }

    pub fn side(&self) -> Side {
        match self {
            Self::BuyLong | Self::SellLong => Side::Yes,
            Self::BuyShort | Self::SellShort => Side::No,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum OrderStatus {
    Pending,
    Open,
    PartiallyFilled,
    Filled,
    Cancelled,
    Rejected,
}

impl OrderStatus {
    pub fn is_open(&self) -> bool {
        matches!(self, Self::Pending | Self::Open | Self::PartiallyFilled)
    }

    pub fn is_terminal(&self) -> bool {
        matches!(self, Self::Filled | Self::Cancelled | Self::Rejected)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum TimeInForce {
    #[serde(rename = "TIME_IN_FORCE_GOOD_TILL_CANCEL")]
    GoodTillCancel,
    #[serde(rename = "TIME_IN_FORCE_GOOD_TILL_DATE")]
    GoodTillDate,
    #[serde(rename = "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL")]
    ImmediateOrCancel,
    #[serde(rename = "TIME_IN_FORCE_FILL_OR_KILL")]
    FillOrKill,
}

impl Default for TimeInForce {
    fn default() -> Self {
        Self::GoodTillCancel
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Side {
    Yes,
    No,
}

impl Side {
    pub fn opposite(&self) -> Self {
        match self {
            Self::Yes => Self::No,
            Self::No => Self::Yes,
        }
    }
}

impl fmt::Display for Side {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Yes => write!(f, "YES"),
            Self::No => write!(f, "NO"),
        }
    }
}

// =============================================================================
// Market Models
// =============================================================================

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PriceLevel {
    pub price: Decimal,
    pub quantity: i64,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct OrderBookSide {
    pub bids: Vec<PriceLevel>,
    pub asks: Vec<PriceLevel>,
}

impl OrderBookSide {
    pub fn best_bid(&self) -> Option<Decimal> {
        self.bids.iter().map(|l| l.price).max()
    }

    pub fn best_ask(&self) -> Option<Decimal> {
        self.asks.iter().map(|l| l.price).min()
    }

    pub fn spread(&self) -> Option<Decimal> {
        match (self.best_bid(), self.best_ask()) {
            (Some(bid), Some(ask)) => Some(ask - bid),
            _ => None,
        }
    }

    pub fn total_bid_depth(&self) -> i64 {
        self.bids.iter().map(|l| l.quantity).sum()
    }

    pub fn total_ask_depth(&self) -> i64 {
        self.asks.iter().map(|l| l.quantity).sum()
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OrderBook {
    #[serde(alias = "marketSlug")]
    pub market_slug: String,
    pub yes: OrderBookSide,
    pub no: OrderBookSide,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Market {
    pub slug: String,
    #[serde(default, alias = "name", alias = "question")]
    pub title: String,
    pub description: Option<String>,
    #[serde(default)]
    pub status: MarketStatus,
    pub category: Option<String>,
    #[serde(default)]
    pub active: bool,
    #[serde(default)]
    pub closed: bool,
    #[serde(default, alias = "resolutionDate")]
    pub resolution_date: Option<DateTime<Utc>>,
    #[serde(default, alias = "yesBid", alias = "bestBid")]
    pub yes_bid: Option<Decimal>,
    #[serde(default, alias = "yesAsk", alias = "bestAsk")]
    pub yes_ask: Option<Decimal>,
    #[serde(default, alias = "noBid")]
    pub no_bid: Option<Decimal>,
    #[serde(default, alias = "noAsk")]
    pub no_ask: Option<Decimal>,
    #[serde(default, alias = "volume24h", alias = "volume")]
    pub volume_24h: Option<Decimal>,
    #[serde(default)]
    pub liquidity: Option<Decimal>,
}

impl Market {
    /// Whether this market is tradeable (active and not closed).
    pub fn is_tradeable(&self) -> bool {
        self.active && !self.closed
    }
}

impl Market {
    pub fn mid_price(&self) -> Option<Decimal> {
        match (self.yes_bid, self.yes_ask) {
            (Some(bid), Some(ask)) => Some((bid + ask) / Decimal::TWO),
            _ => None,
        }
    }

    pub fn spread(&self) -> Option<Decimal> {
        match (self.yes_bid, self.yes_ask) {
            (Some(bid), Some(ask)) => Some(ask - bid),
            _ => None,
        }
    }
}

// =============================================================================
// Order Models
// =============================================================================

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Price {
    pub value: String,
    pub currency: String,
}

impl Price {
    pub fn usd(value: Decimal) -> Self {
        Self {
            value: value.to_string(),
            currency: "USD".to_string(),
        }
    }
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct OrderRequest {
    pub market_slug: String,
    #[serde(rename = "type")]
    pub order_type: OrderType,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub price: Option<Price>,
    pub quantity: i64,
    pub tif: TimeInForce,
    pub intent: OrderIntent,
    pub manual_order_indicator: String,
}

impl OrderRequest {
    pub fn limit_order(
        market_slug: String,
        intent: OrderIntent,
        price: Decimal,
        quantity: i64,
    ) -> Self {
        Self {
            market_slug,
            order_type: OrderType::Limit,
            price: Some(Price::usd(price)),
            quantity,
            tif: TimeInForce::GoodTillCancel,
            intent,
            manual_order_indicator: "MANUAL_ORDER_INDICATOR_AUTOMATIC".to_string(),
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct Order {
    #[serde(alias = "orderId")]
    pub order_id: String,
    #[serde(alias = "marketSlug")]
    pub market_slug: String,
    pub intent: String,
    #[serde(alias = "type")]
    pub order_type: Option<String>,
    pub price: Option<Decimal>,
    pub quantity: i64,
    #[serde(alias = "filledQuantity", default)]
    pub filled_quantity: i64,
    #[serde(alias = "remainingQuantity")]
    pub remaining_quantity: Option<i64>,
    #[serde(alias = "avgFillPrice")]
    pub avg_fill_price: Option<Decimal>,
    pub status: String,
    #[serde(alias = "createdAt")]
    pub created_at: Option<DateTime<Utc>>,
    #[serde(alias = "updatedAt")]
    pub updated_at: Option<DateTime<Utc>>,
}

impl Order {
    pub fn is_open(&self) -> bool {
        matches!(
            self.status.as_str(),
            "PENDING" | "OPEN" | "PARTIALLY_FILLED"
        )
    }

    pub fn is_filled(&self) -> bool {
        self.status == "FILLED"
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct CreateOrderResponse {
    #[serde(alias = "id", alias = "orderId")]
    pub order_id: String,
    #[serde(default)]
    pub executions: Vec<serde_json::Value>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct OrderPreview {
    #[serde(alias = "price")]
    pub estimated_fill_price: Option<Decimal>,
    #[serde(alias = "quantity")]
    pub estimated_fill_quantity: Option<i64>,
    #[serde(alias = "cost")]
    pub estimated_cost: Option<Decimal>,
    #[serde(alias = "estimatedFee")]
    pub estimated_fee: Option<Decimal>,
    #[serde(alias = "estimatedTotal")]
    pub estimated_total: Option<Decimal>,
}

// =============================================================================
// Portfolio Models
// =============================================================================

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Position {
    #[serde(alias = "marketSlug")]
    pub market_slug: String,
    pub side: Side,
    pub quantity: i64,
    #[serde(alias = "avgPrice")]
    pub avg_price: Decimal,
    #[serde(alias = "currentPrice")]
    pub current_price: Option<Decimal>,
    #[serde(alias = "currentValue")]
    pub current_value: Option<Decimal>,
    #[serde(alias = "unrealizedPnl")]
    pub unrealized_pnl: Option<Decimal>,
}

impl Position {
    pub fn cost_basis(&self) -> Decimal {
        self.avg_price * Decimal::from(self.quantity)
    }

    pub fn notional_value(&self) -> Decimal {
        match self.current_price {
            Some(price) => price * Decimal::from(self.quantity),
            None => self.cost_basis(),
        }
    }
}

#[derive(Debug, Clone, Deserialize)]
pub struct Balance {
    #[serde(alias = "availableBalance", alias = "buyingPower")]
    pub available_balance: Decimal,
    #[serde(alias = "totalBalance", alias = "currentBalance")]
    pub total_balance: Option<Decimal>,
    #[serde(default = "default_currency")]
    pub currency: String,
}

fn default_currency() -> String {
    "USD".to_string()
}

// =============================================================================
// Trade Models
// =============================================================================

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Trade {
    #[serde(alias = "tradeId")]
    pub trade_id: Option<String>,
    #[serde(alias = "orderId")]
    pub order_id: String,
    #[serde(alias = "marketSlug")]
    pub market_slug: String,
    pub side: Side,
    pub price: Decimal,
    pub quantity: i64,
    #[serde(default)]
    pub fee: Decimal,
    pub timestamp: DateTime<Utc>,
}

impl Trade {
    pub fn notional_value(&self) -> Decimal {
        self.price * Decimal::from(self.quantity)
    }

    pub fn total_cost(&self) -> Decimal {
        self.notional_value() + self.fee
    }
}

// =============================================================================
// Signal Types (used by strategy engine)
// =============================================================================

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum SignalAction {
    BuyYes,
    SellYes,
    BuyNo,
    SellNo,
    CancelAll,
}

impl SignalAction {
    pub fn is_buy(&self) -> bool {
        matches!(self, Self::BuyYes | Self::BuyNo)
    }

    pub fn is_sell(&self) -> bool {
        matches!(self, Self::SellYes | Self::SellNo)
    }

    pub fn is_cancel(&self) -> bool {
        matches!(self, Self::CancelAll)
    }

    pub fn to_intent(&self) -> Option<OrderIntent> {
        match self {
            Self::BuyYes => Some(OrderIntent::BuyLong),
            Self::SellYes => Some(OrderIntent::SellLong),
            Self::BuyNo => Some(OrderIntent::BuyShort),
            Self::SellNo => Some(OrderIntent::SellShort),
            Self::CancelAll => None,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Urgency {
    Low,
    Medium,
    High,
    Critical,
}

#[derive(Debug, Clone)]
pub struct Signal {
    pub market_slug: String,
    pub action: SignalAction,
    pub price: Decimal,
    pub quantity: i64,
    pub urgency: Urgency,
    pub confidence: f64,
    pub strategy_name: String,
    pub reason: String,
    pub metadata: std::collections::HashMap<String, serde_json::Value>,
    pub timestamp: DateTime<Utc>,
}

impl Signal {
    pub fn is_buy(&self) -> bool {
        self.action.is_buy()
    }

    pub fn is_sell(&self) -> bool {
        self.action.is_sell()
    }

    pub fn is_cancel(&self) -> bool {
        self.action.is_cancel()
    }

    pub fn notional(&self) -> Decimal {
        self.price * Decimal::from(self.quantity)
    }
}

// =============================================================================
// Execution Result
// =============================================================================

#[derive(Debug, Clone)]
pub struct ExecutionResult {
    pub order_id: String,
    pub status: OrderStatus,
    pub filled_quantity: i64,
    pub avg_fill_price: Option<Decimal>,
    pub fee: Decimal,
    pub error: Option<String>,
}
