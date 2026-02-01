"""
Minimal health check server for container monitoring.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import structlog
from aiohttp import web

from ..strategies.strategy_engine import StrategyEngine
from .metrics import FeedMonitor, MetricsRegistry

logger = structlog.get_logger()


async def _health_handler(request: web.Request) -> web.Response:
    data = {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}
    feed_monitor: FeedMonitor | None = request.app.get("feed_monitor")
    metrics: MetricsRegistry | None = request.app.get("metrics")
    engine: StrategyEngine | None = request.app.get("engine")
    executor = request.app.get("executor")

    if feed_monitor is not None:
        data["feeds"] = feed_monitor.snapshot()
    if metrics is not None:
        data["metrics"] = metrics.snapshot()
    if engine is not None:
        data["engine"] = engine.get_metrics()
    if executor is not None:
        # Handle both PaperExecutor and LiveExecutor
        perf = executor.get_performance()
        # PaperExecutor returns object with to_dict(), LiveExecutor returns dict
        if hasattr(perf, 'to_dict'):
            perf_data = perf.to_dict()
            data["paper_performance"] = perf_data
            data["trading_mode"] = "paper"
        else:
            perf_data = perf
            data["live_performance"] = perf_data
            data["trading_mode"] = "live"
        
        # Also include positions if available
        if hasattr(executor, 'get_positions_report'):
            data["positions"] = executor.get_positions_report()

    return web.json_response(data)


async def run_health_server(
    host: str = "0.0.0.0",
    port: int = 8080,
    *,
    feed_monitor: FeedMonitor | None = None,
    metrics: MetricsRegistry | None = None,
    engine: StrategyEngine | None = None,
    executor = None,
) -> None:
    app = web.Application()
    if feed_monitor is not None:
        app["feed_monitor"] = feed_monitor
    if metrics is not None:
        app["metrics"] = metrics
    if engine is not None:
        app["engine"] = engine
    if executor is not None:
        app["executor"] = executor
    app.router.add_get("/health", _health_handler)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, host, port)
    await site.start()

    logger.info("Health server started", host=host, port=port)

    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        logger.info("Health server stopping")
        raise
    finally:
        await runner.cleanup()
