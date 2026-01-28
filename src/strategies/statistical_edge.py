"""
Statistical edge strategy comparing sportsbook odds to Polymarket prices.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Set

import structlog
from pydantic import BaseModel, Field

from ..data.event_bus import EVENT_ODDS_SNAPSHOT, EventBus
from ..data.odds_feed import OddsSnapshot
from ..state.state_manager import MarketState
from ..utils.metrics import MetricsRegistry
from .base_strategy import BaseStrategy, Signal, SignalAction, Urgency

logger = structlog.get_logger()


class StatisticalEdgeConfig(BaseModel):
    """
    Configuration for statistical edge strategy.
    """

    min_edge: Decimal = Field(default=Decimal("0.02"))
    order_size: Decimal = Field(default=Decimal("10.00"))
    cooldown_seconds: float = Field(default=10.0)
    enabled_markets: List[str] = Field(default_factory=list)

    class Config:
        frozen = True


class StatisticalEdgeStrategy(BaseStrategy):
    """
    Generate signals when sportsbook implied probabilities diverge.
    """

    def __init__(
        self,
        config: Optional[StatisticalEdgeConfig] = None,
        event_bus: Optional[EventBus] = None,
        metrics: Optional[MetricsRegistry] = None,
        enabled: bool = True,
    ) -> None:
        super().__init__(enabled=enabled)
        self.config = config or StatisticalEdgeConfig()
        self._event_bus = event_bus
        self._metrics = metrics
        self._queue: Optional[asyncio.Queue] = None
        self._listener_task: Optional[asyncio.Task] = None
        self._pending_markets: Set[str] = set()
        self._latest_odds: Dict[str, OddsSnapshot] = {}
        self._last_signal_at: Dict[str, datetime] = {}

    @property
    def name(self) -> str:
        return "statistical_edge"

    def start(self) -> None:
        super().start()
        if self._event_bus is None:
            logger.warning("StatisticalEdgeStrategy missing event bus", strategy=self.name)
            return
        if self._listener_task and not self._listener_task.done():
            return
        self._listener_task = asyncio.create_task(self._listen_for_updates())

    def stop(self) -> None:
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
        super().stop()

    def on_market_update(self, market: MarketState) -> List[Signal]:
        return []

    def on_tick(self) -> List[Signal]:
        if not self.enabled:
            return []

        now = datetime.now(timezone.utc)
        pending = list(self._pending_markets)
        self._pending_markets.clear()

        signals: List[Signal] = []
        for key in pending:
            snapshot = self._latest_odds.get(key)
            if snapshot is None:
                continue
            market_slug = self._resolve_market_slug(snapshot)
            if not market_slug:
                continue
            if not self._is_market_enabled(market_slug):
                continue
            last_signal = self._last_signal_at.get(market_slug)
            if last_signal:
                elapsed = (now - last_signal).total_seconds()
                if elapsed < self.config.cooldown_seconds:
                    continue

            market = self.get_market(market_slug)
            if market is None:
                continue

            signal = self._generate_signal(market, snapshot)
            if signal:
                signals.append(signal)
                self._last_signal_at[market_slug] = now

        if signals and self._metrics is not None:
            self._metrics.increment("statistical_edge_signals", len(signals))
        return signals

    def ingest_odds_snapshot(self, snapshot: OddsSnapshot) -> None:
        key = snapshot.market_slug or snapshot.event_id
        self._latest_odds[key] = snapshot
        self._pending_markets.add(key)

    async def _listen_for_updates(self) -> None:
        if self._event_bus is None:
            return
        self._queue = await self._event_bus.subscribe(EVENT_ODDS_SNAPSHOT)
        try:
            while True:
                snapshot = await self._queue.get()
                if isinstance(snapshot, OddsSnapshot):
                    self.ingest_odds_snapshot(snapshot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("StatisticalEdgeStrategy feed error", error=str(exc))

    def _resolve_market_slug(self, snapshot: OddsSnapshot) -> Optional[str]:
        if snapshot.market_slug:
            return snapshot.market_slug
        for market in self.get_all_markets():
            if snapshot.event_id in market.market_slug:
                return market.market_slug
        return None

    def _generate_signal(self, market: MarketState, snapshot: OddsSnapshot) -> Optional[Signal]:
        if market.yes_ask is None and market.no_ask is None:
            return None

        fair_yes = snapshot.yes_probability
        best_signal: Optional[Signal] = None
        best_edge = Decimal("0")

        if market.yes_ask is not None:
            edge_yes = fair_yes - market.yes_ask
            if edge_yes >= self.config.min_edge and edge_yes > best_edge:
                price = self.clamp_price(market.yes_ask)
                quantity = int(self.config.order_size / price)
                if quantity > 0:
                    best_edge = edge_yes
                    best_signal = self.create_signal(
                        market_slug=market.market_slug,
                        action=SignalAction.BUY_YES,
                        price=price,
                        quantity=quantity,
                        urgency=Urgency.MEDIUM,
                        confidence=snapshot.confidence,
                        reason=f"Odds edge {edge_yes:.3f} vs {snapshot.provider}",
                        metadata={"true_probability": fair_yes},
                    )

        no_ask = market.no_ask
        if no_ask is None and market.yes_bid is not None:
            no_ask = Decimal("1") - market.yes_bid
        if no_ask is not None:
            fair_no = Decimal("1") - fair_yes
            edge_no = fair_no - no_ask
            if edge_no >= self.config.min_edge and edge_no > best_edge:
                price = self.clamp_price(no_ask)
                quantity = int(self.config.order_size / price)
                if quantity > 0:
                    best_signal = self.create_signal(
                        market_slug=market.market_slug,
                        action=SignalAction.BUY_NO,
                        price=price,
                        quantity=quantity,
                        urgency=Urgency.MEDIUM,
                        confidence=snapshot.confidence,
                        reason=f"Odds edge {edge_no:.3f} vs {snapshot.provider}",
                        metadata={"true_probability": fair_no},
                    )

        return best_signal

    def _is_market_enabled(self, market_slug: str) -> bool:
        if not self.config.enabled_markets:
            return True
        return any(pattern in market_slug for pattern in self.config.enabled_markets)

