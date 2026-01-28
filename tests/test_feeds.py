"""
Tests for event bus, mock feeds, and feed-driven strategies.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.data.event_bus import EVENT_GAME_STATE, EVENT_ODDS_SNAPSHOT, EventBus
from src.data.odds_feed import MockOddsFeed, OddsSnapshot
from src.data.sports_feed import GameState, MockSportsFeed
from src.state.state_manager import MarketState
from src.strategies.live_arbitrage import LiveArbitrageConfig, LiveArbitrageStrategy
from src.strategies.statistical_edge import StatisticalEdgeConfig, StatisticalEdgeStrategy


@pytest.mark.asyncio
async def test_event_bus_fanout():
    bus = EventBus()
    q1 = await bus.subscribe(EVENT_GAME_STATE)
    q2 = await bus.subscribe(EVENT_GAME_STATE)

    payload = {"event": "score"}
    delivered = await bus.publish(EVENT_GAME_STATE, payload)

    assert delivered == 2
    assert await q1.get() == payload
    assert await q2.get() == payload


@pytest.mark.asyncio
async def test_mock_sports_feed_emits_updates():
    bus = EventBus()
    queue = await bus.subscribe(EVENT_GAME_STATE)
    feed = MockSportsFeed(bus, ["aec-nba-lal-bos-2026-01-01"])

    await feed.emit_once()
    first = await queue.get()
    assert isinstance(first, GameState)

    await feed.emit_once()
    second = await queue.get()

    assert (second.home_score + second.away_score) == (first.home_score + first.away_score + 1)


@pytest.mark.asyncio
async def test_mock_odds_feed_emits_updates():
    bus = EventBus()
    queue = await bus.subscribe(EVENT_ODDS_SNAPSHOT)
    feed = MockOddsFeed(bus, ["aec-nba-lal-bos-2026-01-01"])

    await feed.emit_once()
    snapshot = await queue.get()
    assert isinstance(snapshot, OddsSnapshot)
    assert Decimal("0.05") <= snapshot.yes_probability <= Decimal("0.95")


def test_live_arbitrage_generates_signal():
    strategy = LiveArbitrageStrategy(
        config=LiveArbitrageConfig(min_edge=Decimal("0.01"), cooldown_seconds=0),
    )

    market = MarketState(
        market_slug="aec-nba-lal-bos-2026-01-01",
        yes_bid=Decimal("0.44"),
        yes_ask=Decimal("0.45"),
        no_bid=Decimal("0.55"),
        no_ask=Decimal("0.56"),
    )
    strategy.update_market_state(market)

    state = GameState(
        event_id="nba-lal-bos-2026-01-01",
        league=None,
        home_team="BOS",
        away_team="LAL",
        home_score=10,
        away_score=0,
        market_slug=market.market_slug,
        updated_at=datetime.now(timezone.utc),
    )
    strategy.ingest_game_state(state)

    signals = strategy.on_tick()
    assert signals
    assert signals[0].metadata is not None
    assert "true_probability" in signals[0].metadata


def test_statistical_edge_generates_signal():
    strategy = StatisticalEdgeStrategy(
        config=StatisticalEdgeConfig(min_edge=Decimal("0.01"), cooldown_seconds=0),
    )

    market = MarketState(
        market_slug="aec-nba-lal-bos-2026-01-01",
        yes_bid=Decimal("0.44"),
        yes_ask=Decimal("0.45"),
        no_bid=Decimal("0.55"),
        no_ask=Decimal("0.56"),
    )
    strategy.update_market_state(market)

    snapshot = OddsSnapshot(
        event_id="nba-lal-bos-2026-01-01",
        provider="mock",
        yes_probability=Decimal("0.60"),
        market_slug=market.market_slug,
        confidence=0.7,
    )
    strategy.ingest_odds_snapshot(snapshot)

    signals = strategy.on_tick()
    assert signals
    assert signals[0].metadata is not None
    assert "true_probability" in signals[0].metadata
