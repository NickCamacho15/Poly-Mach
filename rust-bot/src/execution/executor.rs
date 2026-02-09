//! Execution engine for Polymarket US.
//!
//! Handles order placement, cancellation, and fill tracking.
//! Supports both paper (simulated) and live trading modes.

#![allow(dead_code)]

use rust_decimal::Decimal;
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use tracing::{info, warn};

use crate::api::client::PolymarketClient;
use crate::api::errors::ApiError;
use crate::data::models::*;
use crate::data::orderbook::OrderBookTracker;
use crate::state::state_manager::{OrderState, StateManager};

/// Execution result returned to the strategy engine.
#[derive(Debug, Clone)]
pub struct ExecResult {
    pub order_id: String,
    pub status: OrderStatus,
    pub filled_quantity: i64,
    pub avg_fill_price: Option<Decimal>,
    pub fee: Decimal,
    pub error: Option<String>,
}

/// Live executor that places real orders via the API.
pub struct LiveExecutor {
    client: PolymarketClient,
    state: StateManager,
    orderbook: OrderBookTracker,
    initial_balance: Decimal,

    // Counters
    total_trades: AtomicU64,
    successful_trades: AtomicU64,
    failed_trades: AtomicU64,

    // Order tracking
    order_market: HashMap<String, String>,
    order_last_filled: HashMap<String, i64>,
    estimated_fees: HashMap<String, Decimal>,
}

impl LiveExecutor {
    pub fn new(
        client: PolymarketClient,
        state: StateManager,
        orderbook: OrderBookTracker,
    ) -> Self {
        let initial_balance = state.get_balance();
        Self {
            client,
            state,
            orderbook,
            initial_balance,
            total_trades: AtomicU64::new(0),
            successful_trades: AtomicU64::new(0),
            failed_trades: AtomicU64::new(0),
            order_market: HashMap::new(),
            order_last_filled: HashMap::new(),
            estimated_fees: HashMap::new(),
        }
    }

    /// Initial state sync from API (call before trading starts).
    pub async fn initialize(&mut self) -> Result<(), ApiError> {
        self.reconcile_state().await?;
        let balance = self.state.get_balance();
        let positions = self.state.get_all_positions().len();
        info!(balance = %balance, positions, "LiveExecutor initialized");
        Ok(())
    }

    /// Execute an approved signal.
    pub async fn execute_signal(&mut self, signal: &Signal) -> ExecResult {
        // Handle cancels.
        if signal.is_cancel() {
            return self.cancel_all(&signal.market_slug).await;
        }

        let intent = match signal.action.to_intent() {
            Some(i) => i,
            None => {
                return ExecResult {
                    order_id: String::new(),
                    status: OrderStatus::Rejected,
                    filled_quantity: 0,
                    avg_fill_price: None,
                    fee: Decimal::ZERO,
                    error: Some("Invalid signal action".to_string()),
                };
            }
        };

        // Balance pre-check for buys.
        if signal.is_buy() && signal.price > Decimal::ZERO {
            let available = self.state.get_balance();
            let buffer = Decimal::new(98, 2);
            let max_affordable = (available * buffer) / signal.price;
            let max_qty = max_affordable
                .floor()
                .to_string()
                .parse::<i64>()
                .unwrap_or(0);

            if max_qty <= 0 {
                self.failed_trades.fetch_add(1, Ordering::Relaxed);
                return ExecResult {
                    order_id: String::new(),
                    status: OrderStatus::Rejected,
                    filled_quantity: 0,
                    avg_fill_price: None,
                    fee: Decimal::ZERO,
                    error: Some(format!(
                        "Insufficient balance: need ${:.2}, have ${:.2}",
                        signal.price * Decimal::from(signal.quantity),
                        available
                    )),
                };
            }
        }

        // Build API order request.
        let order_req = OrderRequest::limit_order(
            signal.market_slug.clone(),
            intent,
            signal.price,
            signal.quantity,
        );

        // Preview (best-effort).
        let estimated_fee = match self.client.preview_order(&order_req).await {
            Ok(preview) => preview.estimated_fee.unwrap_or(Decimal::ZERO),
            Err(_) => Decimal::ZERO,
        };

        // Place order.
        match self.client.create_order(&order_req).await {
            Ok(response) => {
                let order_id = response.order_id.clone();
                self.total_trades.fetch_add(1, Ordering::Relaxed);
                self.successful_trades.fetch_add(1, Ordering::Relaxed);

                // Track order.
                self.order_market
                    .insert(order_id.clone(), signal.market_slug.clone());
                self.order_last_filled.insert(order_id.clone(), 0);
                self.estimated_fees
                    .insert(order_id.clone(), estimated_fee);

                // Store in state.
                self.state.add_order(OrderState {
                    order_id: order_id.clone(),
                    market_slug: signal.market_slug.clone(),
                    intent,
                    price: signal.price,
                    quantity: signal.quantity,
                    filled_quantity: 0,
                    status: OrderStatus::Open,
                });

                info!(
                    order_id = %order_id,
                    market_slug = %signal.market_slug,
                    action = ?signal.action,
                    price = %signal.price,
                    quantity = signal.quantity,
                    "Order placed"
                );

                ExecResult {
                    order_id,
                    status: OrderStatus::Open,
                    filled_quantity: 0,
                    avg_fill_price: None,
                    fee: estimated_fee,
                    error: None,
                }
            }
            Err(e) => {
                self.total_trades.fetch_add(1, Ordering::Relaxed);
                self.failed_trades.fetch_add(1, Ordering::Relaxed);
                warn!(error = %e, market_slug = %signal.market_slug, "Order placement failed");

                ExecResult {
                    order_id: String::new(),
                    status: OrderStatus::Rejected,
                    filled_quantity: 0,
                    avg_fill_price: None,
                    fee: Decimal::ZERO,
                    error: Some(e.to_string()),
                }
            }
        }
    }

    /// Cancel all orders for a market.
    async fn cancel_all(&mut self, market_slug: &str) -> ExecResult {
        match self.client.cancel_all_orders(Some(market_slug)).await {
            Ok(_) => {
                // Clean up state.
                let open_orders = self.state.get_open_orders(Some(market_slug));
                for order in &open_orders {
                    self.state
                        .update_order(&order.order_id, Some(OrderStatus::Cancelled), None);
                    self.state.remove_order(&order.order_id);
                }

                ExecResult {
                    order_id: String::new(),
                    status: OrderStatus::Cancelled,
                    filled_quantity: 0,
                    avg_fill_price: None,
                    fee: Decimal::ZERO,
                    error: None,
                }
            }
            Err(e) => {
                warn!(error = %e, market_slug, "Cancel all failed");
                ExecResult {
                    order_id: String::new(),
                    status: OrderStatus::Rejected,
                    filled_quantity: 0,
                    avg_fill_price: None,
                    fee: Decimal::ZERO,
                    error: Some(e.to_string()),
                }
            }
        }
    }

    /// Reconcile state from API (balance, positions, orders).
    pub async fn reconcile_state(&mut self) -> Result<(), ApiError> {
        // Balance.
        match self.client.get_balance().await {
            Ok(balance) => {
                self.state.update_balance(balance.available_balance);
            }
            Err(e) => warn!(error = %e, "Reconcile balance failed"),
        }

        // Positions.
        match self.client.get_positions().await {
            Ok(positions) => {
                for p in positions {
                    self.state
                        .update_position(&p.market_slug, p.side, p.quantity, p.avg_price);
                }
            }
            Err(e) => warn!(error = %e, "Reconcile positions failed"),
        }

        // Open orders.
        match self.client.get_open_orders(None).await {
            Ok(orders) => {
                let open_ids: std::collections::HashSet<String> =
                    orders.iter().map(|o| o.order_id.clone()).collect();

                for o in &orders {
                    let status = match o.status.as_str() {
                        "PENDING" => OrderStatus::Pending,
                        "OPEN" => OrderStatus::Open,
                        "PARTIALLY_FILLED" => OrderStatus::PartiallyFilled,
                        _ => OrderStatus::Open,
                    };

                    self.order_market
                        .entry(o.order_id.clone())
                        .or_insert_with(|| o.market_slug.clone());

                    self.state.add_order(OrderState {
                        order_id: o.order_id.clone(),
                        market_slug: o.market_slug.clone(),
                        intent: match o.intent.as_str() {
                            "ORDER_INTENT_BUY_LONG" => OrderIntent::BuyLong,
                            "ORDER_INTENT_SELL_LONG" => OrderIntent::SellLong,
                            "ORDER_INTENT_BUY_SHORT" => OrderIntent::BuyShort,
                            "ORDER_INTENT_SELL_SHORT" => OrderIntent::SellShort,
                            _ => OrderIntent::BuyLong,
                        },
                        price: o.price.unwrap_or(Decimal::ZERO),
                        quantity: o.quantity,
                        filled_quantity: o.filled_quantity,
                        status,
                    });
                }

                // Clean up orders that are no longer open.
                let tracked: Vec<String> = self.order_market.keys().cloned().collect();
                for id in tracked {
                    if !open_ids.contains(&id) {
                        if let Some(order) = self.state.get_order(&id) {
                            if order.is_open() {
                                self.state
                                    .update_order(&id, Some(OrderStatus::Filled), None);
                                self.state.remove_order(&id);
                            }
                        }
                    }
                }
            }
            Err(e) => warn!(error = %e, "Reconcile orders failed"),
        }

        Ok(())
    }

    /// Performance metrics.
    pub fn get_performance(&self) -> HashMap<String, serde_json::Value> {
        let equity = self.state.get_total_equity();
        let cash = self.state.get_balance();
        let pos_value = self.state.get_total_position_value();
        let pnl = equity - self.initial_balance;
        let pnl_pct = if self.initial_balance > Decimal::ZERO {
            ((pnl / self.initial_balance) * Decimal::ONE_HUNDRED)
                .to_string()
                .parse::<f64>()
                .unwrap_or(0.0)
        } else {
            0.0
        };

        let mut m = HashMap::new();
        m.insert("mode".to_string(), serde_json::json!("live"));
        m.insert(
            "total_trades".to_string(),
            serde_json::json!(self.total_trades.load(Ordering::Relaxed)),
        );
        m.insert(
            "successful_trades".to_string(),
            serde_json::json!(self.successful_trades.load(Ordering::Relaxed)),
        );
        m.insert(
            "failed_trades".to_string(),
            serde_json::json!(self.failed_trades.load(Ordering::Relaxed)),
        );
        m.insert(
            "initial_balance".to_string(),
            serde_json::json!(self.initial_balance.to_string().parse::<f64>().unwrap_or(0.0)),
        );
        m.insert(
            "current_balance".to_string(),
            serde_json::json!(cash.to_string().parse::<f64>().unwrap_or(0.0)),
        );
        m.insert(
            "position_value".to_string(),
            serde_json::json!(pos_value.to_string().parse::<f64>().unwrap_or(0.0)),
        );
        m.insert(
            "total_equity".to_string(),
            serde_json::json!(equity.to_string().parse::<f64>().unwrap_or(0.0)),
        );
        m.insert(
            "total_pnl".to_string(),
            serde_json::json!(pnl.to_string().parse::<f64>().unwrap_or(0.0)),
        );
        m.insert("pnl_percent".to_string(), serde_json::json!(pnl_pct));
        m.insert(
            "open_positions".to_string(),
            serde_json::json!(self.state.get_all_positions().len()),
        );
        m
    }
}
