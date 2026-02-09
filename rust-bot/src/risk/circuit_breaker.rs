//! Circuit breaker for emergency stop conditions.
//!
//! Monitors daily loss and drawdown to halt trading when risk thresholds
//! are exceeded.

#![allow(dead_code)]

use rust_decimal::Decimal;
use tracing::warn;

/// Circuit breaker that monitors loss thresholds.
pub struct CircuitBreaker {
    daily_loss_limit: Decimal,
    max_drawdown_pct: Decimal,
    starting_equity: Decimal,
    peak_equity: Decimal,
    tripped: bool,
    trip_reason: Option<String>,
}

impl CircuitBreaker {
    pub fn new(daily_loss_limit: Decimal, max_drawdown_pct: Decimal) -> Self {
        Self {
            daily_loss_limit,
            max_drawdown_pct,
            starting_equity: Decimal::ZERO,
            peak_equity: Decimal::ZERO,
            tripped: false,
            trip_reason: None,
        }
    }

    /// Initialize with starting equity (call on startup).
    pub fn initialize(&mut self, equity: Decimal) {
        self.starting_equity = equity;
        self.peak_equity = equity;
        self.tripped = false;
        self.trip_reason = None;
    }

    /// Update with current equity. Checks trip conditions.
    pub fn update(&mut self, current_equity: Decimal) {
        if self.tripped {
            return; // Already tripped
        }

        // Track peak equity for drawdown
        if current_equity > self.peak_equity {
            self.peak_equity = current_equity;
        }

        // Check daily loss
        let daily_loss = self.starting_equity - current_equity;
        if daily_loss >= self.daily_loss_limit {
            self.trip(&format!(
                "Daily loss limit exceeded: ${:.2} >= ${:.2}",
                daily_loss, self.daily_loss_limit
            ));
            return;
        }

        // Check drawdown from peak
        if self.peak_equity > Decimal::ZERO {
            let drawdown_pct = (self.peak_equity - current_equity) / self.peak_equity;
            if drawdown_pct >= self.max_drawdown_pct {
                self.trip(&format!(
                    "Max drawdown exceeded: {:.2}% >= {:.2}%",
                    drawdown_pct * Decimal::ONE_HUNDRED,
                    self.max_drawdown_pct * Decimal::ONE_HUNDRED
                ));
            }
        }
    }

    /// Check if trading is allowed.
    pub fn can_trade(&self) -> (bool, Option<&str>) {
        if self.tripped {
            (false, self.trip_reason.as_deref())
        } else {
            (true, None)
        }
    }

    /// Emergency stop — immediately halt all trading.
    pub fn emergency_stop(&mut self, reason: &str) {
        self.trip(reason);
    }

    /// Reset the circuit breaker (e.g., start of new trading day).
    pub fn reset(&mut self, new_equity: Decimal) {
        self.initialize(new_equity);
    }

    fn trip(&mut self, reason: &str) {
        self.tripped = true;
        self.trip_reason = Some(reason.to_string());
        warn!(reason, "Circuit breaker TRIPPED");
    }

    pub fn is_tripped(&self) -> bool {
        self.tripped
    }
}
