"""
Sports feed interfaces and mock implementation.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterable, Optional

import structlog

from .market_discovery import League
from .event_bus import EVENT_GAME_STATE, EventBus
from ..utils.metrics import FeedMonitor, MetricsRegistry

logger = structlog.get_logger()


class GameStatus(str, Enum):
    """Status for a live game."""

    SCHEDULED = "SCHEDULED"
    IN_PROGRESS = "IN_PROGRESS"
    FINAL = "FINAL"


@dataclass(frozen=True)
class GameState:
    """
    Snapshot of a live sports game.

    This is the primary payload published to the event bus.
    """

    event_id: str
    league: Optional[League]
    home_team: str
    away_team: str
    home_score: int
    away_score: int
    period: Optional[str] = None
    clock: Optional[str] = None
    status: GameStatus = GameStatus.IN_PROGRESS
    market_slug: Optional[str] = None
    home_is_yes: bool = True
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: Optional[Dict[str, Any]] = None

    @property
    def score_diff(self) -> int:
        """Score difference (home - away)."""

        return self.home_score - self.away_score

    @property
    def is_final(self) -> bool:
        return self.status == GameStatus.FINAL


class SportsFeed:
    """
    Base interface for sports data feeds.
    """

    def __init__(
        self,
        event_bus: EventBus,
        feed_monitor: Optional[FeedMonitor] = None,
        metrics: Optional[MetricsRegistry] = None,
    ) -> None:
        self._event_bus = event_bus
        self._feed_monitor = feed_monitor
        self._metrics = metrics
        self._task: Optional[asyncio.Task] = None

    @property
    def name(self) -> str:
        return "sports_feed"

    def start(self) -> asyncio.Task:
        if self._task and not self._task.done():
            return self._task
        self._task = asyncio.create_task(self.run())
        return self._task

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def run(self) -> None:
        raise NotImplementedError

    async def _publish_state(self, state: GameState) -> None:
        await self._event_bus.publish(EVENT_GAME_STATE, state)
        if self._feed_monitor is not None:
            self._feed_monitor.mark_update(self.name, state.updated_at)
        if self._metrics is not None:
            self._metrics.increment("sports_feed_updates")


def _event_id_from_slug(slug: str) -> str:
    parts = slug.split("-")
    if len(parts) >= 6:
        return "-".join(parts[1:6])
    if len(parts) >= 2:
        return "-".join(parts[1:])
    return slug


def _teams_from_slug(slug: str) -> tuple[str, str]:
    parts = slug.split("-")
    if len(parts) >= 4:
        return parts[3].upper(), parts[2].upper()
    return "HOME", "AWAY"


class MockSportsFeed(SportsFeed):
    """
    Deterministic sports feed for local development and tests.
    """

    def __init__(
        self,
        event_bus: EventBus,
        market_slugs: Iterable[str],
        update_interval: float = 2.0,
        feed_monitor: Optional[FeedMonitor] = None,
        metrics: Optional[MetricsRegistry] = None,
    ) -> None:
        super().__init__(event_bus, feed_monitor=feed_monitor, metrics=metrics)
        self._market_slugs = list(market_slugs)
        self._update_interval = update_interval
        self._tick = 0
        self._running = False
        self._states: Dict[str, GameState] = {}

    @property
    def name(self) -> str:
        return "mock_sports_feed"

    async def run(self) -> None:
        self._running = True
        logger.info("MockSportsFeed started", markets=len(self._market_slugs))
        try:
            while self._running:
                await self.emit_once()
                await asyncio.sleep(self._update_interval)
        except asyncio.CancelledError:
            logger.info("MockSportsFeed cancelled")
            raise
        finally:
            self._running = False
            logger.info("MockSportsFeed stopped")

    async def emit_once(self) -> None:
        if not self._market_slugs:
            return
        self._tick += 1
        for slug in self._market_slugs:
            event_id = _event_id_from_slug(slug)
            home, away = _teams_from_slug(slug)
            current = self._states.get(event_id)
            if current is None:
                current = GameState(
                    event_id=event_id,
                    league=None,
                    home_team=home,
                    away_team=away,
                    home_score=0,
                    away_score=0,
                    period="Q1",
                    clock="12:00",
                    status=GameStatus.IN_PROGRESS,
                    market_slug=slug,
                    home_is_yes=True,
                )

            if self._tick % 2 == 0:
                home_score = current.home_score + 1
                away_score = current.away_score
            else:
                home_score = current.home_score
                away_score = current.away_score + 1

            updated = GameState(
                event_id=current.event_id,
                league=current.league,
                home_team=current.home_team,
                away_team=current.away_team,
                home_score=home_score,
                away_score=away_score,
                period=current.period,
                clock=current.clock,
                status=current.status,
                market_slug=current.market_slug,
                home_is_yes=current.home_is_yes,
                updated_at=datetime.now(timezone.utc),
                metadata=current.metadata,
            )
            self._states[event_id] = updated
            await self._publish_state(updated)

