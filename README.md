# Polymarket US Trading Bot

Automated trading bot for Polymarket US sports prediction markets.

## Status
🚧 Under Development

## Deployment

Use the repo’s `Dockerfile` and `docker-compose.yml` to run in production.
Detailed AWS steps are in `docs/05_INFRASTRUCTURE.md`.

Minimum environment variables:

- `PM_API_KEY_ID`
- `PM_PRIVATE_KEY`
- `MARKET_SLUGS`

Common optional settings:

- `TRADING_MODE`, `INITIAL_BALANCE`
- Risk: `RISK_MAX_POSITION_PER_MARKET`, `RISK_MAX_PORTFOLIO_EXPOSURE`,
  `RISK_MAX_DAILY_LOSS`, `RISK_KELLY_FRACTION`, `RISK_MIN_EDGE`,
  `RISK_MIN_TRADE_SIZE`, `RISK_MAX_CORRELATED_EXPOSURE`,
  `RISK_MAX_POSITIONS`, `RISK_MAX_DRAWDOWN_PCT`
- Logging: `LOG_LEVEL`, `LOG_FILE`, `LOG_JSON`
- Health: `HEALTH_HOST`, `HEALTH_PORT`
