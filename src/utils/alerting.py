"""
Alerting helpers (Discord/Slack webhook).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import httpx
import structlog

logger = structlog.get_logger()


class AlertingClient:
    """
    Minimal webhook-based alerting client.
    """

    def __init__(self, webhook_url: str = "") -> None:
        self._webhook_url = webhook_url.strip()

    @property
    def enabled(self) -> bool:
        return bool(self._webhook_url)

    async def send(
        self,
        message: str,
        level: str = "info",
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self.enabled:
            logger.info("Alert", level=level, message=message, extra=extra)
            return

        payload = {"content": f"[{level.upper()}] {message}"}
        if extra:
            payload["embeds"] = [{"title": "Details", "description": str(extra)}]

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(self._webhook_url, json=payload)
        except Exception as exc:
            logger.warning("Alert failed", error=str(exc), level=level, message=message)
