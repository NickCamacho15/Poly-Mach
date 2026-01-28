"""
Tests for main wiring helpers.
"""

from decimal import Decimal

from src.config import Settings
from src.main import build_components, build_risk_config


def test_build_risk_config_maps_settings():
    settings = Settings(
        _env_file=None,
        initial_balance=Decimal("1000.00"),
        kelly_fraction=Decimal("0.30"),
        min_edge=Decimal("0.05"),
        max_position_per_market=Decimal("55.00"),
        max_portfolio_exposure=Decimal("300.00"),
        max_correlated_exposure=Decimal("150.00"),
        max_positions=12,
        max_daily_loss=Decimal("40.00"),
        max_drawdown_pct=Decimal("0.20"),
        min_trade_size=Decimal("2.00"),
    )

    config = build_risk_config(settings)

    assert config.kelly_fraction == Decimal("0.30")
    assert config.min_edge == Decimal("0.05")
    assert config.max_position_per_market == Decimal("55.00")
    assert config.max_portfolio_exposure == Decimal("300.00")
    assert config.max_correlated_exposure == Decimal("150.00")
    assert config.max_positions == 12
    assert config.max_daily_loss == Decimal("40.00")
    assert config.max_drawdown_pct == Decimal("0.20")
    assert config.min_trade_size == Decimal("2.00")


def test_build_components_wires_risk_manager():
    settings = Settings(
        _env_file=None,
        initial_balance=Decimal("1000.00"),
    )

    components = build_components(settings)

    assert components.engine.risk_manager is components.risk_manager
    assert components.state_manager.get_balance() == Decimal("1000.00")
