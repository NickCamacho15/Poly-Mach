//! Paper trading executor: simulates order fills against real order book data.
//!
//! Mirrors the `LiveExecutor` interface exactly so strategies can be tested
//! without risking real money. If paper trading is profitable but live trading
//! isn't, the problem is execution quality (slippage, latency). If paper
//! trading also loses money, the strategy itself is broken.

#![allow(dead_code)]

use rust_decimal::Decimal;
use chrono::Utc;
use std::collections::HashMap;
use tracing::{info, debug};

use crate::data::models::*;
use crate::data::orderbook::OrderBookTracker;
use crate::execution::executor::ExecResult;
use crate::state::state_manager::{StateManager, OrderState};

// =============================================================================
// Constants
// =============================================================================

/// Taker fee: 10 basis points (0.1%).
const TAKER_FEE_RATE: &str = "0.001";

/// Simulated slippage for market orders (5 bps beyond best price).
const MARKET_ORDER_SLIPPAGE_BPS: &str = "0.0005";

// =============================================================================
// Paper Fill
// =============================================================================

/// A simulated fill event.
#[derive(Debug, Clone)]
pub struct PaperFill {
    pub order_id: String,
    pub market_slug: String,
    pub side: Side,
    pub is_buy: bool,
    pub price: Decimal,
    pub quantity: i64,
    pub fee: Decimal,
    pub timestamp: chrono::DateTime<Utc>,
}

// =============================================================================
// Resting Order
// =============================================================================

/// A limit order resting in the simulated book, waiting to be filled.
#[derive(Debug, Clone)]
pub struct RestingOrder {
    pub order_id: String,
    pub market_slug: String,
    pub intent: OrderIntent,
    pub price: Decimal,
    pub total_quantity: i64,
    pub filled_quantity: i64,
    pub created_at: chrono::DateTime<Utc>,
}

impl RestingOrder {
    pub fn remaining(&self) -> i64 {
        self.total_quantity - self.filled_quantity
    }
}

// =============================================================================
// Performance Metrics
// =============================================================================

/// Tracks cumulative paper trading performance.
#[derive(Debug, Clone)]
pub struct PaperPerformance {
    pub total_trades: u64,
    pub winning_trades: u64,
    pub losing_trades: u64,
    pub total_pnl: Decimal,
    pub max_equity: Decimal,
    pub max_drawdown: Decimal,
    pub total_fees_paid: Decimal,
    pub total_volume: Decimal,
}

impl PaperPerformance {
    fn new(initial_equity: Decimal) -> Self {
        Self {
            total_trades: 0,
            winning_trades: 0,
            losing_trades: 0,
            total_pnl: Decimal::ZERO,
            max_equity: initial_equity,
            max_drawdown: Decimal::ZERO,
            total_fees_paid: Decimal::ZERO,
            total_volume: Decimal::ZERO,
        }
    }

    pub fn win_rate(&self) -> f64 {
        if self.total_trades == 0 {
            return 0.0;
        }
        self.winning_trades as f64 / self.total_trades as f64
    }

    /// Update high-water mark and drawdown given current equity.
    fn update_drawdown(&mut self, current_equity: Decimal) {
        if current_equity > self.max_equity {
            self.max_equity = current_equity;
        }
        let drawdown = self.max_equity - current_equity;
        if drawdown > self.max_drawdown {
            self.max_drawdown = drawdown;
        }
    }
}

// =============================================================================
// Paper Position (internal tracking with entry price for P&L)
// =============================================================================

/// Internal position tracker that records entry prices per fill for
/// accurate realized P&L calculation.
#[derive(Debug, Clone)]
struct PaperPosition {
    side: Side,
    quantity: i64,
    avg_price: Decimal,
    total_cost: Decimal,
}

impl PaperPosition {
    fn new(side: Side, quantity: i64, price: Decimal) -> Self {
        Self {
            side,
            quantity,
            avg_price: price,
            total_cost: price * Decimal::from(quantity),
        }
    }

    /// Add to position (same direction). Updates weighted average price.
    fn add(&mut self, quantity: i64, price: Decimal) {
        let new_cost = price * Decimal::from(quantity);
        self.total_cost += new_cost;
        self.quantity += quantity;
        if self.quantity > 0 {
            self.avg_price = self.total_cost / Decimal::from(self.quantity);
        }
    }

    /// Reduce position. Returns realized P&L for the closed portion.
    fn reduce(&mut self, quantity: i64, exit_price: Decimal) -> Decimal {
        let close_qty = quantity.min(self.quantity);
        if close_qty <= 0 {
            return Decimal::ZERO;
        }

        // Realized P&L = (exit_price - avg_entry) * quantity
        let pnl = (exit_price - self.avg_price) * Decimal::from(close_qty);

        // Reduce total cost proportionally.
        let cost_reduction = self.avg_price * Decimal::from(close_qty);
        self.total_cost -= cost_reduction;
        self.quantity -= close_qty;

        if self.quantity <= 0 {
            self.quantity = 0;
            self.total_cost = Decimal::ZERO;
            self.avg_price = Decimal::ZERO;
        }

        pnl
    }
}

// =============================================================================
// Paper Executor
// =============================================================================

/// Paper trading executor that simulates order fills against real order book
/// data. Implements the same `execute_signal` interface as `LiveExecutor`.
pub struct PaperExecutor {
    state: StateManager,
    orderbook: OrderBookTracker,

    /// Starting balance for P&L calculations.
    initial_balance: Decimal,

    /// Internal position tracking with entry price history.
    positions: HashMap<String, PaperPosition>,

    /// Resting limit orders waiting to be filled.
    resting_orders: HashMap<String, RestingOrder>,

    /// Completed fills log.
    fill_history: Vec<PaperFill>,

    /// Performance metrics.
    performance: PaperPerformance,

    /// Monotonic order ID counter.
    next_order_id: u64,

    /// Fee rate.
    fee_rate: Decimal,

    /// Slippage estimate for market orders.
    slippage_bps: Decimal,
}

impl PaperExecutor {
    pub fn new(state: StateManager, orderbook: OrderBookTracker) -> Self {
        let initial_balance = state.get_balance();
        let fee_rate = TAKER_FEE_RATE.parse::<Decimal>().unwrap();
        let slippage_bps = MARKET_ORDER_SLIPPAGE_BPS.parse::<Decimal>().unwrap();
        Self {
            state,
            orderbook,
            initial_balance,
            positions: HashMap::new(),
            resting_orders: HashMap::new(),
            fill_history: Vec::new(),
            performance: PaperPerformance::new(initial_balance),
            next_order_id: 1,
            fee_rate,
            slippage_bps,
        }
    }

    /// Create with a custom fee rate and slippage.
    pub fn with_params(
        state: StateManager,
        orderbook: OrderBookTracker,
        fee_rate: Decimal,
        slippage_bps: Decimal,
    ) -> Self {
        let initial_balance = state.get_balance();
        Self {
            state,
            orderbook,
            initial_balance,
            positions: HashMap::new(),
            resting_orders: HashMap::new(),
            fill_history: Vec::new(),
            performance: PaperPerformance::new(initial_balance),
            next_order_id: 1,
            fee_rate,
            slippage_bps,
        }
    }

    // =========================================================================
    // Public Interface (mirrors LiveExecutor)
    // =========================================================================

    /// Execute a signal exactly as `LiveExecutor::execute_signal` would.
    /// Market orders fill immediately against the book; limit orders may
    /// rest if the book cannot fill them.
    pub fn execute_signal(&mut self, signal: &Signal) -> ExecResult {
        // Handle cancel signals.
        if signal.is_cancel() {
            return self.cancel_all(&signal.market_slug);
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

        let is_buy = signal.is_buy();

        // Balance pre-check for buys.
        if is_buy && signal.price > Decimal::ZERO {
            let available = self.state.get_balance();
            let cost = signal.price * Decimal::from(signal.quantity);
            let fee_estimate = cost * self.fee_rate;
            if cost + fee_estimate > available {
                return ExecResult {
                    order_id: String::new(),
                    status: OrderStatus::Rejected,
                    filled_quantity: 0,
                    avg_fill_price: None,
                    fee: Decimal::ZERO,
                    error: Some(format!(
                        "Insufficient balance: need ${:.4} (+ ${:.4} fee), have ${:.4}",
                        cost, fee_estimate, available
                    )),
                };
            }
        }

        // Position pre-check for sells.
        if signal.is_sell() {
            let side = intent.side();
            let pos_key = Self::position_key(&signal.market_slug, side);
            let held = self.positions.get(&pos_key).map(|p| p.quantity).unwrap_or(0);
            if held <= 0 {
                return ExecResult {
                    order_id: String::new(),
                    status: OrderStatus::Rejected,
                    filled_quantity: 0,
                    avg_fill_price: None,
                    fee: Decimal::ZERO,
                    error: Some(format!(
                        "No {} position in {} to sell",
                        side, signal.market_slug
                    )),
                };
            }
        }

        let order_id = self.generate_order_id();

        // Determine order type by urgency: Critical/High => market, else limit.
        let is_market_order = matches!(signal.urgency, Urgency::Critical | Urgency::High);

        // Try to fill against the order book.
        let book = self.orderbook.get_full(&signal.market_slug);

        if is_market_order {
            self.execute_market_order(
                &order_id,
                signal,
                intent,
                is_buy,
                book.as_ref(),
            )
        } else {
            self.execute_limit_order(
                &order_id,
                signal,
                intent,
                is_buy,
                book.as_ref(),
            )
        }
    }

    /// Check all resting limit orders against the current order book.
    /// Fills any that now have sufficient depth at their limit price.
    /// Returns a list of fills that occurred.
    pub fn check_resting_orders(&mut self) -> Vec<PaperFill> {
        let mut fills = Vec::new();

        // Collect order IDs to avoid borrow conflicts.
        let order_ids: Vec<String> = self.resting_orders.keys().cloned().collect();

        for order_id in order_ids {
            let order = match self.resting_orders.get(&order_id) {
                Some(o) => o.clone(),
                None => continue,
            };

            if order.remaining() <= 0 {
                self.resting_orders.remove(&order_id);
                continue;
            }

            let book = match self.orderbook.get_full(&order.market_slug) {
                Some(b) => b,
                None => continue,
            };

            let is_buy = order.intent.is_buy();
            let side = order.intent.side();

            // Determine which book side to match against.
            let book_side = match (is_buy, side) {
                (true, Side::Yes) => &book.yes,
                (true, Side::No) => &book.no,
                (false, Side::Yes) => &book.yes,
                (false, Side::No) => &book.no,
            };

            let (fill_qty, fill_price) = if is_buy {
                // Buy limit: fills if asks exist at or below limit price.
                self.simulate_limit_buy_fill(book_side, order.price, order.remaining())
            } else {
                // Sell limit: fills if bids exist at or above limit price.
                self.simulate_limit_sell_fill(book_side, order.price, order.remaining())
            };

            if fill_qty > 0 {
                let fill = self.record_fill(
                    &order_id,
                    &order.market_slug,
                    side,
                    is_buy,
                    fill_price,
                    fill_qty,
                );
                fills.push(fill);

                // Update resting order state.
                if let Some(resting) = self.resting_orders.get_mut(&order_id) {
                    resting.filled_quantity += fill_qty;
                    if resting.remaining() <= 0 {
                        // Fully filled: update StateManager order to Filled.
                        self.state.update_order(
                            &order_id,
                            Some(OrderStatus::Filled),
                            Some(resting.total_quantity),
                        );
                        self.resting_orders.remove(&order_id);
                    } else {
                        // Partially filled.
                        self.state.update_order(
                            &order_id,
                            Some(OrderStatus::PartiallyFilled),
                            Some(resting.filled_quantity),
                        );
                    }
                }
            }
        }

        fills
    }

    /// Get all resting (unfilled/partially filled) orders.
    pub fn get_resting_orders(&self) -> Vec<RestingOrder> {
        self.resting_orders.values().cloned().collect()
    }

    /// Get the full fill history.
    pub fn get_fill_history(&self) -> &[PaperFill] {
        &self.fill_history
    }

    /// Get current performance snapshot.
    pub fn get_performance_snapshot(&self) -> &PaperPerformance {
        &self.performance
    }

    /// Performance metrics as a HashMap (same format as LiveExecutor).
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
        m.insert("mode".to_string(), serde_json::json!("paper"));
        m.insert(
            "total_trades".to_string(),
            serde_json::json!(self.performance.total_trades),
        );
        m.insert(
            "winning_trades".to_string(),
            serde_json::json!(self.performance.winning_trades),
        );
        m.insert(
            "losing_trades".to_string(),
            serde_json::json!(self.performance.losing_trades),
        );
        m.insert(
            "win_rate".to_string(),
            serde_json::json!(self.performance.win_rate()),
        );
        m.insert(
            "initial_balance".to_string(),
            serde_json::json!(decimal_to_f64(self.initial_balance)),
        );
        m.insert(
            "current_balance".to_string(),
            serde_json::json!(decimal_to_f64(cash)),
        );
        m.insert(
            "position_value".to_string(),
            serde_json::json!(decimal_to_f64(pos_value)),
        );
        m.insert(
            "total_equity".to_string(),
            serde_json::json!(decimal_to_f64(equity)),
        );
        m.insert(
            "total_pnl".to_string(),
            serde_json::json!(decimal_to_f64(pnl)),
        );
        m.insert("pnl_percent".to_string(), serde_json::json!(pnl_pct));
        m.insert(
            "realized_pnl".to_string(),
            serde_json::json!(decimal_to_f64(self.performance.total_pnl)),
        );
        m.insert(
            "max_drawdown".to_string(),
            serde_json::json!(decimal_to_f64(self.performance.max_drawdown)),
        );
        m.insert(
            "total_fees_paid".to_string(),
            serde_json::json!(decimal_to_f64(self.performance.total_fees_paid)),
        );
        m.insert(
            "total_volume".to_string(),
            serde_json::json!(decimal_to_f64(self.performance.total_volume)),
        );
        m.insert(
            "open_positions".to_string(),
            serde_json::json!(self.positions.values().filter(|p| p.quantity > 0).count()),
        );
        m.insert(
            "resting_orders".to_string(),
            serde_json::json!(self.resting_orders.len()),
        );
        m
    }

    // =========================================================================
    // Market Order Execution
    // =========================================================================

    /// Simulate a market order: fill at best available price + slippage.
    /// If book depth is insufficient, partially fill.
    fn execute_market_order(
        &mut self,
        order_id: &str,
        signal: &Signal,
        intent: OrderIntent,
        is_buy: bool,
        book: Option<&OrderBook>,
    ) -> ExecResult {
        let side = intent.side();

        let book_side = match book {
            Some(b) => match (is_buy, side) {
                // Buying YES: lift asks on the YES side.
                (true, Side::Yes) => Some(&b.yes),
                // Buying NO: lift asks on the NO side.
                (true, Side::No) => Some(&b.no),
                // Selling YES: hit bids on the YES side.
                (false, Side::Yes) => Some(&b.yes),
                // Selling NO: hit bids on the NO side.
                (false, Side::No) => Some(&b.no),
            },
            None => None,
        };

        // Simulate fill by walking the book.
        let (filled_qty, avg_price) = match book_side {
            Some(bs) => {
                if is_buy {
                    self.walk_asks_with_slippage(bs, signal.quantity)
                } else {
                    self.walk_bids_with_slippage(bs, signal.quantity)
                }
            }
            None => {
                // No book data: fill at signal price + slippage as fallback.
                let slipped = if is_buy {
                    signal.price * (Decimal::ONE + self.slippage_bps)
                } else {
                    signal.price * (Decimal::ONE - self.slippage_bps)
                };
                (signal.quantity, slipped)
            }
        };

        if filled_qty == 0 {
            return ExecResult {
                order_id: order_id.to_string(),
                status: OrderStatus::Rejected,
                filled_quantity: 0,
                avg_fill_price: None,
                fee: Decimal::ZERO,
                error: Some("No liquidity available in order book".to_string()),
            };
        }

        // Record the fill (updates balance, position, metrics).
        let fill = self.record_fill(
            order_id,
            &signal.market_slug,
            side,
            is_buy,
            avg_price,
            filled_qty,
        );

        // Store order in StateManager.
        let status = if filled_qty >= signal.quantity {
            OrderStatus::Filled
        } else {
            OrderStatus::PartiallyFilled
        };

        self.state.add_order(OrderState {
            order_id: order_id.to_string(),
            market_slug: signal.market_slug.clone(),
            intent,
            price: avg_price,
            quantity: signal.quantity,
            filled_quantity: filled_qty,
            status,
        });

        info!(
            order_id = %order_id,
            market_slug = %signal.market_slug,
            action = ?signal.action,
            fill_price = %avg_price,
            fill_qty = filled_qty,
            requested_qty = signal.quantity,
            fee = %fill.fee,
            "[PAPER] Market order filled"
        );

        ExecResult {
            order_id: order_id.to_string(),
            status,
            filled_quantity: filled_qty,
            avg_fill_price: Some(avg_price),
            fee: fill.fee,
            error: None,
        }
    }

    // =========================================================================
    // Limit Order Execution
    // =========================================================================

    /// Simulate a limit order: check if the book can fill at the limit
    /// price. Any unfilled remainder rests as an open order.
    fn execute_limit_order(
        &mut self,
        order_id: &str,
        signal: &Signal,
        intent: OrderIntent,
        is_buy: bool,
        book: Option<&OrderBook>,
    ) -> ExecResult {
        let side = intent.side();

        let book_side = match book {
            Some(b) => match (is_buy, side) {
                (true, Side::Yes) => Some(&b.yes),
                (true, Side::No) => Some(&b.no),
                (false, Side::Yes) => Some(&b.yes),
                (false, Side::No) => Some(&b.no),
            },
            None => None,
        };

        // Try immediate fill against crossing levels.
        let (immediate_fill, fill_price) = match book_side {
            Some(bs) => {
                if is_buy {
                    self.simulate_limit_buy_fill(bs, signal.price, signal.quantity)
                } else {
                    self.simulate_limit_sell_fill(bs, signal.price, signal.quantity)
                }
            }
            None => (0, signal.price),
        };

        let remaining = signal.quantity - immediate_fill;
        let mut total_fee = Decimal::ZERO;

        // Record immediate fill if any.
        if immediate_fill > 0 {
            let fill = self.record_fill(
                order_id,
                &signal.market_slug,
                side,
                is_buy,
                fill_price,
                immediate_fill,
            );
            total_fee = fill.fee;

            info!(
                order_id = %order_id,
                market_slug = %signal.market_slug,
                action = ?signal.action,
                fill_price = %fill_price,
                fill_qty = immediate_fill,
                remaining = remaining,
                "[PAPER] Limit order immediate fill"
            );
        }

        // Determine final status.
        let status = if remaining <= 0 {
            OrderStatus::Filled
        } else if immediate_fill > 0 {
            OrderStatus::PartiallyFilled
        } else {
            OrderStatus::Open
        };

        // Store order in StateManager.
        self.state.add_order(OrderState {
            order_id: order_id.to_string(),
            market_slug: signal.market_slug.clone(),
            intent,
            price: signal.price,
            quantity: signal.quantity,
            filled_quantity: immediate_fill,
            status,
        });

        // If there's a remainder, add as a resting order.
        if remaining > 0 {
            let resting = RestingOrder {
                order_id: order_id.to_string(),
                market_slug: signal.market_slug.clone(),
                intent,
                price: signal.price,
                total_quantity: signal.quantity,
                filled_quantity: immediate_fill,
                created_at: Utc::now(),
            };
            self.resting_orders.insert(order_id.to_string(), resting);

            debug!(
                order_id = %order_id,
                market_slug = %signal.market_slug,
                price = %signal.price,
                remaining = remaining,
                "[PAPER] Limit order resting"
            );
        }

        ExecResult {
            order_id: order_id.to_string(),
            status,
            filled_quantity: immediate_fill,
            avg_fill_price: if immediate_fill > 0 {
                Some(fill_price)
            } else {
                None
            },
            fee: total_fee,
            error: None,
        }
    }

    // =========================================================================
    // Book Walking / Fill Simulation
    // =========================================================================

    /// Walk the ask side of the book for a market buy order.
    /// Returns (filled_quantity, volume_weighted_avg_price).
    /// Applies slippage on top of the VWAP.
    fn walk_asks_with_slippage(
        &self,
        book_side: &OrderBookSide,
        requested_qty: i64,
    ) -> (i64, Decimal) {
        let mut asks = book_side.asks.clone();
        // Sort asks ascending by price (best ask first).
        asks.sort_by(|a, b| a.price.cmp(&b.price));

        let (qty, vwap) = Self::walk_levels(&asks, requested_qty);
        if qty == 0 {
            return (0, Decimal::ZERO);
        }
        // Apply slippage: buyer pays slightly more.
        let slipped_price = vwap * (Decimal::ONE + self.slippage_bps);
        (qty, slipped_price)
    }

    /// Walk the bid side of the book for a market sell order.
    /// Returns (filled_quantity, volume_weighted_avg_price).
    /// Applies slippage on top of the VWAP.
    fn walk_bids_with_slippage(
        &self,
        book_side: &OrderBookSide,
        requested_qty: i64,
    ) -> (i64, Decimal) {
        let mut bids = book_side.bids.clone();
        // Sort bids descending by price (best bid first).
        bids.sort_by(|a, b| b.price.cmp(&a.price));

        let (qty, vwap) = Self::walk_levels(&bids, requested_qty);
        if qty == 0 {
            return (0, Decimal::ZERO);
        }
        // Apply slippage: seller receives slightly less.
        let slipped_price = vwap * (Decimal::ONE - self.slippage_bps);
        (qty, slipped_price)
    }

    /// Simulate a limit buy fill: match against asks at or below the limit price.
    /// Returns (filled_quantity, volume_weighted_avg_price).
    fn simulate_limit_buy_fill(
        &self,
        book_side: &OrderBookSide,
        limit_price: Decimal,
        requested_qty: i64,
    ) -> (i64, Decimal) {
        let mut asks: Vec<&PriceLevel> = book_side
            .asks
            .iter()
            .filter(|a| a.price <= limit_price)
            .collect();
        asks.sort_by(|a, b| a.price.cmp(&b.price));

        let mut filled = 0i64;
        let mut cost = Decimal::ZERO;

        for level in asks {
            if filled >= requested_qty {
                break;
            }
            let can_fill = level.quantity.min(requested_qty - filled);
            cost += level.price * Decimal::from(can_fill);
            filled += can_fill;
        }

        if filled == 0 {
            return (0, limit_price);
        }
        let vwap = cost / Decimal::from(filled);
        (filled, vwap)
    }

    /// Simulate a limit sell fill: match against bids at or above the limit price.
    /// Returns (filled_quantity, volume_weighted_avg_price).
    fn simulate_limit_sell_fill(
        &self,
        book_side: &OrderBookSide,
        limit_price: Decimal,
        requested_qty: i64,
    ) -> (i64, Decimal) {
        let mut bids: Vec<&PriceLevel> = book_side
            .bids
            .iter()
            .filter(|b| b.price >= limit_price)
            .collect();
        bids.sort_by(|a, b| b.price.cmp(&a.price));

        let mut filled = 0i64;
        let mut proceeds = Decimal::ZERO;

        for level in bids {
            if filled >= requested_qty {
                break;
            }
            let can_fill = level.quantity.min(requested_qty - filled);
            proceeds += level.price * Decimal::from(can_fill);
            filled += can_fill;
        }

        if filled == 0 {
            return (0, limit_price);
        }
        let vwap = proceeds / Decimal::from(filled);
        (filled, vwap)
    }

    /// Walk sorted price levels, filling up to `requested_qty`.
    /// Returns (filled_quantity, volume_weighted_avg_price).
    fn walk_levels(levels: &[PriceLevel], requested_qty: i64) -> (i64, Decimal) {
        let mut filled = 0i64;
        let mut total_cost = Decimal::ZERO;

        for level in levels {
            if filled >= requested_qty {
                break;
            }
            let can_fill = level.quantity.min(requested_qty - filled);
            total_cost += level.price * Decimal::from(can_fill);
            filled += can_fill;
        }

        if filled == 0 {
            return (0, Decimal::ZERO);
        }

        let vwap = total_cost / Decimal::from(filled);
        (filled, vwap)
    }

    // =========================================================================
    // Fill Recording & State Updates
    // =========================================================================

    /// Record a fill: update balance, position, metrics, and fill history.
    fn record_fill(
        &mut self,
        order_id: &str,
        market_slug: &str,
        side: Side,
        is_buy: bool,
        fill_price: Decimal,
        fill_qty: i64,
    ) -> PaperFill {
        let notional = fill_price * Decimal::from(fill_qty);
        let fee = notional * self.fee_rate;

        // Update balance.
        let current_balance = self.state.get_balance();
        if is_buy {
            // Buying: deduct cost + fee.
            let new_balance = current_balance - notional - fee;
            self.state.update_balance(new_balance);
        } else {
            // Selling: add proceeds - fee.
            let new_balance = current_balance + notional - fee;
            self.state.update_balance(new_balance);
        }

        // Update internal position and calculate realized P&L.
        let pos_key = Self::position_key(market_slug, side);
        let realized_pnl = if is_buy {
            // Opening or adding to a position.
            let pos = self
                .positions
                .entry(pos_key.clone())
                .or_insert_with(|| PaperPosition::new(side, 0, Decimal::ZERO));
            pos.add(fill_qty, fill_price);
            Decimal::ZERO // No realized P&L on buys.
        } else {
            // Closing or reducing a position.
            match self.positions.get_mut(&pos_key) {
                Some(pos) => {
                    let pnl = pos.reduce(fill_qty, fill_price);
                    if pos.quantity <= 0 {
                        self.positions.remove(&pos_key);
                    }
                    pnl
                }
                None => Decimal::ZERO,
            }
        };

        // Sync position to StateManager.
        if let Some(pos) = self.positions.get(&pos_key) {
            self.state
                .update_position(market_slug, side, pos.quantity, pos.avg_price);
        } else {
            self.state.remove_position(market_slug);
        }

        // Update performance metrics.
        self.performance.total_trades += 1;
        self.performance.total_fees_paid += fee;
        self.performance.total_volume += notional;
        if realized_pnl > Decimal::ZERO {
            self.performance.winning_trades += 1;
        } else if realized_pnl < Decimal::ZERO {
            self.performance.losing_trades += 1;
        }
        self.performance.total_pnl += realized_pnl;

        // Update drawdown tracking.
        let equity = self.state.get_total_equity();
        self.performance.update_drawdown(equity);

        // Build fill record.
        let fill = PaperFill {
            order_id: order_id.to_string(),
            market_slug: market_slug.to_string(),
            side,
            is_buy,
            price: fill_price,
            quantity: fill_qty,
            fee,
            timestamp: Utc::now(),
        };

        self.fill_history.push(fill.clone());

        debug!(
            order_id = %order_id,
            market_slug = %market_slug,
            side = %side,
            direction = if is_buy { "BUY" } else { "SELL" },
            price = %fill_price,
            quantity = fill_qty,
            fee = %fee,
            realized_pnl = %realized_pnl,
            balance = %self.state.get_balance(),
            "[PAPER] Fill recorded"
        );

        fill
    }

    // =========================================================================
    // Cancel
    // =========================================================================

    /// Cancel all resting orders for a given market.
    fn cancel_all(&mut self, market_slug: &str) -> ExecResult {
        let to_cancel: Vec<String> = self
            .resting_orders
            .iter()
            .filter(|(_, o)| o.market_slug == market_slug)
            .map(|(id, _)| id.clone())
            .collect();

        let count = to_cancel.len();
        for id in &to_cancel {
            self.resting_orders.remove(id);
            self.state
                .update_order(id, Some(OrderStatus::Cancelled), None);
            self.state.remove_order(id);
        }

        if count > 0 {
            info!(
                market_slug = %market_slug,
                cancelled = count,
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

    // =========================================================================
    // Helpers
    // =========================================================================

    /// Generate a unique paper order ID.
    fn generate_order_id(&mut self) -> String {
        let id = format!("paper-{:06}", self.next_order_id);
        self.next_order_id += 1;
        id
    }

    /// Position key: "{market_slug}:{side}" to distinguish YES vs NO
    /// positions in the same market.
    fn position_key(market_slug: &str, side: Side) -> String {
        format!("{}:{}", market_slug, side)
    }
}

// =============================================================================
// Utility
// =============================================================================

fn decimal_to_f64(d: Decimal) -> f64 {
    d.to_string().parse::<f64>().unwrap_or(0.0)
}

// =============================================================================
// Tests
// =============================================================================

#[cfg(test)]
mod tests {
    use super::*;
    use crate::data::models::{OrderBook, OrderBookSide, PriceLevel};

    fn setup() -> (StateManager, OrderBookTracker) {
        let state = StateManager::new(Decimal::new(10000, 2)); // $100.00
        let ob = OrderBookTracker::new();

        // Seed a simple order book.
        ob.update(OrderBook {
            market_slug: "test-market".to_string(),
            yes: OrderBookSide {
                bids: vec![
                    PriceLevel { price: Decimal::new(50, 2), quantity: 100 },
                    PriceLevel { price: Decimal::new(49, 2), quantity: 200 },
                ],
                asks: vec![
                    PriceLevel { price: Decimal::new(52, 2), quantity: 100 },
                    PriceLevel { price: Decimal::new(53, 2), quantity: 200 },
                ],
            },
            no: OrderBookSide::default(),
        });

        (state, ob)
    }

    fn buy_signal(market: &str, price: Decimal, qty: i64, urgency: Urgency) -> Signal {
        Signal {
            market_slug: market.to_string(),
            action: SignalAction::BuyYes,
            price,
            quantity: qty,
            urgency,
            confidence: 0.8,
            strategy_name: "test".to_string(),
            reason: "test buy".to_string(),
            metadata: HashMap::new(),
            timestamp: Utc::now(),
        }
    }

    fn sell_signal(market: &str, price: Decimal, qty: i64, urgency: Urgency) -> Signal {
        Signal {
            market_slug: market.to_string(),
            action: SignalAction::SellYes,
            price,
            quantity: qty,
            urgency,
            confidence: 0.8,
            strategy_name: "test".to_string(),
            reason: "test sell".to_string(),
            metadata: HashMap::new(),
            timestamp: Utc::now(),
        }
    }

    #[test]
    fn test_market_buy_fills_at_best_ask_plus_slippage() {
        let (state, ob) = setup();
        let mut executor = PaperExecutor::new(state.clone(), ob);

        let signal = buy_signal("test-market", Decimal::new(52, 2), 50, Urgency::Critical);
        let result = executor.execute_signal(&signal);

        assert_eq!(result.status, OrderStatus::Filled);
        assert_eq!(result.filled_quantity, 50);
        assert!(result.avg_fill_price.is_some());
        assert!(result.fee > Decimal::ZERO);
    }

    #[test]
    fn test_market_buy_partial_fill_low_depth() {
        // Use a large balance so the pre-check doesn't reject.
        let state = StateManager::new(Decimal::new(100000, 2)); // $1000.00
        let ob = OrderBookTracker::new();
        ob.update(OrderBook {
            market_slug: "test-market".to_string(),
            yes: OrderBookSide {
                bids: vec![
                    PriceLevel { price: Decimal::new(50, 2), quantity: 100 },
                    PriceLevel { price: Decimal::new(49, 2), quantity: 200 },
                ],
                asks: vec![
                    PriceLevel { price: Decimal::new(52, 2), quantity: 100 },
                    PriceLevel { price: Decimal::new(53, 2), quantity: 200 },
                ],
            },
            no: OrderBookSide::default(),
        });

        let mut executor = PaperExecutor::new(state, ob);

        // Request 500 but only 300 total depth available.
        let signal = buy_signal("test-market", Decimal::new(55, 2), 500, Urgency::Critical);
        let result = executor.execute_signal(&signal);

        assert_eq!(result.status, OrderStatus::PartiallyFilled);
        assert_eq!(result.filled_quantity, 300); // 100 @ 0.52 + 200 @ 0.53
    }

    #[test]
    fn test_limit_buy_rests_when_price_below_asks() {
        let (state, ob) = setup();
        let mut executor = PaperExecutor::new(state.clone(), ob);

        // Limit price below best ask: should rest.
        let signal = buy_signal("test-market", Decimal::new(48, 2), 50, Urgency::Low);
        let result = executor.execute_signal(&signal);

        assert_eq!(result.status, OrderStatus::Open);
        assert_eq!(result.filled_quantity, 0);
        assert_eq!(executor.get_resting_orders().len(), 1);
    }

    #[test]
    fn test_limit_buy_immediate_fill_when_crossing() {
        let (state, ob) = setup();
        let mut executor = PaperExecutor::new(state.clone(), ob);

        // Limit at 0.52: should fill 100 shares at 0.52.
        let signal = buy_signal("test-market", Decimal::new(52, 2), 50, Urgency::Low);
        let result = executor.execute_signal(&signal);

        assert_eq!(result.status, OrderStatus::Filled);
        assert_eq!(result.filled_quantity, 50);
    }

    #[test]
    fn test_sell_requires_position() {
        let (state, ob) = setup();
        let mut executor = PaperExecutor::new(state, ob);

        let signal = sell_signal("test-market", Decimal::new(50, 2), 10, Urgency::Low);
        let result = executor.execute_signal(&signal);

        assert_eq!(result.status, OrderStatus::Rejected);
        assert!(result.error.is_some());
    }

    #[test]
    fn test_buy_then_sell_tracks_pnl() {
        let (state, ob) = setup();
        let mut executor = PaperExecutor::new(state.clone(), ob);

        // Buy 50 at market.
        let buy = buy_signal("test-market", Decimal::new(52, 2), 50, Urgency::Critical);
        let buy_result = executor.execute_signal(&buy);
        assert_eq!(buy_result.status, OrderStatus::Filled);

        // Sell 50 at market.
        let sell = sell_signal("test-market", Decimal::new(50, 2), 50, Urgency::Critical);
        let sell_result = executor.execute_signal(&sell);
        assert_eq!(sell_result.status, OrderStatus::Filled);

        // Should have 2 trades recorded.
        assert_eq!(executor.performance.total_trades, 2);
    }

    #[test]
    fn test_fee_applied_on_every_fill() {
        let (state, ob) = setup();
        let mut executor = PaperExecutor::new(state.clone(), ob);

        let signal = buy_signal("test-market", Decimal::new(52, 2), 10, Urgency::Critical);
        let result = executor.execute_signal(&signal);

        assert!(result.fee > Decimal::ZERO);
        assert!(executor.performance.total_fees_paid > Decimal::ZERO);
    }

    #[test]
    fn test_insufficient_balance_rejected() {
        let state = StateManager::new(Decimal::new(1, 2)); // $0.01
        let ob = OrderBookTracker::new();
        ob.update(OrderBook {
            market_slug: "test-market".to_string(),
            yes: OrderBookSide {
                bids: vec![],
                asks: vec![PriceLevel {
                    price: Decimal::new(50, 2),
                    quantity: 100,
                }],
            },
            no: OrderBookSide::default(),
        });

        let mut executor = PaperExecutor::new(state, ob);
        let signal = buy_signal("test-market", Decimal::new(50, 2), 100, Urgency::Critical);
        let result = executor.execute_signal(&signal);

        assert_eq!(result.status, OrderStatus::Rejected);
        assert!(result.error.unwrap().contains("Insufficient balance"));
    }

    #[test]
    fn test_cancel_removes_resting_orders() {
        let (state, ob) = setup();
        let mut executor = PaperExecutor::new(state, ob);

        // Place a resting limit order.
        let signal = buy_signal("test-market", Decimal::new(45, 2), 50, Urgency::Low);
        executor.execute_signal(&signal);
        assert_eq!(executor.get_resting_orders().len(), 1);

        // Cancel all.
        let cancel = Signal {
            market_slug: "test-market".to_string(),
            action: SignalAction::CancelAll,
            price: Decimal::ZERO,
            quantity: 0,
            urgency: Urgency::Critical,
            confidence: 1.0,
            strategy_name: "test".to_string(),
            reason: "cancel".to_string(),
            metadata: HashMap::new(),
            timestamp: Utc::now(),
        };
        executor.execute_signal(&cancel);
        assert_eq!(executor.get_resting_orders().len(), 0);
    }
}
