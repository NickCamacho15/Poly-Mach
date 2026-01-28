"""
Configuration management.
"""

from decimal import Decimal

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Polymarket API
    pm_api_key_id: str = Field(default="", env="PM_API_KEY_ID")
    pm_private_key: str = Field(default="", env="PM_PRIVATE_KEY")
    pm_base_url: str = "https://api.polymarket.us"
    pm_ws_url: str = "wss://api.polymarket.us/v1/ws"
    
    # Trading
    trading_mode: str = Field(default="paper", env="TRADING_MODE")  # "paper" or "live"
    initial_balance: Decimal = Field(default=Decimal("1000.00"), env="INITIAL_BALANCE")
    
    # Market Selection
    # Option 1: Manual - set specific market slugs (comma-separated)
    market_slugs: str = Field(default="", env="MARKET_SLUGS")
    
    # Option 2: Auto-discovery (used when MARKET_SLUGS is empty)
    # The bot will automatically find and trade all active markets for these leagues
    leagues: str = Field(default="nba,cbb", env="LEAGUES")  # nba,cbb,nfl,nhl,mlb
    market_types: str = Field(default="aec", env="MARKET_TYPES")  # aec=moneyline, asc=spread, tsc=total
    min_liquidity: Decimal = Field(default=Decimal("0"), env="MIN_LIQUIDITY")  # min $ liquidity
    
    # Risk
    max_position_per_market: Decimal = Field(
        # Paper-friendly default: allow larger per-market sizing.
        default=Decimal("50.00"),
        env="RISK_MAX_POSITION_PER_MARKET",
    )
    max_portfolio_exposure: Decimal = Field(
        # Paper-friendly default: allow larger total exposure.
        default=Decimal("350.00"),
        env="RISK_MAX_PORTFOLIO_EXPOSURE",
    )
    max_portfolio_exposure_pct: Decimal = Field(
        default=Decimal("0.35"),
        env="RISK_MAX_PORTFOLIO_EXPOSURE_PCT",
    )
    max_daily_loss: Decimal = Field(
        # Paper-friendly default: avoid frequent circuit-break trips.
        default=Decimal("100.00"),
        env="RISK_MAX_DAILY_LOSS",
    )
    kelly_fraction: Decimal = Field(
        # Paper-friendly default: take more risk per edge signal.
        default=Decimal("1.00"),
        env="RISK_KELLY_FRACTION",
    )
    min_edge: Decimal = Field(default=Decimal("0.02"), env="RISK_MIN_EDGE")
    min_trade_size: Decimal = Field(default=Decimal("1.00"), env="RISK_MIN_TRADE_SIZE")
    max_correlated_exposure: Decimal = Field(
        default=Decimal("2500.00"),
        env="RISK_MAX_CORRELATED_EXPOSURE",
    )
    max_positions: int = Field(default=10, env="RISK_MAX_POSITIONS")
    max_drawdown_pct: Decimal = Field(
        # Paper-friendly default: tolerate large drawdowns before tripping.
        default=Decimal("0.10"),
        env="RISK_MAX_DRAWDOWN_PCT",
    )
    max_total_pnl_drawdown_pct_for_new_buys: Decimal = Field(
        default=Decimal("0.05"),
        env="RISK_MAX_TOTAL_PNL_DRAWDOWN_PCT_FOR_NEW_BUYS",
    )
    
    # Optional integrations
    opticodds_api_key: str = ""
    discord_webhook: str = ""

    # Strategy enable flags
    enable_live_arbitrage: bool = Field(default=False, env="ENABLE_LIVE_ARBITRAGE")
    enable_statistical_edge: bool = Field(default=False, env="ENABLE_STATISTICAL_EDGE")

    # Feed configuration (mock by default)
    use_mock_feeds: bool = Field(default=True, env="USE_MOCK_FEEDS")
    mock_sports_interval: float = Field(default=2.0, env="MOCK_SPORTS_INTERVAL")
    mock_odds_interval: float = Field(default=3.0, env="MOCK_ODDS_INTERVAL")
    feed_stale_seconds: int = Field(default=60, env="FEED_STALE_SECONDS")

    # Live arbitrage strategy tuning
    live_arb_min_edge: Decimal = Field(default=Decimal("0.03"), env="LIVE_ARB_MIN_EDGE")
    live_arb_order_size: Decimal = Field(default=Decimal("10.00"), env="LIVE_ARB_ORDER_SIZE")
    live_arb_cooldown_seconds: float = Field(default=5.0, env="LIVE_ARB_COOLDOWN_SECONDS")
    live_arb_markets: str = Field(default="", env="LIVE_ARB_MARKETS")

    # Statistical edge strategy tuning
    stat_edge_min_edge: Decimal = Field(default=Decimal("0.02"), env="STAT_EDGE_MIN_EDGE")
    stat_edge_order_size: Decimal = Field(default=Decimal("10.00"), env="STAT_EDGE_ORDER_SIZE")
    stat_edge_cooldown_seconds: float = Field(default=10.0, env="STAT_EDGE_COOLDOWN_SECONDS")
    stat_edge_markets: str = Field(default="", env="STAT_EDGE_MARKETS")

    # Logging
    log_level: str = Field(default="INFO", env="LOG_LEVEL")
    log_file: str = Field(default="logs/bot.log", env="LOG_FILE")
    log_json: bool = Field(default=False, env="LOG_JSON")

    # Health check
    health_host: str = Field(default="0.0.0.0", env="HEALTH_HOST")
    health_port: int = Field(default=8080, env="HEALTH_PORT")
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"  # Ignore extra env vars


settings = Settings()
