# System Architecture

## High-Level Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           TRADING BOT SYSTEM                             │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐               │
│  │   SPORTS     │    │  POLYMARKET  │    │   SPORTSBOOK │               │
│  │  DATA FEED   │    │   US API     │    │    ODDS API  │               │
│  │  (OpticOdds) │    │  (WebSocket) │    │  (OpticOdds) │               │
│  └──────┬───────┘    └──────┬───────┘    └──────┬───────┘               │
│         │                   │                   │                        │
│         ▼                   ▼                   ▼                        │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                      DATA AGGREGATOR                             │    │
│  │  • Normalizes data from all sources                              │    │
│  │  • Maintains current state (prices, positions, game state)       │    │
│  │  • Broadcasts updates to strategy engine                         │    │
│  └─────────────────────────────┬───────────────────────────────────┘    │
│                                │                                         │
│                                ▼                                         │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                      STRATEGY ENGINE                             │    │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐              │    │
│  │  │   MARKET    │  │    LIVE     │  │ STATISTICAL │              │    │
│  │  │   MAKING    │  │  ARBITRAGE  │  │    EDGE     │              │    │
│  │  └─────────────┘  └─────────────┘  └─────────────┘              │    │
│  └─────────────────────────────┬───────────────────────────────────┘    │
│                                │                                         │
│                                ▼                                         │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                      RISK MANAGER                                │    │
│  │  • Position sizing (Kelly Criterion)                             │    │
│  │  • Exposure limits                                               │    │
│  │  • Daily loss limits                                             │    │
│  │  • Correlation checks                                            │    │
│  └─────────────────────────────┬───────────────────────────────────┘    │
│                                │                                         │
│                                ▼                                         │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                      ORDER EXECUTOR                              │    │
│  │  • Order creation and signing                                    │    │
│  │  • Fill tracking                                                 │    │
│  │  • Order modification/cancellation                               │    │
│  │  • Retry logic                                                   │    │
│  └─────────────────────────────┬───────────────────────────────────┘    │
│                                │                                         │
│                                ▼                                         │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                      POLYMARKET US API                           │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Component Details

### 1. Data Aggregator (`src/data/`)

**Purpose:** Collect, normalize, and distribute data from all sources.

**Subcomponents:**

| File | Responsibility |
|------|----------------|
| `polymarket_ws.py` | WebSocket connection to Polymarket US |
| `sports_feed.py` | Connection to sports data provider |
| `odds_feed.py` | Sportsbook odds aggregation |
| `state_manager.py` | Maintains current system state |
| `event_bus.py` | Pub/sub for internal event distribution |

**State Manager Data Structure:**

```python
@dataclass
class MarketState:
    market_slug: str
    yes_bid: Decimal
    yes_ask: Decimal
    no_bid: Decimal
    no_ask: Decimal
    last_trade_price: Decimal
    last_trade_time: datetime
    volume_24h: Decimal
    
@dataclass
class GameState:
    game_id: str
    sport: str  # "NBA", "NFL", etc.
    home_team: str
    away_team: str
    home_score: int
    away_score: int
    quarter: int  # or period
    time_remaining: str
    status: str  # "LIVE", "HALFTIME", "FINAL"
    
@dataclass
class PositionState:
    market_slug: str
    side: str  # "YES" or "NO"
    quantity: Decimal
    avg_price: Decimal
    current_value: Decimal
    unrealized_pnl: Decimal
```

---

### 2. Strategy Engine (`src/strategies/`)

**Purpose:** Generate trading signals based on market conditions.

**Subcomponents:**

| File | Responsibility |
|------|----------------|
| `base_strategy.py` | Abstract base class for all strategies |
| `market_maker.py` | Two-sided liquidity provision |
| `live_arbitrage.py` | React to live game events |
| `statistical_edge.py` | Compare Polymarket vs sportsbook odds |
| `signal_aggregator.py` | Combine signals from multiple strategies |

**Strategy Interface:**

```python
from abc import ABC, abstractmethod
from typing import List, Optional
from dataclasses import dataclass
from decimal import Decimal

@dataclass
class Signal:
    market_slug: str
    action: str  # "BUY_YES", "BUY_NO", "SELL_YES", "SELL_NO"
    price: Decimal
    quantity: Decimal
    urgency: str  # "LOW", "MEDIUM", "HIGH"
    strategy_name: str
    confidence: float  # 0.0 to 1.0
    reason: str

class BaseStrategy(ABC):
    @abstractmethod
    def on_market_update(self, market_state: MarketState) -> Optional[List[Signal]]:
        """Called when market prices update."""
        pass
    
    @abstractmethod
    def on_game_update(self, game_state: GameState) -> Optional[List[Signal]]:
        """Called when game state changes (score, time, etc.)."""
        pass
    
    @abstractmethod
    def on_odds_update(self, odds: dict) -> Optional[List[Signal]]:
        """Called when sportsbook odds update."""
        pass
```

---

### 3. Risk Manager (`src/risk/`)

**Purpose:** Validate and size all trades before execution.

**Subcomponents:**

| File | Responsibility |
|------|----------------|
| `position_sizer.py` | Kelly Criterion implementation |
| `exposure_monitor.py` | Track portfolio exposure |
| `circuit_breaker.py` | Daily loss limits, emergency stops |
| `correlation_checker.py` | Prevent over-concentration |

**Risk Checks (in order):**

```python
def validate_trade(self, signal: Signal) -> Tuple[bool, Optional[Signal]]:
    """
    Returns (approved, modified_signal).
    modified_signal may have reduced quantity.
    """
    
    # 1. Circuit breaker check
    if self.daily_loss >= self.max_daily_loss:
        return (False, None)
    
    # 2. Position limit check
    current_exposure = self.get_market_exposure(signal.market_slug)
    if current_exposure >= self.max_position_per_market:
        return (False, None)
    
    # 3. Correlation check
    if self.would_exceed_correlated_exposure(signal):
        return (False, None)
    
    # 4. Kelly sizing
    optimal_size = self.kelly_size(signal)
    if optimal_size < self.min_trade_size:
        return (False, None)
    
    # 5. Adjust quantity to risk limits
    adjusted_quantity = min(
        signal.quantity,
        optimal_size,
        self.max_position_per_market - current_exposure
    )
    
    modified_signal = replace(signal, quantity=adjusted_quantity)
    return (True, modified_signal)
```

---

### 4. Order Executor (`src/execution/`)

**Purpose:** Translate signals into API calls and manage order lifecycle.

**Subcomponents:**

| File | Responsibility |
|------|----------------|
| `api_client.py` | Polymarket US REST API wrapper |
| `order_builder.py` | Construct order payloads |
| `order_tracker.py` | Track order states and fills |
| `retry_handler.py` | Handle transient failures |

**Order State Machine:**

```
CREATED → SUBMITTED → PENDING → FILLED
                   ↘         ↗
                    → PARTIALLY_FILLED
                   ↘
                    → CANCELLED
                   ↘
                    → REJECTED
```

---

### 5. Paper Trading Module (`src/paper/`)

**Purpose:** Simulate trading without real money.

**Subcomponents:**

| File | Responsibility |
|------|----------------|
| `paper_executor.py` | Simulates order fills |
| `paper_portfolio.py` | Tracks simulated positions |
| `fill_simulator.py` | Realistic fill modeling |
| `performance_tracker.py` | Track paper PnL |

**Fill Simulation Logic:**

```python
def simulate_fill(self, order: Order, market_state: MarketState) -> Fill:
    """
    Simulate order fill based on current market state.
    Assumes we're a small trader (no market impact).
    """
    if order.type == "LIMIT":
        # Check if limit price is marketable
        if order.intent == "BUY_LONG":  # Buying YES
            if order.price >= market_state.yes_ask:
                return Fill(
                    quantity=order.quantity,
                    price=market_state.yes_ask,
                    fee=order.quantity * market_state.yes_ask * Decimal("0.001"),
                    fill_type="TAKER"
                )
            else:
                # Order rests on book, may fill later
                return None
    # ... similar logic for other intents
```

---

## Directory Structure

```
polymarket-bot/
├── src/
│   ├── __init__.py
│   ├── main.py                    # Entry point
│   ├── config.py                  # Configuration loading
│   │
│   ├── api/
│   │   ├── __init__.py
│   │   ├── auth.py                # Ed25519 signing
│   │   ├── client.py              # REST API client
│   │   └── websocket.py           # WebSocket client
│   │
│   ├── data/
│   │   ├── __init__.py
│   │   ├── polymarket_ws.py       # Polymarket WebSocket handler
│   │   ├── sports_feed.py         # Sports data integration
│   │   ├── odds_feed.py           # Sportsbook odds
│   │   ├── state_manager.py       # System state
│   │   └── event_bus.py           # Internal pub/sub
│   │
│   ├── strategies/
│   │   ├── __init__.py
│   │   ├── base_strategy.py       # Strategy interface
│   │   ├── market_maker.py        # Market making strategy
│   │   ├── live_arbitrage.py      # Live event arbitrage
│   │   ├── statistical_edge.py    # Odds comparison
│   │   └── signal_aggregator.py   # Combine signals
│   │
│   ├── risk/
│   │   ├── __init__.py
│   │   ├── position_sizer.py      # Kelly Criterion
│   │   ├── exposure_monitor.py    # Portfolio tracking
│   │   ├── circuit_breaker.py     # Loss limits
│   │   └── correlation_checker.py # Concentration limits
│   │
│   ├── execution/
│   │   ├── __init__.py
│   │   ├── order_builder.py       # Build order payloads
│   │   ├── order_tracker.py       # Track order states
│   │   └── executor.py            # Execute orders
│   │
│   ├── paper/
│   │   ├── __init__.py
│   │   ├── paper_executor.py      # Paper trading executor
│   │   ├── paper_portfolio.py     # Simulated portfolio
│   │   ├── fill_simulator.py      # Fill simulation
│   │   └── performance.py         # PnL tracking
│   │
│   └── utils/
│       ├── __init__.py
│       ├── logging.py             # Structured logging
│       ├── metrics.py             # Performance metrics
│       └── helpers.py             # Utility functions
│
├── tests/
│   ├── __init__.py
│   ├── test_auth.py
│   ├── test_strategies.py
│   ├── test_risk.py
│   └── test_execution.py
│
├── config/
│   ├── config.yaml                # Main configuration
│   ├── config.paper.yaml          # Paper trading config
│   └── config.live.yaml           # Live trading config
│
├── logs/                          # Log files
├── data/                          # Historical data storage
│
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── README.md
```

---

## Data Flow

### 1. Market Data Flow

```
Polymarket WebSocket
        │
        ▼
┌───────────────────┐
│ Market Message    │
│ {                 │
│   marketSlug,     │
│   bids: [...],    │
│   asks: [...]     │
│ }                 │
└─────────┬─────────┘
          │
          ▼
┌───────────────────┐
│ State Manager     │
│ Updates:          │
│ - MarketState     │
│ - Order book      │
└─────────┬─────────┘
          │
          ▼
┌───────────────────┐
│ Event Bus         │
│ Publish:          │
│ "market_update"   │
└─────────┬─────────┘
          │
          ▼
┌───────────────────┐
│ All Strategies    │
│ on_market_update()│
└───────────────────┘
```

### 2. Trading Signal Flow

```
Strategy generates Signal
          │
          ▼
┌───────────────────┐
│ Signal Aggregator │
│ - Deduplicate     │
│ - Priority sort   │
└─────────┬─────────┘
          │
          ▼
┌───────────────────┐
│ Risk Manager      │
│ - Validate        │
│ - Size position   │
└─────────┬─────────┘
          │
          ▼
     Approved?
      /     \
    Yes      No
     │        │
     ▼        ▼
┌─────────┐  Log &
│ Order   │  Discard
│Executor │
└────┬────┘
     │
     ▼
┌─────────────────┐
│ API Client      │
│ POST /v1/orders │
└─────────────────┘
```

---

## Concurrency Model

**Async/Await with asyncio**

```python
async def main():
    # Create shared state
    state = StateManager()
    event_bus = EventBus()
    
    # Start data feeds (concurrent)
    polymarket_ws = PolymarketWebSocket(state, event_bus)
    sports_feed = SportsFeed(state, event_bus)
    
    # Start strategy engine
    strategy_engine = StrategyEngine(
        strategies=[
            MarketMaker(config),
            LiveArbitrage(config),
            StatisticalEdge(config),
        ],
        event_bus=event_bus
    )
    
    # Start order executor
    executor = OrderExecutor(api_client, paper_mode=True)
    
    # Run all concurrently
    await asyncio.gather(
        polymarket_ws.run(),
        sports_feed.run(),
        strategy_engine.run(),
        executor.run(),
    )
```

**Why asyncio (not threading):**
- WebSocket connections are I/O bound
- Single-threaded avoids race conditions
- Python's GIL makes threading less effective
- Easier to reason about

---

## Configuration Schema

```yaml
# config/config.yaml

api:
  base_url: "https://api.polymarket.us"
  ws_url: "wss://api.polymarket.us/v1/ws"
  api_key_id: "${PM_API_KEY_ID}"
  private_key: "${PM_PRIVATE_KEY}"

trading:
  mode: "paper"  # "paper" or "live"
  markets:
    - "nba-*"    # Subscribe to all NBA markets
  
risk:
  max_position_per_market: 100.00  # USD
  max_portfolio_exposure: 500.00   # USD
  max_daily_loss: 50.00            # USD
  kelly_fraction: 0.25             # Use 1/4 Kelly
  min_trade_size: 1.00             # USD

strategies:
  market_maker:
    enabled: true
    spread: 0.02          # 2 cent spread
    order_size: 10.00     # USD per side
    refresh_interval: 5   # seconds
    
  live_arbitrage:
    enabled: true
    min_edge: 0.03        # 3% minimum edge
    max_position: 50.00   # USD
    
  statistical_edge:
    enabled: true
    min_edge: 0.05        # 5% vs sportsbook
    confidence_threshold: 0.7

sports_data:
  provider: "opticodds"
  api_key: "${OPTICODDS_API_KEY}"
  
logging:
  level: "INFO"
  file: "logs/bot.log"
  format: "json"
```

---

## Error Handling Strategy

| Error Type | Handling |
|------------|----------|
| API 401 | Re-authenticate, check clock sync |
| API 429 | Exponential backoff |
| API 5xx | Retry with backoff, max 3 attempts |
| WebSocket disconnect | Auto-reconnect with backoff |
| Invalid order | Log, notify, continue |
| Risk limit breach | Block trade, log, continue |
| Sports feed outage | Pause live arbitrage strategy |
| Unhandled exception | Log, attempt graceful shutdown |

---

## Monitoring & Alerting

**Metrics to Track:**
- Orders placed / filled / rejected
- Current positions and exposure
- PnL (realized + unrealized)
- Latency (data feed to order submission)
- WebSocket connection health
- API error rates

**Alert Conditions:**
- Daily loss limit hit (circuit breaker triggered)
- WebSocket disconnected > 30 seconds
- API error rate > 5%
- Position exceeds limits
- Unexpected shutdown
