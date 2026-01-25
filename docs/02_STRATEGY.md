# Trading Strategies

## Strategy Overview

This bot implements three complementary strategies that work together to maximize opportunities:

| Strategy | Description | When Active | Expected Edge |
|----------|-------------|-------------|---------------|
| Market Making | Provide liquidity on both sides | Always | 0.5-2% daily |
| Live Arbitrage | React to game events faster than market | During live games | 5-20% per trade |
| Statistical Edge | Find mispriced lines vs sportsbooks | Pre-game, in-game | 3-10% per trade |

**Combined Approach:** Run all three simultaneously. Market making provides steady income while waiting for arbitrage/edge opportunities.

---

## Strategy 1: Market Making

### Concept

Market makers profit by posting limit orders on both sides of the order book and capturing the spread between bids and asks.

**Example:**
- Post BUY YES at $0.48
- Post SELL YES at $0.52
- If both fill: profit = $0.04 per contract (minus fees)

### Why It Works on Polymarket US

1. **0% maker fees** — Posting limit orders costs nothing
2. **Sports markets are volatile** — Both sides fill frequently during games
3. **Less competition** — Polymarket US is new, fewer sophisticated bots

### Implementation

```python
class MarketMaker(BaseStrategy):
    """
    Two-sided market making strategy.
    Posts limit orders on both YES and NO sides.
    """
    
    def __init__(self, config: dict):
        self.spread = Decimal(config.get("spread", "0.02"))  # 2 cents
        self.order_size = Decimal(config.get("order_size", "10.00"))
        self.refresh_interval = config.get("refresh_interval", 5)
        self.active_orders = {}
        
    def calculate_quotes(self, market: MarketState) -> Tuple[Decimal, Decimal]:
        """Calculate bid and ask prices based on mid-price."""
        # Calculate mid-price from current book
        mid_price = (market.yes_bid + market.yes_ask) / 2
        
        # Calculate our quotes
        our_bid = mid_price - (self.spread / 2)
        our_ask = mid_price + (self.spread / 2)
        
        # Ensure prices are in valid range [0.01, 0.99]
        our_bid = max(Decimal("0.01"), min(Decimal("0.99"), our_bid))
        our_ask = max(Decimal("0.01"), min(Decimal("0.99"), our_ask))
        
        return (our_bid, our_ask)
    
    def on_market_update(self, market: MarketState) -> Optional[List[Signal]]:
        """Generate market making signals."""
        signals = []
        
        our_bid, our_ask = self.calculate_quotes(market)
        
        # Check if we need to update quotes
        if self._should_refresh_quotes(market.market_slug, our_bid, our_ask):
            
            # Cancel existing orders
            signals.append(Signal(
                market_slug=market.market_slug,
                action="CANCEL_ALL",
                price=Decimal("0"),
                quantity=Decimal("0"),
                urgency="LOW",
                strategy_name="market_maker",
                confidence=1.0,
                reason="Refreshing quotes"
            ))
            
            # Post new bid (buy YES)
            signals.append(Signal(
                market_slug=market.market_slug,
                action="BUY_YES",
                price=our_bid,
                quantity=self.order_size / our_bid,  # Convert USD to contracts
                urgency="LOW",
                strategy_name="market_maker",
                confidence=0.8,
                reason=f"Market making bid at {our_bid}"
            ))
            
            # Post new ask (sell YES / buy NO)
            signals.append(Signal(
                market_slug=market.market_slug,
                action="SELL_YES",
                price=our_ask,
                quantity=self.order_size / our_ask,
                urgency="LOW",
                strategy_name="market_maker",
                confidence=0.8,
                reason=f"Market making ask at {our_ask}"
            ))
            
        return signals if signals else None
```

### Risk Management for Market Making

| Parameter | Recommended Value | Reason |
|-----------|-------------------|--------|
| Spread | 2-4 cents | Wider = safer but fewer fills |
| Order Size | $10-25 per side | Small enough to avoid inventory risk |
| Max Inventory | $50 per market | Limit directional exposure |
| Refresh Rate | 5-10 seconds | Balance responsiveness vs API limits |

### Inventory Management

When one side fills more than the other, you accumulate directional risk:

```python
def manage_inventory(self, position: PositionState) -> Optional[Signal]:
    """
    If inventory gets too large, widen spread on one side
    or close out position.
    """
    max_inventory = Decimal("50.00")
    
    if abs(position.current_value) > max_inventory:
        # Too much inventory - aggressively close
        if position.quantity > 0:  # Long YES
            return Signal(
                action="SELL_YES",
                price=position.current_value * Decimal("0.98"),  # 2% discount
                quantity=position.quantity / 2,
                urgency="HIGH",
                reason="Inventory reduction"
            )
        else:  # Short YES / Long NO
            return Signal(
                action="BUY_YES",
                price=position.current_value * Decimal("1.02"),
                quantity=abs(position.quantity) / 2,
                urgency="HIGH",
                reason="Inventory reduction"
            )
    
    return None
```

---

## Strategy 2: Live Arbitrage

### Concept

During live games, events happen that immediately change win probabilities (touchdowns, baskets, injuries). If we learn about these events before Polymarket prices adjust, we can trade profitably.

**Example:**
- Lakers are trailing by 5, YES price = $0.45
- LeBron hits a 3-pointer (we learn via sports feed)
- True probability is now ~$0.52
- We buy YES at $0.45 before price adjusts
- Sell at $0.51 when market catches up
- Profit: $0.06 per contract (13% return)

### The Speed Advantage

```
T + 0 seconds:   Event happens (basket scored)
T + 1-3 seconds: Professional data feeds update (Sportradar, etc.)
T + 5-15 seconds: Polymarket prices begin adjusting
T + 30-60 seconds: Prices reach new equilibrium

Our window: 5-30 seconds to act
```

### Implementation

```python
class LiveArbitrage(BaseStrategy):
    """
    React to live game events before market prices adjust.
    Requires premium sports data feed.
    """
    
    def __init__(self, config: dict):
        self.min_edge = Decimal(config.get("min_edge", "0.03"))  # 3%
        self.max_position = Decimal(config.get("max_position", "50.00"))
        self.probability_model = ProbabilityModel()
        
    def on_game_update(self, game: GameState) -> Optional[List[Signal]]:
        """
        Called when game state changes.
        Calculate new win probability and compare to market.
        """
        signals = []
        
        # Get current market price
        market = self.state_manager.get_market_for_game(game.game_id)
        if not market:
            return None
        
        # Calculate true win probability based on game state
        true_prob = self.probability_model.calculate_win_probability(
            home_score=game.home_score,
            away_score=game.away_score,
            time_remaining=game.time_remaining,
            quarter=game.quarter
        )
        
        # Current market implied probability
        market_prob = market.yes_ask  # Cost to buy YES
        
        # Calculate edge
        edge = true_prob - market_prob
        
        if edge > self.min_edge:
            # Market underpricing YES - buy
            signals.append(Signal(
                market_slug=market.market_slug,
                action="BUY_YES",
                price=market.yes_ask,  # Pay the ask
                quantity=self._calculate_quantity(edge),
                urgency="HIGH",  # Execute immediately
                strategy_name="live_arbitrage",
                confidence=float(min(edge * 10, 1.0)),  # Scale confidence
                reason=f"Live arb: true_prob={true_prob:.2f}, market={market_prob:.2f}, edge={edge:.2%}"
            ))
            
        elif edge < -self.min_edge:
            # Market overpricing YES - sell/buy NO
            signals.append(Signal(
                market_slug=market.market_slug,
                action="BUY_NO",
                price=market.no_ask,
                quantity=self._calculate_quantity(abs(edge)),
                urgency="HIGH",
                strategy_name="live_arbitrage",
                confidence=float(min(abs(edge) * 10, 1.0)),
                reason=f"Live arb: true_prob={true_prob:.2f}, market={market_prob:.2f}, edge={edge:.2%}"
            ))
        
        return signals if signals else None
    
    def _calculate_quantity(self, edge: Decimal) -> Decimal:
        """Size position based on edge magnitude."""
        # Larger edge = larger position (up to max)
        base_size = Decimal("10.00")
        edge_multiplier = min(edge / self.min_edge, Decimal("5"))
        return min(base_size * edge_multiplier, self.max_position)
```

### NBA Win Probability Model

Simple model based on point differential and time remaining:

```python
class NBAProbabilityModel:
    """
    Calculate win probability for NBA games.
    Based on historical data: ~1 point of margin = ~2.5% win probability change.
    """
    
    def calculate_win_probability(
        self,
        home_score: int,
        away_score: int,
        quarter: int,
        time_remaining: str,  # "5:32" format
        is_home: bool = True
    ) -> Decimal:
        """
        Calculate probability that home team wins.
        """
        # Parse time remaining
        minutes, seconds = map(int, time_remaining.split(":"))
        total_seconds = minutes * 60 + seconds
        
        # Calculate total time remaining in game
        quarters_remaining = 4 - quarter
        time_remaining_total = quarters_remaining * 12 * 60 + total_seconds
        
        # Point differential (positive = home leading)
        point_diff = home_score - away_score
        
        # Base probability from point differential
        # Rough approximation: each point = ~2.5% swing when game is close
        # This attenuates as game progresses (less time to recover)
        
        if time_remaining_total <= 0:
            # Game over
            return Decimal("1.0") if point_diff > 0 else Decimal("0.0")
        
        # Time factor: how much can change?
        # Full game (2880 seconds) vs remaining time
        time_factor = time_remaining_total / 2880
        
        # Standard deviation of point swings
        # Roughly: sqrt(possessions_remaining) * points_per_possession_variance
        possessions_remaining = time_remaining_total / 24  # ~24 sec per possession
        swing_std = Decimal(str(possessions_remaining ** 0.5 * 2))  # Rough estimate
        
        # Calculate probability using normal distribution approximation
        if swing_std == 0:
            return Decimal("1.0") if point_diff > 0 else Decimal("0.0")
        
        # Z-score: how many std devs is current lead?
        z_score = Decimal(str(point_diff)) / swing_std
        
        # Convert to probability (approximate normal CDF)
        prob = self._normal_cdf(float(z_score))
        
        # Add home court advantage (~3%)
        if is_home:
            prob = min(prob + 0.015, 0.99)  # Small boost for home team
        
        return Decimal(str(round(prob, 4)))
    
    def _normal_cdf(self, z: float) -> float:
        """Approximate normal CDF."""
        import math
        return 0.5 * (1 + math.erf(z / math.sqrt(2)))
```

### Important: Exit Strategy

Live arbitrage positions should be closed quickly:

```python
def manage_arbitrage_position(self, position: PositionState, market: MarketState) -> Optional[Signal]:
    """
    Close arbitrage positions once edge disappears or reverses.
    """
    # Calculate current edge
    entry_price = position.avg_price
    current_price = market.yes_bid if position.quantity > 0 else market.no_bid
    
    # If we're in profit and edge is closing, exit
    profit_pct = (current_price - entry_price) / entry_price
    
    if profit_pct > Decimal("0.02"):  # 2% profit, take it
        return Signal(
            action="SELL_YES" if position.quantity > 0 else "SELL_NO",
            price=current_price,
            quantity=abs(position.quantity),
            urgency="MEDIUM",
            reason=f"Taking arbitrage profit: {profit_pct:.2%}"
        )
    
    # Stop loss at -3%
    if profit_pct < Decimal("-0.03"):
        return Signal(
            action="SELL_YES" if position.quantity > 0 else "SELL_NO",
            price=current_price,
            quantity=abs(position.quantity),
            urgency="HIGH",
            reason=f"Arbitrage stop loss: {profit_pct:.2%}"
        )
    
    return None
```

---

## Strategy 3: Statistical Edge

### Concept

Compare Polymarket prices to sportsbook odds. If there's a significant discrepancy, one market is wrong—bet on the more accurate one.

**Example:**
- Polymarket: Lakers YES = $0.45 (implied 45% win probability)
- DraftKings: Lakers moneyline = +120 (implied 45.5%)
- FanDuel: Lakers moneyline = +110 (implied 47.6%)
- Average sportsbook: 46.5%
- Edge: 1.5% (sportsbooks usually more accurate)

### Why Sportsbooks Are Often More Accurate

1. **Billions in volume** — More information aggregated
2. **Professional bettors** — Sharps keep lines efficient
3. **Decades of data** — Sophisticated models
4. **Real money at stake** — Strong incentive for accuracy

### Implementation

```python
class StatisticalEdge(BaseStrategy):
    """
    Find mispriced markets by comparing to sportsbook consensus.
    """
    
    def __init__(self, config: dict):
        self.min_edge = Decimal(config.get("min_edge", "0.05"))  # 5%
        self.confidence_threshold = config.get("confidence_threshold", 0.7)
        self.sportsbook_weights = {
            "pinnacle": 1.5,    # Sharp book, highest weight
            "draftkings": 1.0,
            "fanduel": 1.0,
            "betmgm": 0.8,
            "caesars": 0.8,
        }
        
    def on_odds_update(self, odds_data: dict) -> Optional[List[Signal]]:
        """
        Called when sportsbook odds update.
        Compare to Polymarket and generate signals.
        """
        signals = []
        
        for game_id, odds in odds_data.items():
            # Get corresponding Polymarket market
            market = self.state_manager.get_market_for_game(game_id)
            if not market:
                continue
            
            # Calculate consensus sportsbook probability
            consensus_prob = self._calculate_consensus(odds)
            if consensus_prob is None:
                continue
            
            # Polymarket implied probability
            polymarket_prob = market.yes_ask  # Cost to buy YES
            
            # Calculate edge
            edge = consensus_prob - polymarket_prob
            
            # Confidence based on sportsbook agreement
            confidence = self._calculate_confidence(odds)
            
            if abs(edge) > self.min_edge and confidence > self.confidence_threshold:
                if edge > 0:
                    # Polymarket underpricing - buy YES
                    signals.append(Signal(
                        market_slug=market.market_slug,
                        action="BUY_YES",
                        price=market.yes_ask,
                        quantity=self._size_for_edge(edge, confidence),
                        urgency="LOW",  # Not time-sensitive
                        strategy_name="statistical_edge",
                        confidence=confidence,
                        reason=f"Stats edge: consensus={consensus_prob:.2%}, PM={polymarket_prob:.2%}"
                    ))
                else:
                    # Polymarket overpricing - buy NO
                    signals.append(Signal(
                        market_slug=market.market_slug,
                        action="BUY_NO",
                        price=market.no_ask,
                        quantity=self._size_for_edge(abs(edge), confidence),
                        urgency="LOW",
                        strategy_name="statistical_edge",
                        confidence=confidence,
                        reason=f"Stats edge: consensus={consensus_prob:.2%}, PM={polymarket_prob:.2%}"
                    ))
        
        return signals if signals else None
    
    def _calculate_consensus(self, odds: dict) -> Optional[Decimal]:
        """
        Calculate weighted average probability from sportsbooks.
        """
        total_weight = Decimal("0")
        weighted_sum = Decimal("0")
        
        for book, book_odds in odds.items():
            weight = Decimal(str(self.sportsbook_weights.get(book, 0.5)))
            prob = self._american_to_probability(book_odds)
            
            if prob is not None:
                weighted_sum += prob * weight
                total_weight += weight
        
        if total_weight == 0:
            return None
        
        return weighted_sum / total_weight
    
    def _american_to_probability(self, american_odds: int) -> Decimal:
        """
        Convert American odds to implied probability.
        +150 means bet $100 to win $150 → probability = 100/250 = 40%
        -150 means bet $150 to win $100 → probability = 150/250 = 60%
        """
        if american_odds > 0:
            prob = 100 / (american_odds + 100)
        else:
            prob = abs(american_odds) / (abs(american_odds) + 100)
        
        return Decimal(str(round(prob, 4)))
    
    def _calculate_confidence(self, odds: dict) -> float:
        """
        Higher confidence when sportsbooks agree.
        """
        probs = [self._american_to_probability(o) for o in odds.values() if o is not None]
        
        if len(probs) < 2:
            return 0.5
        
        # Standard deviation of probabilities
        mean = sum(probs) / len(probs)
        variance = sum((p - mean) ** 2 for p in probs) / len(probs)
        std_dev = float(variance ** Decimal("0.5"))
        
        # Lower std dev = higher confidence
        # std_dev of 0.01 (1%) = high confidence
        # std_dev of 0.05 (5%) = low confidence
        confidence = max(0.3, min(1.0, 1.0 - std_dev * 10))
        
        return confidence
```

---

## Strategy Interaction & Priority

When multiple strategies generate signals simultaneously:

```python
class SignalAggregator:
    """
    Combine and prioritize signals from all strategies.
    """
    
    PRIORITY_ORDER = {
        "live_arbitrage": 1,    # Highest - time sensitive
        "statistical_edge": 2,
        "market_maker": 3,      # Lowest - can wait
    }
    
    def aggregate(self, all_signals: List[Signal]) -> List[Signal]:
        """
        Deduplicate, prioritize, and return final signal list.
        """
        # Group by market
        by_market = defaultdict(list)
        for signal in all_signals:
            by_market[signal.market_slug].append(signal)
        
        final_signals = []
        
        for market_slug, signals in by_market.items():
            # Sort by priority and confidence
            signals.sort(key=lambda s: (
                self.PRIORITY_ORDER.get(s.strategy_name, 99),
                -s.confidence
            ))
            
            # Take highest priority signal for each action type
            seen_actions = set()
            for signal in signals:
                if signal.action not in seen_actions:
                    final_signals.append(signal)
                    seen_actions.add(signal.action)
        
        return final_signals
```

---

## Expected Performance

### Paper Trading Targets (First 2 Weeks)

| Metric | Target |
|--------|--------|
| Sharpe Ratio | > 1.5 |
| Win Rate | > 52% |
| Max Drawdown | < 10% |
| Daily Trades | 20-50 |

### Live Trading Targets (After Validation)

| Metric | Conservative | Aggressive |
|--------|--------------|------------|
| Monthly Return | 5-10% | 15-30% |
| Max Drawdown | 10% | 20% |
| Win Rate | 53-55% | 50-52% |

**Note:** Higher returns come with higher variance. Start conservative.

---

## Backtesting Recommendations

Before live trading, backtest each strategy:

1. **Market Making:** Simulate with historical order book data
2. **Live Arbitrage:** Replay historical games with delayed price data
3. **Statistical Edge:** Compare historical sportsbook lines to Polymarket

**Data Sources:**
- Polymarket historical: Request from their team or scrape
- Sportsbook odds: OpticOdds historical data
- Game data: NBA API, ESPN API
