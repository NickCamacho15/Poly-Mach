//! Market data ingestion loop.
//!
//! Periodically polls the Polymarket US REST API for order book data
//! and feeds it into the `OrderBookTracker` and `StateManager`.
//! Runs as a background tokio task that can be cancelled via a shutdown
//! signal (`tokio::sync::Notify`).

use std::sync::Arc;
use std::time::Duration;
use tokio::sync::Notify;
use tracing::{info, warn, debug};
use chrono::Utc;

use crate::api::client::PolymarketClient;
use crate::data::orderbook::OrderBookTracker;
use crate::state::state_manager::{StateManager, MarketState};

// =============================================================================
// Configuration
// =============================================================================

/// Configuration for the market data feed.
#[derive(Debug, Clone)]
pub struct MarketFeedConfig {
    /// How often to poll each market's order book (seconds).
    pub poll_interval: Duration,

    /// Maximum number of markets polled concurrently.
    pub max_concurrency: usize,

    /// Warn if a market hasn't been updated for this long.
    pub staleness_threshold: Duration,
}

impl Default for MarketFeedConfig {
    fn default() -> Self {
        Self {
            poll_interval: Duration::from_secs(5),
            max_concurrency: 5,
            staleness_threshold: Duration::from_secs(30),
        }
    }
}

// =============================================================================
// Market Feed
// =============================================================================

/// Continuously polls order book data for all tracked markets and pushes
/// updates into `OrderBookTracker` and `StateManager`.
pub struct MarketFeed {
    client: Arc<PolymarketClient>,
    orderbook: OrderBookTracker,
    state: StateManager,
    config: MarketFeedConfig,
    shutdown: Arc<Notify>,
}

impl MarketFeed {
    pub fn new(
        client: Arc<PolymarketClient>,
        orderbook: OrderBookTracker,
        state: StateManager,
        config: MarketFeedConfig,
        shutdown: Arc<Notify>,
    ) -> Self {
        Self {
            client,
            orderbook,
            state,
            config,
            shutdown,
        }
    }

    /// Create with default configuration.
    pub fn with_defaults(
        client: Arc<PolymarketClient>,
        orderbook: OrderBookTracker,
        state: StateManager,
        shutdown: Arc<Notify>,
    ) -> Self {
        Self::new(client, orderbook, state, MarketFeedConfig::default(), shutdown)
    }

    /// Run the feed loop. This consumes `self` and runs until the shutdown
    /// signal fires. Intended to be spawned as a background tokio task:
    ///
    /// ```ignore
    /// let feed = MarketFeed::new(client, ob, state, config, shutdown);
    /// tokio::spawn(feed.run());
    /// ```
    pub async fn run(self) {
        info!(
            poll_interval_ms = self.config.poll_interval.as_millis() as u64,
            max_concurrency = self.config.max_concurrency,
            staleness_threshold_s = self.config.staleness_threshold.as_secs(),
            "MarketFeed starting"
        );

        let mut cycle: u64 = 0;

        loop {
            tokio::select! {
                _ = self.shutdown.notified() => {
                    info!("MarketFeed received shutdown signal, stopping");
                    break;
                }
                _ = tokio::time::sleep(self.config.poll_interval) => {
                    cycle += 1;
                    self.poll_cycle(cycle).await;
                }
            }
        }

        info!(total_cycles = cycle, "MarketFeed stopped");
    }

    /// Spawn the feed as a background tokio task. Returns the `JoinHandle`.
    pub fn spawn(self) -> tokio::task::JoinHandle<()> {
        tokio::spawn(self.run())
    }

    // =========================================================================
    // Internal: one poll cycle
    // =========================================================================

    /// Execute one full poll cycle: fetch order books for all tracked
    /// markets with bounded concurrency, update trackers.
    async fn poll_cycle(&self, cycle: u64) {
        // Collect slugs to poll: union of orderbook-tracked and state-tracked.
        let mut slugs = self.orderbook.tracked_markets();
        for market in self.state.get_all_markets() {
            if !slugs.contains(&market.market_slug) {
                slugs.push(market.market_slug);
            }
        }

        if slugs.is_empty() {
            debug!(cycle, "No markets to poll");
            return;
        }

        debug!(cycle, market_count = slugs.len(), "Polling order books");

        // Poll markets with bounded concurrency using a semaphore.
        let semaphore = Arc::new(tokio::sync::Semaphore::new(self.config.max_concurrency));
        let mut handles = Vec::with_capacity(slugs.len());

        for slug in slugs {
            let client = Arc::clone(&self.client);
            let sem = Arc::clone(&semaphore);
            let slug_owned = slug.clone();

            let handle = tokio::spawn(async move {
                // Acquire semaphore permit (bounds concurrency).
                let _permit = match sem.acquire().await {
                    Ok(p) => p,
                    Err(_) => return (slug_owned, Err("Semaphore closed".to_string())),
                };

                match client.get_market_sides(&slug_owned).await {
                    Ok(book) => (slug_owned, Ok(book)),
                    Err(e) => (slug_owned, Err(e.to_string())),
                }
            });

            handles.push(handle);
        }

        // Collect results.
        let mut success_count: u32 = 0;
        let mut error_count: u32 = 0;

        for handle in handles {
            match handle.await {
                Ok((slug, Ok(book))) => {
                    // Update OrderBookTracker.
                    let top = self.orderbook.get_top(&slug);
                    self.orderbook.update(book);

                    // Update StateManager market state from the new top-of-book.
                    if let Some(new_top) = self.orderbook.get_top(&slug) {
                        // Preserve existing title if available, otherwise use slug.
                        let title = self
                            .state
                            .get_market(&slug)
                            .map(|m| m.title)
                            .unwrap_or_else(|| slug.clone());

                        self.state.update_market(MarketState {
                            market_slug: slug.clone(),
                            title,
                            yes_bid: new_top.yes_best_bid,
                            yes_ask: new_top.yes_best_ask,
                            no_bid: new_top.no_best_bid,
                            no_ask: new_top.no_best_ask,
                            last_updated: Utc::now(),
                        });

                        // Log meaningful price changes at debug level.
                        if let Some(old_top) = top {
                            if old_top.yes_best_bid != new_top.yes_best_bid
                                || old_top.yes_best_ask != new_top.yes_best_ask
                            {
                                debug!(
                                    market = %slug,
                                    yes_bid = ?new_top.yes_best_bid,
                                    yes_ask = ?new_top.yes_best_ask,
                                    no_bid = ?new_top.no_best_bid,
                                    no_ask = ?new_top.no_best_ask,
                                    "Price update"
                                );
                            }
                        }
                    }

                    success_count += 1;
                }
                Ok((slug, Err(e))) => {
                    warn!(market = %slug, error = %e, "Failed to poll order book");
                    error_count += 1;
                }
                Err(e) => {
                    warn!(error = %e, "Task join error during order book poll");
                    error_count += 1;
                }
            }
        }

        debug!(
            cycle,
            success = success_count,
            errors = error_count,
            "Poll cycle complete"
        );

        // Staleness check: warn about markets that haven't been updated recently.
        self.check_staleness();
    }

    // =========================================================================
    // Staleness Detection
    // =========================================================================

    /// Warn about markets whose `last_updated` exceeds the staleness
    /// threshold.
    fn check_staleness(&self) {
        let now = Utc::now();
        let threshold = chrono::Duration::from_std(self.config.staleness_threshold)
            .unwrap_or(chrono::Duration::seconds(30));

        for market in self.state.get_all_markets() {
            let age = now - market.last_updated;
            if age > threshold {
                warn!(
                    market = %market.market_slug,
                    last_updated = %market.last_updated,
                    age_secs = age.num_seconds(),
                    threshold_secs = threshold.num_seconds(),
                    "Stale market data detected"
                );
            }
        }
    }
}

// =============================================================================
// Convenience: standalone run function
// =============================================================================

/// Convenience function to start the market feed as a background task.
/// Returns a `JoinHandle` that resolves when the feed stops.
///
/// # Example
///
/// ```ignore
/// let handle = run_market_feed(client, ob, state, config, shutdown);
/// // ... later ...
/// shutdown.notify_waiters();
/// handle.await.unwrap();
/// ```
pub fn run_market_feed(
    client: Arc<PolymarketClient>,
    orderbook: OrderBookTracker,
    state: StateManager,
    config: MarketFeedConfig,
    shutdown: Arc<Notify>,
) -> tokio::task::JoinHandle<()> {
    let feed = MarketFeed::new(client, orderbook, state, config, shutdown);
    feed.spawn()
}
