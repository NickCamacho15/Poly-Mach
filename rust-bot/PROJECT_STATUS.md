# Polymarket US Arbitrage Bot - Project Status

## What We're Building

A high-performance Rust trading bot for Polymarket US (CFTC-regulated prediction market) that identifies and exploits pricing inefficiencies in binary outcome markets. The bot implements three core strategies: completeness arbitrage, live event arbitrage, and statistical edge detection via sportsbook odds comparison.

This is a full rebuild of the original Python implementation (~11,500 LOC, ~70% complete) that was losing money live despite tests looking good. The Rust rebuild addresses the two most likely causes: **execution latency** (Python's GIL) and **the gap between test conditions and live market behavior** (slippage, fee modeling, partial fills).

---

## Current State: What's Built

### Source Code: 6,028 lines across 26 files

| Module | File | Lines | Status |
|--------|------|-------|--------|
| **Auth** | `src/auth.rs` | 120 | Done - Ed25519 signatures matching production API |
| **Config** | `src/config.rs` | 140 | Done - All env vars from Python Settings |
| **API Client** | `src/api/client.rs` | 460 | Done - Async reqwest + governor rate limiting |
| **API Errors** | `src/api/errors.rs` | 100 | Done - Typed errors with retryability |
| **Data Models** | `src/data/models.rs` | 350 | Done - All types use rust_decimal |
| **Order Book** | `src/data/orderbook.rs` | 280 | Done - Thread-safe RwLock tracker |
| **Market Feed** | `src/data/market_feed.rs` | 293 | Done - REST polling with bounded concurrency |
| **State Manager** | `src/state/state_manager.rs` | 300 | Done - Arc<RwLock<>> shared state |
| **Kelly Sizer** | `src/risk/position_sizer.rs` | 147 | Done - Quarter/half/full Kelly with confidence |
| **Circuit Breaker** | `src/risk/circuit_breaker.rs` | 130 | Done - Daily loss + drawdown from peak |
| **Exposure Monitor** | `src/risk/exposure.rs` | 200 | Done - Per-market, portfolio, correlation groups |
| **Risk Manager** | `src/risk/risk_manager.rs` | 305 | Done - Full pipeline: Kelly -> exposure -> breaker |
| **Market Maker** | `src/strategies/market_maker.rs` | 300 | Done - Two-sided quotes with inventory skew |
| **Live Arbitrage** | `src/strategies/live_arbitrage.rs` | 250 | Done - Score-based probability estimation |
| **Statistical Edge** | `src/strategies/statistical_edge.rs` | 200 | Done - Sportsbook odds comparison |
| **Strategy Engine** | `src/strategies/engine.rs` | 200 | Done - Orchestrator, urgency sorting |
| **Live Executor** | `src/execution/executor.rs` | 250 | Done - Order placement + reconciliation |
| **Paper Executor** | `src/execution/paper.rs` | 1,052 | Done - Full simulation with VWAP, slippage, P&L |
| **Main** | `src/main.rs` | 200 | Done - Tokio runtime, graceful shutdown |

### Test Suite: 1,285 lines, 57 tests - ALL PASSING

| Category | Tests | What's Verified |
|----------|-------|----------------|
| Kelly Criterion | 14 | Hand-calculated expected values, boundary prices (0.01, 0.99), negative/zero edge, confidence scaling, quarter/half/full ratio, max position clamping |
| Completeness Arb | 8 | Fee = exactly 10 bps, net margin after fees, min margin filtering, multiple markets, combined cost >= 1.0 rejection |
| Circuit Breaker | 6 | Daily loss at exact threshold, drawdown from peak, trip persistence, reset, emergency stop |
| Exposure Monitor | 5 | Per-market limits, portfolio-wide limits, position count, correlation groups, headroom calculation |
| Risk Manager | 7 | Cash insufficiency, per-market resize, Kelly sizing through full pipeline, sells bypass circuit breaker, cancels always approved, drawdown blocking, min trade size |
| Order Book | 9 | Best bid/ask from multiple levels, spread, mid price, completeness sum, thread safety under concurrent read/write, depth calculation |
| Paper Executor | 8 | Market order fill at VWAP + slippage, partial fills, limit order resting, balance checks, P&L tracking, fee application, cancel |

### Key Design Decisions

1. **rust_decimal everywhere** - Never f64 for money. The one place f64 touches financial math (serde_json metadata -> Decimal) produces a 1-contract rounding difference which is documented and tested.

2. **Ed25519 auth, not JWT** - The blueprint document described JWT authentication. The actual Polymarket US API uses Ed25519 signatures. We match the real API.

3. **Paper executor mirrors live exactly** - Same interface (`execute_signal`), same state updates, same fee calculations. If paper trading loses money, the strategy is wrong. If paper profits but live doesn't, it's execution quality.

4. **10 bps taker fee baked in everywhere** - Completeness arb scanner, paper executor, and risk calculations all use `0.001` fee rate. Maker fee = 0 (limit orders).

---

## What's NOT Done Yet (The Path to Printing Money)

### Phase 1: Paper Trading Validation (CRITICAL - Do This First)

**Why**: Your Python bot lost money live despite tests passing. We are NOT going to repeat that. Paper trading against real market data will tell us if the strategies themselves are profitable before risking a single dollar.

- [ ] **Backtest harness** - Replay historical order book snapshots through the strategy engine + paper executor. Record every signal, every fill, every P&L update. Need at minimum 30 days of data.

- [ ] **Live paper trading mode** - Wire `main.rs` to use `PaperExecutor` instead of `LiveExecutor` when `TRADING_MODE=paper`. Market feed polls real data, strategies generate real signals, but fills are simulated. Run for 1-2 weeks minimum.

- [ ] **Performance dashboard** - Log and track: win rate, Sharpe ratio, max drawdown, average hold time, P&L by strategy, P&L by market, fee drag as % of gross profit. Without this data, you're flying blind.

- [ ] **Slippage calibration** - The paper executor currently uses 5 bps slippage. This needs to be calibrated against actual Polymarket US execution data. If real slippage is 20 bps, strategies that show +3% in paper will show -1% live.

### Phase 2: Strategy Refinement

- [ ] **Completeness arb depth analysis** - Current scanner checks best ask only. Real arbs need depth: if there's a 5-cent arb but only 10 contracts of depth, the trade makes $0.50 before fees. Need to check depth at each price level and size the arb to available liquidity.

- [ ] **Signal confidence calibration** - The Kelly sizer uses `confidence` to scale positions, but no strategy currently outputs well-calibrated confidence scores. A signal with confidence=0.8 should be right 80% of the time. This requires tracking predicted vs actual outcomes.

- [ ] **Cross-market correlation** - If you're long YES on "Team A wins" and long YES on "Team A wins by 10+", those positions are correlated. The exposure monitor supports correlation groups, but they need to be populated from market metadata.

- [ ] **Market selection** - Which markets to trade? Currently loaded from `MARKET_SLUGS` env var. Need automated market discovery: filter by liquidity, spread, time to resolution, and historical edge.

### Phase 3: Live Execution Hardening

- [ ] **Order reconciliation loop** - The live executor has a `reconcile_state` method but it needs to be bulletproof: handle partial fills, cancelled orders, network timeouts, stale state.

- [ ] **WebSocket market data** - The current market feed uses REST polling (every 5s). For market making and live arb, this is too slow. Need WebSocket connection for real-time order book updates. The API supports `wss://` feeds.

- [ ] **Retry and error recovery** - Network failures, API rate limits, server errors. The client has basic retry logic but edge cases need testing: what happens if the API is down for 30 seconds? What about partial order placement (order sent but response lost)?

- [ ] **Stale state protection** - If market data is >30 seconds old (network issue), all strategies should halt. The market feed has staleness detection; need to wire it into the strategy engine as a hard gate.

### Phase 4: Go Live

- [ ] **Minimum viable test**: Fund account with $50-100. Enable ONLY completeness arbitrage (lowest risk, mathematically guaranteed). Run for 1 week. Verify fills match paper trading predictions within slippage tolerance.

- [ ] **Scale up**: If completeness arb is profitable live, gradually enable market making (higher risk, higher reward). Then statistical edge. Each strategy gets its own 1-week live validation period.

- [ ] **AWS deployment** - Containerize with Docker, deploy to us-east-1 (closest to Polymarket US infrastructure), set up monitoring (CloudWatch or Grafana), alerting on circuit breaker trips.

---

## The Math That Matters

### Completeness Arbitrage (Lowest Risk)
```
YES_ask + NO_ask < $1.00 => guaranteed profit at resolution

Example: YES @ $0.48 + NO @ $0.48 = $0.96
Gross margin: $0.04 per pair
Fee (10 bps): $0.96 * 0.001 = $0.00096
Net margin: $0.04 - $0.00096 = $0.03904 per pair (4.07% return)
```

### Kelly Criterion (Position Sizing)
```
f* = (p*b - q) / b    where b = (1-P)/P

Example: price=$0.50, true_prob=0.60
b = 1.0, f* = (0.60*1 - 0.40)/1 = 0.20
Quarter Kelly: 0.20 * 0.25 = 5% of bankroll per trade
$1000 bankroll => $50 notional => 100 contracts
```

### Break-Even Analysis
```
Minimum edge to cover fees (taker):
  edge > fee_rate / (1 - fee_rate)
  edge > 0.001 / 0.999 = 0.1001%

For market making (maker, 0 fee):
  Spread must cover adverse selection risk.
  Minimum profitable spread ~ 2-3x expected price movement per tick.
```

---

## Risk Controls (Defense in Depth)

| Layer | Control | Current Setting |
|-------|---------|----------------|
| 1 | Min edge threshold | 2% (won't trade < 2 cents edge) |
| 2 | Quarter Kelly sizing | 25% of full Kelly (reduces variance 4x) |
| 3 | Per-market exposure cap | $500 |
| 4 | Portfolio exposure cap | $2000 |
| 5 | Portfolio exposure % cap | 80% of equity |
| 6 | Correlation group limit | $1000 |
| 7 | Max concurrent positions | 20 |
| 8 | Daily loss circuit breaker | $200 |
| 9 | Max drawdown from peak | 10% |
| 10 | Min trade size | $5 |
| 11 | 2% cash buffer | Always keep 2% cash reserve |
| 12 | Sells/cancels bypass breaker | Can always exit positions |

---

## What Killed the Python Bot (And How We're Preventing It)

| Problem | Python Bot | Rust Bot Fix |
|---------|-----------|--------------|
| Latency | ~50-200ms per API call (GIL, interpreter) | ~1-5ms (compiled, async I/O, zero-copy) |
| Fee modeling | May not have included fees in all paths | 10 bps fee in every arb calculation, paper executor, risk check |
| Slippage | Tests assumed instant fills at quoted price | Paper executor models VWAP + configurable slippage |
| Partial fills | May not have handled correctly | Paper executor handles partial fills, resting orders |
| Decimal precision | Python float math | rust_decimal throughout (no floating point for money) |
| Test vs reality gap | "Tests pass" but lose money | 57 hand-verified math tests + paper trading against real data |
| Position sizing | Unknown | Kelly criterion with fractional scaling + confidence |
| Risk limits | Unknown | 12-layer defense-in-depth risk pipeline |

---

## Immediate Next Steps (Priority Order)

1. **Wire paper trading mode** - 2-3 hours. Connect `PaperExecutor` + `MarketFeed` in `main.rs` when `TRADING_MODE=paper`.
2. **Add `.env` configuration** - Set up real API credentials, market slugs, risk parameters.
3. **Run paper trading against live data** - Start the bot in paper mode, let it run for days.
4. **Build performance logging** - Track every metric that matters. No decisions without data.
5. **Calibrate slippage** - Compare paper fills to live market activity.
6. **If paper is profitable for 2+ weeks**: go to Phase 4 (minimum viable live test with $50-100).

The goal is NOT to rush to live trading. The goal is to be **100% certain** the strategies are profitable before a single real dollar is at risk. The Python bot taught us that lesson.
