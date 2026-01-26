"""
Circuit breaker safety controls.

Implements:
- Daily loss limit
- Max drawdown limit (from high-water mark)
- Emergency shutdown (manual trip)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Callable, Optional

import structlog

logger = structlog.get_logger()


class CircuitBreakerError(Exception):
    """Base exception for circuit breaker errors."""


class CircuitState(str, Enum):
    OPEN = "OPEN"
    TRIPPED = "TRIPPED"


@dataclass(frozen=True)
class CircuitBreakerStatus:
    state: CircuitState
    trip_reason: Optional[str]
    trip_time: Optional[datetime]
    day: date
    day_start_equity: Decimal
    daily_pnl: Decimal
    high_water_mark: Decimal
    drawdown_pct: Decimal


class CircuitBreaker:
    """
    Emergency stop mechanism for the trading bot.

    The circuit breaker is updated with current equity (cash + mark-to-market).
    """

    def __init__(
        self,
        daily_loss_limit: Decimal,
        max_drawdown_pct: Decimal,
        *,
        date_fn: Callable[[], date] = date.today,
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if daily_loss_limit < 0:
            raise CircuitBreakerError("daily_loss_limit must be >= 0")
        if max_drawdown_pct < 0 or max_drawdown_pct > 1:
            raise CircuitBreakerError("max_drawdown_pct must be in [0, 1]")

        self.daily_loss_limit = daily_loss_limit
        self.max_drawdown_pct = max_drawdown_pct
        self._date_fn = date_fn
        self._now_fn = now_fn

        self.state = CircuitState.OPEN
        self.trip_reason: Optional[str] = None
        self.trip_time: Optional[datetime] = None

        # Tracking
        self._day: date = self._date_fn()
        self._day_start_equity: Decimal = Decimal("0")
        self._daily_pnl: Decimal = Decimal("0")
        self._high_water_mark: Decimal = Decimal("0")

        logger.info(
            "CircuitBreaker initialized",
            daily_loss_limit=float(self.daily_loss_limit),
            max_drawdown_pct=float(self.max_drawdown_pct),
        )

    def initialize(self, starting_equity: Decimal) -> None:
        """Initialize starting equity and high-water mark."""
        if starting_equity < 0:
            raise CircuitBreakerError("starting_equity must be >= 0")

        self._day = self._date_fn()
        self._day_start_equity = starting_equity
        self._daily_pnl = Decimal("0")
        self._high_water_mark = starting_equity

        logger.info(
            "CircuitBreaker initialized equity",
            starting_equity=float(starting_equity),
            day=str(self._day),
        )

    def can_trade(self) -> tuple[bool, Optional[str]]:
        """Whether trading is allowed."""
        if self.state == CircuitState.TRIPPED:
            return False, self.trip_reason
        return True, None

    def emergency_stop(self, reason: str = "Emergency stop") -> None:
        """Manually trip the breaker."""
        self._trip(reason)

    def reset(self) -> None:
        """Manually reset the breaker."""
        self.state = CircuitState.OPEN
        self.trip_reason = None
        self.trip_time = None
        logger.warning("CircuitBreaker reset")

    def update(self, current_equity: Decimal) -> None:
        """
        Update breaker state using the current equity.

        This should be called periodically (e.g., once per engine tick),
        and after executions.
        """
        if current_equity < 0:
            # Equity shouldn't be negative; don't trip automatically but log loudly.
            logger.error("CircuitBreaker update with negative equity", equity=float(current_equity))
            return

        # New day: reset daily PnL baseline.
        today = self._date_fn()
        if today != self._day:
            self._day = today
            self._day_start_equity = current_equity
            self._daily_pnl = Decimal("0")
            logger.info("CircuitBreaker day reset", day=str(self._day), equity=float(current_equity))

        # Update high-water mark.
        if current_equity > self._high_water_mark:
            self._high_water_mark = current_equity

        # Compute daily PnL and drawdown.
        self._daily_pnl = current_equity - self._day_start_equity
        drawdown_pct = Decimal("0")
        if self._high_water_mark > 0:
            drawdown_pct = (self._high_water_mark - current_equity) / self._high_water_mark

        # Enforce limits (only trip once).
        if self.state == CircuitState.TRIPPED:
            return

        if self._daily_pnl < -self.daily_loss_limit:
            self._trip(f"Daily loss limit exceeded: {self._daily_pnl:.2f}")
            return

        if drawdown_pct > self.max_drawdown_pct:
            self._trip(f"Max drawdown exceeded: {drawdown_pct:.1%}")
            return

    def status(self) -> CircuitBreakerStatus:
        """Get current breaker status."""
        drawdown_pct = Decimal("0")
        # We don't have current equity stored; report drawdown based on last update's pnl.
        if self._high_water_mark > 0:
            current_equity = self._day_start_equity + self._daily_pnl
            drawdown_pct = (self._high_water_mark - current_equity) / self._high_water_mark

        return CircuitBreakerStatus(
            state=self.state,
            trip_reason=self.trip_reason,
            trip_time=self.trip_time,
            day=self._day,
            day_start_equity=self._day_start_equity,
            daily_pnl=self._daily_pnl,
            high_water_mark=self._high_water_mark,
            drawdown_pct=drawdown_pct,
        )

    def _trip(self, reason: str) -> None:
        """Trip the circuit breaker."""
        self.state = CircuitState.TRIPPED
        self.trip_reason = reason
        self.trip_time = self._now_fn()

        logger.critical(
            "CIRCUIT BREAKER TRIPPED",
            reason=reason,
            time=self.trip_time.isoformat() if self.trip_time else None,
        )

