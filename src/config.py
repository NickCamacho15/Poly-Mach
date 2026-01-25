"""
Configuration management.
"""

import os
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    # Polymarket API
    pm_api_key_id: str = Field(default="", env="PM_API_KEY_ID")
    pm_private_key: str = Field(default="", env="PM_PRIVATE_KEY")
    pm_base_url: str = "https://api.polymarket.us"
    pm_ws_url: str = "wss://api.polymarket.us/v1/ws"
    
    # Trading
    trading_mode: str = "paper"  # "paper" or "live"
    
    # Risk
    max_position_per_market: float = 50.0
    max_portfolio_exposure: float = 250.0
    max_daily_loss: float = 25.0
    kelly_fraction: float = 0.25
    
    # Optional integrations
    opticodds_api_key: str = ""
    discord_webhook: str = ""
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"  # Ignore extra env vars


settings = Settings()
