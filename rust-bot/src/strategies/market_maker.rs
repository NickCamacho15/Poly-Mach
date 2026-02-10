//! Market making strategy for Polymarket US.
//!
//! Two-sided market making: posts bid and ask orders around mid-price,
//! capturing the spread when both sides fill. Includes inventory management,
//! maker-only enforcement, and stop-loss exits.

#![allow(dead_code)]

use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use std::collections::HashMap;
use tracing::{debug, info};

use crate::data::models::{Signal, SignalAction, Urgency};
use crate::state::state_manager::{MarketState, PositionState};

/// Market maker configuration.
#[derive(Debug, Clone)]
pub struct MarketMakerConfig {
    pub spread: Decimal,
    pub order_size: Decimal,
    pub max_inventory: Decimal,
    pub refresh_interval_secs: f64,
    pub min_spread: Decimal,
    pub max_spread: Decimal,
    pub price_tolerance: Decimal,
    pub enabled_markets: Vec<String>,
    pub inventory_skew_factor: Decimal,
    pub min_spread_pct: Decimal,
    pub maker_only: bool,
    pub stop_loss_pct: Decimal,
    pub aggressive_stop_loss_pct: Decimal,
    pub max_underwater_hold_seconds: i64,
}

impl Default for MarketMakerConfig {
    fn default() -> Self {
        Self {
            spread: Decimal::new(2, 2),        // 0.02
            order_size: Decimal::new(10, 0),    // $10
            max_inventory: Decimal::new(50, 0), // $50
            refresh_interval_secs: 5.0,
            min_spread: Decimal::new(1, 2),    // 0.01
            max_spread: Decimal::new(10, 2),   // 0.10
            price_tolerance: Decimal::new(5, 3), // 0.005
            enabled_markets: Vec::new(),
            inventory_skew_factor: Decimal::new(5, 1), // 0.5
            min_spread_pct: Decimal::new(2, 2),        // 0.02 = 2%
            maker_only: true,
            stop_loss_pct: Decimal::new(5, 2),            // 5%
            aggressive_stop_loss_pct: Decimal::new(3, 2), // 3%
            max_underwater_hold_seconds: 600,              // 10 min
        }
    }
}

/// Tracks current quote state for a market.
#[derive(Debug, Clone)]
struct QuoteState {
    bid_price: Option<Decimal>,
    ask_price: Option<Decimal>,
    last_refresh: DateTime<Utc>,
    last_mid_price: Option<Decimal>,
}

/// Two-sided market making strategy.
pub struct MarketMakerStrategy {
    config: MarketMakerConfig,
    quotes: HashMap<String, QuoteState>,
    enabled: bool,
}

impl MarketMakerStrategy {
    pub fn new(config: MarketMakerConfig) -> Self {
        info!(
            spread = %config.spread,
            order_size = %config.order_size,
            max_inventory = %config.max_inventory,
            "MarketMakerStrategy initialized"
        );
        Self {
            config,
            quotes: HashMap::new(),
            enabled: true,
        }
    }

    pub fn set_enabled(&mut self, enabled: bool) {
        self.enabled = enabled;
    }

    /// Generate signals on market update.
    pub fn on_market_update(
        &mut self,
        market: &MarketState,
        position: Option<&PositionState>,
    ) -> Vec<Signal> {
        if !self.enabled {
            return Vec::new();
        }
        if !self.is_market_enabled(&market.market_slug) {
            return Vec::new();
        }
        if !market.has_valid_prices() {
            return Vec::new();
        }

        let quote_state = self.get_or_create_quote(&market.market_slug);
        if self.should_refresh(&market, &quote_state) {
            self.generate_quote_signals(market, position)
        } else {
            Vec::new()
        }
    }

    /// Check positions for stop-loss exits.
    pub fn check_stop_loss(
        &self,
        position: &PositionState,
        market: &MarketState,
    ) -> Vec<Signal> {
        let mut signals = Vec::new();

        // For YES: sell at yes_bid. For NO: sell at no_bid.
        let (exit_price, effective_close_price) = match position.side {
            crate::data::models::Side::Yes => {
                let exit = market.yes_bid;
                (exit, exit)
            }
            crate::data::models::Side::No => {
                let exit = market.no_bid;
                (exit, exit)
            }
        };

        let (exit_price, effective_close_price) = match (exit_price, effective_close_price) {
            (Some(ep), Some(ecp)) if position.avg_price > Decimal::ZERO => (ep, ecp),
            _ => return signals,
        };

        let pnl_pct = (effective_close_price - position.avg_price) / position.avg_price;
        let age_seconds = (Utc::now() - position.created_at).num_seconds();

        let stop_loss_trigger = pnl_pct <= -self.config.aggressive_stop_loss_pct;
        let hard_stop_trigger = pnl_pct <= -self.config.stop_loss_pct;
        let time_exit_trigger =
            age_seconds >= self.config.max_underwater_hold_seconds && pnl_pct < Decimal::ZERO;

        if stop_loss_trigger || hard_stop_trigger || time_exit_trigger {
            let reason = if hard_stop_trigger || stop_loss_trigger {
                format!("Stop-loss: unrealized {:.1}%", pnl_pct * Decimal::ONE_HUNDRED)
            } else {
                format!(
                    "Time-based exit: age={}s unrealized {:.1}%",
                    age_seconds,
                    pnl_pct * Decimal::ONE_HUNDRED
                )
            };

            // SellYes closes YES positions, SellNo closes NO positions.
            let (action, price) = match position.side {
                crate::data::models::Side::Yes => {
                    (SignalAction::SellYes, clamp_price(exit_price))
                }
                crate::data::models::Side::No => {
                    (SignalAction::SellNo, clamp_price(exit_price))
                }
            };

            info!(
                market_slug = %position.market_slug,
                side = %position.side,
                pnl_pct = %pnl_pct,
                reason = %reason,
                "Risk exit triggered"
            );

            signals.push(Signal {
                market_slug: position.market_slug.clone(),
                action,
                price,
                quantity: position.quantity,
                urgency: Urgency::High,
                confidence: 0.95,
                strategy_name: "market_maker".to_string(),
                reason,
                metadata: HashMap::new(),
                timestamp: Utc::now(),
            });
        }

        signals
    }

    /// Generate quote signals (cancel existing + place new).
    fn generate_quote_signals(
        &mut self,
        market: &MarketState,
        position: Option<&PositionState>,
    ) -> Vec<Signal> {
        let mut signals = Vec::new();

        // Check spread requirements: skip if too tight or too wide.
        let spread_pct = self.market_spread_pct(market);
        if spread_pct.map(|s| s < self.config.min_spread_pct).unwrap_or(true) {
            return signals;
        }
        if spread_pct.map(|s| s > self.config.max_spread).unwrap_or(false) {
            return signals;
        }

        // Calculate quotes.
        let (bid_price, ask_price) = match self.calculate_quotes(market, position) {
            Some(prices) => prices,
            None => return signals,
        };

        let bid_qty = self.calculate_quantity(bid_price);
        let ask_qty = self.calculate_quantity(ask_price);

        // Inventory limit: only quote to reduce if at max.
        let (mut final_bid_qty, mut final_ask_qty) = (bid_qty, ask_qty);
        if let Some(pos) = position {
            let pos_value = pos.cost_basis();
            if pos_value >= self.config.max_inventory {
                match pos.side {
                    crate::data::models::Side::Yes => final_bid_qty = 0,
                    crate::data::models::Side::No => final_ask_qty = 0,
                }
            }
        }

        // Cancel existing.
        let has_active = self
            .quotes
            .get(&market.market_slug)
            .map(|q| q.bid_price.is_some() || q.ask_price.is_some())
            .unwrap_or(false);

        if has_active {
            signals.push(Signal {
                market_slug: market.market_slug.clone(),
                action: SignalAction::CancelAll,
                price: Decimal::ZERO,
                quantity: 0,
                urgency: Urgency::Low,
                confidence: 1.0,
                strategy_name: "market_maker".to_string(),
                reason: "Refreshing quotes".to_string(),
                metadata: HashMap::new(),
                timestamp: Utc::now(),
            });
        }

        // Post bid (buy YES).
        if final_bid_qty > 0 {
            signals.push(Signal {
                market_slug: market.market_slug.clone(),
                action: SignalAction::BuyYes,
                price: bid_price,
                quantity: final_bid_qty,
                urgency: Urgency::Low,
                confidence: 0.8,
                strategy_name: "market_maker".to_string(),
                reason: format!("MM bid at {:.4}", bid_price),
                metadata: self.quote_metadata(market, spread_pct),
                timestamp: Utc::now(),
            });
        }

        // Post ask (sell YES).
        if final_ask_qty > 0 {
            signals.push(Signal {
                market_slug: market.market_slug.clone(),
                action: SignalAction::SellYes,
                price: ask_price,
                quantity: final_ask_qty,
                urgency: Urgency::Low,
                confidence: 0.8,
                strategy_name: "market_maker".to_string(),
                reason: format!("MM ask at {:.4}", ask_price),
                metadata: self.quote_metadata(market, spread_pct),
                timestamp: Utc::now(),
            });
        }

        // Update quote state.
        self.quotes.insert(
            market.market_slug.clone(),
            QuoteState {
                bid_price: if final_bid_qty > 0 {
                    Some(bid_price)
                } else {
                    None
                },
                ask_price: if final_ask_qty > 0 {
                    Some(ask_price)
                } else {
                    None
                },
                last_refresh: Utc::now(),
                last_mid_price: market.yes_mid_price(),
            },
        );

        debug!(
            market_slug = %market.market_slug,
            bid = %bid_price,
            ask = %ask_price,
            bid_qty = final_bid_qty,
            ask_qty = final_ask_qty,
            "Generated MM quotes"
        );

        signals
    }

    /// Calculate bid and ask prices from mid-price and spread.
    fn calculate_quotes(
        &self,
        market: &MarketState,
        position: Option<&PositionState>,
    ) -> Option<(Decimal, Decimal)> {
        let mid = market.yes_mid_price()?;
        let half_spread = self.config.spread / Decimal::TWO;

        // Inventory skew.
        let mut bid_skew = Decimal::ZERO;
        let mut ask_skew = Decimal::ZERO;
        if let Some(pos) = position {
            if pos.quantity > 0 && self.config.max_inventory > Decimal::ZERO {
                let pos_value = pos.cost_basis();
                let inventory_ratio =
                    (pos_value / self.config.max_inventory).min(Decimal::TWO);
                let skew = inventory_ratio * self.config.inventory_skew_factor * half_spread;

                match pos.side {
                    crate::data::models::Side::Yes => {
                        bid_skew = -skew;
                        ask_skew = -skew;
                    }
                    crate::data::models::Side::No => {
                        bid_skew = skew;
                        ask_skew = skew;
                    }
                }
            }
        }

        let mut bid = clamp_price(mid - half_spread + bid_skew);
        let mut ask = clamp_price(mid + half_spread + ask_skew);

        // Ensure bid < ask.
        if bid >= ask {
            bid = clamp_price(mid - half_spread);
            ask = clamp_price(mid + half_spread);
        }

        // Maker-only enforcement.
        if self.config.maker_only {
            if let Some(yes_bid) = market.yes_bid {
                bid = bid.min(yes_bid);
            }
            if let Some(yes_ask) = market.yes_ask {
                ask = ask.max(yes_ask);
            }
            bid = clamp_price(bid);
            ask = clamp_price(ask);

            if bid >= ask {
                let m = market.yes_mid_price().unwrap_or(mid);
                let h = self.config.spread / Decimal::TWO;
                bid = clamp_price(
                    m - h
                        + market
                            .yes_bid
                            .map(|b| (m - h).min(b))
                            .unwrap_or(m - h)
                        - (m - h),
                );
                ask = clamp_price(
                    m + h
                        + market
                            .yes_ask
                            .map(|a| (m + h).max(a))
                            .unwrap_or(m + h)
                        - (m + h),
                );
                // Simpler fallback:
                if let (Some(yb), Some(ya)) = (market.yes_bid, market.yes_ask) {
                    bid = clamp_price(yb);
                    ask = clamp_price(ya);
                }
            }
        }

        Some((bid, ask))
    }

    fn calculate_quantity(&self, price: Decimal) -> i64 {
        if price <= Decimal::ZERO {
            return 0;
        }
        let qty = (self.config.order_size / price)
            .floor()
            .to_string()
            .parse::<i64>()
            .unwrap_or(0);
        qty.max(1)
    }

    fn market_spread_pct(&self, market: &MarketState) -> Option<Decimal> {
        let (bid, ask) = (market.yes_bid?, market.yes_ask?);
        if bid <= Decimal::ZERO || ask <= Decimal::ZERO || bid >= ask {
            return None;
        }
        let mid = (bid + ask) / Decimal::TWO;
        if mid <= Decimal::ZERO {
            return None;
        }
        Some((ask - bid) / mid)
    }

    fn should_refresh(&self, market: &MarketState, quote: &QuoteState) -> bool {
        if quote.bid_price.is_none() && quote.ask_price.is_none() {
            return true;
        }
        let elapsed = (Utc::now() - quote.last_refresh).num_milliseconds() as f64 / 1000.0;
        if elapsed >= self.config.refresh_interval_secs {
            return true;
        }
        if let (Some(current_mid), Some(last_mid)) =
            (market.yes_mid_price(), quote.last_mid_price)
        {
            if (current_mid - last_mid).abs() >= self.config.price_tolerance {
                return true;
            }
        }
        false
    }

    fn is_market_enabled(&self, slug: &str) -> bool {
        if self.config.enabled_markets.is_empty() {
            return true;
        }
        self.config
            .enabled_markets
            .iter()
            .any(|p| {
                if p.ends_with('*') {
                    slug.starts_with(&p[..p.len() - 1])
                } else {
                    p == slug
                }
            })
    }

    fn get_or_create_quote(&mut self, slug: &str) -> QuoteState {
        self.quotes
            .entry(slug.to_string())
            .or_insert_with(|| QuoteState {
                bid_price: None,
                ask_price: None,
                last_refresh: Utc::now(),
                last_mid_price: None,
            })
            .clone()
    }

    fn quote_metadata(
        &self,
        market: &MarketState,
        spread_pct: Option<Decimal>,
    ) -> HashMap<String, serde_json::Value> {
        let mut m = HashMap::new();
        if let Some(mid) = market.yes_mid_price() {
            m.insert(
                "mid_price".to_string(),
                serde_json::json!(mid.to_string().parse::<f64>().unwrap_or(0.0)),
            );
        }
        m.insert(
            "spread".to_string(),
            serde_json::json!(self.config.spread.to_string().parse::<f64>().unwrap_or(0.0)),
        );
        if let Some(sp) = spread_pct {
            m.insert(
                "spread_pct".to_string(),
                serde_json::json!(sp.to_string().parse::<f64>().unwrap_or(0.0)),
            );
        }
        m.insert("maker_only".to_string(), serde_json::json!(self.config.maker_only));
        m.insert("post_only".to_string(), serde_json::json!(true));
        m
    }
}

/// Clamp price to [0.01, 0.99] range (valid Polymarket binary contract prices).
fn clamp_price(price: Decimal) -> Decimal {
    let min = Decimal::new(1, 2); // 0.01
    let max = Decimal::new(99, 2); // 0.99
    price.max(min).min(max)
}
