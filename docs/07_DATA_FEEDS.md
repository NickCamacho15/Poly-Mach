# Sports Data Feeds

## Overview

For live arbitrage to work, you need **faster sports data than Polymarket**. This document covers data providers, integration patterns, and latency considerations.

---

## Data Provider Comparison

| Provider | Latency | NBA Coverage | Cost | Best For |
|----------|---------|--------------|------|----------|
| **Sportradar** | <1 sec | Excellent | $500-2000/mo | Professional HFT |
| **OpticOdds** | 1-3 sec | Good | $99-299/mo | **Recommended** |
| **ESPN API** | 5-15 sec | Good | Free | Paper trading |
| **NBA API** | 3-10 sec | Excellent | Free | Backup/validation |
| **BallDontLie** | 5-30 sec | Good | Free | Development |

---

## Recommended: OpticOdds

OpticOdds aggregates real-time odds from 100+ sportsbooks AND provides live game data. This gives you both:
1. Live scores/events for arbitrage
2. Sportsbook consensus for statistical edge

### Pricing

| Plan | Price | Features |
|------|-------|----------|
| Starter | $99/mo | 10 markets, delayed data |
| Pro | $199/mo | 50 markets, real-time |
| Enterprise | $499+/mo | Unlimited, lowest latency |

### API Structure

**Base URL:** `https://api.opticodds.com/v1`

**Endpoints:**
- `GET /odds` - Current odds from all books
- `GET /events` - Live and upcoming events
- `GET /scores` - Live scores
- `WS /stream` - Real-time updates

### Integration

```python
import aiohttp
import asyncio
from typing import Callable, Dict, Any
from decimal import Decimal
import structlog

logger = structlog.get_logger()


class OpticOddsClient:
    """
    Client for OpticOdds real-time sports data.
    """
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.opticodds.com/v1"
        self.ws_url = "wss://api.opticodds.com/v1/stream"
        self.session = None
        
    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self.api_key}"}
        )
        return self
        
    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()
            
    async def get_live_events(self, sport: str = "basketball_nba") -> list:
        """Get all live events for a sport."""
        async with self.session.get(
            f"{self.base_url}/events",
            params={"sport": sport, "status": "live"}
        ) as response:
            data = await response.json()
            return data.get("events", [])
            
    async def get_odds(
        self,
        event_id: str,
        market: str = "moneyline"
    ) -> Dict[str, Any]:
        """
        Get current odds for an event.
        
        Args:
            event_id: OpticOdds event ID
            market: "moneyline", "spread", "total"
            
        Returns:
            Dictionary of odds by sportsbook
        """
        async with self.session.get(
            f"{self.base_url}/odds",
            params={"event_id": event_id, "market": market}
        ) as response:
            data = await response.json()
            return data
            
    async def get_live_score(self, event_id: str) -> Dict[str, Any]:
        """Get current score and game state."""
        async with self.session.get(
            f"{self.base_url}/scores/{event_id}"
        ) as response:
            return await response.json()
            
    async def stream_updates(
        self,
        sport: str,
        on_score: Callable,
        on_odds: Callable
    ):
        """
        Stream real-time updates via WebSocket.
        
        Args:
            sport: Sport key (e.g., "basketball_nba")
            on_score: Callback for score updates
            on_odds: Callback for odds updates
        """
        import websockets
        
        async with websockets.connect(
            self.ws_url,
            extra_headers={"Authorization": f"Bearer {self.api_key}"}
        ) as ws:
            # Subscribe to sport
            await ws.send(json.dumps({
                "action": "subscribe",
                "sport": sport,
                "markets": ["moneyline", "spread", "total"]
            }))
            
            async for message in ws:
                data = json.loads(message)
                
                if data.get("type") == "score":
                    await on_score(data)
                elif data.get("type") == "odds":
                    await on_odds(data)


# Usage
async def main():
    async with OpticOddsClient("your-api-key") as client:
        events = await client.get_live_events()
        
        for event in events:
            print(f"{event['home_team']} vs {event['away_team']}")
            
            odds = await client.get_odds(event['id'])
            print(f"  Odds: {odds}")
```

---

## Free Alternative: ESPN API (Hidden)

ESPN has an undocumented public API. Good for development and paper trading.

**Base URL:** `https://site.api.espn.com/apis/site/v2/sports`

### Endpoints

| Endpoint | Description |
|----------|-------------|
| `/basketball/nba/scoreboard` | All games today |
| `/basketball/nba/summary?event={id}` | Detailed game data |
| `/basketball/nba/teams` | Team information |

### Integration

```python
import aiohttp
from typing import Dict, List, Any, Optional
from datetime import datetime
import structlog

logger = structlog.get_logger()


class ESPNClient:
    """
    Client for ESPN's public API.
    Free but slower than paid providers.
    """
    
    BASE_URL = "https://site.api.espn.com/apis/site/v2/sports"
    
    def __init__(self):
        self.session = None
        
    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self
        
    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()
            
    async def get_nba_scoreboard(self) -> List[Dict]:
        """Get today's NBA games."""
        url = f"{self.BASE_URL}/basketball/nba/scoreboard"
        
        async with self.session.get(url) as response:
            data = await response.json()
            return data.get("events", [])
            
    async def get_game_details(self, event_id: str) -> Dict:
        """Get detailed game information."""
        url = f"{self.BASE_URL}/basketball/nba/summary"
        
        async with self.session.get(
            url,
            params={"event": event_id}
        ) as response:
            return await response.json()
            
    def parse_game_state(self, game: Dict) -> Dict[str, Any]:
        """
        Parse ESPN game data into standard format.
        """
        competition = game.get("competitions", [{}])[0]
        competitors = competition.get("competitors", [])
        
        home = next((c for c in competitors if c.get("homeAway") == "home"), {})
        away = next((c for c in competitors if c.get("homeAway") == "away"), {})
        
        status = competition.get("status", {})
        
        return {
            "game_id": game.get("id"),
            "home_team": home.get("team", {}).get("abbreviation"),
            "away_team": away.get("team", {}).get("abbreviation"),
            "home_score": int(home.get("score", 0)),
            "away_score": int(away.get("score", 0)),
            "quarter": status.get("period", 0),
            "time_remaining": status.get("displayClock", "0:00"),
            "status": status.get("type", {}).get("name", "STATUS_UNKNOWN"),
            "is_live": status.get("type", {}).get("state") == "in"
        }


class ESPNPoller:
    """
    Poll ESPN for live game updates.
    """
    
    def __init__(self, poll_interval: float = 5.0):
        self.poll_interval = poll_interval
        self.client = ESPNClient()
        self.callbacks: List[Callable] = []
        self._running = False
        self._last_states: Dict[str, Dict] = {}
        
    def on_game_update(self, callback: Callable):
        """Register callback for game updates."""
        self.callbacks.append(callback)
        
    async def run(self):
        """Poll for updates."""
        self._running = True
        
        async with self.client:
            while self._running:
                try:
                    games = await self.client.get_nba_scoreboard()
                    
                    for game in games:
                        state = self.client.parse_game_state(game)
                        
                        # Check if state changed
                        game_id = state["game_id"]
                        last_state = self._last_states.get(game_id)
                        
                        if self._state_changed(last_state, state):
                            self._last_states[game_id] = state
                            
                            for callback in self.callbacks:
                                await callback(state)
                                
                except Exception as e:
                    logger.error("ESPN poll error", error=str(e))
                    
                await asyncio.sleep(self.poll_interval)
                
    def _state_changed(
        self,
        old: Optional[Dict],
        new: Dict
    ) -> bool:
        """Check if game state meaningfully changed."""
        if old is None:
            return True
            
        return (
            old.get("home_score") != new.get("home_score") or
            old.get("away_score") != new.get("away_score") or
            old.get("quarter") != new.get("quarter") or
            old.get("status") != new.get("status")
        )
        
    def stop(self):
        self._running = False


# Usage
async def handle_game_update(state: Dict):
    print(f"{state['away_team']} {state['away_score']} @ {state['home_team']} {state['home_score']}")
    print(f"  Q{state['quarter']} - {state['time_remaining']}")

async def main():
    poller = ESPNPoller(poll_interval=5.0)
    poller.on_game_update(handle_game_update)
    await poller.run()
```

---

## Sportsbook Odds Integration

For the **Statistical Edge** strategy, you need sportsbook consensus odds.

### OpticOdds Aggregated Odds

```python
async def get_consensus_probability(
    client: OpticOddsClient,
    event_id: str
) -> Decimal:
    """
    Calculate consensus probability from multiple sportsbooks.
    """
    odds = await client.get_odds(event_id, market="moneyline")
    
    # Weight by book sharpness
    weights = {
        "pinnacle": 1.5,
        "circa": 1.3,
        "draftkings": 1.0,
        "fanduel": 1.0,
        "betmgm": 0.8,
        "caesars": 0.8,
        "pointsbet": 0.7
    }
    
    weighted_sum = Decimal("0")
    total_weight = Decimal("0")
    
    for book, book_odds in odds.get("books", {}).items():
        book_lower = book.lower()
        weight = Decimal(str(weights.get(book_lower, 0.5)))
        
        # Convert American odds to probability
        american = book_odds.get("home_ml")
        if american is None:
            continue
            
        if american > 0:
            prob = Decimal("100") / (Decimal(str(american)) + 100)
        else:
            prob = Decimal(str(abs(american))) / (Decimal(str(abs(american))) + 100)
            
        weighted_sum += prob * weight
        total_weight += weight
        
    if total_weight == 0:
        return None
        
    return weighted_sum / total_weight
```

---

## Matching Sports Data to Polymarket Markets

Polymarket uses slugified market names like:
- `nba-lakers-vs-celtics-2025-01-25`

You need to match these to sports API game IDs:

```python
from datetime import date
from typing import Optional, Dict
import re


class MarketMatcher:
    """
    Match Polymarket market slugs to sports data events.
    """
    
    # NBA team name variations
    TEAM_MAPPINGS = {
        # Polymarket slug -> ESPN/API abbreviation
        "lakers": "LAL",
        "celtics": "BOS",
        "warriors": "GSW",
        "nets": "BKN",
        "knicks": "NYK",
        "bulls": "CHI",
        "heat": "MIA",
        "bucks": "MIL",
        "76ers": "PHI",
        "sixers": "PHI",
        "suns": "PHX",
        "mavericks": "DAL",
        "mavs": "DAL",
        # ... add all teams
    }
    
    def parse_market_slug(self, slug: str) -> Optional[Dict]:
        """
        Parse Polymarket slug into components.
        
        Args:
            slug: e.g., "nba-lakers-vs-celtics-2025-01-25"
            
        Returns:
            {"sport": "nba", "home": "LAL", "away": "BOS", "date": date}
        """
        # Pattern: sport-team1-vs-team2-YYYY-MM-DD
        pattern = r"(\w+)-(\w+)-vs-(\w+)-(\d{4}-\d{2}-\d{2})"
        match = re.match(pattern, slug)
        
        if not match:
            return None
            
        sport, team1, team2, date_str = match.groups()
        
        return {
            "sport": sport.upper(),
            "team1": self.TEAM_MAPPINGS.get(team1.lower(), team1.upper()),
            "team2": self.TEAM_MAPPINGS.get(team2.lower(), team2.upper()),
            "date": date.fromisoformat(date_str)
        }
        
    def find_matching_event(
        self,
        market_slug: str,
        events: list
    ) -> Optional[Dict]:
        """
        Find sports API event matching a Polymarket slug.
        """
        parsed = self.parse_market_slug(market_slug)
        if not parsed:
            return None
            
        for event in events:
            event_home = event.get("home_team", "").upper()
            event_away = event.get("away_team", "").upper()
            event_date = event.get("date")
            
            # Check if teams match (in either order)
            teams_match = (
                {parsed["team1"], parsed["team2"]} == 
                {event_home, event_away}
            )
            
            # Check date
            date_match = str(parsed["date"]) in str(event_date)
            
            if teams_match and date_match:
                return event
                
        return None
```

---

## Complete Sports Feed Integration

```python
from typing import Callable, Dict, Any, Optional
import asyncio
import structlog

logger = structlog.get_logger()


class SportsFeedAggregator:
    """
    Aggregates data from multiple sports sources.
    Provides unified interface for strategies.
    """
    
    def __init__(
        self,
        opticodds_key: Optional[str] = None,
        use_espn_fallback: bool = True
    ):
        self.opticodds_key = opticodds_key
        self.use_espn_fallback = use_espn_fallback
        
        self.market_matcher = MarketMatcher()
        self.game_states: Dict[str, Dict] = {}  # market_slug -> state
        self.odds_cache: Dict[str, Dict] = {}   # market_slug -> odds
        
        self.on_game_update_handlers: list = []
        self.on_odds_update_handlers: list = []
        
    def on_game_update(self, handler: Callable):
        """Register handler for game state updates."""
        self.on_game_update_handlers.append(handler)
        
    def on_odds_update(self, handler: Callable):
        """Register handler for odds updates."""
        self.on_odds_update_handlers.append(handler)
        
    async def run(self, market_slugs: list):
        """
        Run the aggregator for specified markets.
        """
        tasks = []
        
        # Start OpticOdds if available
        if self.opticodds_key:
            tasks.append(self._run_opticodds(market_slugs))
        elif self.use_espn_fallback:
            tasks.append(self._run_espn_fallback(market_slugs))
            
        await asyncio.gather(*tasks)
        
    async def _run_opticodds(self, market_slugs: list):
        """Run OpticOdds integration."""
        async with OpticOddsClient(self.opticodds_key) as client:
            # Map markets to events
            events = await client.get_live_events("basketball_nba")
            
            market_event_map = {}
            for slug in market_slugs:
                event = self.market_matcher.find_matching_event(slug, events)
                if event:
                    market_event_map[slug] = event["id"]
                    
            # Stream updates
            async def handle_score(data):
                for slug, event_id in market_event_map.items():
                    if data.get("event_id") == event_id:
                        state = self._parse_opticodds_score(data)
                        self.game_states[slug] = state
                        
                        for handler in self.on_game_update_handlers:
                            await handler(slug, state)
                            
            async def handle_odds(data):
                for slug, event_id in market_event_map.items():
                    if data.get("event_id") == event_id:
                        self.odds_cache[slug] = data.get("odds", {})
                        
                        for handler in self.on_odds_update_handlers:
                            await handler(slug, data)
                            
            await client.stream_updates(
                "basketball_nba",
                on_score=handle_score,
                on_odds=handle_odds
            )
            
    async def _run_espn_fallback(self, market_slugs: list):
        """Run ESPN fallback polling."""
        poller = ESPNPoller(poll_interval=5.0)
        
        async def handle_espn_update(state: Dict):
            # Find matching market
            for slug in market_slugs:
                parsed = self.market_matcher.parse_market_slug(slug)
                if not parsed:
                    continue
                    
                teams = {state["home_team"], state["away_team"]}
                if parsed["team1"] in teams or parsed["team2"] in teams:
                    self.game_states[slug] = state
                    
                    for handler in self.on_game_update_handlers:
                        await handler(slug, state)
                        
        poller.on_game_update(handle_espn_update)
        await poller.run()
        
    def get_game_state(self, market_slug: str) -> Optional[Dict]:
        """Get current game state for a market."""
        return self.game_states.get(market_slug)
        
    def get_odds(self, market_slug: str) -> Optional[Dict]:
        """Get current odds for a market."""
        return self.odds_cache.get(market_slug)
        
    def _parse_opticodds_score(self, data: Dict) -> Dict:
        """Parse OpticOdds score format to standard format."""
        return {
            "game_id": data.get("event_id"),
            "home_team": data.get("home_team"),
            "away_team": data.get("away_team"),
            "home_score": data.get("home_score", 0),
            "away_score": data.get("away_score", 0),
            "quarter": data.get("period", 0),
            "time_remaining": data.get("clock", "0:00"),
            "status": data.get("status", "UNKNOWN"),
            "is_live": data.get("is_live", False)
        }
```

---

## Latency Benchmarking

Before going live, measure your actual latency:

```python
import time
import asyncio
from statistics import mean, stdev


async def benchmark_latency(client, iterations: int = 100):
    """
    Benchmark data feed latency.
    """
    latencies = []
    
    for i in range(iterations):
        start = time.perf_counter()
        
        # Make a request
        await client.get_live_events()
        
        latency_ms = (time.perf_counter() - start) * 1000
        latencies.append(latency_ms)
        
        await asyncio.sleep(0.5)  # Don't hammer the API
        
    return {
        "min": min(latencies),
        "max": max(latencies),
        "mean": mean(latencies),
        "std": stdev(latencies),
        "p50": sorted(latencies)[len(latencies)//2],
        "p99": sorted(latencies)[int(len(latencies)*0.99)]
    }
    
# Example output:
# {"min": 45.2, "max": 312.5, "mean": 78.3, "std": 42.1, "p50": 65.0, "p99": 195.0}
```

**Target Latencies:**
- **< 100ms average:** Excellent, competitive for arbitrage
- **100-500ms average:** Good, can catch some opportunities
- **> 500ms average:** Too slow for live arbitrage, focus on other strategies

---

## Recommendation for Your Setup

### Starting Out (Paper Trading)

1. Use **ESPN API** (free)
2. Poll every 5 seconds
3. Good enough to test strategies
4. No cost

### Going Live ($99-199/month)

1. Upgrade to **OpticOdds Pro**
2. Real-time WebSocket data
3. Sportsbook odds included
4. Enough for 10-50 active markets

### Scaling ($500+/month)

1. **Sportradar** for lowest latency scores
2. **OpticOdds** for odds aggregation
3. Run multiple data feeds redundantly
4. Sub-second edge on live events
