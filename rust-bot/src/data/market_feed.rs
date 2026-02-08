//! Market data feed via REST order-book polling.
//!
//! Periodically fetches order books for all tracked markets and updates
//! the OrderBookTracker and StateManager with fresh top-of-book data.

use std::collections::HashSet;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::mpsc;
use tracing::{debug, info, warn};

use crate::api::client::PolymarketClient;
use crate::data::orderbook::OrderBookTracker;
use crate::state::state_manager::{MarketState, StateManager};

/// A market update event emitted by the feed.
#[derive(Debug, Clone)]
pub struct MarketUpdate {
    pub market: MarketState,
}

/// Configuration for the market feed.
#[derive(Debug, Clone)]
pub struct MarketFeedConfig {
    pub poll_interval_ms: u64,
    pub max_concurrency: usize,
    pub staleness_threshold_secs: u64,
}

impl Default for MarketFeedConfig {
    fn default() -> Self {
        Self {
            poll_interval_ms: 5000,
            max_concurrency: 5,
            staleness_threshold_secs: 30,
        }
    }
}

/// REST-based market data feed.
pub struct MarketFeed {
    config: MarketFeedConfig,
    market_slugs: Vec<String>,
}

impl MarketFeed {
    pub fn new(config: MarketFeedConfig, market_slugs: Vec<String>) -> Self {
        Self {
            config,
            market_slugs,
        }
    }

    /// Start the feed loop. Returns a channel receiver for market updates.
    /// Runs in a background tokio task.
    pub fn start(
        self,
        client: Arc<PolymarketClient>,
        orderbook: OrderBookTracker,
        state: StateManager,
    ) -> mpsc::Receiver<MarketUpdate> {
        let (tx, rx) = mpsc::channel(256);

        info!(
            poll_interval_ms = self.config.poll_interval_ms,
            max_concurrency = self.config.max_concurrency,
            staleness_threshold_s = self.config.staleness_threshold_secs,
            "MarketFeed starting"
        );
        info!("Market data feed started");

        let config = self.config.clone();
        let slugs = self.market_slugs.clone();

        tokio::spawn(async move {
            let interval = Duration::from_millis(config.poll_interval_ms);
            // Track consecutive failures per slug to remove dead markets.
            let mut failure_counts: std::collections::HashMap<String, u32> = std::collections::HashMap::new();
            let max_consecutive_failures: u32 = 3;
            let mut active_slugs: Vec<String> = slugs.clone();

            loop {
                // Poll all active markets in batches.
                let mut tasks = Vec::new();
                let semaphore = Arc::new(tokio::sync::Semaphore::new(config.max_concurrency));

                for slug in &active_slugs {
                    let client = client.clone();
                    let slug = slug.clone();
                    let sem = semaphore.clone();

                    tasks.push(tokio::spawn(async move {
                        let _permit = sem.acquire().await.ok()?;
                        match client.get_market_sides(&slug).await {
                            Ok(book) => Some((slug, Ok(book))),
                            Err(e) => Some((slug, Err(e))),
                        }
                    }));
                }

                // Collect results.
                let mut updated_count = 0u32;
                let mut with_prices = 0u32;
                let mut error_count = 0u32;
                let mut slugs_to_remove: Vec<String> = Vec::new();

                for task in tasks {
                    if let Ok(Some((slug, result))) = task.await {
                        match result {
                            Ok(book) => {
                                // Reset failure counter on success.
                                failure_counts.remove(&slug);

                                let yes_bid = book.yes.best_bid();
                                let yes_ask = book.yes.best_ask();
                                let no_bid = book.no.best_bid();
                                let no_ask = book.no.best_ask();

                                if yes_bid.is_some() && yes_ask.is_some() {
                                    with_prices += 1;
                                }

                                // Update orderbook tracker.
                                orderbook.update(book);

                                // Update state manager with top-of-book.
                                let market = MarketState {
                                    market_slug: slug.clone(),
                                    title: slug.clone(),
                                    yes_bid,
                                    yes_ask,
                                    no_bid,
                                    no_ask,
                                    last_updated: chrono::Utc::now(),
                                };
                                state.update_market(market.clone());

                                // Send update to engine.
                                let _ = tx.send(MarketUpdate { market }).await;
                                updated_count += 1;
                            }
                            Err(e) => {
                                error_count += 1;
                                let count = failure_counts.entry(slug.clone()).or_insert(0);
                                *count += 1;
                                if *count >= max_consecutive_failures {
                                    warn!(
                                        slug = %slug,
                                        failures = *count,
                                        error = %e,
                                        "Removing dead market after consecutive failures"
                                    );
                                    slugs_to_remove.push(slug);
                                } else {
                                    debug!(
                                        slug = %slug,
                                        failures = *count,
                                        error = %e,
                                        "Order book fetch failed"
                                    );
                                }
                            }
                        }
                    }
                }

                // Remove dead markets from polling.
                if !slugs_to_remove.is_empty() {
                    let remove_set: HashSet<&String> = slugs_to_remove.iter().collect();
                    active_slugs.retain(|s| !remove_set.contains(s));
                    for slug in &slugs_to_remove {
                        failure_counts.remove(slug.as_str());
                    }
                    info!(
                        removed = slugs_to_remove.len(),
                        remaining = active_slugs.len(),
                        "Removed dead markets from polling"
                    );
                }

                info!(
                    updated = updated_count,
                    with_prices = with_prices,
                    errors = error_count,
                    active_markets = active_slugs.len(),
                    "MarketFeed poll cycle complete"
                );

                if active_slugs.is_empty() {
                    warn!("No active markets remaining — feed paused");
                }

                tokio::time::sleep(interval).await;
            }
        });

        rx
    }
}
