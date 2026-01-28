"""
Lightweight metrics and feed liveness tracking.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Dict, Optional


class MetricsRegistry:
    """
    Simple in-memory metrics store.
    """

    def __init__(self) -> None:
        self._counters: Dict[str, int] = defaultdict(int)
        self._gauges: Dict[str, Any] = {}
        self._lock = Lock()

    def increment(self, name: str, value: int = 1) -> None:
        with self._lock:
            self._counters[name] += value

    def set_gauge(self, name: str, value: Any) -> None:
        with self._lock:
            self._gauges[name] = value

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
            }


@dataclass
class FeedStatus:
    last_update: Optional[datetime] = None
    last_payload: Optional[Dict[str, Any]] = None


class FeedMonitor:
    """
    Track feed liveness timestamps for health checks.
    """

    def __init__(self, stale_after_seconds: int = 60) -> None:
        self._stale_after_seconds = stale_after_seconds
        self._feeds: Dict[str, FeedStatus] = {}
        self._lock = Lock()

    def mark_update(
        self,
        feed_name: str,
        timestamp: Optional[datetime] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        ts = timestamp or datetime.now(timezone.utc)
        with self._lock:
            self._feeds[feed_name] = FeedStatus(last_update=ts, last_payload=payload)

    def snapshot(self) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        with self._lock:
            result: Dict[str, Any] = {}
            for name, status in self._feeds.items():
                last = status.last_update
                if last is None:
                    age = None
                else:
                    last_ts = last if last.tzinfo else last.replace(tzinfo=timezone.utc)
                    age = (now - last_ts).total_seconds()
                result[name] = {
                    "last_update": last.isoformat() if last else None,
                    "age_seconds": age,
                    "stale": age is not None and age > self._stale_after_seconds,
                }
            return result

