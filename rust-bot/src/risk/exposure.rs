//! Exposure monitoring for position and portfolio limits.

#![allow(dead_code)]

use rust_decimal::Decimal;
use std::collections::HashMap;

use crate::state::state_manager::StateManager;

/// Exposure limit configuration.
#[derive(Debug, Clone)]
pub struct ExposureConfig {
    pub max_position_per_market: Decimal,
    pub max_portfolio_exposure: Decimal,
    pub max_correlated_exposure: Decimal,
    pub max_positions: usize,
}

/// Result of an exposure check.
#[derive(Debug)]
pub struct ExposureCheck {
    pub allowed: bool,
    pub reason: String,
    pub max_additional_exposure: Decimal,
}

/// Monitors and enforces exposure limits.
pub struct ExposureMonitor {
    config: ExposureConfig,
    correlation_groups: HashMap<String, Vec<String>>,
}

impl ExposureMonitor {
    pub fn new(config: ExposureConfig) -> Self {
        Self {
            config,
            correlation_groups: HashMap::new(),
        }
    }

    /// Define a correlation group (markets that move together).
    pub fn set_correlation_group(&mut self, group_name: &str, markets: Vec<String>) {
        self.correlation_groups
            .insert(group_name.to_string(), markets);
    }

    /// Check if additional exposure can be added.
    pub fn can_add_exposure(
        &self,
        state: &StateManager,
        market_slug: &str,
        additional_exposure: Decimal,
    ) -> ExposureCheck {
        // Per-market limit
        let current_market = state.market_exposure(market_slug);
        let market_headroom = self.config.max_position_per_market - current_market;

        if current_market + additional_exposure > self.config.max_position_per_market {
            return ExposureCheck {
                allowed: false,
                reason: format!(
                    "Per-market limit: current ${:.2} + ${:.2} > ${:.2}",
                    current_market, additional_exposure, self.config.max_position_per_market
                ),
                max_additional_exposure: market_headroom.max(Decimal::ZERO),
            };
        }

        // Portfolio-wide limit
        let total_exposure = self.total_exposure(state);
        let portfolio_headroom = self.config.max_portfolio_exposure - total_exposure;

        if total_exposure + additional_exposure > self.config.max_portfolio_exposure {
            return ExposureCheck {
                allowed: false,
                reason: format!(
                    "Portfolio limit: current ${:.2} + ${:.2} > ${:.2}",
                    total_exposure, additional_exposure, self.config.max_portfolio_exposure
                ),
                max_additional_exposure: portfolio_headroom
                    .min(market_headroom)
                    .max(Decimal::ZERO),
            };
        }

        // Position count limit
        let position_count = state.position_count();
        let is_new_position = state.get_position(market_slug).is_none();
        if is_new_position && position_count >= self.config.max_positions {
            return ExposureCheck {
                allowed: false,
                reason: format!(
                    "Max positions: {} >= {}",
                    position_count, self.config.max_positions
                ),
                max_additional_exposure: Decimal::ZERO,
            };
        }

        // Correlation group limit
        for (_group, group_markets) in &self.correlation_groups {
            if group_markets.contains(&market_slug.to_string()) {
                let group_exposure: Decimal = group_markets
                    .iter()
                    .map(|m| state.market_exposure(m))
                    .sum();
                let corr_headroom = self.config.max_correlated_exposure - group_exposure;

                if group_exposure + additional_exposure > self.config.max_correlated_exposure {
                    return ExposureCheck {
                        allowed: false,
                        reason: "Correlation group limit exceeded".to_string(),
                        max_additional_exposure: corr_headroom
                            .min(market_headroom)
                            .min(portfolio_headroom)
                            .max(Decimal::ZERO),
                    };
                }
            }
        }

        ExposureCheck {
            allowed: true,
            reason: "OK".to_string(),
            max_additional_exposure: market_headroom
                .min(portfolio_headroom)
                .max(Decimal::ZERO),
        }
    }

    /// Total exposure across all positions.
    pub fn total_exposure(&self, state: &StateManager) -> Decimal {
        state.get_total_position_value()
    }
}
