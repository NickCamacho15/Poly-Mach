"""
Exposure monitoring and limits.

Tracks and enforces:
- Per-market exposure limits
- Total portfolio exposure limits
- Correlation-aware exposure limits (related markets)

All amounts are Decimal USD notionals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Set

import structlog

from ..state.state_manager import StateManager

logger = structlog.get_logger()


class ExposureError(Exception):
    """Base exception for exposure monitoring errors."""


@dataclass(frozen=True)
class ExposureConfig:
    """Risk limit configuration."""

    max_position_per_market: Decimal = Decimal("50.00")
    max_portfolio_exposure: Decimal = Decimal("250.00")
    max_correlated_exposure: Decimal = Decimal("125.00")
    max_positions: int = 10


@dataclass(frozen=True)
class ExposureCheckResult:
    """
    Result of an exposure check.

    Attributes:
        allowed: Whether the proposed exposure can be added.
        reason: Human-readable reason.
        max_additional_exposure: Maximum additional exposure permitted given all limits.
    """

    allowed: bool
    reason: str
    max_additional_exposure: Decimal


class ExposureMonitor:
    """
    Monitor exposure from current positions and open orders.

    We define "exposure" as USD notional committed:
    - Positions: cost basis (avg_price * quantity) as tracked by StateManager.
    - Open orders: limit_price * remaining_quantity.
    """

    def __init__(self, config: ExposureConfig):
        self.config = config

        # Correlation groups: group_name -> set of market slugs
        self._groups: Dict[str, Set[str]] = {}
        self._market_to_groups: Dict[str, Set[str]] = {}

        logger.info(
            "ExposureMonitor initialized",
            max_position_per_market=float(config.max_position_per_market),
            max_portfolio_exposure=float(config.max_portfolio_exposure),
            max_correlated_exposure=float(config.max_correlated_exposure),
            max_positions=config.max_positions,
        )

    # -------------------------------------------------------------------------
    # Correlation groups
    # -------------------------------------------------------------------------

    def set_correlation_group(self, group_name: str, markets: List[str]) -> None:
        """Define or replace a correlation group."""
        market_set = set(markets)
        self._groups[group_name] = market_set

        # Rebuild inverse mapping entries for these markets.
        for m in market_set:
            self._market_to_groups.setdefault(m, set()).add(group_name)

        logger.debug(
            "Correlation group set",
            group=group_name,
            markets=len(market_set),
        )

    def get_correlation_groups(self, market_slug: str) -> Set[str]:
        """Get correlation group names for a market."""
        return set(self._market_to_groups.get(market_slug, set()))

    # -------------------------------------------------------------------------
    # Exposure computations
    # -------------------------------------------------------------------------

    def positions_exposure(self, state: StateManager, market_slug: Optional[str] = None) -> Decimal:
        """Exposure from positions only."""
        return state.get_exposure(market_slug)

    def open_orders_exposure(self, state: StateManager, market_slug: Optional[str] = None) -> Decimal:
        """Exposure from open orders only (limit_price * remaining_quantity)."""
        total = Decimal("0")
        for order in state.get_open_orders(market_slug):
            # Remaining_quantity may be 0 if fully filled but still present; ignore.
            if order.remaining_quantity <= 0:
                continue
            total += order.price * order.remaining_quantity
        return total

    def total_exposure(self, state: StateManager, market_slug: Optional[str] = None) -> Decimal:
        """Total exposure from positions + open orders."""
        return self.positions_exposure(state, market_slug) + self.open_orders_exposure(state, market_slug)

    def num_positions(self, state: StateManager) -> int:
        """Number of open positions (not including open orders)."""
        return len(state.get_all_positions())

    def _correlated_exposure(self, state: StateManager, group_name: str) -> Decimal:
        """Compute exposure across markets in a correlation group."""
        markets = self._groups.get(group_name, set())
        if not markets:
            return Decimal("0")
        total = Decimal("0")
        for m in markets:
            total += self.total_exposure(state, m)
        return total

    # -------------------------------------------------------------------------
    # Checks
    # -------------------------------------------------------------------------

    def can_add_exposure(
        self,
        state: StateManager,
        market_slug: str,
        additional_exposure: Decimal,
    ) -> ExposureCheckResult:
        """
        Check whether we can add additional exposure in a market.

        Args:
            state: StateManager containing current positions and open orders.
            market_slug: Market to add exposure to.
            additional_exposure: Proposed USD notional to add (must be >= 0).
        """
        if additional_exposure < 0:
            raise ExposureError("additional_exposure must be >= 0")
        if additional_exposure == 0:
            return ExposureCheckResult(True, "OK", Decimal("0"))

        # Max number of positions (only matters if we'd create a new position).
        has_position = state.get_position(market_slug) is not None
        if not has_position and self.num_positions(state) >= self.config.max_positions:
            return ExposureCheckResult(
                False,
                f"Max positions reached: {self.config.max_positions}",
                Decimal("0"),
            )

        current_market = self.total_exposure(state, market_slug)
        current_total = self.total_exposure(state)

        # Compute tightest allowed additional exposure across all constraints.
        max_additional = self.config.max_portfolio_exposure - current_total
        max_additional = min(max_additional, self.config.max_position_per_market - current_market)

        # Correlation constraints: apply the most restrictive group for this market.
        for group_name in self.get_correlation_groups(market_slug):
            current_group = self._correlated_exposure(state, group_name)
            max_additional = min(max_additional, self.config.max_correlated_exposure - current_group)

        if max_additional <= 0:
            return ExposureCheckResult(False, "Exposure limits reached", Decimal("0"))

        if additional_exposure > max_additional:
            return ExposureCheckResult(
                False,
                "Proposed exposure exceeds limits; reduce size",
                max_additional,
            )

        return ExposureCheckResult(True, "OK", max_additional)


