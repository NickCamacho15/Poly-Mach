//! Live arbitrage strategy based on real-time game state.
//!
//! Generates signals on score changes and game events, capturing
//! mispricing from stale market prices during live games.

use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use std::collections::HashMap;
use tracing::info;

use crate::data::models::{Signal, SignalAction, Urgency};
use crate::state::state_manager::MarketState;

/// Configuration for live arbitrage.
#[derive(Debug, Clone)]
pub struct LiveArbitrageConfig {
    pub min_edge: Decimal,
    pub order_size: Decimal,
    pub lead_multiplier: Decimal,
    pub max_prob_shift: Decimal,
    pub cooldown_seconds: f64,
    pub enabled_markets: Vec<String>,
}

impl Default for LiveArbitrageConfig {
    fn default() -> Self {
        Self {
            min_edge: Decimal::new(3, 2),       // 0.03
            order_size: Decimal::new(10, 0),     // $10
            lead_multiplier: Decimal::new(2, 2), // 0.02
            max_prob_shift: Decimal::new(25, 2), // 0.25
            cooldown_seconds: 5.0,
            enabled_markets: Vec::new(),
        }
    }
}

/// Live game state from sports data feed.
#[derive(Debug, Clone)]
pub struct GameState {
    pub event_id: String,
    pub market_slug: Option<String>,
    pub home_score: i32,
    pub away_score: i32,
    pub home_is_yes: bool,
    pub is_final: bool,
    pub timestamp: DateTime<Utc>,
}

impl GameState {
    pub fn score_diff(&self) -> i32 {
        self.home_score - self.away_score
    }
}

/// Live arbitrage strategy.
pub struct LiveArbitrageStrategy {
    config: LiveArbitrageConfig,
    enabled: bool,
    latest_states: HashMap<String, GameState>,
    last_signal_at: HashMap<String, DateTime<Utc>>,
}

impl LiveArbitrageStrategy {
    pub fn new(config: LiveArbitrageConfig) -> Self {
        Self {
            config,
            enabled: true,
            latest_states: HashMap::new(),
            last_signal_at: HashMap::new(),
        }
    }

    pub fn set_enabled(&mut self, enabled: bool) {
        self.enabled = enabled;
    }

    /// Ingest a new game state update.
    pub fn ingest_game_state(&mut self, state: GameState) {
        self.latest_states
            .insert(state.event_id.clone(), state);
    }

    /// Generate signals from pending game state updates.
    pub fn on_tick(
        &mut self,
        get_market: impl Fn(&str) -> Option<MarketState>,
    ) -> Vec<Signal> {
        if !self.enabled {
            return Vec::new();
        }

        let now = Utc::now();
        let mut signals = Vec::new();

        let states: Vec<GameState> = self.latest_states.values().cloned().collect();

        for state in states {
            if state.is_final {
                continue;
            }

            let market_slug = match &state.market_slug {
                Some(slug) => slug.clone(),
                None => continue,
            };

            if !self.is_market_enabled(&market_slug) {
                continue;
            }

            // Cooldown check.
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

            if let Some(signal) = self.generate_signal(&market, &state) {
                self.last_signal_at.insert(market_slug, now);
                signals.push(signal);
            }
        }

        signals
    }

    fn generate_signal(&self, market: &MarketState, state: &GameState) -> Option<Signal> {
        if market.yes_ask.is_none() && market.no_ask.is_none() {
            return None;
        }

        let fair_yes = self.estimate_yes_probability(state);
        let mut best_signal: Option<Signal> = None;
        let mut best_edge = Decimal::ZERO;

        // Check YES side.
        if let Some(yes_ask) = market.yes_ask {
            let edge = fair_yes - yes_ask;
            if edge >= self.config.min_edge && edge > best_edge {
                let price = clamp_price(yes_ask);
                let quantity = self.calculate_quantity(price);
                if quantity > 0 {
                    best_edge = edge;
                    let confidence = (0.55 + (state.score_diff().unsigned_abs() as f64 * 0.05))
                        .min(0.9);
                    let mut metadata = HashMap::new();
                    metadata.insert(
                        "true_probability".to_string(),
                        serde_json::json!(fair_yes.to_string().parse::<f64>().unwrap_or(0.5)),
                    );
                    metadata.insert("allow_in_game".to_string(), serde_json::json!(true));

                    best_signal = Some(Signal {
                        market_slug: market.market_slug.clone(),
                        action: SignalAction::BuyYes,
                        price,
                        quantity,
                        urgency: Urgency::High,
                        confidence,
                        strategy_name: "live_arbitrage".to_string(),
                        reason: format!("Live edge {:.3} on score update", edge),
                        metadata,
                        timestamp: Utc::now(),
                    });
                }
            }
        }

        // Check NO side.
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
                    let confidence = (0.55 + (state.score_diff().unsigned_abs() as f64 * 0.05))
                        .min(0.9);
                    let mut metadata = HashMap::new();
                    metadata.insert(
                        "true_probability".to_string(),
                        serde_json::json!(fair_no.to_string().parse::<f64>().unwrap_or(0.5)),
                    );
                    metadata.insert("allow_in_game".to_string(), serde_json::json!(true));

                    best_signal = Some(Signal {
                        market_slug: market.market_slug.clone(),
                        action: SignalAction::BuyNo,
                        price,
                        quantity,
                        urgency: Urgency::High,
                        confidence,
                        strategy_name: "live_arbitrage".to_string(),
                        reason: format!("Live edge {:.3} on score update", edge),
                        metadata,
                        timestamp: Utc::now(),
                    });
                }
            }
        }

        best_signal
    }

    fn estimate_yes_probability(&self, state: &GameState) -> Decimal {
        let lead = state.score_diff().unsigned_abs();
        let shift = self
            .config
            .max_prob_shift
            .min(self.config.lead_multiplier * Decimal::from(lead));

        let mut prob = if state.score_diff() >= 0 {
            Decimal::new(5, 1) + shift // 0.5 + shift
        } else {
            Decimal::new(5, 1) - shift // 0.5 - shift
        };

        if !state.home_is_yes {
            prob = Decimal::ONE - prob;
        }

        prob.max(Decimal::new(5, 2)).min(Decimal::new(95, 2)) // clamp [0.05, 0.95]
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
        qty.max(0)
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
