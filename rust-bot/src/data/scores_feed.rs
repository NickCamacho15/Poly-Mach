//! Live scores feed from ESPN public API.
//!
//! Polls ESPN scoreboard endpoints for real-time game scores
//! and converts them into `GameState` updates for the live
//! arbitrage strategy. No API key required.

#![allow(dead_code)]

use std::sync::Arc;
use std::time::Duration;

use chrono::Utc;
use serde::Deserialize;
use tokio::sync::{mpsc, Notify};
use tracing::{debug, info, warn};

use crate::state::state_manager::StateManager;
use crate::strategies::live_arbitrage::GameState;

// =============================================================================
// ESPN API endpoints
// =============================================================================

const ESPN_NBA_SCOREBOARD: &str =
    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard";
const ESPN_CBB_SCOREBOARD: &str =
    "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard";

// =============================================================================
// Configuration
// =============================================================================

#[derive(Debug, Clone)]
pub struct ScoresFeedConfig {
    pub poll_interval: Duration,
    pub leagues: Vec<String>,
}

impl Default for ScoresFeedConfig {
    fn default() -> Self {
        Self {
            poll_interval: Duration::from_secs(15),
            leagues: vec!["nba".to_string(), "cbb".to_string()],
        }
    }
}

// =============================================================================
// ESPN response types
// =============================================================================

#[derive(Debug, Deserialize)]
struct EspnScoreboard {
    #[serde(default)]
    events: Vec<EspnEvent>,
}

#[derive(Debug, Deserialize)]
struct EspnEvent {
    id: String,
    #[serde(default)]
    name: String,
    #[serde(default)]
    competitions: Vec<EspnCompetition>,
}

#[derive(Debug, Deserialize)]
struct EspnCompetition {
    #[serde(default)]
    competitors: Vec<EspnCompetitor>,
    #[serde(default)]
    status: Option<EspnStatus>,
}

#[derive(Debug, Deserialize)]
struct EspnCompetitor {
    #[serde(default)]
    team: Option<EspnTeam>,
    #[serde(rename = "homeAway", default)]
    home_away: String,
    #[serde(default)]
    score: Option<String>,
}

#[derive(Debug, Deserialize)]
struct EspnTeam {
    #[serde(default)]
    abbreviation: Option<String>,
    #[serde(rename = "displayName", default)]
    display_name: Option<String>,
    #[serde(rename = "shortDisplayName", default)]
    short_display_name: Option<String>,
}

#[derive(Debug, Deserialize)]
struct EspnStatus {
    #[serde(rename = "type", default)]
    status_type: Option<EspnStatusType>,
}

#[derive(Debug, Deserialize)]
struct EspnStatusType {
    #[serde(default)]
    completed: bool,
    #[serde(default)]
    name: Option<String>,
}

// =============================================================================
// Parsed event (internal)
// =============================================================================

struct ParsedEvent {
    id: String,
    home_team: String,
    away_team: String,
    home_abbr: String,
    away_abbr: String,
    home_score: i32,
    away_score: i32,
    is_final: bool,
    in_progress: bool,
}

// =============================================================================
// Scores Feed
// =============================================================================

pub struct ScoresFeed {
    client: reqwest::Client,
    state: StateManager,
    config: ScoresFeedConfig,
    sender: mpsc::UnboundedSender<GameState>,
    shutdown: Arc<Notify>,
}

impl ScoresFeed {
    pub fn new(
        state: StateManager,
        config: ScoresFeedConfig,
        sender: mpsc::UnboundedSender<GameState>,
        shutdown: Arc<Notify>,
    ) -> Self {
        Self {
            client: reqwest::Client::builder()
                .timeout(Duration::from_secs(10))
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
            "ScoresFeed starting (ESPN public API)"
        );

        loop {
            tokio::select! {
                _ = self.shutdown.notified() => {
                    info!("ScoresFeed received shutdown signal");
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
            let url = match league.as_str() {
                "nba" => ESPN_NBA_SCOREBOARD,
                "cbb" | "ncaab" => ESPN_CBB_SCOREBOARD,
                _ => continue,
            };

            match self.fetch_and_parse(url).await {
                Ok(events) => {
                    let mut matched = 0;
                    for event in &events {
                        if let Some(game_state) =
                            match_event_to_market(event, &markets, league)
                        {
                            debug!(
                                market = %game_state.market_slug.as_deref().unwrap_or("?"),
                                home = event.home_score,
                                away = event.away_score,
                                final_ = event.is_final,
                                "ScoresFeed matched event"
                            );
                            if self.sender.send(game_state).is_err() {
                                warn!("ScoresFeed channel closed, stopping");
                                return;
                            }
                            matched += 1;
                        }
                    }
                    debug!(league, total = events.len(), matched, "ScoresFeed poll done");
                }
                Err(e) => {
                    warn!(league, error = %e, "ScoresFeed poll failed");
                }
            }
        }
    }

    async fn fetch_and_parse(&self, url: &str) -> Result<Vec<ParsedEvent>, String> {
        let resp = self
            .client
            .get(url)
            .send()
            .await
            .map_err(|e| format!("HTTP error: {e}"))?;

        if !resp.status().is_success() {
            return Err(format!("HTTP {}", resp.status()));
        }

        let scoreboard: EspnScoreboard =
            resp.json().await.map_err(|e| format!("Parse error: {e}"))?;

        let mut results = Vec::new();
        for event in scoreboard.events {
            if let Some(parsed) = parse_espn_event(&event) {
                results.push(parsed);
            }
        }
        Ok(results)
    }
}

// =============================================================================
// Parsing helpers
// =============================================================================

fn parse_espn_event(event: &EspnEvent) -> Option<ParsedEvent> {
    let comp = event.competitions.first()?;
    let status_type = comp.status.as_ref()?.status_type.as_ref()?;

    let is_final = status_type.completed;
    let in_progress = status_type
        .name
        .as_deref()
        .map(|n| n == "STATUS_IN_PROGRESS")
        .unwrap_or(false);

    let mut home_team = String::new();
    let mut away_team = String::new();
    let mut home_abbr = String::new();
    let mut away_abbr = String::new();
    let mut home_score: i32 = 0;
    let mut away_score: i32 = 0;

    for competitor in &comp.competitors {
        let team = competitor.team.as_ref()?;
        let display = team.display_name.as_deref().unwrap_or("");
        let abbr = team.abbreviation.as_deref().unwrap_or("");
        let score: i32 = competitor
            .score
            .as_deref()
            .and_then(|s| s.parse().ok())
            .unwrap_or(0);

        match competitor.home_away.as_str() {
            "home" => {
                home_team = display.to_string();
                home_abbr = abbr.to_lowercase();
                home_score = score;
            }
            "away" => {
                away_team = display.to_string();
                away_abbr = abbr.to_lowercase();
                away_score = score;
            }
            _ => {}
        }
    }

    if home_abbr.is_empty() || away_abbr.is_empty() {
        return None;
    }

    Some(ParsedEvent {
        id: event.id.clone(),
        home_team,
        away_team,
        home_abbr,
        away_abbr,
        home_score,
        away_score,
        is_final,
        in_progress,
    })
}

/// Match an ESPN event to a Polymarket market.
///
/// Matching strategy:
/// 1. Check if the slug contains both team abbreviations (e.g., "det" and "cha")
/// 2. Fall back to title-based matching using team city names
///
/// Returns `None` if the game isn't in progress or can't be matched.
fn match_event_to_market(
    event: &ParsedEvent,
    markets: &[crate::state::state_manager::MarketState],
    league: &str,
) -> Option<GameState> {
    // Only care about live or just-finished games.
    if !event.in_progress && !event.is_final {
        return None;
    }

    for market in markets {
        let slug = &market.market_slug;

        // Slug must contain the league.
        if !slug.contains(league) {
            continue;
        }

        // Strategy 1: match by team abbreviation in slug.
        // Slug format: aec-nba-det-cha-2026-02-09
        let slug_lower = slug.to_lowercase();
        let slug_has_both = slug_lower.contains(&event.away_abbr)
            && slug_lower.contains(&event.home_abbr);

        // Strategy 2: match by team city in title.
        let title_lower = market.title.to_lowercase();
        let home_city = extract_city(&event.home_team).to_lowercase();
        let away_city = extract_city(&event.away_team).to_lowercase();
        let title_has_both = !home_city.is_empty()
            && !away_city.is_empty()
            && title_lower.contains(&home_city)
            && title_lower.contains(&away_city);

        if !slug_has_both && !title_has_both {
            continue;
        }

        // Determine which team is YES.
        // Convention: first team in Polymarket title ("X vs. Y") is YES.
        let first_team = market
            .title
            .split(" vs. ")
            .next()
            .unwrap_or("")
            .to_lowercase();

        // Is the home team the YES team?
        let home_is_yes = first_team.contains(&home_city)
            || first_team.contains(&event.home_abbr);

        return Some(GameState {
            event_id: event.id.clone(),
            market_slug: Some(market.market_slug.clone()),
            home_score: event.home_score,
            away_score: event.away_score,
            home_is_yes,
            is_final: event.is_final,
            timestamp: Utc::now(),
        });
    }

    None
}

/// Extract the city name from a full team name.
/// "Detroit Pistons" → "Detroit", "Golden State Warriors" → "Golden State"
fn extract_city(full_name: &str) -> String {
    // Handle known multi-word city names.
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
        "St. Francis",
        "St. John",
        "Boston College",
        "North Carolina",
        "South Carolina",
        "West Virginia",
        "East Carolina",
        "Central Florida",
        "Texas Southern",
        "Prairie View",
        "Northwestern State",
        "Southern Illinois",
        "Indiana State",
        "Jackson State",
        "Arkansas-Pine Bluff",
        "Bethune-Cookman",
        "Florida A&M",
        "Chicago State",
    ];

    let lower = full_name.to_lowercase();
    for city in &multi_word_cities {
        if lower.starts_with(&city.to_lowercase()) {
            return city.to_string();
        }
    }

    // Default: first word.
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

    #[test]
    fn test_extract_city() {
        assert_eq!(extract_city("Detroit Pistons"), "Detroit");
        assert_eq!(extract_city("Golden State Warriors"), "Golden State");
        assert_eq!(extract_city("Los Angeles Lakers"), "Los Angeles");
        assert_eq!(extract_city("Charlotte Hornets"), "Charlotte");
        assert_eq!(extract_city("Chicago State Cougars"), "Chicago State");
    }

    #[test]
    fn test_match_event_to_market() {
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

        let event = ParsedEvent {
            id: "12345".to_string(),
            home_team: "Charlotte Hornets".to_string(),
            away_team: "Detroit Pistons".to_string(),
            home_abbr: "cha".to_string(),
            away_abbr: "det".to_string(),
            home_score: 85,
            away_score: 90,
            is_final: false,
            in_progress: true,
        };

        let result = match_event_to_market(&event, &markets, "nba");
        assert!(result.is_some());
        let gs = result.unwrap();
        assert_eq!(gs.market_slug, Some("aec-nba-det-cha-2026-02-09".to_string()));
        assert_eq!(gs.home_score, 85);
        assert_eq!(gs.away_score, 90);
        // Detroit is first in title ("Detroit vs. Charlotte"), Detroit is away,
        // so home (Charlotte) is NOT the YES team.
        assert!(!gs.home_is_yes);
    }

    #[test]
    fn test_no_match_for_pregame() {
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

        let event = ParsedEvent {
            id: "12345".to_string(),
            home_team: "Charlotte Hornets".to_string(),
            away_team: "Detroit Pistons".to_string(),
            home_abbr: "cha".to_string(),
            away_abbr: "det".to_string(),
            home_score: 0,
            away_score: 0,
            is_final: false,
            in_progress: false, // Pre-game
        };

        assert!(match_event_to_market(&event, &markets, "nba").is_none());
    }
}
