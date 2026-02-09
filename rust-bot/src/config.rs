//! Configuration management.
//!
//! Loads settings from environment variables and .env file.
//! Faithfully ports Python `src/config.py`.

#![allow(dead_code)]

use rust_decimal::Decimal;
use std::str::FromStr;

/// Application configuration loaded from environment.
#[derive(Debug, Clone)]
pub struct Settings {
    // Polymarket API
    pub pm_api_key_id: String,
    pub pm_private_key: String,
    pub pm_base_url: String,
    pub pm_ws_url: String,

    // Trading
    pub trading_mode: TradingMode,
    pub initial_balance: Decimal,
    pub tick_interval_secs: f64,

    // Market Selection
    pub market_slugs: Vec<String>,
    pub leagues: Vec<String>,
    pub market_types: Vec<String>,
    pub min_liquidity: Decimal,

    // Risk
    pub max_position_per_market: Decimal,
    pub max_portfolio_exposure: Decimal,
    pub max_portfolio_exposure_pct: Decimal,
    pub max_daily_loss: Decimal,
    pub kelly_fraction: Decimal,
    pub min_edge: Decimal,
    pub min_trade_size: Decimal,
    pub max_correlated_exposure: Decimal,
    pub max_positions: usize,
    pub max_drawdown_pct: Decimal,
    pub max_total_pnl_drawdown_pct_for_new_buys: Decimal,

    // Strategy flags
    pub enable_market_maker: bool,
    pub enable_live_arbitrage: bool,
    pub enable_statistical_edge: bool,

    // Market maker tuning
    pub market_maker_order_size: Decimal,
    pub market_maker_spread: Decimal,

    // Live arbitrage tuning
    pub live_arb_min_edge: Decimal,
    pub live_arb_order_size: Decimal,
    pub live_arb_cooldown_seconds: f64,

    // Statistical edge tuning
    pub stat_edge_min_edge: Decimal,
    pub stat_edge_order_size: Decimal,
    pub stat_edge_cooldown_seconds: f64,

    // Feed configuration
    pub use_mock_feeds: bool,

    // Live execution
    pub live_reconcile_interval_seconds: f64,

    // REST orderbook polling
    pub enable_rest_orderbook_polling: bool,
    pub rest_orderbook_poll_interval_seconds: f64,
    pub rest_orderbook_max_markets: usize,
    pub rest_orderbook_concurrency: usize,

    // Logging
    pub log_level: String,
    pub log_json: bool,

    // External data feeds
    pub odds_api_key: String,
    pub odds_api_poll_interval_seconds: f64,
    pub scores_poll_interval_seconds: f64,

    // Health check
    pub health_host: String,
    pub health_port: u16,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TradingMode {
    Paper,
    Live,
}

impl FromStr for TradingMode {
    type Err = String;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s.to_lowercase().as_str() {
            "paper" => Ok(Self::Paper),
            "live" => Ok(Self::Live),
            _ => Err(format!("Invalid trading mode: {s}")),
        }
    }
}

impl Settings {
    /// Load settings from environment variables (and .env file).
    pub fn from_env() -> Self {
        // Try to load .env file (ignore if not found).
        let _ = dotenvy::dotenv();

        Self {
            pm_api_key_id: env_str("PM_API_KEY_ID", ""),
            pm_private_key: env_str("PM_PRIVATE_KEY", ""),
            pm_base_url: env_str("PM_BASE_URL", "https://api.polymarket.us"),
            pm_ws_url: env_str("PM_WS_URL", "wss://api.polymarket.us/v1/ws"),

            trading_mode: env_str("TRADING_MODE", "paper")
                .parse()
                .unwrap_or(TradingMode::Paper),
            initial_balance: env_decimal("INITIAL_BALANCE", Decimal::new(1000, 0)),
            tick_interval_secs: env_f64("TICK_INTERVAL", 1.0),

            market_slugs: env_csv("MARKET_SLUGS"),
            leagues: env_csv_default("LEAGUES", "nba,cbb"),
            market_types: env_csv_default("MARKET_TYPES", "aec"),
            min_liquidity: env_decimal("MIN_LIQUIDITY", Decimal::ZERO),

            max_position_per_market: env_decimal("RISK_MAX_POSITION_PER_MARKET", Decimal::new(50, 0)),
            max_portfolio_exposure: env_decimal("RISK_MAX_PORTFOLIO_EXPOSURE", Decimal::new(350, 0)),
            max_portfolio_exposure_pct: env_decimal("RISK_MAX_PORTFOLIO_EXPOSURE_PCT", Decimal::new(35, 2)),
            max_daily_loss: env_decimal("RISK_MAX_DAILY_LOSS", Decimal::new(100, 0)),
            kelly_fraction: env_decimal("RISK_KELLY_FRACTION", Decimal::new(25, 2)),
            min_edge: env_decimal("RISK_MIN_EDGE", Decimal::new(2, 2)),
            min_trade_size: env_decimal("RISK_MIN_TRADE_SIZE", Decimal::ONE),
            max_correlated_exposure: env_decimal("RISK_MAX_CORRELATED_EXPOSURE", Decimal::new(2500, 0)),
            max_positions: env_usize("RISK_MAX_POSITIONS", 10),
            max_drawdown_pct: env_decimal("RISK_MAX_DRAWDOWN_PCT", Decimal::new(10, 2)),
            max_total_pnl_drawdown_pct_for_new_buys: env_decimal(
                "RISK_MAX_TOTAL_PNL_DRAWDOWN_PCT_FOR_NEW_BUYS",
                Decimal::new(5, 2),
            ),

            enable_market_maker: env_bool("ENABLE_MARKET_MAKER", true),
            enable_live_arbitrage: env_bool("ENABLE_LIVE_ARBITRAGE", false),
            enable_statistical_edge: env_bool("ENABLE_STATISTICAL_EDGE", false),

            market_maker_order_size: env_decimal("MARKET_MAKER_ORDER_SIZE_USD", Decimal::new(10, 0)),
            market_maker_spread: env_decimal("MARKET_MAKER_SPREAD", Decimal::new(2, 2)),

            live_arb_min_edge: env_decimal("LIVE_ARB_MIN_EDGE", Decimal::new(3, 2)),
            live_arb_order_size: env_decimal("LIVE_ARB_ORDER_SIZE", Decimal::new(10, 0)),
            live_arb_cooldown_seconds: env_f64("LIVE_ARB_COOLDOWN_SECONDS", 5.0),

            stat_edge_min_edge: env_decimal("STAT_EDGE_MIN_EDGE", Decimal::new(2, 2)),
            stat_edge_order_size: env_decimal("STAT_EDGE_ORDER_SIZE", Decimal::new(10, 0)),
            stat_edge_cooldown_seconds: env_f64("STAT_EDGE_COOLDOWN_SECONDS", 10.0),

            use_mock_feeds: env_bool("USE_MOCK_FEEDS", true),

            live_reconcile_interval_seconds: env_f64("LIVE_RECONCILE_INTERVAL_SECONDS", 10.0),

            enable_rest_orderbook_polling: env_bool("ENABLE_REST_ORDERBOOK_POLLING", false),
            rest_orderbook_poll_interval_seconds: env_f64("REST_ORDERBOOK_POLL_INTERVAL_SECONDS", 5.0),
            rest_orderbook_max_markets: env_usize("REST_ORDERBOOK_MAX_MARKETS", 50),
            rest_orderbook_concurrency: env_usize("REST_ORDERBOOK_CONCURRENCY", 5),

            odds_api_key: env_str("ODDS_API_KEY", ""),
            odds_api_poll_interval_seconds: env_f64("ODDS_API_POLL_INTERVAL_SECONDS", 300.0),
            scores_poll_interval_seconds: env_f64("SCORES_POLL_INTERVAL_SECONDS", 15.0),

            log_level: env_str("LOG_LEVEL", "info"),
            log_json: env_bool("LOG_JSON", false),

            health_host: env_str("HEALTH_HOST", "0.0.0.0"),
            health_port: env_u16("HEALTH_PORT", 8080),
        }
    }

    /// Validate configuration for critical requirements.
    pub fn validate(&self) -> Result<(), Vec<String>> {
        let mut errors = Vec::new();

        if self.trading_mode == TradingMode::Live {
            if self.pm_api_key_id.is_empty() {
                errors.push("PM_API_KEY_ID is required for live trading".to_string());
            }
            if self.pm_private_key.is_empty() {
                errors.push("PM_PRIVATE_KEY is required for live trading".to_string());
            }
        }

        if self.kelly_fraction <= Decimal::ZERO || self.kelly_fraction > Decimal::ONE {
            errors.push("RISK_KELLY_FRACTION must be in (0, 1]".to_string());
        }

        if self.min_edge < Decimal::ZERO || self.min_edge >= Decimal::ONE {
            errors.push("RISK_MIN_EDGE must be in [0, 1)".to_string());
        }

        if errors.is_empty() {
            Ok(())
        } else {
            Err(errors)
        }
    }
}

// =============================================================================
// Environment helpers
// =============================================================================

fn env_str(key: &str, default: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| default.to_string())
}

fn env_bool(key: &str, default: bool) -> bool {
    std::env::var(key)
        .map(|v| matches!(v.to_lowercase().as_str(), "true" | "1" | "yes"))
        .unwrap_or(default)
}

fn env_decimal(key: &str, default: Decimal) -> Decimal {
    std::env::var(key)
        .ok()
        .and_then(|v| Decimal::from_str(&v).ok())
        .unwrap_or(default)
}

fn env_f64(key: &str, default: f64) -> f64 {
    std::env::var(key)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

fn env_usize(key: &str, default: usize) -> usize {
    std::env::var(key)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

fn env_u16(key: &str, default: u16) -> u16 {
    std::env::var(key)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}

fn env_csv(key: &str) -> Vec<String> {
    std::env::var(key)
        .ok()
        .map(|v| {
            v.split(',')
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
                .collect()
        })
        .unwrap_or_default()
}

fn env_csv_default(key: &str, default: &str) -> Vec<String> {
    let raw = std::env::var(key).unwrap_or_else(|_| default.to_string());
    raw.split(',')
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .collect()
}
