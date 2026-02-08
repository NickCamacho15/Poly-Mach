//! Statistical edge strategy comparing sportsbook odds to Polymarket prices.
//!
//! Generates signals when sportsbook implied probabilities diverge
//! significantly from Polymarket contract prices.

use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use std::collections::HashMap;

use crate::data::models::{Signal, SignalAction, Urgency};
use crate::state::state_manager::MarketState;

/// Configuration for statistical edge strategy.
#[derive(Debug, Clone)]
pub struct StatisticalEdgeConfig {
    pub min_edge: Decimal,
    pub order_size: Decimal,
    pub cooldown_seconds: f64,
    pub enabled_markets: Vec<String>,
}

impl Default for StatisticalEdgeConfig {
    fn default() -> Self {
        Self {
            min_edge: Decimal::new(2, 2),    // 0.02
            order_size: Decimal::new(10, 0), // $10
            cooldown_seconds: 10.0,
            enabled_markets: Vec::new(),
        }
    }
}

/// Snapshot of odds from an external sportsbook.
#[derive(Debug, Clone)]
pub struct OddsSnapshot {
    pub event_id: String,
    pub market_slug: Option<String>,
    pub provider: String,
    pub yes_probability: Decimal,
    pub confidence: f64,
    pub timestamp: DateTime<Utc>,
}

/// Statistical edge strategy.
pub struct StatisticalEdgeStrategy {
    config: StatisticalEdgeConfig,
    enabled: bool,
    latest_odds: HashMap<String, OddsSnapshot>,
    last_signal_at: HashMap<String, DateTime<Utc>>,
}

impl StatisticalEdgeStrategy {
    pub fn new(config: StatisticalEdgeConfig) -> Self {
        Self {
            config,
            enabled: true,
            latest_odds: HashMap::new(),
            last_signal_at: HashMap::new(),
        }
    }

    pub fn set_enabled(&mut self, enabled: bool) {
        self.enabled = enabled;
    }

    /// Ingest a new odds snapshot from sportsbook.
    pub fn ingest_odds(&mut self, snapshot: OddsSnapshot) {
        let key = snapshot
            .market_slug
            .clone()
            .unwrap_or_else(|| snapshot.event_id.clone());
        self.latest_odds.insert(key, snapshot);
    }

    /// Generate signals from pending odds updates.
    pub fn on_tick(
        &mut self,
        get_market: impl Fn(&str) -> Option<MarketState>,
    ) -> Vec<Signal> {
        if !self.enabled {
            return Vec::new();
        }

        let now = Utc::now();
        let mut signals = Vec::new();

        let snapshots: Vec<OddsSnapshot> = self.latest_odds.values().cloned().collect();

        for snapshot in snapshots {
            let market_slug = match &snapshot.market_slug {
                Some(slug) => slug.clone(),
                None => continue,
            };

            if !self.is_market_enabled(&market_slug) {
                continue;
            }

            // Cooldown.
            if let Some(last) = self.last_signal_at.get(&market_slug) {
                let elapsed = (now - *last).num_milliseconds() as f64 / 1000.0;
                if elapsed < self.config.cooldown_seconds {
                    continue;
                }
            }

            let market = match get_market(&market_slug) {
                Some(m) => m,
                None => continue,
            };

            if let Some(signal) = self.generate_signal(&market, &snapshot) {
                self.last_signal_at.insert(market_slug, now);
                signals.push(signal);
            }
        }

        signals
    }

    fn generate_signal(&self, market: &MarketState, snapshot: &OddsSnapshot) -> Option<Signal> {
        if market.yes_ask.is_none() && market.no_ask.is_none() {
            return None;
        }

        let fair_yes = snapshot.yes_probability;
        let mut best_signal: Option<Signal> = None;
        let mut best_edge = Decimal::ZERO;

        // YES side.
        if let Some(yes_ask) = market.yes_ask {
            let edge = fair_yes - yes_ask;
            if edge >= self.config.min_edge && edge > best_edge {
                let price = clamp_price(yes_ask);
                let quantity = self.calculate_quantity(price);
                if quantity > 0 {
                    best_edge = edge;
                    let mut metadata = HashMap::new();
                    metadata.insert(
                        "true_probability".to_string(),
                        serde_json::json!(fair_yes.to_string().parse::<f64>().unwrap_or(0.5)),
                    );

                    best_signal = Some(Signal {
                        market_slug: market.market_slug.clone(),
                        action: SignalAction::BuyYes,
                        price,
                        quantity,
                        urgency: Urgency::Medium,
                        confidence: snapshot.confidence,
                        strategy_name: "statistical_edge".to_string(),
                        reason: format!("Odds edge {:.3} vs {}", edge, snapshot.provider),
                        metadata,
                        timestamp: Utc::now(),
                    });
                }
            }
        }

        // NO side.
        let no_ask = market
            .no_ask
            .or_else(|| market.yes_bid.map(|b| Decimal::ONE - b));
        if let Some(no_ask) = no_ask {
            let fair_no = Decimal::ONE - fair_yes;
            let edge = fair_no - no_ask;
            if edge >= self.config.min_edge && edge > best_edge {
                let price = clamp_price(no_ask);
                let quantity = self.calculate_quantity(price);
                if quantity > 0 {
                    let mut metadata = HashMap::new();
                    metadata.insert(
                        "true_probability".to_string(),
                        serde_json::json!(fair_no.to_string().parse::<f64>().unwrap_or(0.5)),
                    );

                    best_signal = Some(Signal {
                        market_slug: market.market_slug.clone(),
                        action: SignalAction::BuyNo,
                        price,
                        quantity,
                        urgency: Urgency::Medium,
                        confidence: snapshot.confidence,
                        strategy_name: "statistical_edge".to_string(),
                        reason: format!("Odds edge {:.3} vs {}", edge, snapshot.provider),
                        metadata,
                        timestamp: Utc::now(),
                    });
                }
            }
        }

        best_signal
    }

    fn calculate_quantity(&self, price: Decimal) -> i64 {
        if price <= Decimal::ZERO {
            return 0;
        }
        (self.config.order_size / price)
            .floor()
            .to_string()
            .parse::<i64>()
            .unwrap_or(0)
            .max(0)
    }

    fn is_market_enabled(&self, slug: &str) -> bool {
        if self.config.enabled_markets.is_empty() {
            return true;
        }
        self.config
            .enabled_markets
            .iter()
            .any(|p| slug.contains(p.as_str()))
    }
}

fn clamp_price(price: Decimal) -> Decimal {
    price
        .max(Decimal::new(1, 2))
        .min(Decimal::new(99, 2))
}
