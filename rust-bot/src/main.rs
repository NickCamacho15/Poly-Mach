//! Polymarket US High-Performance Trading Bot
//!
//! A Rust-based arbitrage and market-making bot for Polymarket US
//! (CFTC-regulated DCM operated by QCX LLC).
//!
//! Architecture:
//! - Tokio async runtime for concurrent I/O
//! - Ed25519 authenticated REST API client
//! - Real-time order book tracking
//! - Three strategy engines: market maker, live arbitrage, statistical edge
//! - Kelly Criterion position sizing with exposure limits and circuit breakers
//! - Sub-100ms signal-to-order latency target

mod api;
mod auth;
mod config;
mod data;
mod execution;
mod risk;
mod state;
mod strategies;

use std::sync::Arc;
use std::time::Duration;
use tokio::signal;
use tokio::sync::Notify;
use tracing::{error, info, warn};

use auth::PolymarketAuth;
use config::{Settings, TradingMode};
use data::orderbook::OrderBookTracker;
use execution::executor::LiveExecutor;
use risk::risk_manager::{RiskConfig, RiskManager};
use state::state_manager::StateManager;
use strategies::engine::StrategyEngine;
use strategies::live_arbitrage::{LiveArbitrageConfig, LiveArbitrageStrategy};
use strategies::market_maker::{MarketMakerConfig, MarketMakerStrategy};
use strategies::statistical_edge::{StatisticalEdgeConfig, StatisticalEdgeStrategy};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // Load configuration.
    let settings = Settings::from_env();

    // Initialize logging.
    init_logging(&settings);

    info!("=== Polymarket US Trading Bot (Rust) ===");
    info!(
        trading_mode = ?settings.trading_mode,
        base_url = %settings.pm_base_url,
        "Configuration loaded"
    );

    // Validate settings.
    if let Err(errors) = settings.validate() {
        for e in &errors {
            error!(error = %e, "Configuration error");
        }
        anyhow::bail!("Configuration validation failed");
    }

    // Initialize auth.
    let auth = PolymarketAuth::new(&settings.pm_api_key_id, &settings.pm_private_key)?;
    info!(public_key = %auth.public_key_base64(), "Authentication initialized");

    // Initialize API client.
    let client = api::client::PolymarketClient::with_defaults(auth, &settings.pm_base_url)?;

    // Initialize state manager.
    let state = StateManager::new(settings.initial_balance);

    // Initialize order book tracker.
    let orderbook = OrderBookTracker::new();

    // Initialize executor.
    let mut executor = LiveExecutor::new(client, state.clone(), orderbook.clone());

    // Initialize executor (sync state from API).
    if settings.trading_mode == TradingMode::Live {
        info!("Syncing initial state from API...");
        if let Err(e) = executor.initialize().await {
            warn!(error = %e, "Initial state sync failed (continuing with defaults)");
        }
    }

    // Initialize risk manager.
    let risk_config = RiskConfig {
        kelly_fraction: settings.kelly_fraction,
        min_edge: settings.min_edge,
        max_position_per_market: settings.max_position_per_market,
        max_portfolio_exposure: settings.max_portfolio_exposure,
        max_portfolio_exposure_pct: settings.max_portfolio_exposure_pct,
        max_correlated_exposure: settings.max_correlated_exposure,
        max_positions: settings.max_positions,
        max_daily_loss: settings.max_daily_loss,
        max_drawdown_pct: settings.max_drawdown_pct,
        max_total_pnl_drawdown_pct_for_new_buys: settings
            .max_total_pnl_drawdown_pct_for_new_buys,
        min_trade_size: settings.min_trade_size,
    };
    let mut risk_manager = RiskManager::new(risk_config, state.clone());

    // Initialize strategies.
    let market_maker = if settings.enable_market_maker {
        info!("Market maker strategy ENABLED");
        Some(MarketMakerStrategy::new(MarketMakerConfig {
            spread: settings.market_maker_spread,
            order_size: settings.market_maker_order_size,
            ..MarketMakerConfig::default()
        }))
    } else {
        None
    };

    let live_arb = if settings.enable_live_arbitrage {
        info!("Live arbitrage strategy ENABLED");
        Some(LiveArbitrageStrategy::new(LiveArbitrageConfig {
            min_edge: settings.live_arb_min_edge,
            order_size: settings.live_arb_order_size,
            cooldown_seconds: settings.live_arb_cooldown_seconds,
            ..LiveArbitrageConfig::default()
        }))
    } else {
        None
    };

    let stat_edge = if settings.enable_statistical_edge {
        info!("Statistical edge strategy ENABLED");
        Some(StatisticalEdgeStrategy::new(StatisticalEdgeConfig {
            min_edge: settings.stat_edge_min_edge,
            order_size: settings.stat_edge_order_size,
            cooldown_seconds: settings.stat_edge_cooldown_seconds,
            ..StatisticalEdgeConfig::default()
        }))
    } else {
        None
    };

    let mut engine = StrategyEngine::new(state.clone(), market_maker, live_arb, stat_edge);

    // Shutdown signal.
    let shutdown = Arc::new(Notify::new());
    let shutdown_clone = shutdown.clone();
    tokio::spawn(async move {
        signal::ctrl_c().await.expect("Failed to listen for ctrl+c");
        info!("Shutdown signal received");
        shutdown_clone.notify_waiters();
    });

    // Main trading loop.
    info!(
        tick_interval_secs = settings.tick_interval_secs,
        "Starting trading loop"
    );

    let tick_duration = Duration::from_secs_f64(settings.tick_interval_secs);
    let mut tick_count: u64 = 0;

    loop {
        tokio::select! {
            _ = shutdown.notified() => {
                info!("Shutting down trading loop...");
                break;
            }
            _ = tokio::time::sleep(tick_duration) => {
                tick_count += 1;

                // Periodic reconciliation.
                if tick_count % 10 == 0 {
                    if let Err(e) = executor.reconcile_state().await {
                        warn!(error = %e, "Reconciliation failed");
                    }
                }

                // Run strategies.
                let output = engine.on_tick(&mut risk_manager);

                // Execute approved signals.
                for signal in &output.approved_signals {
                    let result = executor.execute_signal(signal).await;
                    if let Some(ref err) = result.error {
                        warn!(
                            market_slug = %signal.market_slug,
                            error = %err,
                            "Execution failed"
                        );
                    }
                }

                // Periodic performance logging.
                if tick_count % 60 == 0 {
                    let perf = executor.get_performance();
                    info!(
                        tick = tick_count,
                        equity = ?perf.get("total_equity"),
                        pnl = ?perf.get("total_pnl"),
                        trades = ?perf.get("total_trades"),
                        positions = ?perf.get("open_positions"),
                        "Performance update"
                    );
                }
            }
        }
    }

    // Graceful shutdown.
    info!("Cancelling all open orders...");
    // Cancel all orders across all tracked markets.
    let markets = state.get_all_markets();
    for market in &markets {
        let _ = executor
            .execute_signal(&data::models::Signal {
                market_slug: market.market_slug.clone(),
                action: data::models::SignalAction::CancelAll,
                price: rust_decimal::Decimal::ZERO,
                quantity: 0,
                urgency: data::models::Urgency::Critical,
                confidence: 1.0,
                strategy_name: "shutdown".to_string(),
                reason: "Graceful shutdown".to_string(),
                metadata: std::collections::HashMap::new(),
                timestamp: chrono::Utc::now(),
            })
            .await;
    }

    let perf = executor.get_performance();
    info!(performance = ?perf, "Final performance report");
    info!("Bot shutdown complete.");

    Ok(())
}

fn init_logging(settings: &Settings) {
    use tracing_subscriber::EnvFilter;

    let filter = EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| EnvFilter::new(&settings.log_level));

    if settings.log_json {
        tracing_subscriber::fmt()
            .json()
            .with_env_filter(filter)
            .with_target(true)
            .with_thread_ids(true)
            .init();
    } else {
        tracing_subscriber::fmt()
            .with_env_filter(filter)
            .with_target(false)
            .compact()
            .init();
    }
}
