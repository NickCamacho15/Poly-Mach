//! Market data feed via REST order-book polling.
//!
//! Periodically fetches order books for all tracked markets and updates
//! the OrderBookTracker and StateManager with fresh top-of-book data.

use rust_decimal::Decimal;
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

            loop {
                // Poll all markets in batches.
                let mut tasks = Vec::new();
                let semaphore = Arc::new(tokio::sync::Semaphore::new(config.max_concurrency));

                for slug in &slugs {
                    let client = client.clone();
                    let slug = slug.clone();
                    let sem = semaphore.clone();

                    tasks.push(tokio::spawn(async move {
                        let _permit = sem.acquire().await.ok()?;
                        match client.get_market_sides(&slug).await {
                            Ok(book) => Some((slug, book)),
                            Err(e) => {
                                debug!(slug = %slug, error = %e, "Failed to fetch order book");
                                None
                            }
                        }
                    }));
                }

                // Collect results.
                for task in tasks {
                    if let Ok(Some((slug, book))) = task.await {
                        let yes_bid = book.yes.best_bid();
                        let yes_ask = book.yes.best_ask();
                        let no_bid = book.no.best_bid();
                        let no_ask = book.no.best_ask();

                        // Update orderbook tracker.
                        orderbook.update(book);

                        // Update state manager with top-of-book.
                        let market = MarketState {
                            market_slug: slug.clone(),
                            title: slug.clone(), // Title set during discovery.
                            yes_bid,
                            yes_ask,
                            no_bid,
                            no_ask,
                            last_updated: chrono::Utc::now(),
                        };
                        state.update_market(market.clone());

                        // Send update to engine.
                        let _ = tx.send(MarketUpdate { market }).await;
                    }
                }

                tokio::time::sleep(interval).await;
            }
        });

        rx
    }
}
