//! Strategy engine: aggregates signals from all strategies, applies risk
//! management, and routes approved signals to the execution engine.

use rust_decimal::Decimal;
use std::collections::HashMap;
use tracing::{debug, info, warn};

use crate::data::models::{Signal, SignalAction, Urgency};
use crate::risk::risk_manager::RiskManager;
use crate::state::state_manager::{MarketState, PositionState, StateManager};

use super::live_arbitrage::{GameState, LiveArbitrageStrategy};
use super::market_maker::MarketMakerStrategy;
use super::statistical_edge::{OddsSnapshot, StatisticalEdgeStrategy};

/// Aggregated signals ready for execution.
#[derive(Debug)]
pub struct EngineOutput {
    pub approved_signals: Vec<Signal>,
    pub rejected_count: usize,
}

/// The strategy engine orchestrates all trading strategies.
pub struct StrategyEngine {
    pub market_maker: Option<MarketMakerStrategy>,
    pub live_arbitrage: Option<LiveArbitrageStrategy>,
    pub statistical_edge: Option<StatisticalEdgeStrategy>,
    state: StateManager,
}

impl StrategyEngine {
    pub fn new(
        state: StateManager,
        market_maker: Option<MarketMakerStrategy>,
        live_arbitrage: Option<LiveArbitrageStrategy>,
        statistical_edge: Option<StatisticalEdgeStrategy>,
    ) -> Self {
        Self {
            market_maker,
            live_arbitrage,
            statistical_edge,
            state,
        }
    }

    /// Process a market update through all strategies.
    pub fn on_market_update(
        &mut self,
        market: &MarketState,
        risk_manager: &mut RiskManager,
    ) -> EngineOutput {
        let mut all_signals = Vec::new();

        let position = self.state.get_position(&market.market_slug);

        // Market maker signals.
        if let Some(ref mut mm) = self.market_maker {
            let mm_signals = mm.on_market_update(market, position.as_ref());
            all_signals.extend(mm_signals);

            // Check stop-loss for existing positions.
            if let Some(ref pos) = position {
                let stop_signals = mm.check_stop_loss(pos, market);
                all_signals.extend(stop_signals);
            }
        }

        // Apply risk management.
        self.filter_through_risk(all_signals, risk_manager)
    }

    /// Process a tick (time-based triggers) through all strategies.
    pub fn on_tick(&mut self, risk_manager: &mut RiskManager) -> EngineOutput {
        let mut all_signals = Vec::new();

        // Market maker: iterate all tracked markets and generate quotes.
        if self.market_maker.is_some() {
            let markets = self.state.get_all_markets();
            for market in &markets {
                let position = self.state.get_position(&market.market_slug);
                if let Some(ref mut mm) = self.market_maker {
                    let mm_signals = mm.on_market_update(market, position.as_ref());
                    all_signals.extend(mm_signals);

                    // Check stop-loss for existing positions.
                    if let Some(ref pos) = position {
                        let stop_signals = mm.check_stop_loss(pos, market);
                        all_signals.extend(stop_signals);
                    }
                }
            }
        }

        // Live arbitrage tick.
        if let Some(ref mut la) = self.live_arbitrage {
            let state_ref = &self.state;
            let la_signals = la.on_tick(|slug| state_ref.get_market(slug));
            all_signals.extend(la_signals);
        }

        // Statistical edge tick.
        if let Some(ref mut se) = self.statistical_edge {
            let state_ref = &self.state;
            let se_signals = se.on_tick(|slug| state_ref.get_market(slug));
            all_signals.extend(se_signals);
        }

        // Apply risk management.
        self.filter_through_risk(all_signals, risk_manager)
    }

    /// Ingest game state for live arbitrage.
    pub fn ingest_game_state(&mut self, state: GameState) {
        if let Some(ref mut la) = self.live_arbitrage {
            la.ingest_game_state(state);
        }
    }

    /// Ingest odds snapshot for statistical edge.
    pub fn ingest_odds(&mut self, snapshot: OddsSnapshot) {
        if let Some(ref mut se) = self.statistical_edge {
            se.ingest_odds(snapshot);
        }
    }

    /// Filter signals through risk manager, prioritize by urgency.
    fn filter_through_risk(
        &self,
        mut signals: Vec<Signal>,
        risk_manager: &mut RiskManager,
    ) -> EngineOutput {
        // Sort by urgency (highest first).
        signals.sort_by(|a, b| b.urgency.cmp(&a.urgency));

        let mut approved = Vec::new();
        let mut rejected = 0;

        for signal in signals {
            let decision = risk_manager.evaluate_signal(signal);
            if decision.approved {
                if let Some(sig) = decision.signal {
                    debug!(
                        market_slug = %sig.market_slug,
                        action = ?sig.action,
                        quantity = sig.quantity,
                        price = %sig.price,
                        reason = %decision.reason,
                        "Signal approved"
                    );
                    approved.push(sig);
                }
            } else {
                debug!(reason = %decision.reason, "Signal rejected");
                rejected += 1;
            }
        }

        EngineOutput {
            approved_signals: approved,
            rejected_count: rejected,
        }
    }
}
