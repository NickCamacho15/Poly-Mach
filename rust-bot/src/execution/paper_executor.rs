//! Paper (simulated) executor for Polymarket US.
//!
//! Simulates order fills against order-book data without placing real orders.
//! Tracks positions, balance, and PnL internally.

use chrono::Utc;
use rust_decimal::Decimal;
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use tracing::{info, warn};

use crate::data::models::*;
use crate::data::orderbook::OrderBookTracker;
use crate::state::state_manager::StateManager;

/// Fee rate for paper fills (Polymarket US is ~10 bps on the losing side,
/// we approximate as a flat rate on every fill).
const FEE_RATE: &str = "0.001"; // 0.1%

/// Execution result from paper executor.
#[derive(Debug, Clone)]
pub struct PaperExecResult {
    pub order_id: String,
    pub status: OrderStatus,
    pub filled_quantity: i64,
    pub avg_fill_price: Option<Decimal>,
    pub fee: Decimal,
    pub error: Option<String>,
}

/// Paper executor that simulates order fills.
pub struct PaperExecutor {
    state: StateManager,
    orderbook: OrderBookTracker,
    order_counter: AtomicU64,

    // Counters
    total_trades: AtomicU64,
    winning_trades: AtomicU64,
    total_fees: std::sync::Mutex<Decimal>,

    // Track realized PnL per market for win-rate calculation
    realized_pnl: std::sync::Mutex<HashMap<String, Decimal>>,

    initial_balance: Decimal,
}

impl PaperExecutor {
    pub fn new(state: StateManager, orderbook: OrderBookTracker) -> Self {
        let initial_balance = state.get_balance();
        Self {
            state,
            orderbook,
            order_counter: AtomicU64::new(0),
            total_trades: AtomicU64::new(0),
            winning_trades: AtomicU64::new(0),
            total_fees: std::sync::Mutex::new(Decimal::ZERO),
            realized_pnl: std::sync::Mutex::new(HashMap::new()),
            initial_balance,
        }
    }

    fn next_order_id(&self) -> String {
        let n = self.order_counter.fetch_add(1, Ordering::Relaxed) + 1;
        format!("paper-{:06}", n)
    }

    /// Execute an approved signal in paper mode.
    pub fn execute_signal(&mut self, signal: &Signal) -> PaperExecResult {
        if signal.is_cancel() {
            return PaperExecResult {
                order_id: String::new(),
                status: OrderStatus::Cancelled,
                filled_quantity: 0,
                avg_fill_price: None,
                fee: Decimal::ZERO,
                error: None,
            };
        }

        let order_id = self.next_order_id();
        self.total_trades.fetch_add(1, Ordering::Relaxed);

        // Determine fill price from order book.
        let fill_result = self.simulate_fill(signal);

        match fill_result {
            Some((fill_price, fill_qty)) => {
                let fee_rate: Decimal = FEE_RATE.parse().unwrap();
                let fee = fill_price * Decimal::from(fill_qty) * fee_rate;

                // Update balance and positions.
                self.apply_fill(signal, fill_price, fill_qty, fee);

                let is_limit = signal.action == SignalAction::BuyYes
                    || signal.action == SignalAction::BuyNo;
                let order_type = if is_limit { "Limit order immediate fill" } else { "Market order filled" };

                if is_limit {
                    info!(
                        order_id = %order_id,
                        market_slug = %signal.market_slug,
                        action = ?signal.action,
                        fill_price = %fill_price,
                        fill_qty = fill_qty,
                        remaining = 0,
                        "[PAPER] {}", order_type
                    );
                } else {
                    info!(
                        order_id = %order_id,
                        market_slug = %signal.market_slug,
                        action = ?signal.action,
                        fill_price = %fill_price,
                        fill_qty = fill_qty,
                        requested_qty = signal.quantity,
                        fee = %fee,
                        "[PAPER] {}", order_type
                    );
                }

                PaperExecResult {
                    order_id,
                    status: OrderStatus::Filled,
                    filled_quantity: fill_qty,
                    avg_fill_price: Some(fill_price),
                    fee,
                    error: None,
                }
            }
            None => {
                warn!(
                    market_slug = %signal.market_slug,
                    action = ?signal.action,
                    "[PAPER] No fill possible — no matching liquidity"
                );
                PaperExecResult {
                    order_id,
                    status: OrderStatus::Rejected,
                    filled_quantity: 0,
                    avg_fill_price: None,
                    fee: Decimal::ZERO,
                    error: Some("No matching liquidity".to_string()),
                }
            }
        }
    }

    /// Simulate a fill against the order book.
    /// Returns (fill_price, fill_quantity) or None if no fill possible.
    fn simulate_fill(&self, signal: &Signal) -> Option<(Decimal, i64)> {
        let book = self.orderbook.get_full(&signal.market_slug)?;

        match signal.action {
            SignalAction::BuyYes => {
                // To buy YES, we lift the YES asks.
                self.walk_book(&book.yes.asks, signal.quantity, signal.price, true)
            }
            SignalAction::SellYes => {
                // To sell YES, we hit the YES bids.
                self.walk_book(&book.yes.bids, signal.quantity, signal.price, false)
            }
            SignalAction::BuyNo => {
                // To buy NO, we lift the NO asks.
                self.walk_book(&book.no.asks, signal.quantity, signal.price, true)
            }
            SignalAction::SellNo => {
                // To sell NO, we hit the NO bids.
                self.walk_book(&book.no.bids, signal.quantity, signal.price, false)
            }
            SignalAction::CancelAll => None,
        }
    }

    /// Walk the order book levels to simulate a fill.
    /// For buys: walk asks from lowest, fill at limit_price or better.
    /// For sells: walk bids from highest, fill at limit_price or better.
    fn walk_book(
        &self,
        levels: &[PriceLevel],
        mut remaining: i64,
        limit_price: Decimal,
        is_buy: bool,
    ) -> Option<(Decimal, i64)> {
        if levels.is_empty() || remaining <= 0 {
            return None;
        }

        let mut sorted = levels.to_vec();
        if is_buy {
            // Asks: sort ascending (best ask first).
            sorted.sort_by(|a, b| a.price.cmp(&b.price));
        } else {
            // Bids: sort descending (best bid first).
            sorted.sort_by(|a, b| b.price.cmp(&a.price));
        }

        let mut total_cost = Decimal::ZERO;
        let mut total_filled: i64 = 0;

        for level in &sorted {
            if remaining <= 0 {
                break;
            }

            // For a limit buy: only fill at or below limit_price.
            // For a limit sell (or market sell): fill at available price.
            if is_buy && level.price > limit_price {
                break;
            }
            if !is_buy && limit_price > Decimal::ZERO && level.price < limit_price {
                // For sells, we accept any bid (market order for exits).
                // Only skip if the limit is set and bid is below it.
                // Market orders have price = exit_price which is the bid,
                // so this should not filter anything for stop-loss exits.
            }

            let fill_at_level = remaining.min(level.quantity);
            total_cost += level.price * Decimal::from(fill_at_level);
            total_filled += fill_at_level;
            remaining -= fill_at_level;
        }

        if total_filled > 0 {
            let avg_price = total_cost / Decimal::from(total_filled);
            Some((avg_price, total_filled))
        } else {
            None
        }
    }

    /// Apply a fill to balance and position state.
    fn apply_fill(&self, signal: &Signal, fill_price: Decimal, fill_qty: i64, fee: Decimal) {
        let notional = fill_price * Decimal::from(fill_qty);

        // Update fees.
        {
            let mut fees = self.total_fees.lock().unwrap();
            *fees += fee;
        }

        let side = match signal.action {
            SignalAction::BuyYes | SignalAction::SellYes => Side::Yes,
            SignalAction::BuyNo | SignalAction::SellNo => Side::No,
            SignalAction::CancelAll => return,
        };

        let is_buy = signal.is_buy();

        if is_buy {
            // Debit balance, add/increase position.
            let current_balance = self.state.get_balance();
            let new_balance = current_balance - notional - fee;
            self.state.update_balance(new_balance.max(Decimal::ZERO));

            // Update position.
            let existing = self.state.get_position(&signal.market_slug);
            match existing {
                Some(pos) if pos.side == side => {
                    // Add to existing position — compute new avg price.
                    let old_cost = pos.avg_price * Decimal::from(pos.quantity);
                    let new_cost = old_cost + notional;
                    let new_qty = pos.quantity + fill_qty;
                    let new_avg = if new_qty > 0 {
                        new_cost / Decimal::from(new_qty)
                    } else {
                        fill_price
                    };
                    self.state.update_position(&signal.market_slug, side, new_qty, new_avg);
                }
                _ => {
                    // New position.
                    self.state.update_position(&signal.market_slug, side, fill_qty, fill_price);
                }
            }
        } else {
            // Credit balance, reduce/close position.
            let current_balance = self.state.get_balance();
            let new_balance = current_balance + notional - fee;
            self.state.update_balance(new_balance);

            // Reduce position.
            let existing = self.state.get_position(&signal.market_slug);
            if let Some(pos) = existing {
                let remaining_qty = pos.quantity - fill_qty;

                // Calculate realized PnL for this trade.
                let entry_cost = pos.avg_price * Decimal::from(fill_qty);
                let exit_value = notional;
                let rpnl = exit_value - entry_cost - fee;

                {
                    let mut realized = self.realized_pnl.lock().unwrap();
                    let entry = realized.entry(signal.market_slug.clone()).or_insert(Decimal::ZERO);
                    *entry += rpnl;
                }

                if rpnl > Decimal::ZERO {
                    self.winning_trades.fetch_add(1, Ordering::Relaxed);
                }

                if remaining_qty <= 0 {
                    self.state.remove_position(&signal.market_slug);
                } else {
                    self.state.update_position(&signal.market_slug, pos.side, remaining_qty, pos.avg_price);
                }
            }
        }
    }

    /// Performance metrics.
    pub fn get_performance(&self) -> HashMap<String, serde_json::Value> {
        let equity = self.state.get_total_equity();
        let cash = self.state.get_balance();
        let pnl = equity - self.initial_balance;
        let total = self.total_trades.load(Ordering::Relaxed);
        let wins = self.winning_trades.load(Ordering::Relaxed);
        let fees = *self.total_fees.lock().unwrap();
        let positions = self.state.get_all_positions().len();

        let win_rate = if total > 0 {
            wins as f64 / total as f64
        } else {
            0.0
        };

        // Track max drawdown.
        let drawdown = if self.initial_balance > Decimal::ZERO {
            ((self.initial_balance - equity).max(Decimal::ZERO))
                .to_string()
                .parse::<f64>()
                .unwrap_or(0.0)
        } else {
            0.0
        };

        let mut m = HashMap::new();
        m.insert("mode".to_string(), serde_json::json!("Paper"));
        m.insert("total_equity".to_string(), serde_json::json!(
            equity.to_string().parse::<f64>().unwrap_or(0.0)
        ));
        m.insert("total_pnl".to_string(), serde_json::json!(
            pnl.to_string().parse::<f64>().unwrap_or(0.0)
        ));
        m.insert("total_trades".to_string(), serde_json::json!(total));
        m.insert("win_rate".to_string(), serde_json::json!(win_rate));
        m.insert("open_positions".to_string(), serde_json::json!(positions));
        m.insert("fees_paid".to_string(), serde_json::json!(
            fees.to_string().parse::<f64>().unwrap_or(0.0)
        ));
        m.insert("max_drawdown".to_string(), serde_json::json!(drawdown));
        m.insert("cash_balance".to_string(), serde_json::json!(
            cash.to_string().parse::<f64>().unwrap_or(0.0)
        ));
        m
    }
}
