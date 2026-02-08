//! Thread-safe state container for market, position, order, and balance data.
//!
//! Provides a centralized view of the bot's current state, updated by
//! the data pipeline and execution engine.

#![allow(dead_code)]

use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use std::collections::HashMap;
use std::sync::{Arc, RwLock};

use crate::data::models::{OrderIntent, OrderStatus, Side};

// =============================================================================
// State Types
// =============================================================================

#[derive(Debug, Clone)]
pub struct MarketState {
    pub market_slug: String,
    pub title: String,
    pub yes_bid: Option<Decimal>,
    pub yes_ask: Option<Decimal>,
    pub no_bid: Option<Decimal>,
    pub no_ask: Option<Decimal>,
    pub last_updated: DateTime<Utc>,
}

impl MarketState {
    pub fn yes_mid_price(&self) -> Option<Decimal> {
        match (self.yes_bid, self.yes_ask) {
            (Some(bid), Some(ask)) => Some((bid + ask) / Decimal::TWO),
            _ => None,
        }
    }

    pub fn has_valid_prices(&self) -> bool {
        matches!(
            (self.yes_bid, self.yes_ask),
            (Some(bid), Some(ask)) if bid > Decimal::ZERO && ask > Decimal::ZERO && bid < ask
        )
    }
}

#[derive(Debug, Clone)]
pub struct PositionState {
    pub market_slug: String,
    pub side: Side,
    pub quantity: i64,
    pub avg_price: Decimal,
    pub created_at: DateTime<Utc>,
}

impl PositionState {
    pub fn cost_basis(&self) -> Decimal {
        self.avg_price * Decimal::from(self.quantity)
    }

    pub fn notional_value(&self, current_price: Decimal) -> Decimal {
        current_price * Decimal::from(self.quantity)
    }
}

#[derive(Debug, Clone)]
pub struct OrderState {
    pub order_id: String,
    pub market_slug: String,
    pub intent: OrderIntent,
    pub price: Decimal,
    pub quantity: i64,
    pub filled_quantity: i64,
    pub status: OrderStatus,
}

impl OrderState {
    pub fn is_open(&self) -> bool {
        self.status.is_open()
    }
}

// =============================================================================
// State Manager
// =============================================================================

#[derive(Debug)]
struct Inner {
    balance: Decimal,
    markets: HashMap<String, MarketState>,
    positions: HashMap<String, PositionState>,
    orders: HashMap<String, OrderState>,
}

/// Thread-safe centralized state container.
#[derive(Debug, Clone)]
pub struct StateManager {
    inner: Arc<RwLock<Inner>>,
}

impl StateManager {
    pub fn new(initial_balance: Decimal) -> Self {
        Self {
            inner: Arc::new(RwLock::new(Inner {
                balance: initial_balance,
                markets: HashMap::new(),
                positions: HashMap::new(),
                orders: HashMap::new(),
            })),
        }
    }

    // =========================================================================
    // Balance
    // =========================================================================

    pub fn get_balance(&self) -> Decimal {
        self.inner.read().unwrap().balance
    }

    pub fn update_balance(&self, balance: Decimal) {
        self.inner.write().unwrap().balance = balance;
    }

    pub fn get_total_equity(&self) -> Decimal {
        let inner = self.inner.read().unwrap();
        let position_value: Decimal = inner
            .positions
            .values()
            .map(|p| p.cost_basis())
            .sum();
        inner.balance + position_value
    }

    pub fn get_total_position_value(&self) -> Decimal {
        let inner = self.inner.read().unwrap();
        inner.positions.values().map(|p| p.cost_basis()).sum()
    }

    // =========================================================================
    // Markets
    // =========================================================================

    pub fn update_market(&self, market: MarketState) {
        let mut inner = self.inner.write().unwrap();
        inner.markets.insert(market.market_slug.clone(), market);
    }

    pub fn get_market(&self, slug: &str) -> Option<MarketState> {
        self.inner.read().unwrap().markets.get(slug).cloned()
    }

    pub fn get_all_markets(&self) -> Vec<MarketState> {
        self.inner.read().unwrap().markets.values().cloned().collect()
    }

    // =========================================================================
    // Positions
    // =========================================================================

    pub fn update_position(
        &self,
        market_slug: &str,
        side: Side,
        quantity: i64,
        avg_price: Decimal,
    ) {
        let mut inner = self.inner.write().unwrap();
        if quantity <= 0 {
            inner.positions.remove(market_slug);
        } else {
            let entry = inner
                .positions
                .entry(market_slug.to_string())
                .or_insert_with(|| PositionState {
                    market_slug: market_slug.to_string(),
                    side,
                    quantity,
                    avg_price,
                    created_at: Utc::now(),
                });
            entry.side = side;
            entry.quantity = quantity;
            entry.avg_price = avg_price;
        }
    }

    pub fn get_position(&self, market_slug: &str) -> Option<PositionState> {
        self.inner.read().unwrap().positions.get(market_slug).cloned()
    }

    pub fn get_all_positions(&self) -> Vec<PositionState> {
        self.inner
            .read()
            .unwrap()
            .positions
            .values()
            .cloned()
            .collect()
    }

    pub fn remove_position(&self, market_slug: &str) {
        self.inner.write().unwrap().positions.remove(market_slug);
    }

    // =========================================================================
    // Orders
    // =========================================================================

    pub fn add_order(&self, order: OrderState) {
        let mut inner = self.inner.write().unwrap();
        inner.orders.insert(order.order_id.clone(), order);
    }

    pub fn get_order(&self, order_id: &str) -> Option<OrderState> {
        self.inner.read().unwrap().orders.get(order_id).cloned()
    }

    pub fn get_open_orders(&self, market_slug: Option<&str>) -> Vec<OrderState> {
        let inner = self.inner.read().unwrap();
        inner
            .orders
            .values()
            .filter(|o| {
                o.is_open()
                    && market_slug
                        .map(|s| o.market_slug == s)
                        .unwrap_or(true)
            })
            .cloned()
            .collect()
    }

    pub fn update_order(
        &self,
        order_id: &str,
        status: Option<OrderStatus>,
        filled_quantity: Option<i64>,
    ) {
        let mut inner = self.inner.write().unwrap();
        if let Some(order) = inner.orders.get_mut(order_id) {
            if let Some(s) = status {
                order.status = s;
            }
            if let Some(fq) = filled_quantity {
                order.filled_quantity = fq;
            }
        }
    }

    pub fn remove_order(&self, order_id: &str) {
        self.inner.write().unwrap().orders.remove(order_id);
    }

    /// Count of active positions.
    pub fn position_count(&self) -> usize {
        self.inner.read().unwrap().positions.len()
    }

    /// Total exposure for a specific market.
    pub fn market_exposure(&self, market_slug: &str) -> Decimal {
        self.inner
            .read()
            .unwrap()
            .positions
            .get(market_slug)
            .map(|p| p.cost_basis())
            .unwrap_or(Decimal::ZERO)
    }
}
