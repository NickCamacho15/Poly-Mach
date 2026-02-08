//! Order book tracker with efficient updates.
//!
//! Maintains a local copy of order books for all tracked markets,
//! providing sub-microsecond access to best bid/ask prices.

use rust_decimal::Decimal;
use std::collections::HashMap;
use std::sync::{Arc, RwLock};

use super::models::{OrderBook, OrderBookSide, PriceLevel, Side};

/// Compact top-of-book snapshot for fast access.
#[derive(Debug, Clone, Default)]
pub struct TopOfBook {
    pub yes_best_bid: Option<Decimal>,
    pub yes_best_ask: Option<Decimal>,
    pub no_best_bid: Option<Decimal>,
    pub no_best_ask: Option<Decimal>,
}

impl TopOfBook {
    pub fn yes_mid(&self) -> Option<Decimal> {
        match (self.yes_best_bid, self.yes_best_ask) {
            (Some(bid), Some(ask)) => Some((bid + ask) / Decimal::TWO),
            _ => None,
        }
    }

    pub fn yes_spread(&self) -> Option<Decimal> {
        match (self.yes_best_bid, self.yes_best_ask) {
            (Some(bid), Some(ask)) => Some(ask - bid),
            _ => None,
        }
    }

    pub fn completeness_sum(&self) -> Option<Decimal> {
        match (self.yes_best_ask, self.no_best_ask) {
            (Some(yes_ask), Some(no_ask)) => Some(yes_ask + no_ask),
            _ => None,
        }
    }
}

/// Thread-safe order book tracker for all markets.
#[derive(Debug, Clone)]
pub struct OrderBookTracker {
    inner: Arc<RwLock<TrackerInner>>,
}

#[derive(Debug, Default)]
struct TrackerInner {
    books: HashMap<String, OrderBook>,
    tops: HashMap<String, TopOfBook>,
}

impl OrderBookTracker {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(RwLock::new(TrackerInner::default())),
        }
    }

    /// Update the full order book for a market.
    pub fn update(&self, book: OrderBook) {
        let mut inner = self.inner.write().unwrap();
        let top = Self::compute_top(&book);
        inner.tops.insert(book.market_slug.clone(), top);
        inner.books.insert(book.market_slug.clone(), book);
    }

    /// Update just one side (YES or NO) from a partial update.
    pub fn update_side(&self, market_slug: &str, side: Side, book_side: OrderBookSide) {
        let mut inner = self.inner.write().unwrap();
        if let Some(book) = inner.books.get_mut(market_slug) {
            match side {
                Side::Yes => book.yes = book_side,
                Side::No => book.no = book_side,
            }
            let top = Self::compute_top(book);
            inner.tops.insert(market_slug.to_string(), top);
        }
    }

    /// Get top-of-book snapshot (fast path).
    pub fn get_top(&self, market_slug: &str) -> Option<TopOfBook> {
        let inner = self.inner.read().unwrap();
        inner.tops.get(market_slug).cloned()
    }

    /// Get full order book (slower path, clones all levels).
    pub fn get_full(&self, market_slug: &str) -> Option<OrderBook> {
        let inner = self.inner.read().unwrap();
        inner.books.get(market_slug).cloned()
    }

    /// Get all tracked market slugs.
    pub fn tracked_markets(&self) -> Vec<String> {
        let inner = self.inner.read().unwrap();
        inner.books.keys().cloned().collect()
    }

    /// Remove a market from tracking.
    pub fn remove(&self, market_slug: &str) {
        let mut inner = self.inner.write().unwrap();
        inner.books.remove(market_slug);
        inner.tops.remove(market_slug);
    }

    /// Compute top-of-book from full order book.
    fn compute_top(book: &OrderBook) -> TopOfBook {
        TopOfBook {
            yes_best_bid: book.yes.best_bid(),
            yes_best_ask: book.yes.best_ask(),
            no_best_bid: book.no.best_bid(),
            no_best_ask: book.no.best_ask(),
        }
    }

    /// Find completeness arbitrage opportunities.
    /// Returns markets where YES ask + NO ask < $1.00.
    pub fn scan_completeness_arb(&self, min_margin: Decimal) -> Vec<CompletenessArbSignal> {
        let inner = self.inner.read().unwrap();
        let fee_rate = Decimal::new(1, 3); // 10 bps = 0.001

        inner
            .tops
            .iter()
            .filter_map(|(slug, top)| {
                let combined = top.completeness_sum()?;
                if combined >= Decimal::ONE {
                    return None;
                }
                let gross_margin = Decimal::ONE - combined;
                let fee_cost = combined * fee_rate;
                let net_margin = gross_margin - fee_cost;
                if net_margin > min_margin {
                    Some(CompletenessArbSignal {
                        market_slug: slug.clone(),
                        yes_ask: top.yes_best_ask?,
                        no_ask: top.no_best_ask?,
                        combined_cost: combined,
                        gross_margin,
                        net_margin,
                    })
                } else {
                    None
                }
            })
            .collect()
    }
}

impl Default for OrderBookTracker {
    fn default() -> Self {
        Self::new()
    }
}

/// Signal from completeness arbitrage scanner.
#[derive(Debug, Clone)]
pub struct CompletenessArbSignal {
    pub market_slug: String,
    pub yes_ask: Decimal,
    pub no_ask: Decimal,
    pub combined_cost: Decimal,
    pub gross_margin: Decimal,
    pub net_margin: Decimal,
}

/// Parse a `/v1/markets/{slug}/book` response into an OrderBook.
///
/// The API returns a single book with `bids` and `offers` (not YES/NO sides).
/// Each entry has: `{ "px": { "value": "0.55", "currency": "USD" }, "qty": "1000" }`
///
/// For a binary market:
/// - `bids` = YES buy orders
/// - `offers` = YES sell orders (= NO buy orders)
/// - NO prices are derived: NO ask = 1 - YES bid, NO bid = 1 - YES ask
pub fn parse_book_response(market_slug: &str, market_data: &serde_json::Value) -> OrderBook {
    let slug = market_data
        .get("marketSlug")
        .and_then(|v| v.as_str())
        .unwrap_or(market_slug)
        .to_string();

    // Parse bids → YES bids
    let yes_bids = parse_book_entries(market_data.get("bids"));

    // Parse offers → YES asks
    let yes_asks = parse_book_entries(market_data.get("offers"));

    // Derive NO side from YES: NO ask = 1 - YES bid, NO bid = 1 - YES ask
    let no_bids: Vec<PriceLevel> = yes_asks
        .iter()
        .map(|level| PriceLevel {
            price: Decimal::ONE - level.price,
            quantity: level.quantity,
        })
        .collect();

    let no_asks: Vec<PriceLevel> = yes_bids
        .iter()
        .map(|level| PriceLevel {
            price: Decimal::ONE - level.price,
            quantity: level.quantity,
        })
        .collect();

    OrderBook {
        market_slug: slug,
        yes: OrderBookSide { bids: yes_bids, asks: yes_asks },
        no: OrderBookSide { bids: no_bids, asks: no_asks },
    }
}

/// Parse book entries from the `/book` response format.
/// Each entry: `{ "px": { "value": "0.55", "currency": "USD" }, "qty": "1000" }`
fn parse_book_entries(data: Option<&serde_json::Value>) -> Vec<PriceLevel> {
    data.and_then(|v| v.as_array())
        .map(|arr| {
            arr.iter()
                .filter_map(|entry| {
                    let price = entry
                        .get("px")
                        .and_then(|px| px.get("value"))
                        .and_then(|v| v.as_str())
                        .and_then(|s| s.parse::<Decimal>().ok())?;
                    let quantity = entry
                        .get("qty")
                        .and_then(|v| v.as_str())
                        .and_then(|s| s.parse::<i64>().ok())?;
                    Some(PriceLevel { price, quantity })
                })
                .collect()
        })
        .unwrap_or_default()
}
