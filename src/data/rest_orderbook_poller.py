"""
REST orderbook polling fallback.

This is a safety net for when Polymarket's market-data websocket is quiet or
unavailable. It polls `/v1/market/{slug}/sides` for subscribed markets and then
feeds a synthetic MARKET_DATA message through the same handlers used by the WS.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional

import structlog

from src.api.client import PolymarketClient
from src.data.models import OrderBook

logger = structlog.get_logger()

MessageHandler = Callable[[Dict[str, Any]], Awaitable[None]]


def _orderbook_to_market_data_message(book: OrderBook) -> Dict[str, Any]:
    def side_to_lists(side) -> Dict[str, List[List[str]]]:
        bids = [[str(l.price), str(l.quantity)] for l in side.bids]
        asks = [[str(l.price), str(l.quantity)] for l in side.asks]
        return {"bids": bids, "asks": asks}

    return {
        "type": "MARKET_DATA",
        "marketSlug": book.market_slug,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "yes": side_to_lists(book.yes),
        "no": side_to_lists(book.no),
        "source": "rest_poll",
    }


@dataclass
class RestOrderbookPoller:
    client: PolymarketClient
    market_slugs: List[str]
    handlers: List[MessageHandler]
    interval_seconds: float = 5.0
    max_markets: int = 50
    concurrency: int = 5

    _running: bool = False

    async def _poll_one(self, slug: str, sem: asyncio.Semaphore) -> None:
        async with sem:
            try:
                book = await self.client.get_market_sides(slug)
            except Exception as exc:
                logger.debug("REST orderbook poll failed", market_slug=slug, error=str(exc))
                return

            try:
                msg = _orderbook_to_market_data_message(book)
                for h in self.handlers:
                    await h(msg)
            except Exception as exc:
                logger.warning("REST orderbook dispatch failed", market_slug=slug, error=str(exc))

    async def run(self) -> None:
        self._running = True

        # Basic sanity defaults
        interval = self.interval_seconds if self.interval_seconds and self.interval_seconds > 0 else 5.0
        max_markets = self.max_markets if self.max_markets and self.max_markets > 0 else 50
        concurrency = self.concurrency if self.concurrency and self.concurrency > 0 else 5

        sem = asyncio.Semaphore(concurrency)

        logger.info(
            "REST orderbook poller enabled",
            interval_seconds=interval,
            max_markets=max_markets,
            concurrency=concurrency,
            markets=len(self.market_slugs),
        )

        while self._running:
            slugs = list(self.market_slugs)[:max_markets]
            if not slugs:
                await asyncio.sleep(interval)
                continue

            tasks = [asyncio.create_task(self._poll_one(slug, sem)) for slug in slugs]
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(interval)

    def stop(self) -> None:
        self._running = False

