//! Polymarket US High-Performance Trading Bot
//!
//! A Rust-based arbitrage and market-making bot for Polymarket US
//! (CFTC-regulated DCM operated by QCX LLC).
//!
//! Architecture:
//! - Tokio async runtime for concurrent I/O
//! - Ed25519 authenticated REST API client
//! - Real-time order book tracking via REST polling
//! - Three strategy engines: market maker, live arbitrage, statistical edge
//! - Kelly Criterion position sizing with exposure limits and circuit breakers
//! - Paper trading mode for strategy validation before live deployment

mod api;
mod auth;
mod config;
mod data;
mod execution;
mod risk;
mod state;
mod strategies;

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tokio::signal;
use tokio::sync::Notify;
use tracing::{error, info, warn};

use auth::PolymarketAuth;
use config::{Settings, TradingMode};
use data::market_feed::{MarketFeed, MarketFeedConfig};
use data::orderbook::OrderBookTracker;
use execution::executor::LiveExecutor;
use execution::paper::PaperExecutor;
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

    // Initialize API client (shared across feed + executor).
    let client = Arc::new(
        api::client::PolymarketClient::with_defaults(auth, &settings.pm_base_url)?,
    );

    // Initialize state manager.
    let state = StateManager::new(settings.initial_balance);

    // Initialize order book tracker.
    let orderbook = OrderBookTracker::new();

    // Shutdown signal using AtomicBool (reliable across select! loops).
    let shutdown_flag = Arc::new(AtomicBool::new(false));
    let shutdown_notify = Arc::new(Notify::new());
    {
        let flag = shutdown_flag.clone();
        let notify = shutdown_notify.clone();
        tokio::spawn(async move {
            signal::ctrl_c().await.expect("Failed to listen for ctrl+c");
            info!("Shutdown signal received (Ctrl+C)");
            flag.store(true, Ordering::SeqCst);
            notify.notify_waiters();
        });
    }

    // =========================================================================
    // Discover markets to trade
    // =========================================================================
    let market_slugs = if settings.market_slugs.is_empty() {
        info!("No MARKET_SLUGS configured, discovering open markets from API...");
        // Pass closed=false to only get open (non-closed) markets,
        // matching the Python bot's discover_markets() approach.
        let mut all_markets = Vec::new();
        for offset in (0..500).step_by(100) {
            match client.get_markets(None, None, 100, offset, Some("false")).await {
                Ok(batch) => {
                    if batch.is_empty() {
                        break;
                    }
                    all_markets.extend(batch);
                }
                Err(e) => {
                    warn!(error = %e, offset, "Market fetch failed at offset");
                    break;
                }
            }
        }
        info!(total_fetched = all_markets.len(), "Fetched open markets from API");

        // Additional client-side filter: active, future game date.
        let today = chrono::Utc::now().format("%Y-%m-%d").to_string();
        let tradeable: Vec<&data::models::Market> = all_markets
            .iter()
            .filter(|m| m.active)
            .filter(|m| {
                // Extract date from slug: aec-nfl-lac-ten-YYYY-MM-DD
                let parts: Vec<&str> = m.slug.split('-').collect();
                if parts.len() >= 7 {
                    let date_str = format!("{}-{}-{}", parts[4], parts[5], parts[6]);
                    date_str >= today
                } else {
                    // Keep markets with unknown date format.
                    true
                }
            })
            .collect();

        let slugs: Vec<String> = tradeable.iter().map(|m| m.slug.clone()).collect();
        info!(
            total_fetched = all_markets.len(),
            tradeable = tradeable.len(),
            "Discovered tradeable markets"
        );
        for (i, m) in tradeable.iter().enumerate().take(10) {
            info!(
                slug = %m.slug,
                title = %m.title,
                yes_bid = ?m.yes_bid,
                yes_ask = ?m.yes_ask,
                no_bid = ?m.no_bid,
                no_ask = ?m.no_ask,
                "  [{}] Market", i + 1
            );
        }
        if slugs.len() > 10 {
            info!("  ... and {} more", slugs.len() - 10);
        }

        // Verify book endpoint parses correctly.
        if let Some(first_slug) = slugs.first() {
            match client.get_market_book(first_slug).await {
                Ok(book) => {
                    info!(
                        slug = %first_slug,
                        yes_bids = book.yes.bids.len(),
                        yes_asks = book.yes.asks.len(),
                        yes_best_bid = ?book.yes.best_bid(),
                        yes_best_ask = ?book.yes.best_ask(),
                        no_best_bid = ?book.no.best_bid(),
                        no_best_ask = ?book.no.best_ask(),
                        "Order book probe OK"
                    );
                }
                Err(e) => warn!(slug = %first_slug, error = %e, "Order book probe failed"),
            }
        }

        slugs
    } else {
        info!(count = settings.market_slugs.len(), "Using configured MARKET_SLUGS");
        settings.market_slugs.clone()
    };

    // Seed state manager with discovered markets so the feed knows what to poll.
    for slug in &market_slugs {
        state.update_market(state::state_manager::MarketState {
            market_slug: slug.clone(),
            title: slug.clone(),
            yes_bid: None,
            yes_ask: None,
            no_bid: None,
            no_ask: None,
            last_updated: chrono::Utc::now(),
        });
    }

    // =========================================================================
    // Start market data feed (background task)
    // =========================================================================
    let feed_config = MarketFeedConfig {
        poll_interval: Duration::from_secs_f64(
            settings.rest_orderbook_poll_interval_seconds,
        ),
        max_concurrency: settings.rest_orderbook_concurrency,
        staleness_threshold: Duration::from_secs(30),
    };
    let feed = MarketFeed::new(
        client.clone(),
        orderbook.clone(),
        state.clone(),
        feed_config,
        shutdown_notify.clone(),
    );
    let feed_handle = feed.spawn();
    info!("Market data feed started");

    // =========================================================================
    // Initialize risk manager
    // =========================================================================
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

    // =========================================================================
    // Initialize strategies
    // =========================================================================
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

    // =========================================================================
    // Initialize executor (paper or live)
    // =========================================================================
    // We use an enum-like approach to avoid trait objects.
    let mut paper_executor = if settings.trading_mode == TradingMode::Paper {
        info!("Paper executor initialized (NO real orders will be placed)");
        Some(PaperExecutor::new(state.clone(), orderbook.clone()))
    } else {
        None
    };

    let mut live_executor = if settings.trading_mode == TradingMode::Live {
        // Live executor needs its own client instance (feed holds the Arc'd one).
        let live_auth = PolymarketAuth::new(&settings.pm_api_key_id, &settings.pm_private_key)?;
        let live_client = api::client::PolymarketClient::with_defaults(
            live_auth,
            &settings.pm_base_url,
        )?;
        let mut exec = LiveExecutor::new(live_client, state.clone(), orderbook.clone());
        info!("Syncing initial state from API...");
        if let Err(e) = exec.initialize().await {
            warn!(error = %e, "Initial state sync failed (continuing with defaults)");
        }
        Some(exec)
    } else {
        None
    };

    // =========================================================================
    // Main trading loop
    // =========================================================================
    info!(
        tick_interval_secs = settings.tick_interval_secs,
        trading_mode = ?settings.trading_mode,
        markets = market_slugs.len(),
        "Starting trading loop"
    );

    let tick_duration = Duration::from_secs_f64(settings.tick_interval_secs);
    let mut tick_count: u64 = 0;

    // Let market feed run a few cycles before we start trading.
    info!("Waiting 10s for initial market data...");
    tokio::time::sleep(Duration::from_secs(10)).await;

    loop {
        if shutdown_flag.load(Ordering::SeqCst) {
            info!("Shutdown flag detected, exiting trading loop");
            break;
        }

        tokio::time::sleep(tick_duration).await;
        tick_count += 1;

        // Check shutdown again after sleep.
        if shutdown_flag.load(Ordering::SeqCst) {
            break;
        }

        // Check resting orders in paper mode.
        if let Some(ref mut paper) = paper_executor {
            let fills = paper.check_resting_orders();
            for fill in &fills {
                info!(
                    order_id = %fill.order_id,
                    market = %fill.market_slug,
                    side = %fill.side,
                    price = %fill.price,
                    qty = fill.quantity,
                    fee = %fill.fee,
                    "[PAPER] Resting order filled"
                );
            }
        }

        // Scan for completeness arbitrage opportunities.
        let arb_signals = orderbook.scan_completeness_arb(settings.min_edge);
        if !arb_signals.is_empty() {
            for arb in &arb_signals {
                info!(
                    market = %arb.market_slug,
                    yes_ask = %arb.yes_ask,
                    no_ask = %arb.no_ask,
                    combined = %arb.combined_cost,
                    net_margin = %arb.net_margin,
                    "Completeness ARB detected"
                );
            }
        }

        // Run strategy engine.
        let output = engine.on_tick(&mut risk_manager);

        // Execute approved signals.
        for signal in &output.approved_signals {
            if let Some(ref mut paper) = paper_executor {
                let result = paper.execute_signal(signal);
                if let Some(ref err) = result.error {
                    warn!(
                        market_slug = %signal.market_slug,
                        error = %err,
                        "[PAPER] Execution failed"
                    );
                }
            } else if let Some(ref mut live) = live_executor {
                let result = live.execute_signal(signal).await;
                if let Some(ref err) = result.error {
                    warn!(
                        market_slug = %signal.market_slug,
                        error = %err,
                        "Execution failed"
                    );
                }
            }
        }

        // Live executor periodic reconciliation.
        if let Some(ref mut live) = live_executor {
            if tick_count % 10 == 0 {
                if let Err(e) = live.reconcile_state().await {
                    warn!(error = %e, "Reconciliation failed");
                }
            }
        }

        // Periodic performance logging.
        if tick_count % 30 == 0 {
            let perf = if let Some(ref paper) = paper_executor {
                paper.get_performance()
            } else if let Some(ref live) = live_executor {
                live.get_performance()
            } else {
                std::collections::HashMap::new()
            };

            let markets_with_data = state.get_all_markets().len();
            let arb_count = orderbook.scan_completeness_arb(settings.min_edge).len();

            info!(
                tick = tick_count,
                mode = ?settings.trading_mode,
                markets_tracked = markets_with_data,
                active_arbs = arb_count,
                equity = ?perf.get("total_equity"),
                pnl = ?perf.get("total_pnl"),
                trades = ?perf.get("total_trades"),
                win_rate = ?perf.get("win_rate"),
                positions = ?perf.get("open_positions"),
                fees_paid = ?perf.get("total_fees_paid"),
                max_drawdown = ?perf.get("max_drawdown"),
                "Performance update"
            );
        }
    }

    // =========================================================================
    // Graceful shutdown
    // =========================================================================
    info!("Shutting down...");

    // Signal feed to stop.
    shutdown_notify.notify_waiters();

    // Wait for feed to finish (with timeout).
    tokio::select! {
        _ = feed_handle => {
            info!("Market feed stopped");
        }
        _ = tokio::time::sleep(Duration::from_secs(5)) => {
            warn!("Market feed didn't stop within 5s, proceeding with shutdown");
        }
    }

    // Cancel open orders in live mode.
    if let Some(ref mut live) = live_executor {
        info!("Cancelling all open orders...");
        let markets = state.get_all_markets();
        for market in &markets {
            let _ = live
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
    }

    // Final performance report.
    let perf = if let Some(ref paper) = paper_executor {
        paper.get_performance()
    } else if let Some(ref live) = live_executor {
        live.get_performance()
    } else {
        std::collections::HashMap::new()
    };
    info!("========================================");
    info!("        FINAL PERFORMANCE REPORT        ");
    info!("========================================");
    for (k, v) in &perf {
        info!("  {}: {}", k, v);
    }
    info!("========================================");
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
