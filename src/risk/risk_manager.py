"""
Risk manager.

Combines:
- Position sizing (Kelly)
- Exposure monitoring (per-market / portfolio / correlation)
- Circuit breaker (daily loss / drawdown / emergency stop)

Designed to integrate with StrategyEngine by validating and potentially
modifying signals before they are executed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional, Tuple

import structlog

from ..state.state_manager import StateManager
from ..strategies.base_strategy import Signal, SignalAction
from .circuit_breaker import CircuitBreaker
from .exposure_monitor import ExposureConfig, ExposureMonitor
from .position_sizer import EdgeEstimate, KellyPositionSizer

logger = structlog.get_logger()


class RiskError(Exception):
    """Base exception for risk manager errors."""


@dataclass(frozen=True)
class RiskConfig:
    # Kelly
    kelly_fraction: Decimal = Decimal("0.25")
    min_edge: Decimal = Decimal("0.02")

    # Exposure limits
    max_position_per_market: Decimal = Decimal("50.00")
    max_portfolio_exposure: Decimal = Decimal("250.00")
    max_correlated_exposure: Decimal = Decimal("125.00")
    max_positions: int = 10
    max_portfolio_exposure_pct: Decimal = Decimal("0.35")

    # Circuit breakers
    max_daily_loss: Decimal = Decimal("25.00")
    max_drawdown_pct: Decimal = Decimal("0.15")
    max_total_pnl_drawdown_pct_for_new_buys: Decimal = Decimal("0.05")

    # Minimums
    min_trade_size: Decimal = Decimal("1.00")


@dataclass(frozen=True)
class RiskDecision:
    """
    Decision returned by RiskManager when evaluating a signal.

    Attributes:
        approved: Whether to allow execution
        signal: Possibly modified signal (e.g., reduced quantity). None if rejected.
        reason: Human-readable reason
        metadata: Optional extra info for logging/debugging
    """

    approved: bool
    signal: Optional[Signal]
    reason: str
    metadata: Optional[Dict[str, Any]] = None


class RiskManager:
    """
    Complete risk management system.

    Integration contract with StrategyEngine:
    - Call `risk_manager.evaluate_signal(signal)` before execution.
    - If approved, execute the returned (possibly modified) signal.
    - Call `risk_manager.on_state_update()` periodically to update breaker metrics.
    """

    TRUE_PROB_KEY = "true_probability"

    def __init__(self, config: RiskConfig, state: StateManager):
        self.config = config
        self.state = state

        self.position_sizer = KellyPositionSizer(
            kelly_fraction=config.kelly_fraction,
            # Convert absolute per-market dollars to % of bankroll dynamically;
            # we still clamp again using ExposureMonitor, so this is an upper bound.
            max_position_pct=Decimal("1.0"),
            min_edge=config.min_edge,
        )

        self.exposure_monitor = ExposureMonitor(
            ExposureConfig(
                max_position_per_market=config.max_position_per_market,
                max_portfolio_exposure=config.max_portfolio_exposure,
                max_correlated_exposure=config.max_correlated_exposure,
                max_positions=config.max_positions,
            )
        )

        self.circuit_breaker = CircuitBreaker(
            daily_loss_limit=config.max_daily_loss,
            max_drawdown_pct=config.max_drawdown_pct,
        )

        # Initialize breaker with current equity.
        self._starting_equity = self._current_equity()
        self.circuit_breaker.initialize(self._starting_equity)

        logger.info(
            "RiskManager initialized",
            max_position_per_market=float(config.max_position_per_market),
            max_portfolio_exposure=float(config.max_portfolio_exposure),
            max_portfolio_exposure_pct=float(config.max_portfolio_exposure_pct),
            max_daily_loss=float(config.max_daily_loss),
            kelly_fraction=float(config.kelly_fraction),
            starting_equity=float(self._starting_equity),
        )

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def set_correlation_group(self, group_name: str, markets: list[str]) -> None:
        self.exposure_monitor.set_correlation_group(group_name, markets)

    def on_state_update(self) -> None:
        """Update circuit breaker based on latest equity."""
        self.circuit_breaker.update(self._current_equity())

    def reset_starting_equity(self) -> None:
        """
        Reset the circuit breaker baseline to current equity.

        Intended for live-mode startup after an initial API sync populates
        StateManager with real balance/positions so daily loss/drawdown checks
        do not start from a blind default.
        """
        self._starting_equity = self._current_equity()
        self.circuit_breaker.initialize(self._starting_equity)
        logger.info("RiskManager starting equity reset", starting_equity=float(self._starting_equity))

    def evaluate_signal(self, signal: Signal) -> RiskDecision:
        """
        Validate and (optionally) resize a signal.

        For BUY signals:
        - If signal.metadata contains TRUE_PROB_KEY, use Kelly sizing.
        - Otherwise, treat signal.quantity as intent and only enforce limits.

        For SELL signals:
        - We don't apply Kelly sizing (selling reduces risk), but we still enforce
          circuit breaker for new risk-taking (i.e., sells always allowed unless
          breaker is tripped via emergency stop).
        """
        # Always allow cancels.
        if signal.is_cancel:
            return RiskDecision(True, signal, "Approved: cancel")

        # Update breaker before decision.
        self.on_state_update()

        can_trade, reason = self.circuit_breaker.can_trade()
        if not can_trade:
            if signal.is_sell:
                return RiskDecision(True, signal, "Approved: circuit breaker allows exits")
            return RiskDecision(False, None, f"Circuit breaker: {reason}")

        # Base sizing starts from the strategy's requested quantity.
        qty = signal.quantity
        price = signal.price

        if qty <= 0:
            return RiskDecision(False, None, "Rejected: non-positive quantity")

        sizing_info: Optional[Dict[str, Any]] = None
        if signal.is_buy and price is not None:
            available_cash = self.state.get_balance()
            cash_buffer = Decimal("0.98")
            max_affordable = (available_cash * cash_buffer) / price if price > 0 else Decimal("0")
            max_affordable_qty = int(max_affordable)
            if max_affordable_qty <= 0:
                return RiskDecision(
                    False,
                    None,
                    "Rejected: insufficient available cash",
                    {
                        "available_cash": float(available_cash),
                        "price": float(price),
                        "cash_buffer": float(cash_buffer),
                    },
                )
            if qty > max_affordable_qty:
                qty = max_affordable_qty
                sizing_info = {
                    **(sizing_info or {}),
                    "reduced_for_cash": True,
                    "available_cash": float(available_cash),
                    "max_affordable_qty": max_affordable_qty,
                    "cash_buffer": float(cash_buffer),
                }

        # Apply Kelly sizing for BUY signals only if we have a probability estimate.
        if signal.is_buy:
            true_prob = self._get_true_probability(signal)
            if true_prob is not None:
                result = self.position_sizer.calculate_position_size(
                    bankroll=self._current_equity(),
                    market_price=price,
                    edge=EdgeEstimate.from_confidence(true_prob, signal.confidence),
                )
                if result is None:
                    return RiskDecision(False, None, "Rejected: insufficient edge/confidence")

                # Respect the strategy's maximum size if it already requested smaller.
                qty = min(qty, result.contracts)
                sizing_info = {
                    **(sizing_info or {}),
                    "edge": float(result.edge),
                    "kelly_full": float(result.kelly_full),
                    "kelly_adjusted": float(result.kelly_adjusted),
                    "kelly_notional": float(result.notional),
                    "kelly_contracts": result.contracts,
                }

        # Enforce minimum trade size.
        notional = price * qty
        if notional < self.config.min_trade_size:
            return RiskDecision(False, None, f"Rejected: below min trade size ${notional:.2f}")

        # Enforce exposure limits for BUY signals (SELL reduces exposure).
        if signal.is_buy:
            if self._is_new_buy_blocked_by_drawdown():
                return RiskDecision(
                    False,
                    None,
                    "Rejected: portfolio drawdown blocks new buys",
                )

            check = self.exposure_monitor.can_add_exposure(
                state=self.state,
                market_slug=signal.market_slug,
                additional_exposure=notional,
            )

            current_total_exposure = self.exposure_monitor.total_exposure(self.state)
            max_additional_pct = Decimal("0")
            if self.config.max_portfolio_exposure_pct > 0:
                max_by_pct = (self._current_equity() * self.config.max_portfolio_exposure_pct)
                max_additional_pct = max_by_pct - current_total_exposure
                if max_additional_pct < 0:
                    max_additional_pct = Decimal("0")

            max_additional = check.max_additional_exposure
            limit_reason = check.reason if not check.allowed else "Exposure limits reached"
            if self.config.max_portfolio_exposure_pct > 0 and max_additional_pct < max_additional:
                max_additional = max_additional_pct
                limit_reason = "Portfolio exposure percent limit reached"

            if not check.allowed and max_additional <= 0:
                return RiskDecision(
                    False,
                    None,
                    f"Rejected: {check.reason}",
                    {
                        "current_total_exposure": float(current_total_exposure),
                        "equity": float(self._current_equity()),
                        "max_portfolio_exposure_pct": float(self.config.max_portfolio_exposure_pct),
                        "max_portfolio_exposure": float(self.config.max_portfolio_exposure),
                        "max_by_pct": float(max_by_pct) if self.config.max_portfolio_exposure_pct > 0 else None,
                    },
                )

            if notional > max_additional:
                # If we can reduce size, do so.
                if max_additional >= self.config.min_trade_size:
                    reduced_qty = int(max_additional / price)
                    if reduced_qty <= 0:
                        return RiskDecision(False, None, "Rejected: exposure limits")
                    qty = min(qty, reduced_qty)
                    notional = price * qty
                    sizing_info = {
                        **(sizing_info or {}),
                        "reduced_for_exposure": True,
                        "max_additional_exposure": float(max_additional),
                    }
                else:
                    return RiskDecision(
                        False,
                        None,
                        f"Rejected: {limit_reason}",
                        {
                            "current_total_exposure": float(current_total_exposure),
                            "equity": float(self._current_equity()),
                            "max_portfolio_exposure_pct": float(self.config.max_portfolio_exposure_pct),
                            "max_portfolio_exposure": float(self.config.max_portfolio_exposure),
                            "max_by_pct": float(max_by_pct) if self.config.max_portfolio_exposure_pct > 0 else None,
                        },
                    )

            # Re-check min trade size after reduction.
            if notional < self.config.min_trade_size:
                return RiskDecision(False, None, f"Rejected: below min trade size ${notional:.2f}")

        # Produce modified signal if needed.
        if qty != signal.quantity:
            signal = replace(signal, quantity=qty)
            return RiskDecision(True, signal, "Approved: resized", sizing_info)

        return RiskDecision(True, signal, "Approved", sizing_info)

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _current_equity(self) -> Decimal:
        return self.state.get_total_equity()

    def _get_true_probability(self, signal: Signal) -> Optional[Decimal]:
        if not signal.metadata:
            return None
        if self.TRUE_PROB_KEY not in signal.metadata:
            return None

        raw = signal.metadata.get(self.TRUE_PROB_KEY)
        try:
            prob = raw if isinstance(raw, Decimal) else Decimal(str(raw))
        except (InvalidOperation, ValueError):
            logger.warning(
                "Invalid true_probability in signal metadata",
                market_slug=signal.market_slug,
                strategy=signal.strategy_name,
                value=raw,
            )
            return None

        if prob < 0 or prob > 1:
            logger.warning(
                "Out-of-range true_probability in signal metadata",
                market_slug=signal.market_slug,
                strategy=signal.strategy_name,
                value=float(prob),
            )
            return None

        return prob

    def _is_new_buy_blocked_by_drawdown(self) -> bool:
        if self.config.max_total_pnl_drawdown_pct_for_new_buys <= 0:
            return False
        if self._starting_equity <= 0:
            return False
        current_equity = self._current_equity()
        drawdown_pct = (self._starting_equity - current_equity) / self._starting_equity
        return drawdown_pct >= self.config.max_total_pnl_drawdown_pct_for_new_buys