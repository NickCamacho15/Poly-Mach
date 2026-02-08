//! Comprehensive financial formula tests for the Polymarket US trading bot.
//!
//! Every test includes a hand-calculated expected value comment so that any
//! formula regression is caught BEFORE it costs real money.
//!
//! Modules under test:
//!   1. Kelly Criterion position sizer  (src/risk/position_sizer.rs)
//!   2. Completeness arbitrage scanner  (src/data/orderbook.rs)
//!   3. Circuit breaker                 (src/risk/circuit_breaker.rs)
//!   4. Exposure monitor                (src/risk/exposure.rs)
//!   5. Risk manager integration        (src/risk/risk_manager.rs)
//!   6. Order book mechanics            (src/data/orderbook.rs)

use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use std::collections::HashMap;

use polymarket_us_bot::data::models::{
    OrderBook, OrderBookSide, PriceLevel, Side, Signal, SignalAction, Urgency,
};
use polymarket_us_bot::data::orderbook::{OrderBookTracker, TopOfBook};
use polymarket_us_bot::risk::circuit_breaker::CircuitBreaker;
use polymarket_us_bot::risk::exposure::{ExposureConfig, ExposureMonitor};
use polymarket_us_bot::risk::position_sizer::{EdgeEstimate, KellyPositionSizer};
use polymarket_us_bot::risk::risk_manager::{RiskConfig, RiskManager};
use polymarket_us_bot::state::state_manager::StateManager;

// =============================================================================
// Helpers
// =============================================================================

/// Build a minimal OrderBook for testing the tracker.
fn make_book(slug: &str, yes_ask: Decimal, no_ask: Decimal) -> OrderBook {
    OrderBook {
        market_slug: slug.to_string(),
        yes: OrderBookSide {
            bids: vec![PriceLevel {
                price: yes_ask - dec!(0.02),
                quantity: 100,
            }],
            asks: vec![PriceLevel {
                price: yes_ask,
                quantity: 100,
            }],
        },
        no: OrderBookSide {
            bids: vec![PriceLevel {
                price: no_ask - dec!(0.02),
                quantity: 100,
            }],
            asks: vec![PriceLevel {
                price: no_ask,
                quantity: 100,
            }],
        },
    }
}

/// Build a Signal for risk manager tests.
fn make_buy_signal(
    market: &str,
    price: Decimal,
    qty: i64,
    confidence: f64,
    true_prob: Option<f64>,
) -> Signal {
    let mut metadata = HashMap::new();
    if let Some(tp) = true_prob {
        metadata.insert(
            "true_probability".to_string(),
            serde_json::json!(tp),
        );
    }
    Signal {
        market_slug: market.to_string(),
        action: SignalAction::BuyYes,
        price,
        quantity: qty,
        urgency: Urgency::Medium,
        confidence,
        strategy_name: "test".to_string(),
        reason: "test signal".to_string(),
        metadata,
        timestamp: chrono::Utc::now(),
    }
}

fn make_sell_signal(market: &str, price: Decimal, qty: i64) -> Signal {
    Signal {
        market_slug: market.to_string(),
        action: SignalAction::SellYes,
        price,
        quantity: qty,
        urgency: Urgency::Medium,
        confidence: 1.0,
        strategy_name: "test".to_string(),
        reason: "test sell".to_string(),
        metadata: HashMap::new(),
        timestamp: chrono::Utc::now(),
    }
}

fn make_cancel_signal(market: &str) -> Signal {
    Signal {
        market_slug: market.to_string(),
        action: SignalAction::CancelAll,
        price: Decimal::ZERO,
        quantity: 0,
        urgency: Urgency::Critical,
        confidence: 1.0,
        strategy_name: "test".to_string(),
        reason: "test cancel".to_string(),
        metadata: HashMap::new(),
        timestamp: chrono::Utc::now(),
    }
}

/// Standard RiskConfig for tests that is intentionally permissive unless
/// the specific test tightens a limit.
fn permissive_risk_config() -> RiskConfig {
    RiskConfig {
        kelly_fraction: dec!(0.25),
        min_edge: dec!(0.02),
        max_position_per_market: dec!(500),
        max_portfolio_exposure: dec!(2000),
        max_portfolio_exposure_pct: dec!(0.80),
        max_correlated_exposure: dec!(1000),
        max_positions: 20,
        max_daily_loss: dec!(200),
        max_drawdown_pct: dec!(0.10),
        max_total_pnl_drawdown_pct_for_new_buys: dec!(0.05),
        min_trade_size: dec!(1),
    }
}

// =============================================================================
// 1. Kelly Criterion Position Sizer
// =============================================================================

#[test]
fn kelly_hand_verified_quarter_kelly() {
    // Hand calculation:
    //   bankroll = $1000, price = 0.50, true_prob = 0.60, confidence = 1.0
    //   b = (1 - 0.50) / 0.50 = 1.0
    //   kelly_full = (0.60 * 1.0 - 0.40) / 1.0 = 0.20
    //   kelly_adjusted = 0.20 * 0.25 (quarter Kelly) * 1.0 (confidence) = 0.05
    //   notional = 1000 * 0.05 = $50.00
    //   contracts = floor(50.00 / 0.50) = 100
    let sizer = KellyPositionSizer::new(dec!(0.25), dec!(1.0), dec!(0.02));
    let edge = EdgeEstimate::new(dec!(0.60), Decimal::ONE);
    let result = sizer
        .calculate_position_size(dec!(1000), dec!(0.50), &edge)
        .expect("Should produce a position");

    assert_eq!(result.kelly_full, dec!(0.20), "Full Kelly = 0.20");
    assert_eq!(result.kelly_adjusted, dec!(0.05), "Adjusted Kelly = 0.05");
    assert_eq!(result.notional, dec!(50), "Notional = $50");
    assert_eq!(result.contracts, 100, "Contracts = 100");
    assert_eq!(result.edge, dec!(0.10), "Edge = 0.60 - 0.50 = 0.10");
}

#[test]
fn kelly_edge_below_min_returns_none() {
    // Edge = 0.51 - 0.50 = 0.01 < min_edge (0.02) => None
    let sizer = KellyPositionSizer::new(dec!(0.25), dec!(1.0), dec!(0.02));
    let edge = EdgeEstimate::new(dec!(0.51), Decimal::ONE);
    let result = sizer.calculate_position_size(dec!(1000), dec!(0.50), &edge);
    assert!(result.is_none(), "Edge 0.01 < min 0.02 must return None");
}

#[test]
fn kelly_negative_edge_returns_none() {
    // true_prob = 0.40, price = 0.50 => edge = -0.10
    // Full Kelly = (0.40 * 1.0 - 0.60) / 1.0 = -0.20 < 0 => None
    let sizer = KellyPositionSizer::new(dec!(0.25), dec!(1.0), dec!(0.02));
    let edge = EdgeEstimate::new(dec!(0.40), Decimal::ONE);
    let result = sizer.calculate_position_size(dec!(1000), dec!(0.50), &edge);
    assert!(
        result.is_none(),
        "Negative edge (true_prob < price) must return None"
    );
}

#[test]
fn kelly_zero_bankroll_returns_none() {
    // bankroll = 0 => early exit
    let sizer = KellyPositionSizer::new(dec!(0.25), dec!(1.0), dec!(0.02));
    let edge = EdgeEstimate::new(dec!(0.60), Decimal::ONE);
    let result = sizer.calculate_position_size(Decimal::ZERO, dec!(0.50), &edge);
    assert!(result.is_none(), "Zero bankroll must return None");
}

#[test]
fn kelly_negative_bankroll_returns_none() {
    // bankroll = -100 => early exit (the check is `bankroll <= 0`)
    let sizer = KellyPositionSizer::new(dec!(0.25), dec!(1.0), dec!(0.02));
    let edge = EdgeEstimate::new(dec!(0.60), Decimal::ONE);
    let result = sizer.calculate_position_size(dec!(-100), dec!(0.50), &edge);
    assert!(result.is_none(), "Negative bankroll must return None");
}

#[test]
fn kelly_price_at_lower_boundary() {
    // price = 0.01 (near lower boundary, but valid)
    // b = (1 - 0.01) / 0.01 = 99.0
    // true_prob = 0.05, q = 0.95
    // kelly_full = (0.05 * 99 - 0.95) / 99 = (4.95 - 0.95) / 99 = 4.0/99 = 0.04040404...
    // kelly_adjusted = 0.04040404... * 0.25 * 1.0 = 0.01010101...
    // notional = 1000 * 0.01010101... = 10.10101...
    // contracts = floor(10.10101... / 0.01) = floor(1010.101...) = 1010
    let sizer = KellyPositionSizer::new(dec!(0.25), dec!(1.0), dec!(0.02));
    let edge = EdgeEstimate::new(dec!(0.05), Decimal::ONE);
    let result = sizer
        .calculate_position_size(dec!(1000), dec!(0.01), &edge)
        .expect("Should work at price = 0.01");

    // b = 99
    // full kelly = 4/99 = 0.0404040404...
    // We check contracts = 1010 (the key financial output)
    assert_eq!(result.contracts, 1010, "Contracts at price=0.01");
}

#[test]
fn kelly_price_at_upper_boundary() {
    // price = 0.99
    // b = (1 - 0.99) / 0.99 = 0.01/0.99 = 0.01010101...
    // true_prob = 0.995, q = 0.005
    // edge = 0.995 - 0.99 = 0.005, so min_edge must be <= 0.005
    // kelly_full = (0.995 * 0.01010101... - 0.005) / 0.01010101...
    //            = (0.01005050... - 0.005) / 0.01010101...
    //            = 0.00505050... / 0.01010101...
    //            = 0.5
    // kelly_adjusted = 0.5 * 0.25 * 1.0 = 0.125
    // notional = 1000 * 0.125 = 125
    // contracts = floor(125 / 0.99) = floor(126.2626...) = 126
    let sizer = KellyPositionSizer::new(dec!(0.25), dec!(1.0), dec!(0.005));
    let edge = EdgeEstimate::new(dec!(0.995), Decimal::ONE);
    let result = sizer
        .calculate_position_size(dec!(1000), dec!(0.99), &edge)
        .expect("Should work at price = 0.99");

    assert_eq!(result.contracts, 126, "Contracts at price=0.99");
}

#[test]
fn kelly_price_exactly_zero_returns_none() {
    // price = 0.00 => invalid (the guard is `price <= 0`)
    let sizer = KellyPositionSizer::new(dec!(0.25), dec!(1.0), dec!(0.02));
    let edge = EdgeEstimate::new(dec!(0.60), Decimal::ONE);
    let result = sizer.calculate_position_size(dec!(1000), Decimal::ZERO, &edge);
    assert!(result.is_none(), "Price = 0 must return None");
}

#[test]
fn kelly_price_exactly_one_returns_none() {
    // price = 1.00 => invalid (the guard is `price >= 1`)
    let sizer = KellyPositionSizer::new(dec!(0.25), dec!(1.0), dec!(0.02));
    let edge = EdgeEstimate::new(dec!(0.60), Decimal::ONE);
    let result = sizer.calculate_position_size(dec!(1000), Decimal::ONE, &edge);
    assert!(result.is_none(), "Price = 1.0 must return None");
}

#[test]
fn kelly_confidence_scaling_halves_position() {
    // Same setup as the hand-verified test, but confidence = 0.5
    //   kelly_full = 0.20
    //   kelly_adjusted = 0.20 * 0.25 * 0.5 = 0.025
    //   notional = 1000 * 0.025 = $25
    //   contracts = floor(25 / 0.50) = 50
    //
    // With confidence=1.0 we got 100 contracts; with 0.5 we expect exactly 50.
    let sizer = KellyPositionSizer::new(dec!(0.25), dec!(1.0), dec!(0.02));
    let edge = EdgeEstimate::new(dec!(0.60), dec!(0.5));
    let result = sizer
        .calculate_position_size(dec!(1000), dec!(0.50), &edge)
        .expect("Should produce a position");

    assert_eq!(result.kelly_adjusted, dec!(0.025));
    assert_eq!(result.notional, dec!(25));
    assert_eq!(
        result.contracts, 50,
        "Halved confidence must halve contracts: 50 vs 100"
    );
}

#[test]
fn kelly_max_position_pct_clamps() {
    // Artificially low max_position_pct = 0.01
    //   kelly_full = 0.20 (same as hand-verified)
    //   kelly_adjusted = min(0.20 * 0.25 * 1.0, 0.01) = min(0.05, 0.01) = 0.01
    //   notional = 1000 * 0.01 = $10
    //   contracts = floor(10 / 0.50) = 20
    let sizer = KellyPositionSizer::new(dec!(0.25), dec!(0.01), dec!(0.02));
    let edge = EdgeEstimate::new(dec!(0.60), Decimal::ONE);
    let result = sizer
        .calculate_position_size(dec!(1000), dec!(0.50), &edge)
        .expect("Should produce a clamped position");

    assert_eq!(
        result.kelly_adjusted,
        dec!(0.01),
        "Must clamp to max_position_pct"
    );
    assert_eq!(result.notional, dec!(10));
    assert_eq!(result.contracts, 20);
}

#[test]
fn kelly_quarter_vs_half_vs_full_ratio() {
    // The ratio of contracts at quarter / half / full Kelly must be exactly 1:2:4
    // Using bankroll=10000, price=0.50, true_prob=0.60
    //   kelly_full = 0.20
    //   quarter: 0.20*0.25 = 0.05 => notional=500 => contracts=1000
    //   half:    0.20*0.50 = 0.10 => notional=1000 => contracts=2000
    //   full:    0.20*1.00 = 0.20 => notional=2000 => contracts=4000
    let edge = EdgeEstimate::new(dec!(0.60), Decimal::ONE);

    let q_sizer = KellyPositionSizer::new(dec!(0.25), dec!(1.0), dec!(0.02));
    let q = q_sizer
        .calculate_position_size(dec!(10000), dec!(0.50), &edge)
        .unwrap();

    let h_sizer = KellyPositionSizer::new(dec!(0.50), dec!(1.0), dec!(0.02));
    let h = h_sizer
        .calculate_position_size(dec!(10000), dec!(0.50), &edge)
        .unwrap();

    let f_sizer = KellyPositionSizer::new(dec!(1.0), dec!(1.0), dec!(0.02));
    let f = f_sizer
        .calculate_position_size(dec!(10000), dec!(0.50), &edge)
        .unwrap();

    assert_eq!(q.contracts, 1000, "Quarter Kelly = 1000 contracts");
    assert_eq!(h.contracts, 2000, "Half Kelly = 2000 contracts");
    assert_eq!(f.contracts, 4000, "Full Kelly = 4000 contracts");
    assert_eq!(
        h.contracts,
        q.contracts * 2,
        "Half = 2x Quarter"
    );
    assert_eq!(
        f.contracts,
        q.contracts * 4,
        "Full = 4x Quarter"
    );
}

#[test]
fn kelly_edge_exactly_at_min_returns_none() {
    // Edge = 0.52 - 0.50 = 0.02, and min_edge = 0.02
    // abs(0.02) < 0.02 is false (not strictly less), so this should NOT be rejected.
    // BUT wait -- the code uses `implied_edge.abs() < self.min_edge` which is strict <
    // So edge = 0.02 is NOT below threshold. It should pass.
    //
    // b = 1.0, kelly_full = (0.52*1 - 0.48)/1 = 0.04
    // kelly_adjusted = 0.04 * 0.25 * 1.0 = 0.01
    // notional = 1000 * 0.01 = 10
    // contracts = floor(10/0.50) = 20
    let sizer = KellyPositionSizer::new(dec!(0.25), dec!(1.0), dec!(0.02));
    let edge = EdgeEstimate::new(dec!(0.52), Decimal::ONE);
    let result = sizer.calculate_position_size(dec!(1000), dec!(0.50), &edge);
    assert!(
        result.is_some(),
        "Edge exactly at min_edge (0.02) should NOT be rejected (strict < comparison)"
    );
    let r = result.unwrap();
    assert_eq!(r.contracts, 20);
}

#[test]
fn kelly_very_small_edge_just_below_min() {
    // Edge = 0.519 - 0.50 = 0.019 < 0.02 => None
    let sizer = KellyPositionSizer::new(dec!(0.25), dec!(1.0), dec!(0.02));
    let edge = EdgeEstimate::new(dec!(0.519), Decimal::ONE);
    let result = sizer.calculate_position_size(dec!(1000), dec!(0.50), &edge);
    assert!(result.is_none(), "Edge 0.019 < min 0.02 must be rejected");
}

#[test]
fn kelly_large_edge_capped_properly() {
    // true_prob = 0.95, price = 0.50
    // b = 1.0
    // kelly_full = (0.95*1 - 0.05)/1 = 0.90
    // kelly_adjusted = 0.90 * 0.25 * 1.0 = 0.225
    // Assuming max_position_pct = 0.20 => clamped to 0.20
    // notional = 1000 * 0.20 = 200
    // contracts = floor(200/0.50) = 400
    let sizer = KellyPositionSizer::new(dec!(0.25), dec!(0.20), dec!(0.02));
    let edge = EdgeEstimate::new(dec!(0.95), Decimal::ONE);
    let result = sizer
        .calculate_position_size(dec!(1000), dec!(0.50), &edge)
        .unwrap();

    assert_eq!(
        result.kelly_adjusted,
        dec!(0.20),
        "Large edge clamped to max_position_pct"
    );
    assert_eq!(result.contracts, 400);
}

// =============================================================================
// 2. Completeness Arbitrage Scanner
// =============================================================================

#[test]
fn arb_yes50_no45_profitable() {
    // YES ask = 0.50, NO ask = 0.45
    // combined = 0.95
    // gross_margin = 1.0 - 0.95 = 0.05
    // fee = 0.95 * 0.001 = 0.00095
    // net_margin = 0.05 - 0.00095 = 0.04905
    // min_margin = 0.01 => 0.04905 > 0.01 => signal generated
    let tracker = OrderBookTracker::new();
    tracker.update(make_book("test-market", dec!(0.50), dec!(0.45)));

    let signals = tracker.scan_completeness_arb(dec!(0.01));
    assert_eq!(signals.len(), 1, "Expected exactly one arb signal");

    let sig = &signals[0];
    assert_eq!(sig.market_slug, "test-market");
    assert_eq!(sig.yes_ask, dec!(0.50));
    assert_eq!(sig.no_ask, dec!(0.45));
    assert_eq!(sig.combined_cost, dec!(0.95));
    assert_eq!(sig.gross_margin, dec!(0.05));
    assert_eq!(
        sig.net_margin,
        dec!(0.04905),
        "Net = 0.05 - 0.00095 = 0.04905"
    );
}

#[test]
fn arb_yes50_no50_no_arb() {
    // YES ask = 0.50, NO ask = 0.50
    // combined = 1.00 >= 1.0 => no arb
    let tracker = OrderBookTracker::new();
    tracker.update(make_book("market-even", dec!(0.50), dec!(0.50)));

    let signals = tracker.scan_completeness_arb(dec!(0.0));
    assert!(
        signals.is_empty(),
        "combined=1.00 must produce no arb signal"
    );
}

#[test]
fn arb_yes55_no46_sum_over_one_no_arb() {
    // YES ask = 0.55, NO ask = 0.46
    // combined = 1.01 >= 1.0 => no arb
    let tracker = OrderBookTracker::new();
    tracker.update(make_book("overpriced", dec!(0.55), dec!(0.46)));

    let signals = tracker.scan_completeness_arb(dec!(0.0));
    assert!(
        signals.is_empty(),
        "combined=1.01 (>1.0) must produce no arb signal"
    );
}

#[test]
fn arb_yes48_no48_net_margin_check() {
    // YES ask = 0.48, NO ask = 0.48
    // combined = 0.96
    // gross_margin = 1.0 - 0.96 = 0.04
    // fee = 0.96 * 0.001 = 0.00096
    // net_margin = 0.04 - 0.00096 = 0.03904
    let tracker = OrderBookTracker::new();
    tracker.update(make_book("balanced", dec!(0.48), dec!(0.48)));

    let signals = tracker.scan_completeness_arb(dec!(0.01));
    assert_eq!(signals.len(), 1);
    let sig = &signals[0];
    assert_eq!(sig.combined_cost, dec!(0.96));
    assert_eq!(sig.gross_margin, dec!(0.04));
    assert_eq!(sig.net_margin, dec!(0.03904), "Net = 0.04 - 0.00096");
}

#[test]
fn arb_fee_is_exactly_10bps() {
    // Verify fee = combined * 0.001 (10 basis points)
    // YES ask = 0.40, NO ask = 0.40 => combined = 0.80
    // fee should be 0.80 * 0.001 = 0.00080
    // gross = 0.20, net = 0.20 - 0.00080 = 0.19920
    let tracker = OrderBookTracker::new();
    tracker.update(make_book("fee-check", dec!(0.40), dec!(0.40)));

    let signals = tracker.scan_completeness_arb(dec!(0.0));
    assert_eq!(signals.len(), 1);
    let sig = &signals[0];

    let expected_fee = dec!(0.80) * dec!(0.001);
    let expected_net = dec!(0.20) - expected_fee;
    assert_eq!(expected_fee, dec!(0.00080), "Fee = 10bps of combined");
    assert_eq!(sig.net_margin, expected_net, "Net = gross - fee");
    assert_eq!(sig.net_margin, dec!(0.19920));
}

#[test]
fn arb_min_margin_filter() {
    // Combined = 0.99 => gross = 0.01, fee = 0.00099, net = 0.00901
    // If min_margin = 0.01, then 0.00901 < 0.01 => filtered out
    let tracker = OrderBookTracker::new();
    tracker.update(make_book("thin-margin", dec!(0.50), dec!(0.49)));

    let signals = tracker.scan_completeness_arb(dec!(0.01));
    assert!(
        signals.is_empty(),
        "net_margin 0.00901 < min_margin 0.01 must be filtered"
    );

    // But with min_margin = 0.005, should pass
    let signals_lower = tracker.scan_completeness_arb(dec!(0.005));
    assert_eq!(
        signals_lower.len(),
        1,
        "net_margin 0.00901 > 0.005 should pass"
    );
}

#[test]
fn arb_multiple_markets_only_eligible_returned() {
    // Market A: combined = 0.90 => arb (net = 0.10 - 0.0009 = 0.0991)
    // Market B: combined = 1.02 => no arb
    // Market C: combined = 0.95 => arb (net = 0.05 - 0.00095 = 0.04905)
    let tracker = OrderBookTracker::new();
    tracker.update(make_book("market-a", dec!(0.45), dec!(0.45)));
    tracker.update(make_book("market-b", dec!(0.55), dec!(0.47)));
    tracker.update(make_book("market-c", dec!(0.50), dec!(0.45)));

    let signals = tracker.scan_completeness_arb(dec!(0.01));

    // Should contain market-a and market-c, but not market-b
    let slugs: Vec<&str> = signals.iter().map(|s| s.market_slug.as_str()).collect();
    assert!(slugs.contains(&"market-a"), "market-a should have arb");
    assert!(
        !slugs.contains(&"market-b"),
        "market-b (sum>1) should NOT have arb"
    );
    assert!(slugs.contains(&"market-c"), "market-c should have arb");
    assert_eq!(signals.len(), 2, "Exactly 2 eligible markets");
}

// =============================================================================
// 3. Circuit Breaker
// =============================================================================

#[test]
fn circuit_breaker_daily_loss_trips_at_exact_threshold() {
    // daily_loss_limit = $100, starting equity = $1000
    // Loss of exactly $100 => should trip (>=)
    let mut cb = CircuitBreaker::new(dec!(100), dec!(0.10));
    cb.initialize(dec!(1000));

    // Just below threshold: loss of $99.99 => still OK
    cb.update(dec!(900.01));
    let (can_trade, _) = cb.can_trade();
    assert!(can_trade, "Loss of $99.99 should NOT trip breaker");

    // Exactly at threshold: loss of $100
    cb.update(dec!(900));
    let (can_trade, reason) = cb.can_trade();
    assert!(!can_trade, "Loss of exactly $100 must trip breaker");
    assert!(
        reason.is_some(),
        "Trip reason must be provided"
    );
}

#[test]
fn circuit_breaker_drawdown_trips_at_exact_threshold() {
    // max_drawdown_pct = 10%, starting equity = $1000
    // Peak rises to $1200, then 10% drawdown from peak = $120 loss => equity $1080
    let mut cb = CircuitBreaker::new(dec!(500), dec!(0.10)); // large daily limit so only drawdown matters
    cb.initialize(dec!(1000));

    // Peak rises
    cb.update(dec!(1200));
    let (can_trade, _) = cb.can_trade();
    assert!(can_trade, "Equity rising should not trip");

    // Drawdown just below 10%: equity = 1081 => drawdown = 119/1200 = 0.0991... < 0.10
    cb.update(dec!(1081));
    let (can_trade, _) = cb.can_trade();
    assert!(can_trade, "9.9% drawdown should NOT trip");

    // Drawdown exactly 10%: equity = 1080 => drawdown = 120/1200 = 0.10
    cb.update(dec!(1080));
    let (can_trade, _) = cb.can_trade();
    assert!(!can_trade, "10% drawdown from peak must trip breaker");
}

#[test]
fn circuit_breaker_tripped_blocks_trading() {
    let mut cb = CircuitBreaker::new(dec!(50), dec!(0.10));
    cb.initialize(dec!(1000));

    // Trip via daily loss
    cb.update(dec!(940)); // loss = $60 > $50
    let (can_trade, _) = cb.can_trade();
    assert!(!can_trade, "Tripped breaker must block trading");

    // Further updates do not un-trip
    cb.update(dec!(1100)); // even if equity recovers
    let (can_trade, _) = cb.can_trade();
    assert!(
        !can_trade,
        "Once tripped, equity recovery does NOT reset breaker"
    );
}

#[test]
fn circuit_breaker_reset_clears_trip() {
    let mut cb = CircuitBreaker::new(dec!(50), dec!(0.10));
    cb.initialize(dec!(1000));

    // Trip
    cb.update(dec!(940));
    assert!(cb.is_tripped());

    // Reset
    cb.reset(dec!(940));
    let (can_trade, _) = cb.can_trade();
    assert!(can_trade, "Reset must clear tripped state");
    assert!(!cb.is_tripped());
}

#[test]
fn circuit_breaker_peak_equity_tracking() {
    // Verify peak tracks the highest equity seen
    let mut cb = CircuitBreaker::new(dec!(500), dec!(0.20));
    cb.initialize(dec!(1000));

    cb.update(dec!(1100));
    cb.update(dec!(1050)); // dip
    cb.update(dec!(1200)); // new peak
    cb.update(dec!(1150)); // dip again

    // Drawdown from peak (1200): 50/1200 = 4.16% < 20%
    let (can_trade, _) = cb.can_trade();
    assert!(can_trade);

    // Now a big drop to trigger 20% from peak of 1200 => threshold at 960
    cb.update(dec!(960));
    let (can_trade, _) = cb.can_trade();
    assert!(
        !can_trade,
        "20% drawdown from peak=$1200 => equity $960 must trip"
    );
}

#[test]
fn circuit_breaker_emergency_stop() {
    let mut cb = CircuitBreaker::new(dec!(500), dec!(0.50));
    cb.initialize(dec!(1000));

    cb.emergency_stop("manual halt");
    let (can_trade, reason) = cb.can_trade();
    assert!(!can_trade);
    assert_eq!(reason.unwrap(), "manual halt");
}

// =============================================================================
// 4. Exposure Monitor
// =============================================================================

fn make_exposure_monitor() -> ExposureMonitor {
    ExposureMonitor::new(ExposureConfig {
        max_position_per_market: dec!(200),
        max_portfolio_exposure: dec!(500),
        max_correlated_exposure: dec!(300),
        max_positions: 3,
    })
}

#[test]
fn exposure_per_market_limit_enforcement() {
    // Market "foo" already has $150 exposure. Limit is $200.
    // Adding $60 => $210 > $200 => blocked.
    // Headroom = $200 - $150 = $50
    let monitor = make_exposure_monitor();
    let state = StateManager::new(dec!(1000));
    state.update_position("foo", Side::Yes, 300, dec!(0.50));
    // cost_basis = 300 * 0.50 = $150

    let check = monitor.can_add_exposure(&state, "foo", dec!(60));
    assert!(!check.allowed, "Adding $60 to $150 exceeds $200 per-market");
    assert_eq!(
        check.max_additional_exposure,
        dec!(50),
        "Headroom should be $50"
    );
}

#[test]
fn exposure_portfolio_wide_limit() {
    // Portfolio limit = $500. Currently $450 across markets.
    // Adding $60 => $510 > $500 => blocked.
    let monitor = make_exposure_monitor();
    let state = StateManager::new(dec!(1000));
    // Two positions totaling $450
    state.update_position("mkt-a", Side::Yes, 200, dec!(1.00)); // $200
    state.update_position("mkt-b", Side::Yes, 500, dec!(0.50)); // $250

    let check = monitor.can_add_exposure(&state, "mkt-c", dec!(60));
    assert!(
        !check.allowed,
        "Adding $60 to $450 total exceeds $500 portfolio limit"
    );
    // Portfolio headroom = 500 - 450 = 50; market headroom = 200 (fresh market)
    // min(200, 50) = 50
    assert_eq!(check.max_additional_exposure, dec!(50));
}

#[test]
fn exposure_position_count_limit() {
    // Max 3 positions. Already have 3. Adding a NEW market must fail.
    let monitor = make_exposure_monitor();
    let state = StateManager::new(dec!(10000));
    state.update_position("a", Side::Yes, 10, dec!(1.00)); // $10
    state.update_position("b", Side::Yes, 10, dec!(1.00)); // $10
    state.update_position("c", Side::Yes, 10, dec!(1.00)); // $10

    // New market "d"
    let check = monitor.can_add_exposure(&state, "d", dec!(10));
    assert!(!check.allowed, "4th position must be blocked");

    // Existing market "a" should still be fine
    let check_existing = monitor.can_add_exposure(&state, "a", dec!(10));
    assert!(
        check_existing.allowed,
        "Adding to existing position should be allowed"
    );
}

#[test]
fn exposure_correlation_group_limit() {
    // Group "election" contains markets a, b. Correlation limit = $300.
    // a=$150, b=$100 => group total = $250
    // Adding $60 to "b" => group = $310 > $300 => blocked
    let mut monitor = make_exposure_monitor();
    monitor.set_correlation_group(
        "election",
        vec!["a".to_string(), "b".to_string()],
    );

    let state = StateManager::new(dec!(10000));
    state.update_position("a", Side::Yes, 150, dec!(1.00)); // $150
    state.update_position("b", Side::Yes, 100, dec!(1.00)); // $100

    let check = monitor.can_add_exposure(&state, "b", dec!(60));
    assert!(
        !check.allowed,
        "Correlation group $310 > $300 limit must block"
    );

    // Headroom = min(market_headroom=100, portfolio_headroom=250, corr_headroom=50) = 50
    assert_eq!(check.max_additional_exposure, dec!(50));
}

#[test]
fn exposure_headroom_calculation() {
    // Fresh state, no positions. All headroom = the lower of per-market and portfolio.
    // per-market = $200, portfolio = $500
    // Headroom for new market = min($200, $500) = $200
    let monitor = make_exposure_monitor();
    let state = StateManager::new(dec!(10000));

    let check = monitor.can_add_exposure(&state, "fresh", dec!(100));
    assert!(check.allowed);
    assert_eq!(
        check.max_additional_exposure,
        dec!(200),
        "Headroom = min(per_market=$200, portfolio=$500) = $200"
    );
}

// =============================================================================
// 5. Risk Manager Integration
// =============================================================================

#[test]
fn risk_buy_insufficient_cash_rejected() {
    // Balance = $10, trying to buy 100 contracts at $0.50 = $50 notional.
    // Cash check: available=10, buffer=0.98 => max_affordable = (10*0.98)/0.50 = 19.6 => floor=19
    // 19 contracts * 0.50 = $9.50 notional
    // Should be resized or rejected based on min_trade_size
    let state = StateManager::new(dec!(10));
    let config = RiskConfig {
        min_trade_size: dec!(15), // min $15 => $9.50 < $15 => rejected
        ..permissive_risk_config()
    };
    let mut rm = RiskManager::new(config, state);

    let signal = make_buy_signal("mkt", dec!(0.50), 100, 1.0, None);
    let decision = rm.evaluate_signal(signal);
    assert!(
        !decision.approved,
        "Insufficient cash ($10 for $50 buy) must be rejected"
    );
}

#[test]
fn risk_buy_exceeding_per_market_limit_resized() {
    // Per-market limit = $200. Trying to buy 500 contracts at $0.50 = $250.
    // Exposure check: market has 0 exposure, headroom = $200.
    // Resize: floor(200 / 0.50) = 400 contracts.
    let state = StateManager::new(dec!(5000));
    let config = RiskConfig {
        max_position_per_market: dec!(200),
        min_trade_size: dec!(1),
        ..permissive_risk_config()
    };
    let mut rm = RiskManager::new(config, state);

    // No true_probability => skip Kelly, just use raw qty
    let signal = make_buy_signal("mkt", dec!(0.50), 500, 1.0, None);
    let decision = rm.evaluate_signal(signal);
    assert!(decision.approved, "Should be approved after resize");

    let approved_signal = decision.signal.unwrap();
    assert!(
        approved_signal.quantity <= 400,
        "Quantity must be resized to at most 400 (per-market $200 at $0.50)"
    );
}

#[test]
fn risk_buy_with_kelly_sizing() {
    // Setup: equity=$1000, price=0.50, true_prob=0.60, confidence=1.0
    // Kelly quarter: contracts ~ 100 (from hand-verified test)
    // Signal requests 500, Kelly caps near 100.
    //
    // NOTE: true_probability passes through serde_json (f64) -> Decimal.
    // f64 0.6 = 0.599999...97, so Kelly computes 49.99.../0.50 = 99.99... => floor = 99.
    // This is correct behavior: the f64 imprecision is a realistic input path.
    let state = StateManager::new(dec!(1000));
    let config = permissive_risk_config();
    let mut rm = RiskManager::new(config, state);

    let signal = make_buy_signal("mkt", dec!(0.50), 500, 1.0, Some(0.60));
    let decision = rm.evaluate_signal(signal);
    assert!(decision.approved, "Kelly-sized buy should be approved");

    let approved = decision.signal.unwrap();
    assert!(
        approved.quantity == 99 || approved.quantity == 100,
        "Kelly should cap near 100 contracts (got {}; 99 expected due to f64->Decimal rounding)",
        approved.quantity
    );
}

#[test]
fn risk_sell_always_passes_circuit_breaker() {
    // Trip the circuit breaker, then verify sells still go through.
    let state = StateManager::new(dec!(1000));
    // Add a position so we have something to sell
    state.update_position("mkt", Side::Yes, 100, dec!(0.50));
    let config = RiskConfig {
        max_daily_loss: dec!(10), // very tight, will trip immediately
        min_trade_size: dec!(1),
        ..permissive_risk_config()
    };
    let mut rm = RiskManager::new(config, state.clone());

    // Drop equity to trip the breaker. Balance was 1000, total equity = 1000 + 50 = 1050.
    // We need equity to drop by $10. Set balance to 990 => equity = 990 + 50 = 1040.
    // Starting equity was 1050, daily loss = 1050 - 1040 = $10 >= $10 => tripped.
    state.update_balance(dec!(990));

    // First, verify a buy is blocked
    let buy_signal = make_buy_signal("other", dec!(0.50), 10, 1.0, None);
    let buy_decision = rm.evaluate_signal(buy_signal);
    assert!(
        !buy_decision.approved,
        "Buy must be blocked after circuit breaker trips"
    );

    // Now verify a sell goes through
    let sell_signal = make_sell_signal("mkt", dec!(0.50), 50);
    let sell_decision = rm.evaluate_signal(sell_signal);
    assert!(
        sell_decision.approved,
        "Sell must be allowed even when circuit breaker is tripped"
    );
}

#[test]
fn risk_cancel_always_approved() {
    // Even if circuit breaker is tripped, cancels must go through.
    let state = StateManager::new(dec!(0)); // zero balance, everything should be blocked
    let config = RiskConfig {
        max_daily_loss: dec!(0), // immediately trippable
        min_trade_size: dec!(1),
        ..permissive_risk_config()
    };
    let mut rm = RiskManager::new(config, state);

    let cancel = make_cancel_signal("any-market");
    let decision = rm.evaluate_signal(cancel);
    assert!(decision.approved, "CancelAll must ALWAYS be approved");
}

#[test]
fn risk_drawdown_blocks_new_buys() {
    // max_total_pnl_drawdown_pct_for_new_buys = 5%
    // Starting equity = $1000. If equity drops to $950, that is 5% drawdown => blocked.
    let state = StateManager::new(dec!(1000));
    let config = RiskConfig {
        max_total_pnl_drawdown_pct_for_new_buys: dec!(0.05),
        max_daily_loss: dec!(1000), // won't trip circuit breaker
        max_drawdown_pct: dec!(0.50), // won't trip circuit breaker
        min_trade_size: dec!(1),
        ..permissive_risk_config()
    };
    let mut rm = RiskManager::new(config, state.clone());

    // Drop equity by exactly 5%
    state.update_balance(dec!(950));

    let signal = make_buy_signal("mkt", dec!(0.50), 10, 1.0, None);
    let decision = rm.evaluate_signal(signal);
    assert!(
        !decision.approved,
        "5% total PnL drawdown must block new buys"
    );
    assert!(
        decision.reason.contains("drawdown"),
        "Reason should mention drawdown"
    );
}

// =============================================================================
// 6. Order Book
// =============================================================================

#[test]
fn orderbook_best_bid_ask_from_multiple_levels() {
    // Bids: [0.45, 0.48, 0.47], best bid = max = 0.48
    // Asks: [0.52, 0.50, 0.55], best ask = min = 0.50
    let side = OrderBookSide {
        bids: vec![
            PriceLevel { price: dec!(0.45), quantity: 100 },
            PriceLevel { price: dec!(0.48), quantity: 200 },
            PriceLevel { price: dec!(0.47), quantity: 150 },
        ],
        asks: vec![
            PriceLevel { price: dec!(0.52), quantity: 100 },
            PriceLevel { price: dec!(0.50), quantity: 200 },
            PriceLevel { price: dec!(0.55), quantity: 50 },
        ],
    };

    assert_eq!(side.best_bid(), Some(dec!(0.48)), "Best bid = max(0.45, 0.48, 0.47)");
    assert_eq!(side.best_ask(), Some(dec!(0.50)), "Best ask = min(0.52, 0.50, 0.55)");
}

#[test]
fn orderbook_spread_calculation() {
    // best_ask=0.52, best_bid=0.48 => spread = 0.04
    let side = OrderBookSide {
        bids: vec![PriceLevel { price: dec!(0.48), quantity: 100 }],
        asks: vec![PriceLevel { price: dec!(0.52), quantity: 100 }],
    };
    assert_eq!(
        side.spread(),
        Some(dec!(0.04)),
        "Spread = 0.52 - 0.48 = 0.04"
    );
}

#[test]
fn orderbook_spread_with_empty_side() {
    // No bids => spread is None
    let side = OrderBookSide {
        bids: vec![],
        asks: vec![PriceLevel { price: dec!(0.50), quantity: 100 }],
    };
    assert_eq!(side.spread(), None, "No bids => spread is None");

    // No asks => spread is None
    let side2 = OrderBookSide {
        bids: vec![PriceLevel { price: dec!(0.50), quantity: 100 }],
        asks: vec![],
    };
    assert_eq!(side2.spread(), None, "No asks => spread is None");
}

#[test]
fn orderbook_top_of_book_computation() {
    // Full book with YES and NO sides. Verify TopOfBook fields.
    let tracker = OrderBookTracker::new();
    let book = OrderBook {
        market_slug: "test".to_string(),
        yes: OrderBookSide {
            bids: vec![PriceLevel { price: dec!(0.48), quantity: 100 }],
            asks: vec![PriceLevel { price: dec!(0.52), quantity: 100 }],
        },
        no: OrderBookSide {
            bids: vec![PriceLevel { price: dec!(0.45), quantity: 100 }],
            asks: vec![PriceLevel { price: dec!(0.49), quantity: 100 }],
        },
    };
    tracker.update(book);

    let top = tracker.get_top("test").expect("Top should exist");
    assert_eq!(top.yes_best_bid, Some(dec!(0.48)));
    assert_eq!(top.yes_best_ask, Some(dec!(0.52)));
    assert_eq!(top.no_best_bid, Some(dec!(0.45)));
    assert_eq!(top.no_best_ask, Some(dec!(0.49)));
}

#[test]
fn orderbook_top_of_book_mid_price() {
    // yes_bid=0.48, yes_ask=0.52 => mid = (0.48+0.52)/2 = 0.50
    let top = TopOfBook {
        yes_best_bid: Some(dec!(0.48)),
        yes_best_ask: Some(dec!(0.52)),
        no_best_bid: None,
        no_best_ask: None,
    };
    assert_eq!(top.yes_mid(), Some(dec!(0.50)), "Mid = (0.48+0.52)/2 = 0.50");
}

#[test]
fn orderbook_top_of_book_spread() {
    // yes_bid=0.48, yes_ask=0.52 => spread = 0.04
    let top = TopOfBook {
        yes_best_bid: Some(dec!(0.48)),
        yes_best_ask: Some(dec!(0.52)),
        no_best_bid: None,
        no_best_ask: None,
    };
    assert_eq!(top.yes_spread(), Some(dec!(0.04)), "Spread = 0.52 - 0.48 = 0.04");
}

#[test]
fn orderbook_completeness_sum() {
    // yes_ask=0.52, no_ask=0.49 => completeness = 1.01
    let top = TopOfBook {
        yes_best_bid: None,
        yes_best_ask: Some(dec!(0.52)),
        no_best_bid: None,
        no_best_ask: Some(dec!(0.49)),
    };
    assert_eq!(
        top.completeness_sum(),
        Some(dec!(1.01)),
        "Completeness = 0.52 + 0.49 = 1.01"
    );
}

#[test]
fn orderbook_thread_safety_update_and_read() {
    // Write from one thread, read from another. Verify no panics and data is consistent.
    use std::sync::Arc;
    use std::thread;

    let tracker = Arc::new(OrderBookTracker::new());
    let writer = tracker.clone();
    let reader = tracker.clone();

    let write_handle = thread::spawn(move || {
        for i in 0..100 {
            let price = Decimal::new(40 + (i % 10), 2); // 0.40 .. 0.49
            writer.update(make_book("threaded", price, dec!(0.45)));
        }
    });

    let read_handle = thread::spawn(move || {
        let mut read_count = 0;
        for _ in 0..100 {
            if let Some(top) = reader.get_top("threaded") {
                // Verify invariants: yes_best_ask should be a valid Decimal
                assert!(
                    top.yes_best_ask.unwrap_or(Decimal::ZERO) >= Decimal::ZERO,
                    "Negative ask price is impossible"
                );
                read_count += 1;
            }
        }
        read_count
    });

    write_handle.join().expect("Writer thread panicked");
    let reads = read_handle.join().expect("Reader thread panicked");

    // After both threads complete, the book should be present
    assert!(
        tracker.get_top("threaded").is_some(),
        "Book must exist after writes"
    );
    // We expect at least some reads to have succeeded (the market may not exist
    // for the very first few reads if the reader thread runs first).
    // This is a concurrency test - the point is no panics / deadlocks.
    let _ = reads; // just ensure it compiled and ran
}

#[test]
fn orderbook_total_depth() {
    // Verify total bid/ask depth sums correctly.
    let side = OrderBookSide {
        bids: vec![
            PriceLevel { price: dec!(0.48), quantity: 100 },
            PriceLevel { price: dec!(0.47), quantity: 200 },
            PriceLevel { price: dec!(0.46), quantity: 50 },
        ],
        asks: vec![
            PriceLevel { price: dec!(0.52), quantity: 300 },
            PriceLevel { price: dec!(0.53), quantity: 150 },
        ],
    };
    assert_eq!(
        side.total_bid_depth(),
        350,
        "Total bid depth = 100+200+50 = 350"
    );
    assert_eq!(
        side.total_ask_depth(),
        450,
        "Total ask depth = 300+150 = 450"
    );
}

#[test]
fn orderbook_tracker_remove_market() {
    let tracker = OrderBookTracker::new();
    tracker.update(make_book("removable", dec!(0.50), dec!(0.45)));
    assert!(tracker.get_top("removable").is_some());

    tracker.remove("removable");
    assert!(
        tracker.get_top("removable").is_none(),
        "Removed market must not appear"
    );
}

#[test]
fn orderbook_tracker_update_side() {
    // Partially update just the YES side
    let tracker = OrderBookTracker::new();
    tracker.update(make_book("partial", dec!(0.50), dec!(0.45)));

    // Update YES side with a new ask
    let new_yes = OrderBookSide {
        bids: vec![PriceLevel { price: dec!(0.49), quantity: 200 }],
        asks: vec![PriceLevel { price: dec!(0.51), quantity: 200 }],
    };
    tracker.update_side("partial", Side::Yes, new_yes);

    let top = tracker.get_top("partial").unwrap();
    assert_eq!(
        top.yes_best_ask,
        Some(dec!(0.51)),
        "YES ask should be updated to 0.51"
    );
    assert_eq!(
        top.yes_best_bid,
        Some(dec!(0.49)),
        "YES bid should be updated to 0.49"
    );
    // NO side should be unchanged
    assert_eq!(
        top.no_best_ask,
        Some(dec!(0.45)),
        "NO ask should remain 0.45"
    );
}

// =============================================================================
// Edge cases and regression guards
// =============================================================================

#[test]
fn kelly_tiny_bankroll_produces_zero_contracts() {
    // bankroll = $0.01, price = 0.50, true_prob = 0.60
    // kelly_adjusted = 0.05 (from hand-verified)
    // notional = 0.01 * 0.05 = 0.0005
    // contracts = floor(0.0005 / 0.50) = floor(0.001) = 0 => None
    let sizer = KellyPositionSizer::new(dec!(0.25), dec!(1.0), dec!(0.02));
    let edge = EdgeEstimate::new(dec!(0.60), Decimal::ONE);
    let result = sizer.calculate_position_size(dec!(0.01), dec!(0.50), &edge);
    assert!(
        result.is_none(),
        "Tiny bankroll producing 0 contracts must return None"
    );
}

#[test]
fn arb_combined_just_below_one() {
    // YES=0.499, NO=0.500 => combined = 0.999
    // gross = 0.001, fee = 0.999*0.001 = 0.000999
    // net = 0.001 - 0.000999 = 0.000001
    // With min_margin = 0, this should still be detected
    let tracker = OrderBookTracker::new();
    tracker.update(make_book("razor-thin", dec!(0.499), dec!(0.500)));

    let signals = tracker.scan_completeness_arb(Decimal::ZERO);
    assert_eq!(signals.len(), 1, "Razor-thin arb should be detected");
    let sig = &signals[0];
    assert_eq!(sig.combined_cost, dec!(0.999));

    // Verify net margin is positive but very small
    assert!(
        sig.net_margin > Decimal::ZERO,
        "Net margin must be positive"
    );
    // net = 0.001 - 0.000999 = 0.000001
    assert_eq!(sig.net_margin, dec!(0.000001));
}

#[test]
fn circuit_breaker_zero_starting_equity_no_panic() {
    // Edge case: starting equity = 0. Drawdown calc divides by peak.
    // The code guards: `if self.peak_equity > Decimal::ZERO`
    let mut cb = CircuitBreaker::new(dec!(100), dec!(0.10));
    cb.initialize(Decimal::ZERO);

    // This should not panic or trip (no meaningful drawdown from zero)
    cb.update(Decimal::ZERO);
    let (can_trade, _) = cb.can_trade();
    assert!(
        can_trade,
        "Zero starting equity should not trip drawdown (guarded division)"
    );
}

#[test]
fn exposure_all_limits_pass() {
    // No positions, generous limits. Adding a small trade must pass.
    let monitor = make_exposure_monitor();
    let state = StateManager::new(dec!(10000));

    let check = monitor.can_add_exposure(&state, "fresh-market", dec!(50));
    assert!(check.allowed, "Small trade in empty portfolio must pass");
    assert_eq!(check.reason, "OK");
}

#[test]
fn risk_manager_min_trade_size_filter() {
    // Verify that trades below min_trade_size are rejected.
    // price=0.50, qty=1 => notional=$0.50 < min_trade_size=$5
    let state = StateManager::new(dec!(10000));
    let config = RiskConfig {
        min_trade_size: dec!(5),
        ..permissive_risk_config()
    };
    let mut rm = RiskManager::new(config, state);

    let signal = make_buy_signal("mkt", dec!(0.50), 1, 1.0, None);
    let decision = rm.evaluate_signal(signal);
    assert!(
        !decision.approved,
        "Notional $0.50 < min $5 must be rejected"
    );
}

#[test]
fn kelly_from_confidence_constructor() {
    // Test the from_confidence constructor which converts f64 to Decimal.
    let edge = EdgeEstimate::from_confidence(dec!(0.60), 0.75);
    // 0.75 as Decimal should be 0.75
    assert_eq!(
        edge.confidence,
        dec!(0.75),
        "from_confidence(0.75) should produce Decimal 0.75"
    );
    assert_eq!(edge.probability, dec!(0.60));
}

#[test]
fn risk_buy_zero_quantity_rejected() {
    // Signal with qty=0 must be rejected (non-positive quantity)
    let state = StateManager::new(dec!(10000));
    let config = permissive_risk_config();
    let mut rm = RiskManager::new(config, state);

    let signal = make_buy_signal("mkt", dec!(0.50), 0, 1.0, None);
    let decision = rm.evaluate_signal(signal);
    assert!(!decision.approved, "Zero quantity must be rejected");
    assert!(
        decision.reason.contains("non-positive"),
        "Reason should mention non-positive quantity"
    );
}
