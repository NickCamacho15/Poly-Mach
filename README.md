# Polymarket US Trading Bot

Automated trading bot for Polymarket US sports prediction markets.

## Status
🚧 Under Development

## Deployment

This repo supports two production run modes:

- **Systemd + Python venv (recommended for this project plan)**: see `deploy/systemd/` and `deploy/aws/`.
- **Docker Compose**: use `Dockerfile` + `docker-compose.yml`.

Detailed AWS background is in `docs/05_INFRASTRUCTURE.md`.

Minimum environment variables:

- `PM_API_KEY_ID`
- `PM_PRIVATE_KEY`

Market selection:

- **Auto-discovery (default)**: leave `MARKET_SLUGS` empty and set `LEAGUES` / `MARKET_TYPES`.
- **Manual**: set `MARKET_SLUGS` to a comma-separated list of market slugs.

Common optional settings:

- `TRADING_MODE`, `INITIAL_BALANCE`
- Risk: `RISK_MAX_POSITION_PER_MARKET`, `RISK_MAX_PORTFOLIO_EXPOSURE`,
  `RISK_MAX_DAILY_LOSS`, `RISK_KELLY_FRACTION`, `RISK_MIN_EDGE`,
  `RISK_MIN_TRADE_SIZE`, `RISK_MAX_CORRELATED_EXPOSURE`,
  `RISK_MAX_POSITIONS`, `RISK_MAX_DRAWDOWN_PCT`
- Logging: `LOG_LEVEL`, `LOG_FILE`, `LOG_JSON`
- Health: `HEALTH_HOST`, `HEALTH_PORT`
