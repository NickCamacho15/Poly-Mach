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
use execution::paper::PaperExecutor;
use risk::risk_manager::{RiskConfig, RiskManager};
use state::state_manager::{MarketState, StateManager};
use strategies::engine::StrategyEngine;
use strategies::live_arbitrage::{LiveArbitrageConfig, LiveArbitrageStrategy};
use strategies::market_maker::{MarketMakerConfig, MarketMakerStrategy};
use strategies::statistical_edge::{StatisticalEdgeConfig, StatisticalEdgeStrategy};

use chrono::Utc;

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

    // =========================================================================
    // Market discovery
    // =========================================================================
    let market_slugs = if !settings.market_slugs.is_empty() {
        info!(
            count = settings.market_slugs.len(),
            "Using configured market slugs"
        );
        settings.market_slugs.clone()
    } else {
        info!("No MARKET_SLUGS configured, discovering open markets from API...");
        match client.get_markets(Some("OPEN"), None, 100, 0).await {
            Ok(markets) => {
                info!(total_fetched = markets.len(), "Fetched open markets from API");
                let slugs: Vec<String> = markets
                    .iter()
                    .filter(|m| m.status == data::models::MarketStatus::Open)
                    .map(|m| m.slug.clone())
                    .collect();
                info!(tradeable = slugs.len(), "Discovered tradeable markets");

                // Seed state with initial prices from discovery.
                for m in &markets {
                    if m.status != data::models::MarketStatus::Open {
                        continue;
                    }
                    state.update_market(MarketState {
                        market_slug: m.slug.clone(),
                        title: m.title.clone(),
                        yes_bid: m.yes_bid,
                        yes_ask: m.yes_ask,
                        no_bid: m.no_bid,
                        no_ask: m.no_ask,
                        last_updated: Utc::now(),
                    });
                }
                slugs
            }
            Err(e) => {
                error!(error = %e, "Failed to discover markets");
                anyhow::bail!("Cannot start without markets");
            }
        }
    };

    if market_slugs.is_empty() {
        anyhow::bail!("No markets to trade");
    }

    // Log discovered markets.
    for (i, slug) in market_slugs.iter().enumerate().take(10) {
        let ms = state.get_market(slug);
        info!(
            "[{}] Market slug={} yes_bid={:?} yes_ask={:?}",
            i + 1,
            slug,
            ms.as_ref().and_then(|m| m.yes_bid),
            ms.as_ref().and_then(|m| m.yes_ask),
        );
    }
    if market_slugs.len() > 10 {
        info!("  ... and {} more", market_slugs.len() - 10);
    }

    // =========================================================================
    // Probe one order book to verify API connectivity
    // =========================================================================
    if let Some(first_slug) = market_slugs.first() {
        match client.get_market_sides(first_slug).await {
            Ok(book) => {
                orderbook.update(book.clone());
                let top = orderbook.get_top(first_slug);
                info!(
                    slug = %first_slug,
                    yes_bids = book.yes.bids.len(),
                    yes_asks = book.yes.asks.len(),
                    yes_best_bid = ?top.as_ref().and_then(|t| t.yes_best_bid),
                    yes_best_ask = ?top.as_ref().and_then(|t| t.yes_best_ask),
                    no_best_bid = ?top.as_ref().and_then(|t| t.no_best_bid),
                    no_best_ask = ?top.as_ref().and_then(|t| t.no_best_ask),
                    "Order book probe OK"
                );
                // Update state from book.
                if let Some(t) = top {
                    state.update_market(MarketState {
                        market_slug: first_slug.clone(),
                        title: String::new(),
                        yes_bid: t.yes_best_bid,
                        yes_ask: t.yes_best_ask,
                        no_bid: t.no_best_bid,
                        no_ask: t.no_best_ask,
                        last_updated: Utc::now(),
                    });
                }
            }
            Err(e) => warn!(error = %e, slug = %first_slug, "Order book probe failed"),
        }
    }

    // =========================================================================
    // Market feed: background polling of order books
    // =========================================================================
    let feed_state = state.clone();
    let feed_ob = orderbook.clone();
    let feed_slugs = market_slugs.clone();
    let feed_base_url = settings.pm_base_url.clone();
    let feed_auth = PolymarketAuth::new(&settings.pm_api_key_id, &settings.pm_private_key)?;
    let feed_client =
        api::client::PolymarketClient::with_defaults(feed_auth, &feed_base_url)?;
    let poll_interval = Duration::from_secs_f64(settings.rest_orderbook_poll_interval_seconds);
    let max_concurrent = settings.rest_orderbook_concurrency;
    let staleness_threshold = Duration::from_secs(30);

    let feed_shutdown = Arc::new(Notify::new());
    let feed_shutdown_rx = feed_shutdown.clone();

    let feed_handle = tokio::spawn(async move {
        info!(
            poll_interval_ms = poll_interval.as_millis() as u64,
            max_concurrency = max_concurrent,
            staleness_threshold_s = staleness_threshold.as_secs(),
            "MarketFeed starting"
        );
        let mut cycle = 0u64;
        loop {
            tokio::select! {
                _ = feed_shutdown_rx.notified() => {
                    info!("MarketFeed received shutdown signal, stopping");
                    break;
                }
                _ = tokio::time::sleep(poll_interval) => {
                    cycle += 1;
                    // Poll markets in batches.
                    let mut updated = 0usize;
                    for chunk in feed_slugs.chunks(max_concurrent) {
                        let futs: Vec<_> = chunk.iter().map(|slug| {
                            feed_client.get_market_sides(slug)
                        }).collect();
                        let results = futures::future::join_all(futs).await;
                        for (slug, result) in chunk.iter().zip(results) {
                            match result {
                                Ok(book) => {
                                    feed_ob.update(book.clone());
                                    let top = feed_ob.get_top(slug);
                                    if let Some(t) = top {
                                        // Preserve existing title.
                                        let title = feed_state.get_market(slug)
                                            .map(|m| m.title)
                                            .unwrap_or_default();
                                        feed_state.update_market(MarketState {
                                            market_slug: slug.clone(),
                                            title,
                                            yes_bid: t.yes_best_bid,
                                            yes_ask: t.yes_best_ask,
                                            no_bid: t.no_best_bid,
                                            no_ask: t.no_best_ask,
                                            last_updated: Utc::now(),
                                        });
                                        updated += 1;
                                    }
                                }
                                Err(e) => {
                                    warn!(slug = %slug, error = %e, "Order book poll failed");
                                }
                            }
                        }
                    }
                    if cycle % 6 == 0 {
                        info!(cycle, updated, total = feed_slugs.len(), "MarketFeed poll complete");
                    }
                }
            }
        }
        info!(total_cycles = cycle, "MarketFeed stopped");
    });

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
    let mut paper_exec: Option<PaperExecutor> = None;
    let mut live_exec: Option<LiveExecutor> = None;

    match settings.trading_mode {
        TradingMode::Paper => {
            paper_exec = Some(PaperExecutor::new(state.clone()));
        }
        TradingMode::Live => {
            let mut exec = LiveExecutor::new(client, state.clone(), orderbook.clone());
            info!("Syncing initial state from API...");
            if let Err(e) = exec.initialize().await {
                warn!(error = %e, "Initial state sync failed (continuing with defaults)");
            }
            live_exec = Some(exec);
        }
    }

    // =========================================================================
    // Shutdown signal
    // =========================================================================
    let shutdown = Arc::new(Notify::new());
    let shutdown_clone = shutdown.clone();
    tokio::spawn(async move {
        signal::ctrl_c().await.expect("Failed to listen for ctrl+c");
        info!("Shutdown signal received (Ctrl+C)");
        shutdown_clone.notify_waiters();
    });

    // =========================================================================
    // Main trading loop
    // =========================================================================
    let tick_duration = Duration::from_secs_f64(settings.tick_interval_secs);
    let mut tick_count: u64 = 0;

    info!(
        tick_interval_secs = settings.tick_interval_secs,
        trading_mode = ?settings.trading_mode,
        markets = market_slugs.len(),
        "Starting trading loop"
    );

    // Wait for initial market data to arrive.
    info!("Waiting 10s for initial market data...");
    tokio::time::sleep(Duration::from_secs(10)).await;

    loop {
        tokio::select! {
            _ = shutdown.notified() => {
                info!("Shutting down trading loop...");
                break;
            }
            _ = tokio::time::sleep(tick_duration) => {
                tick_count += 1;

                // =============================================================
                // 1. Run market maker on each market with fresh data
                // =============================================================
                let markets = state.get_all_markets();
                for market in &markets {
                    if !market.has_valid_prices() {
                        continue;
                    }
                    let output = engine.on_market_update(market, &mut risk_manager);
                    for signal in &output.approved_signals {
                        execute_signal_dispatch(
                            signal,
                            &mut paper_exec,
                            &mut live_exec,
                        ).await;
                    }
                }

                // =============================================================
                // 2. Run tick-based strategies (live arb, stat edge)
                // =============================================================
                let tick_output = engine.on_tick(&mut risk_manager);
                for signal in &tick_output.approved_signals {
                    execute_signal_dispatch(
                        signal,
                        &mut paper_exec,
                        &mut live_exec,
                    ).await;
                }

                // =============================================================
                // 3. Paper fill simulation
                // =============================================================
                if let Some(ref mut pe) = paper_exec {
                    pe.check_fills();
                }

                // =============================================================
                // 4. Periodic reconciliation (live mode only)
                // =============================================================
                if settings.trading_mode == TradingMode::Live && tick_count % 10 == 0 {
                    if let Some(ref mut le) = live_exec {
                        if let Err(e) = le.reconcile_state().await {
                            warn!(error = %e, "Reconciliation failed");
                        }
                    }
                }

                // =============================================================
                // 5. Periodic performance logging
                // =============================================================
                if tick_count % 30 == 0 {
                    let perf = get_performance(&paper_exec, &live_exec);
                    let active_arbs = orderbook.scan_completeness_arb(
                        rust_decimal::Decimal::new(1, 3) // 0.001 min margin
                    ).len();
                    info!(
                        tick = tick_count,
                        mode = ?settings.trading_mode,
                        markets_tracked = markets.len(),
                        active_arbs,
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
        }
    }

    // =========================================================================
    // Graceful shutdown
    // =========================================================================
    info!("Shutting down...");
    feed_shutdown.notify_waiters();
    let _ = feed_handle.await;
    info!("Market feed stopped");

    // Print final report.
    let perf = get_performance(&paper_exec, &live_exec);
    info!("========================================");
    info!("         FINAL PERFORMANCE REPORT        ");
    info!("========================================");
    for (k, v) in &perf {
        info!("   {}: {}", k, v);
    }
    info!("========================================");
    info!("Bot shutdown complete.");

    Ok(())
}

/// Dispatch a signal to whichever executor is active.
async fn execute_signal_dispatch(
    signal: &data::models::Signal,
    paper_exec: &mut Option<PaperExecutor>,
    live_exec: &mut Option<LiveExecutor>,
) {
    if let Some(ref mut pe) = paper_exec {
        let result = pe.execute_signal(signal);
        if let Some(ref err) = result.error {
            warn!(
                market_slug = %signal.market_slug,
                error = %err,
                "Paper execution failed"
            );
        }
    } else if let Some(ref mut le) = live_exec {
        let result = le.execute_signal(signal).await;
        if let Some(ref err) = result.error {
            warn!(
                market_slug = %signal.market_slug,
                error = %err,
                "Execution failed"
            );
        }
    }
}

/// Get performance from whichever executor is active.
fn get_performance(
    paper_exec: &Option<PaperExecutor>,
    live_exec: &Option<LiveExecutor>,
) -> std::collections::HashMap<String, serde_json::Value> {
    if let Some(ref pe) = paper_exec {
        pe.get_performance()
    } else if let Some(ref le) = live_exec {
        le.get_performance()
    } else {
        std::collections::HashMap::new()
    }
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
