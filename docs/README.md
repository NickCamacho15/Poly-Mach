# Polymarket US Sports Trading Bot

## Quick Start for Cursor

This documentation package contains everything you need to build a high-frequency trading bot for Polymarket US sports markets.

---

## 🚀 FIRST TIME? START HERE:

**If you don't have a project folder yet, read `09_COMPLETE_SETUP_GUIDE.md` FIRST!**

It walks you through everything from creating a folder to setting up AWS.

---

## Reading Order (After Setup)

Start with these documents in order:

| # | Document | Purpose | Time |
|---|----------|---------|------|
| 1 | `00_PROJECT_OVERVIEW.md` | Understand the project goals | 5 min |
| 2 | `01_ARCHITECTURE.md` | System design and components | 15 min |
| 3 | `02_STRATEGY.md` | Trading strategies explained | 20 min |
| 4 | `08_MATH.md` | Formulas and calculations | 15 min |
| 5 | `03_API_REFERENCE.md` | Polymarket US API details | 10 min |
| 6 | `04_IMPLEMENTATION_PLAN.md` | **Build instructions** | Follow along |

Reference documents (read as needed):
- `05_INFRASTRUCTURE.md` — AWS deployment
- `06_RISK_MANAGEMENT.md` — Position sizing, limits
- `07_DATA_FEEDS.md` — Sports data integration

---

## Project Summary

**Goal:** Build an automated trading bot that profits from Polymarket US sports markets by:
1. Reacting to live game events faster than the market (arbitrage)
2. Providing liquidity and earning spreads (market making)
3. Finding mispriced lines vs sportsbooks (statistical edge)

**Starting Capital:** $1,000
**Primary Market:** NBA
**Mode:** Paper trading first, then live

---

## Key Technical Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Language | Python 3.11+ | Fast development, your expertise |
| Database | SQLite | Simple, no setup needed |
| Hosting | AWS EC2 us-east-1 | Low latency to Polymarket |
| Sports Data | ESPN (free) → OpticOdds (paid) | Start free, upgrade when profitable |

---

## Risk Parameters (For $1,000 Account)

| Parameter | Value |
|-----------|-------|
| Max position per market | $50 (5%) |
| Max total exposure | $250 (25%) |
| Max daily loss | $25 (2.5%) |
| Position sizing | Quarter Kelly |
| Minimum edge | 2% |

---

## Cursor Prompts

### Starting the Project
```
I'm building a trading bot for Polymarket US. Read the documentation in this 
folder, especially 04_IMPLEMENTATION_PLAN.md, and help me build Phase 1: 
Project Setup & Authentication.
```

### Implementing a Component
```
Implement the [WebSocket client / Strategy / Risk Manager] according to the 
spec in [document]. Follow the patterns in [existing file]. Include error 
handling and logging.
```

### Debugging
```
I'm getting [error] when [situation]. The relevant code is in [file]. 
Help me understand why and fix it.
```

---

## File Structure to Build

```
polymarket-bot/
├── src/
│   ├── main.py              # Entry point
│   ├── config.py            # Configuration
│   ├── api/                 # Polymarket API client
│   │   ├── auth.py          # Ed25519 signing
│   │   ├── client.py        # REST API
│   │   └── websocket.py     # WebSocket
│   ├── data/                # External data
│   │   └── sports_feed.py   # ESPN/OpticOdds
│   ├── strategies/          # Trading logic
│   │   ├── arbitrage.py
│   │   ├── market_making.py
│   │   └── statistical.py
│   ├── execution/           # Order management
│   │   ├── order_manager.py
│   │   └── risk_manager.py
│   └── state/               # State tracking
│       └── manager.py
├── tests/
├── config/
├── .env
└── requirements.txt
```

---

## Environment Variables Needed

Create a `.env` file:

```bash
# Polymarket US API (from polymarket.us/developer)
PM_API_KEY_ID=your-uuid-here
PM_PRIVATE_KEY=your-base64-private-key-here

# Sports Data (optional, for live arbitrage)
OPTICODDS_API_KEY=your-key-here

# Alerting (optional)
DISCORD_WEBHOOK=https://discord.com/api/webhooks/...
```

---

## Build Phases

### Phase 1: Foundation (Week 1)
- [x] Documentation ✓ (you have this)
- [ ] Project setup
- [ ] Ed25519 authentication
- [ ] Basic API client
- [ ] WebSocket connection

### Phase 2: Paper Trading (Week 2)
- [ ] State management
- [ ] Paper trading executor
- [ ] P&L tracking
- [ ] Basic logging

### Phase 3: Strategies (Week 3-4)
- [ ] Market making strategy
- [ ] Statistical edge strategy
- [ ] Live arbitrage (requires sports data)

### Phase 4: Risk & Production (Week 5-6)
- [ ] Risk manager
- [ ] Circuit breakers
- [ ] AWS deployment
- [ ] Go live with small capital

---

## Success Metrics

Before going live, your paper trading should show:

| Metric | Target |
|--------|--------|
| Sharpe Ratio | > 1.5 |
| Win Rate | > 52% |
| Max Drawdown | < 10% |
| Profitable Days | > 60% |
| Paper Trading Duration | 2+ weeks |

---

## Important Notes

1. **Tennis Markets:** You mentioned tennis, but Polymarket US documentation only shows NFL, NBA, NHL, and college football. Verify tennis availability before building tennis-specific features.

2. **Paper Trade First:** The system includes a paper trading mode. Use it for at least 2 weeks before risking real money.

3. **Start Small:** Even after paper trading, start live trading with only $100-200 until you're confident.

4. **API Limits:** Polymarket US has limits (5 WS connections, 100 markets/subscription). The architecture accounts for this.

5. **Latency Matters:** For live arbitrage, every millisecond counts. Start with the free ESPN API for paper trading, then upgrade to paid data when profitable.

---

## Getting Help

If you get stuck:

1. **API Issues:** Check `03_API_REFERENCE.md`
2. **Strategy Questions:** Check `02_STRATEGY.md`
3. **Math/Calculations:** Check `08_MATH.md`
4. **Risk Settings:** Check `06_RISK_MANAGEMENT.md`

---

## Let's Build! 🚀

Start with `04_IMPLEMENTATION_PLAN.md` Phase 1.

Good luck!
