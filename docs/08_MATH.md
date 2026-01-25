# Mathematical Foundations

## Overview

This document contains all the mathematical formulas, algorithms, and calculations used by the trading bot. Understanding these is crucial for tuning parameters and debugging strategies.

---

## 1. Probability Conversions

### American Odds to Probability

American odds are the standard format for US sportsbooks.

**Formula:**
```
If odds > 0 (underdog):
    probability = 100 / (odds + 100)

If odds < 0 (favorite):
    probability = |odds| / (|odds| + 100)
```

**Examples:**
```
+150 → 100 / (150 + 100) = 100 / 250 = 0.40 (40%)
-150 → 150 / (150 + 100) = 150 / 250 = 0.60 (60%)
+300 → 100 / (300 + 100) = 100 / 400 = 0.25 (25%)
-300 → 300 / (300 + 100) = 300 / 400 = 0.75 (75%)
```

**Python:**
```python
def american_to_probability(odds: int) -> float:
    if odds > 0:
        return 100 / (odds + 100)
    else:
        return abs(odds) / (abs(odds) + 100)
```

### Decimal Odds to Probability

European format, less common in US.

**Formula:**
```
probability = 1 / decimal_odds
```

**Example:**
```
2.50 → 1 / 2.50 = 0.40 (40%)
1.67 → 1 / 1.67 = 0.60 (60%)
```

### Probability to Polymarket Price

Polymarket prices are probabilities expressed as decimals (0.01 to 0.99).

```
price = probability
```

A YES share at $0.65 implies 65% probability of that outcome.

---

## 2. Edge Calculation

### Basic Edge

**Formula:**
```
edge = true_probability - market_price
```

**Example:**
```
Your model: Lakers have 55% chance to win
Polymarket: Lakers YES trading at $0.50

edge = 0.55 - 0.50 = 0.05 (5% edge)
```

### Edge After Fees

Polymarket US charges 0.10% (10 basis points) on taker orders.

**Formula:**
```
net_edge = edge - (taker_fee × entry_price)
```

**Example:**
```
edge = 5%
entry_price = $0.50
taker_fee = 0.1%

fee_cost = 0.001 × 0.50 = 0.0005 (0.05%)
net_edge = 0.05 - 0.0005 = 0.0495 (4.95%)
```

### Minimum Profitable Edge

To break even after fees:

```
min_edge > taker_fee × (entry_price + exit_price)
```

For a round-trip trade (buy and sell):
```
min_edge > 0.001 × (0.50 + 0.60) = 0.0011 (0.11%)
```

**Practical minimum:** Target edges > 2% to have meaningful profit after slippage and fees.

---

## 3. Kelly Criterion Position Sizing

### Full Kelly Formula

**Formula:**
```
f* = (p × b - q) / b

Where:
f* = Fraction of bankroll to bet
p  = Probability of winning
q  = 1 - p (probability of losing)
b  = Net odds (profit / stake if win)
```

### Calculating 'b' for Polymarket

If you buy YES at price P and it wins, you receive $1.00:

```
b = (1 - P) / P
```

**Example:**
```
Buy YES at $0.40
If win: Receive $1.00, profit = $0.60
b = 0.60 / 0.40 = 1.5
```

### Full Kelly Example

```
Scenario:
- You believe Lakers have 60% chance to win (p = 0.60)
- Polymarket YES price is $0.50 (implied 50%)
- b = (1 - 0.50) / 0.50 = 1.0

f* = (0.60 × 1.0 - 0.40) / 1.0
f* = (0.60 - 0.40) / 1.0
f* = 0.20 (20% of bankroll)
```

### Fractional Kelly (RECOMMENDED)

Full Kelly is too aggressive. Use a fraction:

| Fraction | Formula | Use Case |
|----------|---------|----------|
| Half Kelly | f*/2 | Aggressive |
| Quarter Kelly | f*/4 | **Recommended** |
| Eighth Kelly | f*/8 | Conservative |

**Quarter Kelly Example:**
```
Full Kelly = 20%
Quarter Kelly = 20% × 0.25 = 5% of bankroll
```

### Kelly with Confidence Adjustment

Adjust for uncertainty in your probability estimate:

```
adjusted_kelly = f* × kelly_fraction × confidence
```

**Example:**
```
Full Kelly = 20%
Kelly fraction = 0.25 (quarter Kelly)
Confidence = 0.80 (80% confident in estimate)

adjusted = 0.20 × 0.25 × 0.80 = 0.04 (4% of bankroll)
```

### Python Implementation

```python
from decimal import Decimal

def calculate_kelly(
    true_prob: Decimal,
    market_price: Decimal,
    kelly_fraction: Decimal = Decimal("0.25"),
    confidence: Decimal = Decimal("1.0")
) -> Decimal:
    """
    Calculate Kelly Criterion bet size.
    
    Returns fraction of bankroll to bet (0 if no edge).
    """
    if true_prob <= market_price:
        return Decimal("0")  # No edge
    
    p = true_prob
    q = Decimal("1") - p
    b = (Decimal("1") - market_price) / market_price
    
    if b <= 0:
        return Decimal("0")
    
    kelly = (p * b - q) / b
    
    if kelly <= 0:
        return Decimal("0")
    
    return kelly * kelly_fraction * confidence
```

---

## 4. Win Probability Models

### NBA In-Game Win Probability

Based on point differential and time remaining.

**Key Insight:** Each point of lead is worth approximately 2-3% win probability, but this varies with time remaining.

**Formula (simplified):**
```
win_prob = 0.5 + (point_diff / expected_swing) × 0.5

Where:
expected_swing = sqrt(time_remaining_minutes) × pace_factor
pace_factor ≈ 0.4 for NBA
```

**Python Implementation:**
```python
import math

def nba_win_probability(
    home_score: int,
    away_score: int,
    quarter: int,
    minutes_remaining: float,
    is_home: bool = True
) -> float:
    """
    Estimate win probability for NBA game.
    Returns probability that the team wins.
    """
    point_diff = home_score - away_score
    if not is_home:
        point_diff = -point_diff
    
    # Total minutes remaining
    quarters_left = 4 - quarter
    total_minutes = quarters_left * 12 + minutes_remaining
    
    if total_minutes <= 0:
        return 1.0 if point_diff > 0 else (0.5 if point_diff == 0 else 0.0)
    
    # Expected point swing (std dev of remaining score differential)
    # NBA averages about 0.4 points per minute of variance
    expected_swing = math.sqrt(total_minutes) * 2.5
    
    # Calculate probability using normal approximation
    if expected_swing == 0:
        return 1.0 if point_diff > 0 else 0.0
    
    z_score = point_diff / expected_swing
    
    # Normal CDF approximation
    prob = 0.5 * (1 + math.erf(z_score / math.sqrt(2)))
    
    # Home court advantage (~3% boost)
    if is_home:
        prob = min(prob + 0.015, 0.99)
    
    return round(prob, 4)
```

**Example Calculations:**
```
Lakers lead by 5 with 6 minutes left in 4th quarter:
- total_minutes = 6
- expected_swing = sqrt(6) × 2.5 ≈ 6.1
- z_score = 5 / 6.1 ≈ 0.82
- prob ≈ 79%

Lakers lead by 5 with 24 minutes left (end of 2nd quarter):
- total_minutes = 24
- expected_swing = sqrt(24) × 2.5 ≈ 12.2
- z_score = 5 / 12.2 ≈ 0.41
- prob ≈ 66%
```

### Score Change Impact Table

| Time Remaining | +3 Point Impact | +7 Point Impact |
|----------------|-----------------|-----------------|
| 48 min (start) | +3.6% | +8.4% |
| 24 min (half)  | +5.1% | +11.8% |
| 12 min (Q4)    | +7.2% | +16.8% |
| 6 min          | +10.2% | +23.8% |
| 2 min          | +17.7% | +41.3% |
| 30 sec         | +32.3% | +75.4% |

---

## 5. Arbitrage Detection

### Basic Arbitrage Condition

For a binary market (YES/NO), arbitrage exists when:

```
YES_price + NO_price < 1.00
```

**Example:**
```
YES ask: $0.48
NO ask: $0.50
Total: $0.98

Arbitrage profit: $1.00 - $0.98 = $0.02 (2%)
```

### Arbitrage Profit Calculation

```
profit_per_pair = 1.00 - YES_price - NO_price - fees
```

**With 0.1% taker fee on both sides:**
```
fees = 0.001 × (YES_price + NO_price)
profit = 1.00 - 0.48 - 0.50 - 0.001 × 0.98
profit = 0.02 - 0.00098
profit = 0.0190 (1.9%)
```

### Synthetic Arbitrage (RN1 Strategy)

Instead of selling YES, buy NO to create the same economic position but save on fees:

**Traditional:**
- Sell YES (taker fee applies)

**Synthetic:**
- Buy NO (maker order, 0% fee if you post to book)

This saves the taker fee on the "sell" side.

### Cross-Platform Arbitrage

Compare Polymarket to sportsbooks:

```
sportsbook_prob = american_to_probability(sportsbook_odds)
polymarket_prob = polymarket_price

edge = sportsbook_prob - polymarket_prob
```

**Example:**
```
Pinnacle: Lakers -150 → 60% implied
Polymarket: Lakers YES at $0.55 → 55% implied

edge = 0.60 - 0.55 = 0.05 (5%)
```

---

## 6. Expected Value (EV)

### Basic EV Formula

```
EV = (win_prob × profit_if_win) - (lose_prob × loss_if_lose)
```

**Example:**
```
Buy YES at $0.50, your estimate is 60% chance of winning
Profit if win: $0.50 (receive $1.00, paid $0.50)
Loss if lose: $0.50 (receive $0.00, paid $0.50)

EV = (0.60 × $0.50) - (0.40 × $0.50)
EV = $0.30 - $0.20
EV = $0.10 per share (20% return on $0.50)
```

### EV with Fees

```
EV = (win_prob × net_profit) - (lose_prob × loss)

net_profit = (1.00 - entry_price) - entry_fee
loss = entry_price + entry_fee
```

---

## 7. Market Making Math

### Spread Calculation

**Formula:**
```
bid = fair_value - (spread / 2)
ask = fair_value + (spread / 2)
```

**Example:**
```
Fair value: $0.55
Spread: $0.04 (4 cents)

Bid: $0.55 - $0.02 = $0.53
Ask: $0.55 + $0.02 = $0.57
```

### Market Making Profit (Both Sides Fill)

```
profit_per_roundtrip = ask_price - bid_price - fees
```

With maker orders (0% fee):
```
profit = $0.57 - $0.53 = $0.04 (4 cents per share)
```

### Inventory Risk

If only one side fills, you have directional risk:

```
max_loss = (fair_value - fill_price) × position_size
```

**Example:**
```
Bought at $0.53, fair value drops to $0.48
Loss per share = $0.53 - $0.48 = $0.05 (5 cents)
```

### Optimal Spread Width

Wider spread = fewer fills but more profit per fill
Narrower spread = more fills but less profit per fill

**Factors:**
- Market volatility (higher vol → wider spread)
- Competition (more market makers → narrower spread)
- Your risk tolerance

**Recommended:** Start with 3-4 cent spread, adjust based on fill rate.

---

## 8. Sharpe Ratio

Measure of risk-adjusted returns.

### Formula

```
Sharpe = (R - Rf) / σ

Where:
R  = Average return (daily or annualized)
Rf = Risk-free rate (often set to 0 for short-term trading)
σ  = Standard deviation of returns
```

### Annualized Sharpe

```
Annualized Sharpe = Daily Sharpe × sqrt(365)
```

### Interpretation

| Sharpe | Quality |
|--------|---------|
| < 0 | Losing strategy |
| 0-1 | Below average |
| 1-2 | Good |
| 2-3 | Very good |
| > 3 | Excellent |

**Target:** Sharpe > 2.0

---

## 9. Drawdown

### Maximum Drawdown Formula

```
Drawdown = (Peak_Value - Current_Value) / Peak_Value × 100%
```

**Example:**
```
Account peaked at $1,200
Current value: $1,050

Drawdown = ($1,200 - $1,050) / $1,200 = 12.5%
```

### Rolling Maximum Drawdown

Track the worst drawdown over a rolling window:

```python
def calculate_max_drawdown(equity_curve: list) -> float:
    """Calculate maximum drawdown from equity curve."""
    peak = equity_curve[0]
    max_dd = 0
    
    for value in equity_curve:
        if value > peak:
            peak = value
        
        drawdown = (peak - value) / peak
        max_dd = max(max_dd, drawdown)
    
    return max_dd
```

---

## 10. Order Book Analysis

### Bid-Ask Spread

```
spread = best_ask - best_bid
spread_percent = spread / mid_price × 100%

mid_price = (best_ask + best_bid) / 2
```

### Order Book Imbalance (OBI)

Predicts short-term price movement:

```
OBI = (bid_volume - ask_volume) / (bid_volume + ask_volume)
```

| OBI | Interpretation |
|-----|----------------|
| > 0.5 | Strong buying pressure, price likely to rise |
| -0.5 to 0.5 | Balanced |
| < -0.5 | Strong selling pressure, price likely to fall |

### Depth-Weighted Mid Price

Better fair value estimate than simple mid:

```
weighted_mid = (best_bid × ask_size + best_ask × bid_size) / (bid_size + ask_size)
```

---

## 11. Correlation

### Why It Matters

Correlated positions amplify risk. If you have:
- Lakers vs Celtics: Lakers YES
- Lakers season over 50 wins: YES
- Lakers vs Warriors next week: Lakers YES

All three lose together if Lakers perform poorly.

### Correlation Coefficient

```
ρ = Cov(X, Y) / (σ_X × σ_Y)
```

| ρ | Relationship |
|---|--------------|
| 1.0 | Perfect positive correlation |
| 0.5 | Moderate positive |
| 0 | No correlation |
| -0.5 | Moderate negative |
| -1.0 | Perfect negative correlation |

### Correlated Position Limit

```
max_correlated_exposure = max_single_position × (1 + correlation_count)
```

**Example:**
```
Max position per market: $50
3 correlated Lakers positions

Max total Lakers exposure: $50 × (1 + 0.5 × 2) = $100
(Using 0.5 correlation factor for related but not identical markets)
```

---

## Quick Reference Table

| Calculation | Formula | Example |
|-------------|---------|---------|
| American to Prob | 100/(odds+100) if + | +150 → 40% |
| Edge | true_prob - price | 55% - 50% = 5% |
| Kelly (full) | (p×b - q) / b | 20% |
| Kelly (quarter) | kelly × 0.25 | 5% |
| Arbitrage profit | 1 - YES - NO - fees | 2% |
| Spread profit | ask - bid | $0.04 |
| Sharpe | (R - Rf) / σ | 2.5 |
| Max drawdown | (peak - current) / peak | 12.5% |
| OBI | (bid_vol - ask_vol) / total | 0.3 |
