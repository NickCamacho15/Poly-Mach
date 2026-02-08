//! Paper (simulated) execution engine for Polymarket US.
//!
//! Tracks resting orders in memory and simulates fills when market
//! prices cross order prices.  No real API calls are made for order
//! placement—only for market data.

use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use tracing::{debug, info, warn};

use crate::data::models::*;
use crate::state::state_manager::StateManager;

/// A resting paper order waiting to be filled.
#[derive(Debug, Clone)]
struct PaperOrder {
    order_id: String,
    market_slug: String,
    action: SignalAction,
    price: Decimal,
    quantity: i64,
    created_at: DateTime<Utc>,
}

/// Execution result (mirrors LiveExecutor's ExecResult).
#[derive(Debug, Clone)]
pub struct ExecResult {
    pub order_id: String,
    pub status: OrderStatus,
    pub filled_quantity: i64,
    pub avg_fill_price: Option<Decimal>,
    pub fee: Decimal,
    pub error: Option<String>,
}

/// Paper executor: simulates fills locally, no real orders placed.
pub struct PaperExecutor {
    state: StateManager,
    initial_balance: Decimal,
    resting_orders: HashMap<String, PaperOrder>,
    next_order_id: AtomicU64,
    fee_rate: Decimal,

    // Performance tracking
    pub total_trades: u64,
    pub winning_trades: u64,
    pub losing_trades: u64,
    pub total_volume: Decimal,
    pub total_fees: Decimal,
    pub realized_pnl: Decimal,
    pub max_drawdown: Decimal,
    peak_equity: Decimal,
}

impl PaperExecutor {
    pub fn new(state: StateManager) -> Self {
        let initial_balance = state.get_balance();
        info!("[PAPER] Paper executor initialized (NO real orders will be placed)");
        Self {
            state,
            initial_balance,
            resting_orders: HashMap::new(),
            next_order_id: AtomicU64::new(1),
            fee_rate: Decimal::new(2, 3), // 0.002 = 20 bps
            total_trades: 0,
            winning_trades: 0,
            losing_trades: 0,
            total_volume: Decimal::ZERO,
            total_fees: Decimal::ZERO,
            realized_pnl: Decimal::ZERO,
            max_drawdown: Decimal::ZERO,
            peak_equity: initial_balance,
        }
    }

    /// Execute an approved signal.  Cancels are handled immediately;
    /// buy/sell signals become resting orders.
    pub fn execute_signal(&mut self, signal: &Signal) -> ExecResult {
        if signal.is_cancel() {
            return self.cancel_all(&signal.market_slug);
        }

        let order_id = format!(
            "paper-{}",
            self.next_order_id.fetch_add(1, Ordering::Relaxed)
        );

        // Balance pre-check for buys.
        if signal.is_buy() && signal.price > Decimal::ZERO {
            let available = self.state.get_balance();
            let cost = signal.price * Decimal::from(signal.quantity);
            if cost > available {
                return ExecResult {
                    order_id: String::new(),
                    status: OrderStatus::Rejected,
                    filled_quantity: 0,
                    avg_fill_price: None,
                    fee: Decimal::ZERO,
                    error: Some(format!(
                        "[PAPER] Insufficient balance: need ${:.2}, have ${:.2}",
                        cost, available
                    )),
                };
            }
        }

        debug!(
            order_id = %order_id,
            market_slug = %signal.market_slug,
            action = ?signal.action,
            price = %signal.price,
            quantity = signal.quantity,
            "[PAPER] Resting order placed"
        );

        self.resting_orders.insert(
            order_id.clone(),
            PaperOrder {
                order_id: order_id.clone(),
                market_slug: signal.market_slug.clone(),
                action: signal.action,
                price: signal.price,
                quantity: signal.quantity,
                created_at: Utc::now(),
            },
        );

        ExecResult {
            order_id,
            status: OrderStatus::Open,
            filled_quantity: 0,
            avg_fill_price: None,
            fee: Decimal::ZERO,
            error: None,
        }
    }

    /// Cancel all resting orders for a market.
    fn cancel_all(&mut self, market_slug: &str) -> ExecResult {
        let before = self.resting_orders.len();
        self.resting_orders
            .retain(|_, o| o.market_slug != market_slug);
        let cancelled = before - self.resting_orders.len();
        if cancelled > 0 {
            info!(
                market_slug = %market_slug,
                cancelled,
                "[PAPER] Orders cancelled"
            );
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

    /// Check all resting orders for fills against current market data.
    /// Call this once per tick after market data is updated.
    pub fn check_fills(&mut self) {
        let mut filled_ids = Vec::new();

        for (id, order) in &self.resting_orders {
            let market = match self.state.get_market(&order.market_slug) {
                Some(m) => m,
                None => continue,
            };

            let should_fill = match order.action {
                // Our resting buy-YES at `price` fills when market ask <= price
                SignalAction::BuyYes => market
                    .yes_ask
                    .map(|ask| ask <= order.price)
                    .unwrap_or(false),
                // Our resting sell-YES at `price` fills when market bid >= price
                SignalAction::SellYes => market
                    .yes_bid
                    .map(|bid| bid >= order.price)
                    .unwrap_or(false),
                // Our resting buy-NO at `price` fills when no_ask <= price
                SignalAction::BuyNo => market
                    .no_ask
                    .map(|ask| ask <= order.price)
                    .unwrap_or(false),
                // Our resting sell-NO fills when no_bid >= price
                SignalAction::SellNo => market
                    .no_bid
                    .map(|bid| bid >= order.price)
                    .unwrap_or(false),
                SignalAction::CancelAll => false,
            };

            if should_fill {
                filled_ids.push(id.clone());
            }
        }

        for id in filled_ids {
            if let Some(order) = self.resting_orders.remove(&id) {
                self.simulate_fill(&order);
            }
        }

        // Track drawdown
        let equity = self.state.get_total_equity();
        if equity > self.peak_equity {
            self.peak_equity = equity;
        }
        if self.peak_equity > Decimal::ZERO {
            let dd = (self.peak_equity - equity) / self.peak_equity;
            if dd > self.max_drawdown {
                self.max_drawdown = dd;
            }
        }
    }

    /// Simulate a fill: update balance, positions, and PnL.
    fn simulate_fill(&mut self, order: &PaperOrder) {
        let cost = order.price * Decimal::from(order.quantity);
        let fee = cost * self.fee_rate;

        match order.action {
            SignalAction::BuyYes => {
                let total_cost = cost + fee;
                let balance = self.state.get_balance();
                if total_cost > balance {
                    warn!(
                        market_slug = %order.market_slug,
                        cost = %total_cost,
                        balance = %balance,
                        "[PAPER] Fill skipped: insufficient balance"
                    );
                    return;
                }
                self.state.update_balance(balance - total_cost);
                self.add_to_position(&order.market_slug, Side::Yes, order.quantity, order.price);
            }
            SignalAction::SellYes => {
                // Selling YES position — credit balance
                let proceeds = cost - fee;
                let balance = self.state.get_balance();
                self.state.update_balance(balance + proceeds);
                self.reduce_position(&order.market_slug, Side::Yes, order.quantity, order.price);
            }
            SignalAction::BuyNo => {
                let total_cost = cost + fee;
                let balance = self.state.get_balance();
                if total_cost > balance {
                    warn!(
                        market_slug = %order.market_slug,
                        "[PAPER] Fill skipped: insufficient balance"
                    );
                    return;
                }
                self.state.update_balance(balance - total_cost);
                self.add_to_position(&order.market_slug, Side::No, order.quantity, order.price);
            }
            SignalAction::SellNo => {
                let proceeds = cost - fee;
                let balance = self.state.get_balance();
                self.state.update_balance(balance + proceeds);
                self.reduce_position(&order.market_slug, Side::No, order.quantity, order.price);
            }
            SignalAction::CancelAll => {}
        }

        self.total_trades += 1;
        self.total_volume += cost;
        self.total_fees += fee;

        info!(
            order_id = %order.order_id,
            market_slug = %order.market_slug,
            action = ?order.action,
            price = %order.price,
            quantity = order.quantity,
            fee = %fee,
            "[PAPER] Order filled"
        );
    }

    fn add_to_position(&self, market_slug: &str, side: Side, quantity: i64, price: Decimal) {
        let existing = self.state.get_position(market_slug);
        match existing {
            Some(pos) if pos.side == side => {
                // Average in
                let total_qty = pos.quantity + quantity;
                let avg = (pos.avg_price * Decimal::from(pos.quantity)
                    + price * Decimal::from(quantity))
                    / Decimal::from(total_qty);
                self.state
                    .update_position(market_slug, side, total_qty, avg);
            }
            Some(_pos) => {
                // Opposite side — net off (simplified: just replace)
                self.state
                    .update_position(market_slug, side, quantity, price);
            }
            None => {
                self.state
                    .update_position(market_slug, side, quantity, price);
            }
        }
    }

    fn reduce_position(&mut self, market_slug: &str, side: Side, quantity: i64, fill_price: Decimal) {
        if let Some(pos) = self.state.get_position(market_slug) {
            if pos.side == side {
                let pnl = (fill_price - pos.avg_price) * Decimal::from(quantity.min(pos.quantity));
                self.realized_pnl += pnl;
                if pnl > Decimal::ZERO {
                    self.winning_trades += 1;
                } else if pnl < Decimal::ZERO {
                    self.losing_trades += 1;
                }

                let remaining = pos.quantity - quantity;
                if remaining <= 0 {
                    self.state.remove_position(market_slug);
                } else {
                    self.state
                        .update_position(market_slug, side, remaining, pos.avg_price);
                }
            }
        }
    }

    /// Number of resting orders.
    pub fn resting_order_count(&self) -> usize {
        self.resting_orders.len()
    }

    /// Performance metrics (same shape as LiveExecutor).
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
        let win_rate = if self.total_trades > 0 {
            self.winning_trades as f64 / self.total_trades as f64
        } else {
            0.0
        };

        let mut m = HashMap::new();
        m.insert("mode".to_string(), serde_json::json!("paper"));
        m.insert(
            "initial_balance".to_string(),
            serde_json::json!(dec_f64(self.initial_balance)),
        );
        m.insert(
            "current_balance".to_string(),
            serde_json::json!(dec_f64(cash)),
        );
        m.insert(
            "position_value".to_string(),
            serde_json::json!(dec_f64(pos_value)),
        );
        m.insert(
            "total_equity".to_string(),
            serde_json::json!(dec_f64(equity)),
        );
        m.insert("total_pnl".to_string(), serde_json::json!(dec_f64(pnl)));
        m.insert("pnl_percent".to_string(), serde_json::json!(pnl_pct));
        m.insert(
            "realized_pnl".to_string(),
            serde_json::json!(dec_f64(self.realized_pnl)),
        );
        m.insert(
            "total_trades".to_string(),
            serde_json::json!(self.total_trades),
        );
        m.insert(
            "winning_trades".to_string(),
            serde_json::json!(self.winning_trades),
        );
        m.insert(
            "losing_trades".to_string(),
            serde_json::json!(self.losing_trades),
        );
        m.insert("win_rate".to_string(), serde_json::json!(win_rate));
        m.insert(
            "total_volume".to_string(),
            serde_json::json!(dec_f64(self.total_volume)),
        );
        m.insert(
            "total_fees_paid".to_string(),
            serde_json::json!(dec_f64(self.total_fees)),
        );
        m.insert(
            "open_positions".to_string(),
            serde_json::json!(self.state.get_all_positions().len()),
        );
        m.insert(
            "resting_orders".to_string(),
            serde_json::json!(self.resting_orders.len()),
        );
        m.insert(
            "max_drawdown".to_string(),
            serde_json::json!(dec_f64(self.max_drawdown)),
        );
        m
    }
}

fn dec_f64(d: Decimal) -> f64 {
    d.to_string().parse::<f64>().unwrap_or(0.0)
}
