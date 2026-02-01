"""
Tests for the Risk module (Phase 5).

Run with: pytest tests/test_risk.py -v
"""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from src.data.models import OrderIntent, OrderStatus, Side
from src.data.orderbook import OrderBookTracker
from src.execution.paper_executor import PaperExecutor
from src.execution.async_paper_executor import AsyncPaperExecutor
from src.state.state_manager import OrderState, StateManager
from src.strategies.base_strategy import Signal, SignalAction, Urgency
from src.strategies.strategy_engine import StrategyEngine
from src.risk.circuit_breaker import CircuitBreaker, CircuitState
from src.risk.exposure_monitor import ExposureConfig, ExposureMonitor
from src.risk.position_sizer import (
    EdgeEstimate,
    InvalidInputsError,
    KellyPositionSizer,
)
from src.risk.risk_manager import RiskConfig, RiskManager


class TestEdgeEstimate:
    def test_valid(self):
        e = EdgeEstimate(probability=Decimal("0.55"), confidence=Decimal("0.8"))
        assert e.probability == Decimal("0.55")
        assert e.confidence == Decimal("0.8")

    def test_invalid_probability(self):
        with pytest.raises(InvalidInputsError, match="probability must be between"):
            EdgeEstimate(probability=Decimal("1.1"), confidence=Decimal("0.5"))

    def test_invalid_confidence(self):
        with pytest.raises(InvalidInputsError, match="confidence must be between"):
            EdgeEstimate(probability=Decimal("0.55"), confidence=Decimal("-0.1"))

    def test_from_confidence_float(self):
        e = EdgeEstimate.from_confidence(probability=Decimal("0.55"), confidence=0.8)
        assert e.confidence == Decimal("0.8")


class TestKellyPositionSizer:
    def test_invalid_config(self):
        with pytest.raises(InvalidInputsError, match="kelly_fraction"):
            KellyPositionSizer(kelly_fraction=Decimal("0"))
        with pytest.raises(InvalidInputsError, match="max_position_pct"):
            KellyPositionSizer(max_position_pct=Decimal("0"))
        with pytest.raises(InvalidInputsError, match="min_edge"):
            KellyPositionSizer(min_edge=Decimal("1"))

    def test_invalid_inputs(self):
        sizer = KellyPositionSizer()
        with pytest.raises(InvalidInputsError, match="bankroll"):
            sizer.calculate_position_size(
                bankroll=Decimal("0"),
                market_price=Decimal("0.5"),
                edge=EdgeEstimate(probability=Decimal("0.6"), confidence=Decimal("1")),
            )
        with pytest.raises(InvalidInputsError, match="market_price"):
            sizer.calculate_position_size(
                bankroll=Decimal("1000"),
                market_price=Decimal("1.0"),
                edge=EdgeEstimate(probability=Decimal("0.6"), confidence=Decimal("1")),
            )

    def test_below_min_edge_returns_none(self):
        sizer = KellyPositionSizer(min_edge=Decimal("0.02"))
        # Edge = 0.51 - 0.50 = 0.01 < 0.02
        result = sizer.calculate_position_size(
            bankroll=Decimal("1000"),
            market_price=Decimal("0.50"),
            edge=EdgeEstimate(probability=Decimal("0.51"), confidence=Decimal("1")),
        )
        assert result is None

    def test_quarter_kelly_with_confidence(self):
        # Scenario from docs:
        # p=0.60, price=0.50 => b=1, full Kelly=(0.60*1 - 0.40)/1=0.20
        # quarter Kelly => 0.05, confidence=0.8 => 0.04
        sizer = KellyPositionSizer(
            kelly_fraction=Decimal("0.25"),
            max_position_pct=Decimal("0.10"),
            min_edge=Decimal("0.02"),
        )
        result = sizer.calculate_position_size(
            bankroll=Decimal("1000"),
            market_price=Decimal("0.50"),
            edge=EdgeEstimate(probability=Decimal("0.60"), confidence=Decimal("0.8")),
        )
        assert result is not None
        assert result.kelly_full == Decimal("0.2")
        assert result.kelly_adjusted == Decimal("0.04")
        assert result.notional == Decimal("40.0")
        assert result.contracts == 80  # 40 / 0.50

    def test_clamps_to_max_position_pct(self):
        # Make full Kelly large; clamp to max_position_pct
        sizer = KellyPositionSizer(
            kelly_fraction=Decimal("1.0"),
            max_position_pct=Decimal("0.10"),
            min_edge=Decimal("0.00"),
        )
        # p high, price low -> big kelly, but should clamp to 10%
        result = sizer.calculate_position_size(
            bankroll=Decimal("1000"),
            market_price=Decimal("0.10"),
            edge=EdgeEstimate(probability=Decimal("0.90"), confidence=Decimal("1")),
        )
        assert result is not None
        assert result.kelly_adjusted == Decimal("0.10")
        assert result.notional == Decimal("100.0")

    def test_contracts_from_notional(self):
        sizer = KellyPositionSizer()
        assert sizer.contracts_from_notional(Decimal("0"), Decimal("0.5")) == 0
        assert sizer.contracts_from_notional(Decimal("10"), Decimal("0.5")) == 20
        assert sizer.contracts_from_notional(Decimal("10"), Decimal("0.6")) == 16
        with pytest.raises(InvalidInputsError, match="price must be > 0"):
            sizer.contracts_from_notional(Decimal("10"), Decimal("0"))


class TestExposureMonitor:
    @pytest.fixture
    def state(self) -> StateManager:
        return StateManager(initial_balance=Decimal("1000"))

    def test_exposure_includes_open_orders(self, state: StateManager):
        # Position exposure: 100 * 0.50 = 50
        state.update_position("m1", Side.YES, 100, Decimal("0.50"))

        # Open order exposure: 40 * 0.50 = 20
        state.add_order(
            OrderState(
                order_id="o1",
                market_slug="m1",
                intent=OrderIntent.BUY_LONG,
                price=Decimal("0.50"),
                quantity=40,
                status=OrderStatus.OPEN,
            )
        )

        monitor = ExposureMonitor(
            ExposureConfig(
                max_position_per_market=Decimal("1000"),
                max_portfolio_exposure=Decimal("1000"),
                max_correlated_exposure=Decimal("1000"),
                max_positions=10,
            )
        )

        assert monitor.positions_exposure(state, "m1") == Decimal("50")
        assert monitor.open_orders_exposure(state, "m1") == Decimal("20")
        assert monitor.total_exposure(state, "m1") == Decimal("70")

    def test_per_market_limit(self, state: StateManager):
        state.update_position("m1", Side.YES, 80, Decimal("0.50"))  # 40 exposure

        monitor = ExposureMonitor(
            ExposureConfig(
                max_position_per_market=Decimal("50"),
                max_portfolio_exposure=Decimal("1000"),
                max_correlated_exposure=Decimal("1000"),
                max_positions=10,
            )
        )

        check = monitor.can_add_exposure(state, "m1", Decimal("20"))
        assert check.allowed is False
        assert check.max_additional_exposure == Decimal("10")

    def test_correlation_limit(self, state: StateManager):
        state.update_position("m1", Side.YES, 100, Decimal("0.50"))  # 50
        state.update_position("m2", Side.YES, 100, Decimal("0.50"))  # 50

        monitor = ExposureMonitor(
            ExposureConfig(
                max_position_per_market=Decimal("1000"),
                max_portfolio_exposure=Decimal("1000"),
                max_correlated_exposure=Decimal("80"),
                max_positions=10,
            )
        )
        monitor.set_correlation_group("grp", ["m1", "m2"])

        check = monitor.can_add_exposure(state, "m1", Decimal("1"))
        assert check.allowed is False


class TestCircuitBreaker:
    def test_daily_loss_trip(self):
        fixed_day = date(2026, 1, 26)
        cb = CircuitBreaker(
            daily_loss_limit=Decimal("25"),
            max_drawdown_pct=Decimal("1.0"),
            date_fn=lambda: fixed_day,
            now_fn=lambda: datetime(2026, 1, 26, tzinfo=timezone.utc),
        )
        cb.initialize(Decimal("1000"))

        cb.update(Decimal("980"))
        assert cb.can_trade()[0] is True

        cb.update(Decimal("970"))
        allowed, _ = cb.can_trade()
        assert allowed is False
        assert cb.state == CircuitState.TRIPPED

    def test_drawdown_trip(self):
        fixed_day = date(2026, 1, 26)
        cb = CircuitBreaker(
            daily_loss_limit=Decimal("1000"),
            max_drawdown_pct=Decimal("0.10"),
            date_fn=lambda: fixed_day,
            now_fn=lambda: datetime(2026, 1, 26, tzinfo=timezone.utc),
        )
        cb.initialize(Decimal("1000"))

        cb.update(Decimal("1100"))  # new high water mark
        cb.update(Decimal("990"))  # 10% drawdown, should NOT trip (strict >)
        assert cb.can_trade()[0] is True

        cb.update(Decimal("989"))  # >10% drawdown
        assert cb.can_trade()[0] is False


class TestRiskManager:
    @pytest.fixture
    def state(self) -> StateManager:
        return StateManager(initial_balance=Decimal("1000"))

    def test_kelly_resizes_buy_signal(self, state: StateManager):
        rm = RiskManager(
            RiskConfig(
                max_position_per_market=Decimal("1000"),
                max_portfolio_exposure=Decimal("1000"),
                max_daily_loss=Decimal("1000"),
                max_drawdown_pct=Decimal("1.0"),
                kelly_fraction=Decimal("0.25"),
                min_edge=Decimal("0.02"),
                min_trade_size=Decimal("1.00"),
            ),
            state=state,
        )

        signal = Signal(
            market_slug="m1",
            action=SignalAction.BUY_YES,
            price=Decimal("0.50"),
            quantity=10_000,
            urgency=Urgency.LOW,
            strategy_name="test",
            confidence=0.8,
            reason="kelly",
            metadata={"true_probability": "0.60"},
        )

        decision = rm.evaluate_signal(signal)
        assert decision.approved is True
        assert decision.signal is not None
        assert decision.signal.quantity == 80  # from docs example

    def test_exposure_reduces_signal_without_probability(self, state: StateManager):
        rm = RiskManager(
            RiskConfig(
                max_position_per_market=Decimal("50"),
                max_portfolio_exposure=Decimal("50"),
                max_daily_loss=Decimal("1000"),
                max_drawdown_pct=Decimal("1.0"),
                min_trade_size=Decimal("1.00"),
                min_edge=Decimal("0.00"),
            ),
            state=state,
        )

        # No true_probability metadata => no Kelly sizing, but exposure limits apply.
        signal = Signal(
            market_slug="m1",
            action=SignalAction.BUY_YES,
            price=Decimal("0.50"),
            quantity=1000,  # $500 notional
            urgency=Urgency.LOW,
            strategy_name="test",
            confidence=0.5,
            reason="limit",
        )

        decision = rm.evaluate_signal(signal)
        assert decision.approved is True
        assert decision.signal is not None
        assert decision.signal.quantity == 100  # $50 / 0.50

    def test_emergency_stop_blocks_trading(self, state: StateManager):
        rm = RiskManager(RiskConfig(), state=state)
        rm.circuit_breaker.emergency_stop("test")

        signal = Signal(
            market_slug="m1",
            action=SignalAction.BUY_YES,
            price=Decimal("0.50"),
            quantity=10,
            urgency=Urgency.LOW,
            strategy_name="test",
            confidence=0.5,
            reason="",
        )

        decision = rm.evaluate_signal(signal)
        assert decision.approved is False


class TestStrategyEngineRiskIntegration:
    @pytest.mark.asyncio
    async def test_engine_applies_risk_manager_resizing(self):
        state = StateManager(initial_balance=Decimal("1000"))
        orderbook = OrderBookTracker()
        executor = AsyncPaperExecutor(PaperExecutor(state, orderbook))

        market_slug = "m1"
        orderbook.update(
            market_slug,
            {
                "yes": {"bids": [["0.49", "500"]], "asks": [["0.50", "500"]]},
                "no": {"bids": [["0.50", "500"]], "asks": [["0.51", "500"]]},
            },
        )
        state.update_market(market_slug, yes_bid=Decimal("0.49"), yes_ask=Decimal("0.50"))

        rm = RiskManager(
            RiskConfig(
                max_position_per_market=Decimal("1000"),
                max_portfolio_exposure=Decimal("1000"),
                max_daily_loss=Decimal("1000"),
                max_drawdown_pct=Decimal("1.0"),
                kelly_fraction=Decimal("0.25"),
                min_edge=Decimal("0.02"),
                min_trade_size=Decimal("1.00"),
            ),
            state=state,
        )

        engine = StrategyEngine(
            state_manager=state,
            orderbook=orderbook,
            executor=executor,
            risk_manager=rm,
        )

        signal = Signal(
            market_slug=market_slug,
            action=SignalAction.BUY_YES,
            price=Decimal("0.50"),
            quantity=10_000,
            urgency=Urgency.LOW,
            strategy_name="test",
            confidence=0.8,
            reason="kelly",
            metadata={"true_probability": "0.60"},
        )

        results = await engine.execute_signals([signal])
        assert results["executed"] == 1

        pos = state.get_position(market_slug)
        assert pos is not None
        assert pos.quantity == 80

