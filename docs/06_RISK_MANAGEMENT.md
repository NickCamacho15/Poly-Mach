# Risk Management

## Overview

Risk management is the difference between consistent profits and blowing up your account. This document covers:

1. Position sizing with Kelly Criterion
2. Exposure limits
3. Circuit breakers
4. Correlation management
5. Implementation code

---

## Position Sizing: Kelly Criterion

### The Formula

The Kelly Criterion calculates the optimal bet size to maximize long-term growth:

```
f* = (p × b - q) / b

Where:
f* = Fraction of bankroll to bet
p  = Probability of winning
q  = Probability of losing (1 - p)
b  = Odds received (net profit if win / stake)
```

### Example Calculation

**Scenario:**
- Your model says YES has 60% chance of being correct
- Current YES price is $0.50
- If you win, you get $1.00 (profit of $0.50 on $0.50 stake)

```
p = 0.60 (your edge)
q = 0.40
b = 0.50 / 0.50 = 1.0 (even money)

f* = (0.60 × 1.0 - 0.40) / 1.0
f* = 0.20 (20% of bankroll)
```

### Fractional Kelly (IMPORTANT)

**Never use full Kelly.** It's mathematically optimal but assumes perfect edge estimation. In practice, use:

| Fraction | Volatility | Recommended For |
|----------|------------|-----------------|
| Full Kelly (1.0) | Extreme | Never in practice |
| Half Kelly (0.5) | High | Aggressive traders |
| Quarter Kelly (0.25) | Medium | **Recommended** |
| Eighth Kelly (0.125) | Low | Conservative |

**Why Quarter Kelly:**
- Reduces variance by 75% vs full Kelly
- Only reduces expected growth by 6%
- Provides cushion for edge estimation errors

### Implementation

```python
from decimal import Decimal
from typing import Optional
from dataclasses import dataclass


@dataclass
class EdgeEstimate:
    """Estimated edge for a trade."""
    probability: Decimal  # Our estimated true probability
    confidence: float     # Confidence in our estimate (0-1)
    

class KellyPositionSizer:
    """
    Position sizing using Kelly Criterion.
    """
    
    def __init__(
        self,
        kelly_fraction: Decimal = Decimal("0.25"),  # Quarter Kelly
        max_position_pct: Decimal = Decimal("0.10"),  # Max 10% of bankroll
        min_edge: Decimal = Decimal("0.02")  # Minimum 2% edge required
    ):
        self.kelly_fraction = kelly_fraction
        self.max_position_pct = max_position_pct
        self.min_edge = min_edge
        
    def calculate_position_size(
        self,
        bankroll: Decimal,
        market_price: Decimal,
        edge: EdgeEstimate
    ) -> Optional[Decimal]:
        """
        Calculate position size for a trade.
        
        Args:
            bankroll: Total available capital
            market_price: Current market price (e.g., 0.50)
            edge: Our edge estimate
            
        Returns:
            Position size in dollars, or None if no trade
        """
        # Calculate implied edge
        implied_edge = edge.probability - market_price
        
        # Check minimum edge threshold
        if abs(implied_edge) < self.min_edge:
            return None
        
        # Calculate Kelly parameters
        if implied_edge > 0:
            # Buying: bet that price should be higher
            p = edge.probability
            b = (Decimal("1") - market_price) / market_price  # Profit ratio
        else:
            # Selling/Shorting: bet that price should be lower
            p = Decimal("1") - edge.probability
            b = market_price / (Decimal("1") - market_price)
        
        q = Decimal("1") - p
        
        # Kelly formula
        if b <= 0:
            return None
        
        kelly = (p * b - q) / b
        
        # Apply fractional Kelly
        kelly = kelly * self.kelly_fraction
        
        # Apply confidence discount
        kelly = kelly * Decimal(str(edge.confidence))
        
        # Enforce maximum position
        kelly = min(kelly, self.max_position_pct)
        
        # Calculate dollar amount
        position_size = bankroll * kelly
        
        # Don't take negative positions (the math says don't trade)
        if position_size <= 0:
            return None
        
        return position_size
    
    def calculate_contracts(
        self,
        position_size: Decimal,
        price: Decimal
    ) -> int:
        """Convert dollar position to number of contracts."""
        return int(position_size / price)


# Usage example
sizer = KellyPositionSizer(
    kelly_fraction=Decimal("0.25"),
    max_position_pct=Decimal("0.10"),
    min_edge=Decimal("0.02")
)

bankroll = Decimal("1000.00")
market_price = Decimal("0.45")
edge = EdgeEstimate(
    probability=Decimal("0.52"),  # We think it's 52%
    confidence=0.8  # 80% confident
)

position = sizer.calculate_position_size(bankroll, market_price, edge)
if position:
    contracts = sizer.calculate_contracts(position, market_price)
    print(f"Bet ${position:.2f} ({contracts} contracts)")
```

---

## Exposure Limits

### Per-Market Limits

| Limit | Value | Reason |
|-------|-------|--------|
| Max position per market | 10% of bankroll | Single market shouldn't dominate |
| Max open orders per market | 2 (1 bid, 1 ask) | Avoid order confusion |
| Max daily volume per market | 50% of bankroll | Limit overtrading |

### Portfolio-Level Limits

| Limit | Value | Reason |
|-------|-------|--------|
| Max total exposure | 50% of bankroll | Keep dry powder |
| Max correlated exposure | 25% of bankroll | Don't bet the same thing twice |
| Max positions | 10 | Focus on quality |

### Implementation

```python
from decimal import Decimal
from typing import Dict, List
from dataclasses import dataclass


@dataclass 
class ExposureConfig:
    """Risk limit configuration."""
    max_position_per_market: Decimal = Decimal("100.00")  # $100
    max_portfolio_exposure: Decimal = Decimal("500.00")   # $500
    max_correlated_exposure: Decimal = Decimal("250.00")  # $250
    max_positions: int = 10


class ExposureMonitor:
    """
    Monitors and enforces exposure limits.
    """
    
    def __init__(self, config: ExposureConfig, bankroll: Decimal):
        self.config = config
        self.bankroll = bankroll
        self.positions: Dict[str, Decimal] = {}  # market_slug -> exposure
        self.correlations: Dict[str, List[str]] = {}  # correlation groups
        
    def can_open_position(
        self,
        market_slug: str,
        amount: Decimal,
        correlation_group: str = None
    ) -> tuple[bool, str]:
        """
        Check if a new position is allowed.
        
        Args:
            market_slug: Market to trade
            amount: Position size in dollars
            correlation_group: Optional correlation group (e.g., "NBA_LAKERS")
            
        Returns:
            (allowed: bool, reason: str)
        """
        # Check per-market limit
        current_exposure = self.positions.get(market_slug, Decimal("0"))
        new_exposure = current_exposure + amount
        
        if new_exposure > self.config.max_position_per_market:
            return (False, f"Exceeds per-market limit: ${new_exposure} > ${self.config.max_position_per_market}")
        
        # Check total portfolio exposure
        total_exposure = sum(self.positions.values()) + amount
        
        if total_exposure > self.config.max_portfolio_exposure:
            return (False, f"Exceeds portfolio limit: ${total_exposure} > ${self.config.max_portfolio_exposure}")
        
        # Check number of positions
        if market_slug not in self.positions and len(self.positions) >= self.config.max_positions:
            return (False, f"Max positions reached: {self.config.max_positions}")
        
        # Check correlated exposure
        if correlation_group:
            correlated_markets = self.correlations.get(correlation_group, [])
            correlated_exposure = sum(
                self.positions.get(m, Decimal("0"))
                for m in correlated_markets
            ) + amount
            
            if correlated_exposure > self.config.max_correlated_exposure:
                return (False, f"Exceeds correlated limit: ${correlated_exposure}")
        
        return (True, "OK")
    
    def update_position(self, market_slug: str, exposure: Decimal):
        """Update position exposure."""
        if exposure <= 0:
            self.positions.pop(market_slug, None)
        else:
            self.positions[market_slug] = exposure
            
    def add_correlation(self, group: str, markets: List[str]):
        """Define a correlation group."""
        self.correlations[group] = markets
        
    def get_summary(self) -> Dict:
        """Get exposure summary."""
        total = sum(self.positions.values())
        return {
            "total_exposure": float(total),
            "exposure_pct": float(total / self.bankroll * 100),
            "num_positions": len(self.positions),
            "available": float(self.config.max_portfolio_exposure - total)
        }
```

---

## Circuit Breakers

### Types of Circuit Breakers

1. **Daily Loss Limit:** Stop all trading if daily losses exceed threshold
2. **Drawdown Limit:** Stop if total account value drops below threshold
3. **Error Rate Limit:** Stop if too many API errors
4. **Latency Limit:** Stop if execution latency spikes

### Implementation

```python
from decimal import Decimal
from datetime import datetime, date
from typing import Optional
from enum import Enum
import structlog

logger = structlog.get_logger()


class CircuitState(Enum):
    OPEN = "OPEN"      # Trading allowed
    TRIPPED = "TRIPPED"  # Trading halted


class CircuitBreaker:
    """
    Emergency stop mechanism for the trading bot.
    """
    
    def __init__(
        self,
        daily_loss_limit: Decimal,
        max_drawdown_pct: Decimal,
        max_error_rate: float = 0.1,
        max_latency_ms: int = 5000
    ):
        self.daily_loss_limit = daily_loss_limit
        self.max_drawdown_pct = max_drawdown_pct
        self.max_error_rate = max_error_rate
        self.max_latency_ms = max_latency_ms
        
        self.state = CircuitState.OPEN
        self.trip_reason: Optional[str] = None
        self.trip_time: Optional[datetime] = None
        
        # Tracking
        self.starting_balance: Decimal = Decimal("0")
        self.high_water_mark: Decimal = Decimal("0")
        self.today: date = date.today()
        self.daily_pnl: Decimal = Decimal("0")
        
        # Error tracking
        self.request_count: int = 0
        self.error_count: int = 0
        
        # Latency tracking
        self.recent_latencies: list = []
        
    def initialize(self, balance: Decimal):
        """Initialize with starting balance."""
        self.starting_balance = balance
        self.high_water_mark = balance
        
    def can_trade(self) -> tuple[bool, Optional[str]]:
        """
        Check if trading is allowed.
        
        Returns:
            (can_trade: bool, reason: Optional[str])
        """
        if self.state == CircuitState.TRIPPED:
            return (False, self.trip_reason)
        return (True, None)
    
    def check_daily_loss(self, current_pnl: Decimal):
        """Check daily loss limit."""
        # Reset if new day
        if date.today() != self.today:
            self.today = date.today()
            self.daily_pnl = Decimal("0")
        
        self.daily_pnl = current_pnl
        
        if self.daily_pnl < -self.daily_loss_limit:
            self._trip(f"Daily loss limit exceeded: ${-self.daily_pnl:.2f}")
            
    def check_drawdown(self, current_balance: Decimal):
        """Check max drawdown."""
        # Update high water mark
        if current_balance > self.high_water_mark:
            self.high_water_mark = current_balance
        
        # Calculate drawdown
        drawdown = (self.high_water_mark - current_balance) / self.high_water_mark
        
        if drawdown > self.max_drawdown_pct:
            self._trip(f"Max drawdown exceeded: {drawdown:.1%}")
            
    def record_request(self, success: bool):
        """Record API request result."""
        self.request_count += 1
        if not success:
            self.error_count += 1
        
        # Check error rate (over last 100 requests)
        if self.request_count >= 100:
            error_rate = self.error_count / self.request_count
            if error_rate > self.max_error_rate:
                self._trip(f"High error rate: {error_rate:.1%}")
            
            # Reset counters
            self.request_count = 0
            self.error_count = 0
            
    def record_latency(self, latency_ms: int):
        """Record request latency."""
        self.recent_latencies.append(latency_ms)
        
        # Keep last 50 latencies
        if len(self.recent_latencies) > 50:
            self.recent_latencies.pop(0)
        
        # Check if latency spiking
        if len(self.recent_latencies) >= 10:
            avg_latency = sum(self.recent_latencies[-10:]) / 10
            if avg_latency > self.max_latency_ms:
                self._trip(f"High latency: {avg_latency:.0f}ms average")
                
    def _trip(self, reason: str):
        """Trip the circuit breaker."""
        self.state = CircuitState.TRIPPED
        self.trip_reason = reason
        self.trip_time = datetime.utcnow()
        
        logger.critical(
            "CIRCUIT BREAKER TRIPPED",
            reason=reason,
            time=self.trip_time.isoformat()
        )
        
    def reset(self):
        """Manually reset the circuit breaker."""
        self.state = CircuitState.OPEN
        self.trip_reason = None
        self.trip_time = None
        logger.info("Circuit breaker reset")
        
    def get_status(self) -> dict:
        """Get circuit breaker status."""
        return {
            "state": self.state.value,
            "trip_reason": self.trip_reason,
            "trip_time": self.trip_time.isoformat() if self.trip_time else None,
            "daily_pnl": float(self.daily_pnl),
            "error_rate": self.error_count / max(self.request_count, 1),
            "avg_latency": sum(self.recent_latencies) / max(len(self.recent_latencies), 1)
        }
```

---

## Correlation Management

### Why It Matters

If you have positions in:
- "Lakers vs Celtics - Lakers win"
- "Lakers vs Celtics - Over 220 points"
- "Lakers season wins over 45"

These are **correlated** - a Lakers injury affects all three. Without correlation management, you could have 3x more exposure than intended.

### Correlation Groups

```python
# Example correlation groups for NBA
CORRELATION_GROUPS = {
    # Same game - highest correlation
    "game_lakers_celtics_2025_01_25": [
        "nba-lakers-vs-celtics-2025-01-25",
        "nba-lakers-vs-celtics-2025-01-25-over-220",
        "nba-lakers-vs-celtics-2025-01-25-spread-minus-5"
    ],
    
    # Same team - medium correlation
    "team_lakers": [
        "nba-lakers-vs-celtics-2025-01-25",
        "nba-lakers-vs-warriors-2025-01-27",
        "nba-lakers-season-wins-over-45"
    ],
    
    # Same division - low correlation
    "division_pacific": [
        "nba-lakers-season-wins-over-45",
        "nba-warriors-season-wins-over-50",
        "nba-clippers-season-wins-over-42"
    ]
}
```

### Implementation

```python
class CorrelationManager:
    """
    Manages correlated exposure across markets.
    """
    
    def __init__(self, max_correlated_exposure: Decimal):
        self.max_exposure = max_correlated_exposure
        self.groups: Dict[str, List[str]] = {}
        self.market_to_groups: Dict[str, List[str]] = {}
        
    def add_group(self, group_name: str, markets: List[str]):
        """Add a correlation group."""
        self.groups[group_name] = markets
        
        for market in markets:
            if market not in self.market_to_groups:
                self.market_to_groups[market] = []
            self.market_to_groups[market].append(group_name)
            
    def check_correlated_exposure(
        self,
        market_slug: str,
        new_exposure: Decimal,
        current_positions: Dict[str, Decimal]
    ) -> tuple[bool, str]:
        """
        Check if new position would exceed correlated limits.
        """
        groups = self.market_to_groups.get(market_slug, [])
        
        for group_name in groups:
            group_markets = self.groups[group_name]
            
            total_exposure = new_exposure + sum(
                current_positions.get(m, Decimal("0"))
                for m in group_markets
                if m != market_slug
            )
            
            if total_exposure > self.max_exposure:
                return (
                    False,
                    f"Exceeds correlated limit for {group_name}: ${total_exposure:.2f}"
                )
        
        return (True, "OK")
```

---

## Complete Risk Manager

Combining all components:

```python
from decimal import Decimal
from typing import Optional
from dataclasses import dataclass
import structlog

logger = structlog.get_logger()


@dataclass
class RiskConfig:
    """Complete risk configuration."""
    # Kelly
    kelly_fraction: Decimal = Decimal("0.25")
    min_edge: Decimal = Decimal("0.02")
    
    # Exposure limits
    max_position_per_market: Decimal = Decimal("50.00")
    max_portfolio_exposure: Decimal = Decimal("250.00")
    max_correlated_exposure: Decimal = Decimal("125.00")
    max_positions: int = 10
    
    # Circuit breakers
    max_daily_loss: Decimal = Decimal("25.00")
    max_drawdown_pct: Decimal = Decimal("0.15")  # 15%
    
    # Minimums
    min_trade_size: Decimal = Decimal("1.00")


class RiskManager:
    """
    Complete risk management system.
    """
    
    def __init__(self, config: RiskConfig, initial_balance: Decimal):
        self.config = config
        self.bankroll = initial_balance
        
        self.position_sizer = KellyPositionSizer(
            kelly_fraction=config.kelly_fraction,
            max_position_pct=config.max_position_per_market / initial_balance,
            min_edge=config.min_edge
        )
        
        self.exposure_monitor = ExposureMonitor(
            ExposureConfig(
                max_position_per_market=config.max_position_per_market,
                max_portfolio_exposure=config.max_portfolio_exposure,
                max_correlated_exposure=config.max_correlated_exposure,
                max_positions=config.max_positions
            ),
            initial_balance
        )
        
        self.circuit_breaker = CircuitBreaker(
            daily_loss_limit=config.max_daily_loss,
            max_drawdown_pct=config.max_drawdown_pct
        )
        self.circuit_breaker.initialize(initial_balance)
        
        self.correlation_manager = CorrelationManager(
            config.max_correlated_exposure
        )
        
    def validate_trade(
        self,
        market_slug: str,
        price: Decimal,
        edge: EdgeEstimate,
        correlation_group: Optional[str] = None
    ) -> tuple[bool, Optional[Decimal], str]:
        """
        Validate and size a potential trade.
        
        Returns:
            (approved: bool, position_size: Optional[Decimal], reason: str)
        """
        # Check circuit breaker first
        can_trade, reason = self.circuit_breaker.can_trade()
        if not can_trade:
            return (False, None, f"Circuit breaker: {reason}")
        
        # Calculate Kelly position size
        position_size = self.position_sizer.calculate_position_size(
            self.bankroll,
            price,
            edge
        )
        
        if position_size is None:
            return (False, None, "No edge or insufficient confidence")
        
        # Check minimum trade size
        if position_size < self.config.min_trade_size:
            return (False, None, f"Below minimum: ${position_size:.2f}")
        
        # Check exposure limits
        allowed, reason = self.exposure_monitor.can_open_position(
            market_slug,
            position_size,
            correlation_group
        )
        
        if not allowed:
            # Try with reduced size
            available = self.config.max_position_per_market - self.exposure_monitor.positions.get(market_slug, Decimal("0"))
            
            if available >= self.config.min_trade_size:
                position_size = available
                logger.info(
                    "Position size reduced to fit limits",
                    original=float(position_size),
                    reduced=float(available)
                )
            else:
                return (False, None, reason)
        
        # Check correlations
        if correlation_group:
            allowed, reason = self.correlation_manager.check_correlated_exposure(
                market_slug,
                position_size,
                self.exposure_monitor.positions
            )
            if not allowed:
                return (False, None, reason)
        
        return (True, position_size, "Approved")
    
    def record_trade(
        self,
        market_slug: str,
        exposure: Decimal,
        pnl: Decimal = Decimal("0")
    ):
        """Record a completed trade."""
        self.exposure_monitor.update_position(market_slug, exposure)
        self.circuit_breaker.check_daily_loss(pnl)
        self.circuit_breaker.check_drawdown(self.bankroll + pnl)
        
    def update_bankroll(self, new_balance: Decimal):
        """Update bankroll after settlements."""
        self.bankroll = new_balance
        self.circuit_breaker.check_drawdown(new_balance)
```

---

## Risk Parameters for Your $1,000 Account

| Parameter | Value | Reason |
|-----------|-------|--------|
| Kelly Fraction | 0.25 | Conservative, reduce variance |
| Max Position/Market | $50 | 5% of bankroll |
| Max Portfolio Exposure | $250 | 25% max deployed |
| Max Correlated Exposure | $125 | Don't double down |
| Max Daily Loss | $25 | 2.5% daily stop |
| Max Drawdown | 15% | $150 absolute stop |
| Min Trade Size | $1 | Avoid micro-trades |
| Min Edge | 2% | Don't trade noise |

**With these settings:**
- Worst single trade loss: ~$50
- Worst day: ~$25
- Worst drawdown before stop: ~$150
- You'd need ~40 consecutive losses to blow up (virtually impossible with proper edge)
