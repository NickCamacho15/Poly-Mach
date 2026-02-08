//! Polymarket US High-Performance Trading Bot
//!
//! A Rust-based arbitrage and market-making bot for Polymarket US
//! (CFTC-regulated DCM operated by QCX LLC).
//!
//! Architecture:
//! - Tokio async runtime for concurrent I/O
//! - Ed25519 authenticated REST API client
//! - REST-polled order book tracking via MarketFeed
//! - Three strategy engines: market maker, live arbitrage, statistical edge
//! - Paper and live execution modes
//! - Kelly Criterion position sizing with exposure limits and circuit breakers

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
use chrono::Utc;

use auth::PolymarketAuth;
use config::{Settings, TradingMode};
use data::market_feed::{MarketFeed, MarketFeedConfig};
use data::orderbook::OrderBookTracker;
use execution::paper_executor::PaperExecutor;
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

    // Initialize API client (shared via Arc for MarketFeed).
    let client = api::client::PolymarketClient::with_defaults(auth, &settings.pm_base_url)?;
    let client = Arc::new(client);

    // Initialize state manager.
    let state = StateManager::new(settings.initial_balance);

    // Initialize order book tracker.
    let orderbook = OrderBookTracker::new();

    // =========================================================================
    // Market discovery
    // =========================================================================
    let market_slugs = discover_markets(&client, &settings).await;
    if market_slugs.is_empty() {
        error!("No tradeable markets found — exiting");
        anyhow::bail!("No tradeable markets");
    }
    info!(
        tradeable = market_slugs.len(),
        "Discovered tradeable markets"
    );

    // Log first 10 markets.
    for (i, slug) in market_slugs.iter().enumerate().take(10) {
        info!("  [{}] Market slug={}", i + 1, slug);
    }
    if market_slugs.len() > 10 {
        info!("  ... and {} more", market_slugs.len() - 10);
    }

    // Probe the first market's order book to verify connectivity.
    if let Some(first_slug) = market_slugs.first() {
        match client.get_market_sides(first_slug).await {
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
            Err(e) => {
                warn!(error = %e, slug = %first_slug, "Order book probe failed");
            }
        }
    }

    // =========================================================================
    // Start MarketFeed (background order book polling)
    // =========================================================================
    let feed_config = MarketFeedConfig {
        poll_interval_ms: (settings.rest_orderbook_poll_interval_seconds * 1000.0) as u64,
        max_concurrency: settings.rest_orderbook_concurrency,
        staleness_threshold_secs: 30,
    };
    let feed = MarketFeed::new(feed_config, market_slugs.clone());
    let mut market_rx = feed.start(client.clone(), orderbook.clone(), state.clone());

    // =========================================================================
    // Initialize executor
    // =========================================================================
    let is_paper = settings.trading_mode == TradingMode::Paper;
    let mut paper_executor = if is_paper {
        info!("Paper executor initialized (NO real orders will be placed)");
        Some(PaperExecutor::new(state.clone(), orderbook.clone()))
    } else {
        None
    };

    // For live mode, use the LiveExecutor (requires owned client).
    // Currently paper mode is the primary path; live mode would need
    // a separate client instance or Arc-based LiveExecutor refactor.
    if !is_paper {
        warn!("Live mode not yet fully supported in this version — use Paper mode");
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
    let mode_str = if is_paper { "Paper" } else { "Live" };
    info!(
        tick_interval_secs = settings.tick_interval_secs,
        trading_mode = mode_str,
        markets = market_slugs.len(),
        "Starting trading loop"
    );

    let tick_duration = Duration::from_secs_f64(settings.tick_interval_secs);
    let mut tick_count: u64 = 0;

    // Wait for initial market data to populate order books.
    info!("Waiting 10s for initial market data...");
    tokio::time::sleep(Duration::from_secs(10)).await;
    info!("Initial data period complete — starting trading");

    loop {
        tokio::select! {
            _ = shutdown.notified() => {
                info!("Shutting down trading loop...");
                break;
            }
            // Process market data updates → drive market maker strategy.
            Some(update) = market_rx.recv() => {
                let output = engine.on_market_update(&update.market, &mut risk_manager);

                for signal in &output.approved_signals {
                    if let Some(ref mut pe) = paper_executor {
                        pe.execute_signal(signal);
                    }
                }
            }
            // Periodic tick for time-based strategies.
            _ = tokio::time::sleep(tick_duration) => {
                tick_count += 1;

                // Run time-based strategies (arb, stat edge).
                let output = engine.on_tick(&mut risk_manager);

                for signal in &output.approved_signals {
                    if let Some(ref mut pe) = paper_executor {
                        pe.execute_signal(signal);
                    }
                }

                // Periodic performance logging.
                if tick_count % 30 == 0 {
                    if let Some(ref pe) = paper_executor {
                        let perf = pe.get_performance();
                        info!(
                            tick = tick_count,
                            mode = mode_str,
                            markets_tracked = market_slugs.len(),
                            active_arbs = 0,
                            equity = ?perf.get("total_equity"),
                            pnl = ?perf.get("total_pnl"),
                            trades = ?perf.get("total_trades"),
                            win_rate = ?perf.get("win_rate"),
                            positions = ?perf.get("open_positions"),
                            fees_paid = ?perf.get("fees_paid"),
                            max_drawdown = ?perf.get("max_drawdown"),
                            "Performance update"
                        );
                    }
                }
            }
        }
    }

    // Graceful shutdown.
    info!("Shutting down...");
    if let Some(ref pe) = paper_executor {
        let perf = pe.get_performance();
        info!(performance = ?perf, "Final performance report");
    }
    info!("Bot shutdown complete.");

    Ok(())
}

/// Parse a trailing YYYY-MM-DD date from a market slug.
/// Returns None if the slug doesn't end with a date.
fn parse_slug_date(slug: &str) -> Option<chrono::NaiveDate> {
    // Slug format: aec-nba-lal-bos-2026-01-27
    // Date is the last 10 characters: YYYY-MM-DD
    let parts: Vec<&str> = slug.split('-').collect();
    if parts.len() >= 3 {
        // Try the last 3 parts as YYYY-MM-DD
        let n = parts.len();
        if let (Ok(y), Ok(m), Ok(d)) = (
            parts[n - 3].parse::<i32>(),
            parts[n - 2].parse::<u32>(),
            parts[n - 1].parse::<u32>(),
        ) {
            if (2020..=2030).contains(&y) && (1..=12).contains(&m) && (1..=31).contains(&d) {
                return chrono::NaiveDate::from_ymd_opt(y, m, d);
            }
        }
    }
    None
}

/// Check if a market slug is for a current/future event (not past).
fn is_tradeable_slug(slug: &str) -> bool {
    match parse_slug_date(slug) {
        Some(slug_date) => {
            let today = Utc::now().date_naive();
            slug_date >= today
        }
        // No date in slug → allow (non-sports or unknown format)
        None => true,
    }
}

/// Check if a slug matches configured leagues.
fn slug_matches_league(slug: &str, leagues: &[String]) -> bool {
    if leagues.is_empty() {
        return true;
    }
    let parts: Vec<&str> = slug.split('-').collect();
    if parts.len() >= 2 {
        let slug_league = parts[1].to_lowercase();
        leagues.iter().any(|l| l.to_lowercase() == slug_league)
    } else {
        false
    }
}

/// Discover tradeable markets from the API.
async fn discover_markets(
    client: &api::client::PolymarketClient,
    settings: &Settings,
) -> Vec<String> {
    // If explicit slugs are configured, use those.
    if !settings.market_slugs.is_empty() {
        info!(
            count = settings.market_slugs.len(),
            "Using configured MARKET_SLUGS"
        );
        return settings.market_slugs.clone();
    }

    info!("No MARKET_SLUGS configured, discovering open markets from API...");

    let max_markets = settings.rest_orderbook_max_markets;
    let mut candidate_slugs = Vec::new();
    let mut offset = 0u32;
    let limit = 100u32; // Fetch more per page since we'll filter aggressively
    let mut total_fetched = 0u32;
    let mut filtered_date = 0u32;
    let mut filtered_closed = 0u32;
    let mut filtered_type = 0u32;
    let mut filtered_league = 0u32;

    // Paginate through open markets.
    loop {
        if candidate_slugs.len() >= max_markets {
            info!(
                max_markets = max_markets,
                "Reached max market limit — stopping discovery"
            );
            break;
        }
        match client.get_markets(None, None, limit, offset).await {
            Ok(markets) => {
                if markets.is_empty() {
                    info!("No more markets returned — end of list");
                    break;
                }
                let batch_count = markets.len();
                total_fetched += batch_count as u32;

                for m in &markets {
                    if candidate_slugs.len() >= max_markets {
                        break;
                    }

                    // Filter: skip closed markets.
                    if m.closed {
                        filtered_closed += 1;
                        continue;
                    }

                    // Filter: market type prefix (aec, asc, tsc, etc.).
                    let type_match = settings.market_types.is_empty()
                        || settings.market_types.iter().any(|t| m.slug.starts_with(t));
                    if !type_match {
                        filtered_type += 1;
                        continue;
                    }

                    // Filter: league from slug (nba, cbb, nfl, etc.).
                    if !slug_matches_league(&m.slug, &settings.leagues) {
                        filtered_league += 1;
                        continue;
                    }

                    // Filter: slug date — only current/future events.
                    if !is_tradeable_slug(&m.slug) {
                        filtered_date += 1;
                        continue;
                    }

                    candidate_slugs.push(m.slug.clone());
                }

                info!(
                    candidates = candidate_slugs.len(),
                    batch = batch_count,
                    total_fetched = total_fetched,
                    filtered_date = filtered_date,
                    filtered_closed = filtered_closed,
                    filtered_type = filtered_type,
                    filtered_league = filtered_league,
                    "Market discovery batch"
                );

                if batch_count < limit as usize {
                    break; // Last page.
                }
                offset += limit;
            }
            Err(e) => {
                warn!(error = %e, "Failed to fetch markets");
                break;
            }
        }
    }

    info!(
        candidates = candidate_slugs.len(),
        total_fetched = total_fetched,
        filtered_date = filtered_date,
        filtered_closed = filtered_closed,
        filtered_type = filtered_type,
        filtered_league = filtered_league,
        "Discovery filtering complete"
    );

    if candidate_slugs.is_empty() {
        warn!("No candidate markets after filtering — check LEAGUES and MARKET_TYPES settings");
        return candidate_slugs;
    }

    // Log candidate slugs for diagnostics.
    for (i, slug) in candidate_slugs.iter().enumerate().take(20) {
        info!("  candidate[{}] {}", i + 1, slug);
    }
    if candidate_slugs.len() > 20 {
        info!("  ... and {} more candidates", candidate_slugs.len() - 20);
    }

    // Validate: probe a sample of order books to check API connectivity.
    // Accept all candidates that the API says are active — order books may be
    // empty now but will fill as game time approaches. The MarketFeed will
    // keep polling and pick up data when it arrives.
    let mut valid_slugs = Vec::new();
    let mut with_prices = 0u32;
    let mut empty_books = 0u32;
    let mut probe_err = 0u32;

    // Probe first few to verify the endpoint works, then accept the rest.
    let probe_count = candidate_slugs.len().min(5);
    for (i, slug) in candidate_slugs.iter().enumerate() {
        if i < probe_count {
            match client.get_market_sides(slug).await {
                Ok(book) => {
                    let has_prices = book.yes.best_bid().is_some() || book.yes.best_ask().is_some()
                        || book.no.best_bid().is_some() || book.no.best_ask().is_some();
                    if has_prices {
                        with_prices += 1;
                    } else {
                        empty_books += 1;
                    }
                    valid_slugs.push(slug.clone());
                }
                Err(e) => {
                    let err_str = e.to_string();
                    if err_str.contains("404") || err_str.contains("Not Found") {
                        // 404 means market isn't tradeable yet — skip it.
                        info!(slug = %slug, "Skipping: 404 on order book");
                    } else {
                        probe_err += 1;
                        warn!(slug = %slug, error = %e, "Order book probe error");
                        // Still accept — might be a transient error.
                        valid_slugs.push(slug.clone());
                    }
                }
            }
        } else {
            // Accept remaining candidates without probing (API already
            // confirmed they're active=true, closed=false).
            valid_slugs.push(slug.clone());
        }

        if valid_slugs.len() >= max_markets {
            break;
        }
    }

    info!(
        accepted = valid_slugs.len(),
        probed = probe_count,
        with_prices = with_prices,
        empty_books = empty_books,
        probe_errors = probe_err,
        "Market validation complete (empty books are normal for upcoming games)"
    );

    valid_slugs
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
