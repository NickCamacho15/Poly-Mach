"""
Position sizing utilities.

Implements Kelly Criterion-based sizing for Polymarket binary markets.

Notes:
- All money/price/probability values use Decimal.
- Confidence is accepted as float or Decimal but is applied as a Decimal
  multiplier in [0, 1].
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional, Union

import structlog

logger = structlog.get_logger()


DecimalLike = Union[Decimal, int, float, str]


class PositionSizerError(Exception):
    """Base exception for position sizing errors."""


class InvalidInputsError(PositionSizerError):
    """Raised when inputs are invalid (e.g., price out of bounds)."""


@dataclass(frozen=True)
class EdgeEstimate:
    """
    Estimated probability edge for a trade.

    Attributes:
        probability: Estimated true probability for the outcome being traded.
        confidence: Confidence in the estimate in [0, 1].
    """

    probability: Decimal
    confidence: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        if not (Decimal("0") <= self.probability <= Decimal("1")):
            raise InvalidInputsError("probability must be between 0 and 1 (inclusive)")
        if not (Decimal("0") <= self.confidence <= Decimal("1")):
            raise InvalidInputsError("confidence must be between 0 and 1 (inclusive)")

    @classmethod
    def from_confidence(
        cls,
        probability: Decimal,
        confidence: Union[float, Decimal],
    ) -> "EdgeEstimate":
        try:
            conf = confidence if isinstance(confidence, Decimal) else Decimal(str(confidence))
        except (InvalidOperation, ValueError) as e:
            raise InvalidInputsError(f"invalid confidence: {confidence!r}") from e
        return cls(probability=probability, confidence=conf)


@dataclass(frozen=True)
class PositionSizeResult:
    """
    Result of a sizing calculation.

    Attributes:
        edge: Probability edge (true_probability - market_price)
        kelly_full: Full Kelly fraction before fractional/confidence scaling
        kelly_adjusted: Final fraction after fractional Kelly + confidence and clamping
        notional: Dollar amount to allocate (bankroll * kelly_adjusted)
        contracts: Integer number of contracts implied by notional/price (floor)
    """

    edge: Decimal
    kelly_full: Decimal
    kelly_adjusted: Decimal
    notional: Decimal
    contracts: int


class KellyPositionSizer:
    """
    Kelly Criterion position sizer for binary markets.

    For a contract priced at P in (0, 1), if the outcome occurs, the payout is $1,
    so the net-odds ratio is:
        b = (1 - P) / P

    Full Kelly fraction:
        f* = (p*b - q) / b

    We apply:
    - fractional Kelly (e.g., 0.25 for quarter Kelly)
    - confidence multiplier in [0, 1]
    - clamp to [0, max_position_pct]
    """

    def __init__(
        self,
        kelly_fraction: Decimal = Decimal("0.25"),
        max_position_pct: Decimal = Decimal("0.10"),
        min_edge: Decimal = Decimal("0.02"),
    ) -> None:
        self.kelly_fraction = kelly_fraction
        self.max_position_pct = max_position_pct
        self.min_edge = min_edge

        self._validate_config()

        logger.info(
            "KellyPositionSizer initialized",
            kelly_fraction=float(self.kelly_fraction),
            max_position_pct=float(self.max_position_pct),
            min_edge=float(self.min_edge),
        )

    def _validate_config(self) -> None:
        if self.kelly_fraction <= 0 or self.kelly_fraction > 1:
            raise InvalidInputsError("kelly_fraction must be in (0, 1]")
        if self.max_position_pct <= 0 or self.max_position_pct > 1:
            raise InvalidInputsError("max_position_pct must be in (0, 1]")
        if self.min_edge < 0 or self.min_edge >= 1:
            raise InvalidInputsError("min_edge must be in [0, 1)")

    @staticmethod
    def _to_decimal(value: DecimalLike, field_name: str) -> Decimal:
        try:
            if isinstance(value, Decimal):
                return value
            return Decimal(str(value))
        except (InvalidOperation, ValueError) as e:
            raise InvalidInputsError(f"invalid {field_name}: {value!r}") from e

    def calculate_position_size(
        self,
        bankroll: Decimal,
        market_price: Decimal,
        edge: EdgeEstimate,
    ) -> Optional[PositionSizeResult]:
        """
        Calculate position sizing for a bet on the outcome whose price is market_price.

        Args:
            bankroll: Available bankroll in USD.
            market_price: Current contract price for the outcome in (0, 1).
            edge: EdgeEstimate for that same outcome.

        Returns:
            PositionSizeResult, or None if the trade should be skipped (no edge / too small).
        """
        if bankroll <= 0:
            raise InvalidInputsError("bankroll must be > 0")
        if market_price <= 0 or market_price >= 1:
            raise InvalidInputsError("market_price must be between 0 and 1 (exclusive)")

        # Edge is defined as true probability - market price.
        implied_edge = edge.probability - market_price

        # Minimum edge threshold (absolute edge as per spec; direction handled by caller).
        if abs(implied_edge) < self.min_edge:
            logger.debug(
                "PositionSizer: below min edge",
                edge=float(implied_edge),
                min_edge=float(self.min_edge),
            )
            return None

        # Kelly expects p = probability of winning this bet.
        p = edge.probability
        q = Decimal("1") - p

        # Net odds ratio for Polymarket binary payout.
        b = (Decimal("1") - market_price) / market_price
        if b <= 0:
            logger.debug("PositionSizer: non-positive odds ratio", b=float(b))
            return None

        # Full Kelly.
        kelly_full = (p * b - q) / b
        if kelly_full <= 0:
            # Even with "edge" threshold, rounding/fees/etc may still yield <= 0.
            logger.debug("PositionSizer: non-positive full Kelly", kelly=float(kelly_full))
            return None

        # Apply fractional Kelly and confidence.
        kelly_adjusted = kelly_full * self.kelly_fraction * edge.confidence

        # Clamp.
        if kelly_adjusted < 0:
            kelly_adjusted = Decimal("0")
        if kelly_adjusted > self.max_position_pct:
            kelly_adjusted = self.max_position_pct

        notional = bankroll * kelly_adjusted
        if notional <= 0:
            return None

        contracts = self.contracts_from_notional(notional=notional, price=market_price)
        if contracts <= 0:
            return None

        return PositionSizeResult(
            edge=implied_edge,
            kelly_full=kelly_full,
            kelly_adjusted=kelly_adjusted,
            notional=notional,
            contracts=contracts,
        )

    def contracts_from_notional(self, notional: Decimal, price: Decimal) -> int:
        """
        Convert a USD notional amount to integer contracts at the given price.

        Uses floor division semantics (like existing strategies) and ensures a
        non-negative integer result.
        """
        if notional <= 0:
            return 0
        if price <= 0:
            raise InvalidInputsError("price must be > 0")

        # Floor to integer number of contracts.
        try:
            qty = int(notional / price)
        except (InvalidOperation, ValueError) as e:
            raise InvalidInputsError("failed to compute contracts") from e

        return max(0, qty)

