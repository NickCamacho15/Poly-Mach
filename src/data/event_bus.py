"""
Minimal async pub/sub event bus.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any, Dict, List

import structlog

logger = structlog.get_logger()

EVENT_GAME_STATE = "game_state"
EVENT_ODDS_SNAPSHOT = "odds_snapshot"


class EventBus:
    """
    Simple in-memory event bus for async pub/sub.
    """

    def __init__(self) -> None:
        self._subscribers: Dict[str, List[asyncio.Queue]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def subscribe(self, topic: str, maxsize: int = 0) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        async with self._lock:
            self._subscribers[topic].append(queue)
        logger.debug("Event bus subscribed", topic=topic, total=len(self._subscribers[topic]))
        return queue

    async def unsubscribe(self, topic: str, queue: asyncio.Queue) -> None:
        async with self._lock:
            if topic in self._subscribers and queue in self._subscribers[topic]:
                self._subscribers[topic].remove(queue)
        logger.debug("Event bus unsubscribed", topic=topic)

    async def publish(self, topic: str, payload: Any) -> int:
        async with self._lock:
            subscribers = list(self._subscribers.get(topic, []))
        delivered = 0
        for queue in subscribers:
            try:
                queue.put_nowait(payload)
                delivered += 1
            except asyncio.QueueFull:
                logger.warning("Event bus queue full", topic=topic)
        return delivered

    async def subscriber_count(self, topic: str) -> int:
        async with self._lock:
            return len(self._subscribers.get(topic, []))

