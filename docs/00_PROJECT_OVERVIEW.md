# Polymarket US Sports Trading Bot - Project Overview

## Executive Summary

This project builds an automated trading bot for Polymarket US, a CFTC-regulated prediction market platform focused on sports events. The bot will exploit market inefficiencies through multiple strategies: live sports arbitrage, market making, and statistical edge modeling.

**Target Performance:** Replicate the success of traders like RN1 who turned $1,000 into $2M+ on international Polymarket through systematic, mathematical trading—not prediction.

**Key Insight:** We don't need to predict who wins. We need to:
1. React to live events faster than the market adjusts
2. Provide liquidity and earn spreads
3. Find mispriced lines compared to sportsbooks

---

## Platform: Polymarket US

| Attribute | Value |
|-----------|-------|
| Regulator | CFTC (Designated Contract Market) |
| Settlement | Fiat USD (not crypto) |
| API Base URL | `https://api.polymarket.us` |
| WebSocket | `wss://api.polymarket.us/v1/ws/` |
| Authentication | Ed25519 signatures |
| Maker Fee | 0% |
| Taker Fee | 0.10% (10 basis points) |
| Current Markets | NFL, NBA, NHL, College Football |

---

## Project Goals

### Phase 1: Foundation (Week 1-2)
- [ ] Build API client with Ed25519 authentication
- [ ] Implement WebSocket connections for real-time data
- [ ] Create paper trading simulation environment
- [ ] Set up logging and monitoring

### Phase 2: Strategy Implementation (Week 3-4)
- [ ] Implement market making strategy
- [ ] Integrate sports data feed (OpticOdds or similar)
- [ ] Build arbitrage detection engine
- [ ] Implement statistical edge calculator

### Phase 3: Risk Management (Week 5)
- [ ] Position sizing with Kelly Criterion
- [ ] Daily loss limits and circuit breakers
- [ ] Portfolio exposure monitoring
- [ ] Automated hedging logic

### Phase 4: Production (Week 6+)
- [ ] Deploy to AWS
- [ ] Monitor paper trading performance
- [ ] Transition to live trading with small capital
- [ ] Scale based on results

---

## Documentation Index

| Document | Purpose |
|----------|---------|
| `01_ARCHITECTURE.md` | System design, components, data flow |
| `02_STRATEGY.md` | Trading strategies explained in detail |
| `03_API_REFERENCE.md` | Complete Polymarket US API documentation |
| `04_IMPLEMENTATION_PLAN.md` | Step-by-step build instructions for Cursor |
| `05_INFRASTRUCTURE.md` | AWS setup, deployment, monitoring |
| `06_RISK_MANAGEMENT.md` | Position sizing, loss limits, safeguards |
| `07_DATA_FEEDS.md` | Sports data provider integration |
| `08_MATH.md` | Formulas, algorithms, and calculations |

---

## Key Decisions

### Language: Python
**Rationale:** 
- Fastest path to working MVP
- User's existing expertise
- Polymarket US is centralized (no on-chain latency to optimize for)
- Can migrate hot paths to Rust later if needed

### Primary Strategy: Market Making + Live Arbitrage Hybrid
**Rationale:**
- Market making provides consistent returns with 0% maker fees
- Live arbitrage captures big moves during games
- Combined approach maximizes opportunity capture

### Sports Focus: NBA
**Rationale:**
- High game frequency (82 games/team/season)
- Games have frequent scoring events (arbitrage opportunities)
- High liquidity expected on Polymarket US
- Currently in-season

### Paper Trading First
**Rationale:**
- Validate strategy profitability before risking capital
- Test system reliability
- Build confidence in edge calculations
- ~2 weeks minimum before live trading

---

## Success Metrics

| Metric | Target | Measurement |
|--------|--------|-------------|
| Win Rate | >55% | Profitable trades / Total trades |
| Sharpe Ratio | >2.0 | Risk-adjusted returns |
| Max Drawdown | <15% | Largest peak-to-trough decline |
| Daily PnL | >0.5% | Consistent positive expectancy |
| System Uptime | >99% | During market hours |

---

## Risk Acknowledgments

1. **Market Risk:** Prices can move against positions
2. **Execution Risk:** Orders may not fill at expected prices
3. **Technical Risk:** System failures, API outages
4. **Regulatory Risk:** CFTC rule changes
5. **Data Risk:** Sports feed delays or errors
6. **Capital Risk:** Can lose entire starting capital

**Mitigation:** Paper trade first, strict position limits, automated stop-losses, diversification across games.

---

## Getting Started

1. Read `01_ARCHITECTURE.md` to understand the system design
2. Read `02_STRATEGY.md` to understand the trading logic
3. Follow `04_IMPLEMENTATION_PLAN.md` step-by-step in Cursor
4. Use `03_API_REFERENCE.md` as your API documentation
5. Configure risk parameters per `06_RISK_MANAGEMENT.md`

---

## Contact & Resources

- **Polymarket US Docs:** https://docs.polymarket.us
- **Developer Portal:** https://polymarket.us/developer
- **API Base:** https://api.polymarket.us
