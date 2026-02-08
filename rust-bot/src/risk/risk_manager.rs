//! Complete risk management system.
//!
//! Combines:
//! - Position sizing (Kelly)
//! - Exposure monitoring (per-market / portfolio / correlation)
//! - Circuit breaker (daily loss / drawdown / emergency stop)

#![allow(dead_code)]

use rust_decimal::Decimal;
use tracing::info;

use crate::data::models::Signal;
use crate::state::state_manager::StateManager;

use super::circuit_breaker::CircuitBreaker;
use super::exposure::{ExposureConfig, ExposureMonitor};
use super::position_sizer::{EdgeEstimate, KellyPositionSizer};

/// Risk configuration.
#[derive(Debug, Clone)]
pub struct RiskConfig {
    pub kelly_fraction: Decimal,
    pub min_edge: Decimal,
    pub max_position_per_market: Decimal,
    pub max_portfolio_exposure: Decimal,
    pub max_portfolio_exposure_pct: Decimal,
    pub max_correlated_exposure: Decimal,
    pub max_positions: usize,
    pub max_daily_loss: Decimal,
    pub max_drawdown_pct: Decimal,
    pub max_total_pnl_drawdown_pct_for_new_buys: Decimal,
    pub min_trade_size: Decimal,
}

/// Decision from risk evaluation.
#[derive(Debug)]
pub struct RiskDecision {
    pub approved: bool,
    pub signal: Option<Signal>,
    pub reason: String,
}

/// Complete risk management system.
pub struct RiskManager {
    config: RiskConfig,
    state: StateManager,
    position_sizer: KellyPositionSizer,
    exposure_monitor: ExposureMonitor,
    circuit_breaker: CircuitBreaker,
    starting_equity: Decimal,
}

impl RiskManager {
    pub fn new(config: RiskConfig, state: StateManager) -> Self {
        let starting_equity = state.get_total_equity();

        let position_sizer = KellyPositionSizer::new(
            config.kelly_fraction,
            Decimal::ONE, // max_position_pct (clamped again by exposure monitor)
            config.min_edge,
        );

        let exposure_monitor = ExposureMonitor::new(ExposureConfig {
            max_position_per_market: config.max_position_per_market,
            max_portfolio_exposure: config.max_portfolio_exposure,
            max_correlated_exposure: config.max_correlated_exposure,
            max_positions: config.max_positions,
        });

        let mut circuit_breaker =
            CircuitBreaker::new(config.max_daily_loss, config.max_drawdown_pct);
        circuit_breaker.initialize(starting_equity);

        info!(
            max_position_per_market = %config.max_position_per_market,
            max_portfolio_exposure = %config.max_portfolio_exposure,
            max_daily_loss = %config.max_daily_loss,
            kelly_fraction = %config.kelly_fraction,
            starting_equity = %starting_equity,
            "RiskManager initialized"
        );

        Self {
            config,
            state,
            position_sizer,
            exposure_monitor,
            circuit_breaker,
            starting_equity,
        }
    }

    /// Update circuit breaker with current equity.
    pub fn on_state_update(&mut self) {
        let equity = self.state.get_total_equity();
        self.circuit_breaker.update(equity);
    }

    /// Reset starting equity (e.g., after initial API sync).
    pub fn reset_starting_equity(&mut self) {
        self.starting_equity = self.state.get_total_equity();
        self.circuit_breaker.initialize(self.starting_equity);
        info!(starting_equity = %self.starting_equity, "Starting equity reset");
    }

    /// Evaluate a signal through all risk checks.
    pub fn evaluate_signal(&mut self, signal: Signal) -> RiskDecision {
        // Always allow cancels.
        if signal.is_cancel() {
            return RiskDecision {
                approved: true,
                signal: Some(signal),
                reason: "Approved: cancel".to_string(),
            };
        }

        // Update breaker.
        self.on_state_update();

        // Circuit breaker check.
        let (can_trade, reason) = self.circuit_breaker.can_trade();
        if !can_trade {
            if signal.is_sell() {
                return RiskDecision {
                    approved: true,
                    signal: Some(signal),
                    reason: "Approved: circuit breaker allows exits".to_string(),
                };
            }
            return RiskDecision {
                approved: false,
                signal: None,
                reason: format!(
                    "Circuit breaker: {}",
                    reason.unwrap_or("tripped")
                ),
            };
        }

        let mut qty = signal.quantity;
        let price = signal.price;

        if qty <= 0 {
            return RiskDecision {
                approved: false,
                signal: None,
                reason: "Rejected: non-positive quantity".to_string(),
            };
        }

        // Cash check for buys.
        if signal.is_buy() && price > Decimal::ZERO {
            let available_cash = self.state.get_balance();
            let cash_buffer = Decimal::new(98, 2); // 0.98
            let max_affordable = (available_cash * cash_buffer) / price;
            let max_affordable_qty = max_affordable
                .floor()
                .to_string()
                .parse::<i64>()
                .unwrap_or(0);

            if max_affordable_qty <= 0 {
                return RiskDecision {
                    approved: false,
                    signal: None,
                    reason: format!(
                        "Rejected: insufficient cash (${:.2} available)",
                        available_cash
                    ),
                };
            }
            if qty > max_affordable_qty {
                qty = max_affordable_qty;
            }
        }

        // Kelly sizing for buys with probability estimates.
        if signal.is_buy() {
            if let Some(true_prob) = signal
                .metadata
                .get("true_probability")
                .and_then(|v| v.as_f64())
                .and_then(|f| Decimal::from_f64_retain(f))
            {
                let edge = EdgeEstimate::from_confidence(true_prob, signal.confidence);
                if let Some(result) =
                    self.position_sizer
                        .calculate_position_size(self.state.get_total_equity(), price, &edge)
                {
                    qty = qty.min(result.contracts);
                } else {
                    return RiskDecision {
                        approved: false,
                        signal: None,
                        reason: "Rejected: insufficient edge/confidence".to_string(),
                    };
                }
            }
        }

        // Min trade size check.
        let notional = price * Decimal::from(qty);
        if notional < self.config.min_trade_size {
            return RiskDecision {
                approved: false,
                signal: None,
                reason: format!("Rejected: below min trade size ${:.2}", notional),
            };
        }

        // Exposure limits for buys.
        if signal.is_buy() {
            // Portfolio drawdown check.
            if self.is_new_buy_blocked_by_drawdown() {
                return RiskDecision {
                    approved: false,
                    signal: None,
                    reason: "Rejected: portfolio drawdown blocks new buys".to_string(),
                };
            }

            let check = self.exposure_monitor.can_add_exposure(
                &self.state,
                &signal.market_slug,
                notional,
            );

            // Portfolio exposure % check.
            let current_total = self.exposure_monitor.total_exposure(&self.state);
            let equity = self.state.get_total_equity();
            let max_by_pct = equity * self.config.max_portfolio_exposure_pct;
            let max_additional_pct = (max_by_pct - current_total).max(Decimal::ZERO);
            let max_additional = check.max_additional_exposure.min(max_additional_pct);

            if !check.allowed && max_additional <= Decimal::ZERO {
                return RiskDecision {
                    approved: false,
                    signal: None,
                    reason: format!("Rejected: {}", check.reason),
                };
            }

            if notional > max_additional {
                if max_additional >= self.config.min_trade_size {
                    let reduced_qty = (max_additional / price)
                        .floor()
                        .to_string()
                        .parse::<i64>()
                        .unwrap_or(0);
                    if reduced_qty <= 0 {
                        return RiskDecision {
                            approved: false,
                            signal: None,
                            reason: "Rejected: exposure limits".to_string(),
                        };
                    }
                    qty = qty.min(reduced_qty);
                } else {
                    return RiskDecision {
                        approved: false,
                        signal: None,
                        reason: "Rejected: exposure limits".to_string(),
                    };
                }
            }

            // Re-check min trade after reduction.
            let final_notional = price * Decimal::from(qty);
            if final_notional < self.config.min_trade_size {
                return RiskDecision {
                    approved: false,
                    signal: None,
                    reason: format!("Rejected: below min trade size ${:.2}", final_notional),
                };
            }
        }

        // Produce (possibly resized) signal.
        let mut approved_signal = signal;
        approved_signal.quantity = qty;

        RiskDecision {
            approved: true,
            signal: Some(approved_signal),
            reason: "Approved".to_string(),
        }
    }

    fn is_new_buy_blocked_by_drawdown(&self) -> bool {
        if self.config.max_total_pnl_drawdown_pct_for_new_buys <= Decimal::ZERO {
            return false;
        }
        if self.starting_equity <= Decimal::ZERO {
            return false;
        }
        let current = self.state.get_total_equity();
        let drawdown_pct = (self.starting_equity - current) / self.starting_equity;
        drawdown_pct >= self.config.max_total_pnl_drawdown_pct_for_new_buys
    }

    pub fn set_correlation_group(&mut self, group_name: &str, markets: Vec<String>) {
        self.exposure_monitor
            .set_correlation_group(group_name, markets);
    }
}
