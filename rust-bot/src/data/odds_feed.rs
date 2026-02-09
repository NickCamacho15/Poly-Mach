//! External sportsbook odds feed from The Odds API.
//!
//! Polls The Odds API for pre-game and live odds from multiple
//! sportsbooks (DraftKings, FanDuel, BetMGM, etc.) and converts
//! them into `OddsSnapshot` updates for the statistical edge strategy.
//!
//! Requires an API key from <https://the-odds-api.com>.
//! Free tier: 500 requests/month. Paid tiers available for higher volume.

#![allow(dead_code)]

use std::sync::Arc;
use std::time::Duration;

use chrono::Utc;
use rust_decimal::Decimal;
use serde::Deserialize;
use tokio::sync::{mpsc, Notify};
use tracing::{debug, info, warn};

use crate::state::state_manager::StateManager;
use crate::strategies::statistical_edge::OddsSnapshot;

// =============================================================================
// Configuration
// =============================================================================

#[derive(Debug, Clone)]
pub struct OddsFeedConfig {
    pub api_key: String,
    /// How often to poll (seconds). Default 300s to conserve rate limit.
    pub poll_interval: Duration,
    /// Leagues to poll. Maps to Odds API sport keys.
    pub leagues: Vec<String>,
}

impl Default for OddsFeedConfig {
    fn default() -> Self {
        Self {
            api_key: String::new(),
            poll_interval: Duration::from_secs(300),
            leagues: vec!["nba".to_string(), "cbb".to_string()],
        }
    }
}

// =============================================================================
// The Odds API response types
// =============================================================================

#[derive(Debug, Deserialize)]
struct OddsEvent {
    id: String,
    sport_key: String,
    #[serde(default)]
    commence_time: String,
    home_team: String,
    away_team: String,
    #[serde(default)]
    bookmakers: Vec<Bookmaker>,
}

#[derive(Debug, Deserialize)]
struct Bookmaker {
    key: String,
    title: String,
    #[serde(default)]
    markets: Vec<BookmakerMarket>,
}

#[derive(Debug, Deserialize)]
struct BookmakerMarket {
    key: String,
    #[serde(default)]
    outcomes: Vec<Outcome>,
}

#[derive(Debug, Deserialize)]
struct Outcome {
    name: String,
    price: f64, // Decimal odds (e.g., 1.85 means $1 bet returns $1.85)
}

// =============================================================================
// Odds Feed
// =============================================================================

pub struct OddsFeed {
    client: reqwest::Client,
    state: StateManager,
    config: OddsFeedConfig,
    sender: mpsc::UnboundedSender<OddsSnapshot>,
    shutdown: Arc<Notify>,
}

impl OddsFeed {
    pub fn new(
        state: StateManager,
        config: OddsFeedConfig,
        sender: mpsc::UnboundedSender<OddsSnapshot>,
        shutdown: Arc<Notify>,
    ) -> Self {
        Self {
            client: reqwest::Client::builder()
                .timeout(Duration::from_secs(15))
                .build()
                .unwrap_or_default(),
            state,
            config,
            sender,
            shutdown,
        }
    }

    pub fn spawn(self) -> tokio::task::JoinHandle<()> {
        tokio::spawn(self.run())
    }

    async fn run(self) {
        info!(
            poll_interval_s = self.config.poll_interval.as_secs(),
            leagues = ?self.config.leagues,
            "OddsFeed starting (The Odds API)"
        );

        loop {
            tokio::select! {
                _ = self.shutdown.notified() => {
                    info!("OddsFeed received shutdown signal");
                    break;
                }
                _ = tokio::time::sleep(self.config.poll_interval) => {
                    self.poll_all_leagues().await;
                }
            }
        }
    }

    async fn poll_all_leagues(&self) {
        let markets = self.state.get_all_markets();

        for league in &self.config.leagues {
            let sport_key = match league.as_str() {
                "nba" => "basketball_nba",
                "cbb" | "ncaab" => "basketball_ncaab",
                "nfl" => "americanfootball_nfl",
                "nhl" => "icehockey_nhl",
                "mlb" => "baseball_mlb",
                other => {
                    warn!(league = other, "Unknown league for Odds API");
                    continue;
                }
            };

            match self.fetch_odds(sport_key).await {
                Ok(events) => {
                    let mut matched = 0;
                    for event in &events {
                        if let Some(snapshot) =
                            match_odds_to_market(event, &markets, league)
                        {
                            debug!(
                                market = %snapshot.market_slug.as_deref().unwrap_or("?"),
                                provider = %snapshot.provider,
                                yes_prob = %snapshot.yes_probability,
                                "OddsFeed matched event"
                            );
                            if self.sender.send(snapshot).is_err() {
                                warn!("OddsFeed channel closed, stopping");
                                return;
                            }
                            matched += 1;
                        }
                    }
                    debug!(league, total = events.len(), matched, "OddsFeed poll done");
                }
                Err(e) => {
                    warn!(league, error = %e, "OddsFeed poll failed");
                }
            }
        }
    }

    async fn fetch_odds(&self, sport_key: &str) -> Result<Vec<OddsEvent>, String> {
        let url = format!(
            "https://api.the-odds-api.com/v4/sports/{}/odds/",
            sport_key
        );

        let resp = self
            .client
            .get(&url)
            .query(&[
                ("apiKey", self.config.api_key.as_str()),
                ("regions", "us"),
                ("markets", "h2h"),
                ("oddsFormat", "decimal"),
            ])
            .send()
            .await
            .map_err(|e| format!("HTTP error: {e}"))?;

        if !resp.status().is_success() {
            let status = resp.status();
            let body = resp.text().await.unwrap_or_default();
            return Err(format!("HTTP {}: {}", status, body));
        }

        let events: Vec<OddsEvent> = resp
            .json()
            .await
            .map_err(|e| format!("Parse error: {e}"))?;

        Ok(events)
    }
}

// =============================================================================
// Matching and conversion
// =============================================================================

/// Match an Odds API event to a Polymarket market and compute
/// consensus implied probability from all bookmakers.
fn match_odds_to_market(
    event: &OddsEvent,
    markets: &[crate::state::state_manager::MarketState],
    league: &str,
) -> Option<OddsSnapshot> {
    // Compute consensus YES probability from bookmakers.
    let (yes_prob, provider_name, book_count) = consensus_probability(event)?;

    // Find matching Polymarket market.
    let home_city = extract_city(&event.home_team).to_lowercase();
    let away_city = extract_city(&event.away_team).to_lowercase();

    for market in markets {
        let slug = &market.market_slug;

        if !slug.contains(league) {
            continue;
        }

        let title_lower = market.title.to_lowercase();

        // Match by team city in title.
        let title_has_both = !home_city.is_empty()
            && !away_city.is_empty()
            && title_lower.contains(&home_city)
            && title_lower.contains(&away_city);

        if !title_has_both {
            continue;
        }

        // Determine which team is YES.
        // Convention: first team in "X vs. Y" title is YES.
        let first_team = market
            .title
            .split(" vs. ")
            .next()
            .unwrap_or("")
            .to_lowercase();

        // Is the home team the first-listed (YES) team?
        let first_is_home = first_team.contains(&home_city);

        // If first team is home → YES prob = home win prob.
        // If first team is away → YES prob = away win prob = 1 - home win prob.
        let market_yes_prob = if first_is_home {
            yes_prob // yes_prob is home win probability
        } else {
            Decimal::ONE - yes_prob
        };

        // Confidence scales with number of bookmakers (more = more reliable).
        let confidence = (0.5 + (book_count as f64 * 0.05)).min(0.95);

        return Some(OddsSnapshot {
            event_id: event.id.clone(),
            market_slug: Some(market.market_slug.clone()),
            provider: provider_name,
            yes_probability: market_yes_prob,
            confidence,
            timestamp: Utc::now(),
        });
    }

    None
}

/// Compute devigged consensus HOME win probability from all bookmakers.
///
/// Returns (home_fair_prob, provider_summary, bookmaker_count).
fn consensus_probability(event: &OddsEvent) -> Option<(Decimal, String, usize)> {
    let mut fair_probs: Vec<f64> = Vec::new();
    let mut provider_names: Vec<String> = Vec::new();

    for bookmaker in &event.bookmakers {
        let h2h = bookmaker
            .markets
            .iter()
            .find(|m| m.key == "h2h")?;

        if h2h.outcomes.len() < 2 {
            continue;
        }

        // Find home and away outcomes.
        let home_outcome = h2h.outcomes.iter().find(|o| o.name == event.home_team);
        let away_outcome = h2h.outcomes.iter().find(|o| o.name == event.away_team);

        let (home_odds, away_odds) = match (home_outcome, away_outcome) {
            (Some(h), Some(a)) => (h.price, a.price),
            _ => continue,
        };

        if home_odds <= 0.0 || away_odds <= 0.0 {
            continue;
        }

        // Convert decimal odds to raw implied probabilities.
        let home_raw = 1.0 / home_odds;
        let away_raw = 1.0 / away_odds;
        let total = home_raw + away_raw;

        if total <= 0.0 {
            continue;
        }

        // Devig: remove bookmaker margin by normalizing.
        let home_fair = home_raw / total;
        fair_probs.push(home_fair);
        provider_names.push(bookmaker.title.clone());
    }

    if fair_probs.is_empty() {
        return None;
    }

    let avg: f64 = fair_probs.iter().sum::<f64>() / fair_probs.len() as f64;
    let count = fair_probs.len();

    // Convert to Decimal, round to 4 decimal places.
    let prob_dec = Decimal::from_str_exact(&format!("{:.4}", avg)).ok()?;

    let provider_str = if provider_names.len() <= 3 {
        provider_names.join(", ")
    } else {
        format!(
            "{} +{} more",
            provider_names[..2].join(", "),
            provider_names.len() - 2
        )
    };

    Some((prob_dec, provider_str, count))
}

/// Extract city name from full team name (same logic as scores_feed).
fn extract_city(full_name: &str) -> String {
    let multi_word_cities = [
        "Golden State",
        "Los Angeles",
        "New York",
        "New Orleans",
        "Oklahoma City",
        "San Antonio",
        "San Francisco",
        "Salt Lake",
        "Kansas City",
    ];

    let lower = full_name.to_lowercase();
    for city in &multi_word_cities {
        if lower.starts_with(&city.to_lowercase()) {
            return city.to_string();
        }
    }

    full_name
        .split_whitespace()
        .next()
        .unwrap_or(full_name)
        .to_string()
}

// =============================================================================
// Tests
// =============================================================================

#[cfg(test)]
mod tests {
    use super::*;

    fn make_event(home: &str, away: &str, home_odds: f64, away_odds: f64) -> OddsEvent {
        OddsEvent {
            id: "test-1".to_string(),
            sport_key: "basketball_nba".to_string(),
            commence_time: "2026-02-09T00:00:00Z".to_string(),
            home_team: home.to_string(),
            away_team: away.to_string(),
            bookmakers: vec![
                Bookmaker {
                    key: "draftkings".to_string(),
                    title: "DraftKings".to_string(),
                    markets: vec![BookmakerMarket {
                        key: "h2h".to_string(),
                        outcomes: vec![
                            Outcome {
                                name: home.to_string(),
                                price: home_odds,
                            },
                            Outcome {
                                name: away.to_string(),
                                price: away_odds,
                            },
                        ],
                    }],
                },
                Bookmaker {
                    key: "fanduel".to_string(),
                    title: "FanDuel".to_string(),
                    markets: vec![BookmakerMarket {
                        key: "h2h".to_string(),
                        outcomes: vec![
                            Outcome {
                                name: home.to_string(),
                                price: home_odds,
                            },
                            Outcome {
                                name: away.to_string(),
                                price: away_odds,
                            },
                        ],
                    }],
                },
            ],
        }
    }

    #[test]
    fn test_consensus_probability() {
        // Home favored: 1.50 odds = 66.7% raw implied.
        // Away underdog: 2.80 odds = 35.7% raw implied.
        // Total = 102.4% (2.4% vig).
        // Devigged: home = 0.667/1.024 ≈ 0.651, away ≈ 0.349.
        let event = make_event("Charlotte Hornets", "Detroit Pistons", 1.50, 2.80);
        let (prob, provider, count) = consensus_probability(&event).unwrap();
        assert_eq!(count, 2);
        assert!(prob > Decimal::new(60, 2)); // > 0.60
        assert!(prob < Decimal::new(70, 2)); // < 0.70
        assert!(provider.contains("DraftKings"));
    }

    #[test]
    fn test_match_odds_to_market() {
        use crate::state::state_manager::MarketState;

        let markets = vec![MarketState {
            market_slug: "aec-nba-det-cha-2026-02-09".to_string(),
            title: "Detroit vs. Charlotte".to_string(),
            yes_bid: None,
            yes_ask: None,
            no_bid: None,
            no_ask: None,
            last_updated: Utc::now(),
        }];

        // Charlotte is home and favored (1.50 = ~65%).
        // Detroit is away and underdog (2.80 = ~35%).
        let event = make_event("Charlotte Hornets", "Detroit Pistons", 1.50, 2.80);
        let snapshot = match_odds_to_market(&event, &markets, "nba").unwrap();

        assert_eq!(
            snapshot.market_slug,
            Some("aec-nba-det-cha-2026-02-09".to_string())
        );
        // YES = Detroit (first in title), Detroit is away → prob ≈ 35%
        assert!(snapshot.yes_probability < Decimal::new(50, 2));
        assert!(snapshot.yes_probability > Decimal::new(30, 2));
    }

    #[test]
    fn test_devig_removes_margin() {
        // Even odds with vig: both sides at 1.90 (implied 52.6% each = 105.2% total).
        let event = make_event("Team A", "Team B", 1.90, 1.90);
        let (prob, _, _) = consensus_probability(&event).unwrap();
        // After devigging, should be exactly 50%.
        assert_eq!(prob, Decimal::new(5000, 4));
    }
}
