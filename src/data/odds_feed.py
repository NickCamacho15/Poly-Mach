"""
Odds feed interfaces and mock implementation.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, Optional

import structlog

from .event_bus import EVENT_ODDS_SNAPSHOT, EventBus
from .market_discovery import League
from ..utils.metrics import FeedMonitor, MetricsRegistry

logger = structlog.get_logger()


@dataclass(frozen=True)
class OddsSnapshot:
    """
    Snapshot of sportsbook odds translated to implied probabilities.
    """

    event_id: str
    provider: str
    yes_probability: Decimal
    league: Optional[League] = None
    market_slug: Optional[str] = None
    confidence: float = 0.5
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: Optional[Dict[str, Any]] = None

    @property
    def no_probability(self) -> Decimal:
        return Decimal("1") - self.yes_probability


class OddsFeed:
    """
    Base interface for sportsbook odds feeds.
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
        return "odds_feed"

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

    async def _publish_snapshot(self, snapshot: OddsSnapshot) -> None:
        await self._event_bus.publish(EVENT_ODDS_SNAPSHOT, snapshot)
        if self._feed_monitor is not None:
            self._feed_monitor.mark_update(self.name, snapshot.updated_at)
        if self._metrics is not None:
            self._metrics.increment("odds_feed_updates")


class MockOddsFeed(OddsFeed):
    """
    Deterministic odds feed for local development and tests.
    """

    def __init__(
        self,
        event_bus: EventBus,
        market_slugs: Iterable[str],
        update_interval: float = 3.0,
        feed_monitor: Optional[FeedMonitor] = None,
        metrics: Optional[MetricsRegistry] = None,
    ) -> None:
        super().__init__(event_bus, feed_monitor=feed_monitor, metrics=metrics)
        self._market_slugs = list(market_slugs)
        self._update_interval = update_interval
        self._tick = 0
        self._running = False

    @property
    def name(self) -> str:
        return "mock_odds_feed"

    async def run(self) -> None:
        self._running = True
        logger.info("MockOddsFeed started", markets=len(self._market_slugs))
        try:
            while self._running:
                await self.emit_once()
                await asyncio.sleep(self._update_interval)
        except asyncio.CancelledError:
            logger.info("MockOddsFeed cancelled")
            raise
        finally:
            self._running = False
            logger.info("MockOddsFeed stopped")

    async def emit_once(self) -> None:
        if not self._market_slugs:
            return
        self._tick += 1
        drift = Decimal("0.01") * Decimal(str((self._tick % 5) - 2))
        base = Decimal("0.50")
        for slug in self._market_slugs:
            event_id = slug.split("-", 1)[-1]
            yes_prob = base + drift
            yes_prob = max(Decimal("0.05"), min(Decimal("0.95"), yes_prob))
            snapshot = OddsSnapshot(
                event_id=event_id,
                provider="mock",
                yes_probability=yes_prob,
                market_slug=slug,
                confidence=0.6,
                updated_at=datetime.now(timezone.utc),
            )
            await self._publish_snapshot(snapshot)

