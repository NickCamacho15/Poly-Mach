//! Kelly Criterion position sizing for binary prediction markets.
//!
//! For a contract priced at P in (0, 1), if the outcome occurs, the payout is $1,
//! so the net-odds ratio is:
//!     b = (1 - P) / P
//!
//! Full Kelly fraction:
//!     f* = (p*b - q) / b
//!
//! We apply:
//! - Fractional Kelly (e.g., 0.25 for quarter Kelly)
//! - Confidence multiplier in [0, 1]
//! - Clamp to [0, max_position_pct]

use rust_decimal::Decimal;
use tracing::debug;

/// Estimated edge for a trade.
#[derive(Debug, Clone)]
pub struct EdgeEstimate {
    /// Estimated true probability for the outcome being traded.
    pub probability: Decimal,
    /// Confidence in the estimate in [0, 1].
    pub confidence: Decimal,
}

impl EdgeEstimate {
    pub fn new(probability: Decimal, confidence: Decimal) -> Self {
        Self {
            probability,
            confidence,
        }
    }

    pub fn from_confidence(probability: Decimal, confidence: f64) -> Self {
        Self {
            probability,
            confidence: Decimal::from_f64_retain(confidence).unwrap_or(Decimal::ONE),
        }
    }
}

/// Result of a sizing calculation.
#[derive(Debug, Clone)]
pub struct PositionSizeResult {
    /// Probability edge (true_probability - market_price).
    pub edge: Decimal,
    /// Full Kelly fraction before scaling.
    pub kelly_full: Decimal,
    /// Final fraction after fractional Kelly + confidence.
    pub kelly_adjusted: Decimal,
    /// Dollar amount to allocate.
    pub notional: Decimal,
    /// Integer number of contracts.
    pub contracts: i64,
}

/// Kelly Criterion position sizer for binary markets.
pub struct KellyPositionSizer {
    pub kelly_fraction: Decimal,
    pub max_position_pct: Decimal,
    pub min_edge: Decimal,
}

impl KellyPositionSizer {
    pub fn new(
        kelly_fraction: Decimal,
        max_position_pct: Decimal,
        min_edge: Decimal,
    ) -> Self {
        Self {
            kelly_fraction,
            max_position_pct,
            min_edge,
        }
    }

    /// Calculate position sizing for a bet.
    ///
    /// Returns None if the trade should be skipped (no edge / too small).
    pub fn calculate_position_size(
        &self,
        bankroll: Decimal,
        market_price: Decimal,
        edge: &EdgeEstimate,
    ) -> Option<PositionSizeResult> {
        if bankroll <= Decimal::ZERO {
            return None;
        }
        if market_price <= Decimal::ZERO || market_price >= Decimal::ONE {
            return None;
        }

        // Edge = true probability - market price
        let implied_edge = edge.probability - market_price;

        // Minimum edge threshold
        if implied_edge.abs() < self.min_edge {
            debug!(edge = %implied_edge, min_edge = %self.min_edge, "Below min edge");
            return None;
        }

        let p = edge.probability;
        let q = Decimal::ONE - p;

        // Net odds ratio for binary payout: b = (1 - P) / P
        let b = (Decimal::ONE - market_price) / market_price;
        if b <= Decimal::ZERO {
            return None;
        }

        // Full Kelly: f* = (p*b - q) / b
        let kelly_full = (p * b - q) / b;
        if kelly_full <= Decimal::ZERO {
            return None;
        }

        // Apply fractional Kelly and confidence
        let kelly_adjusted = (kelly_full * self.kelly_fraction * edge.confidence)
            .max(Decimal::ZERO)
            .min(self.max_position_pct);

        let notional = bankroll * kelly_adjusted;
        if notional <= Decimal::ZERO {
            return None;
        }

        let contracts = (notional / market_price)
            .floor()
            .to_string()
            .parse::<i64>()
            .unwrap_or(0);

        if contracts <= 0 {
            return None;
        }

        Some(PositionSizeResult {
            edge: implied_edge,
            kelly_full,
            kelly_adjusted,
            notional,
            contracts,
        })
    }
}
