"""
Polymarket US Trading Bot - Entry Point
"""

from __future__ import annotations

import asyncio
import signal
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Set

import structlog

from src.api.auth import PolymarketAuth
from src.api.client import PolymarketClient
from src.api.websocket import Endpoint, PolymarketWebSocket, SubscriptionType
from src.config import Settings, settings
from src.data.event_bus import EventBus
from src.data.market_discovery import League, MarketDiscovery, MarketProduct
from src.data.orderbook import OrderBookTracker, create_orderbook_handler
from src.data.odds_feed import MockOddsFeed
from src.data.sports_feed import MockSportsFeed
from src.execution.paper_executor import PaperExecutor
from src.execution.async_paper_executor import AsyncPaperExecutor
from src.execution.live_executor import LiveExecutor
from src.execution.executor_protocol import ExecutorProtocol
from src.risk.risk_manager import RiskConfig, RiskManager
from src.state.state_manager import StateManager
from src.strategies.live_arbitrage import LiveArbitrageConfig, LiveArbitrageStrategy
from src.strategies.market_maker import MarketMakerStrategy
from src.strategies.statistical_edge import StatisticalEdgeConfig, StatisticalEdgeStrategy
from src.strategies.strategy_engine import StrategyEngine
from src.utils.health import run_health_server
from src.utils.logging import configure_logging
from src.utils.market_time import is_tradeable_slug
from src.utils.metrics import FeedMonitor, MetricsRegistry

logger = structlog.get_logger()

# How often to check for new markets (seconds)
MARKET_REFRESH_INTERVAL = 300  # 5 minutes


@dataclass(frozen=True)
class AppComponents:
    state_manager: StateManager
    orderbook: OrderBookTracker
    executor: ExecutorProtocol
    risk_manager: RiskManager
    engine: StrategyEngine
    event_bus: EventBus
    feed_monitor: FeedMonitor
    metrics: MetricsRegistry


def _parse_market_slugs(raw: str) -> List[str]:
    if not raw:
        return []
    return [slug.strip() for slug in raw.split(",") if slug.strip()]


def _parse_patterns(raw: str) -> List[str]:
    if not raw:
        return []
    return [pattern.strip() for pattern in raw.split(",") if pattern.strip()]


def _parse_leagues(raw: str) -> List[League]:
    """Parse comma-separated league codes into League enums."""
    leagues = []
    for code in raw.split(","):
        code = code.strip().lower()
        if code:
            try:
                leagues.append(League(code))
            except ValueError:
                logger.warning("Unknown league code", code=code)
    return leagues


def _parse_products(raw: str) -> List[MarketProduct]:
    """Parse comma-separated product codes into MarketProduct enums."""
    products = []
    for code in raw.split(","):
        code = code.strip().lower()
        if code:
            try:
                products.append(MarketProduct(code))
            except ValueError:
                logger.warning("Unknown product code", code=code)
    return products


async def discover_markets(
    client: PolymarketClient,
    leagues: List[League],
    products: Optional[List[MarketProduct]] = None,
    *,
    allow_in_game: bool = False,
) -> List[str]:
    """
    Auto-discover active markets from the Polymarket API.
    
    Args:
        client: API client
        leagues: Leagues to include (NBA, CBB, etc.)
        products: Market products to include (moneyline, spread, total)
        
    Returns:
        List of market slugs to trade
    """
    logger.info("Discovering markets", leagues=[l.value for l in leagues])
    
    try:
        response = await client._request("GET", "/v1/markets", params={
            "limit": 500,
            "closed": "false",  # Only open markets
        })
        markets_data = response.get("markets", [])
    except Exception as e:
        logger.error("Failed to fetch markets", error=str(e))
        return []
    
    discovery = MarketDiscovery()
    markets = discovery.parse_markets(markets_data)
    
    # Filter by league
    if leagues:
        markets = discovery.filter_by_leagues(markets, leagues)
    
    # Filter by product type
    if products:
        markets = discovery.filter_by_products(markets, products)
    
    # Only open markets
    markets = [m for m in markets if not m.closed]
    
    slugs = [m.slug for m in markets]
    now = datetime.now(timezone.utc)
    filtered_slugs = [s for s in slugs if is_tradeable_slug(s, now, allow_in_game=allow_in_game)]
    dropped = len(slugs) - len(filtered_slugs)
    if dropped:
        logger.info(
            "Filtered non-tradeable slugs (slug date gate)",
            dropped=dropped,
            total_before=len(slugs),
            total_after=len(filtered_slugs),
            allow_in_game=allow_in_game,
        )

    # Safety: if we filtered everything out (often after midnight UTC) and we're not
    # explicitly allowing in-game, fall back to subscribing to today's markets so
    # the bot doesn't crash-loop on "No markets found". Trading is still gated at
    # execution time by StrategyEngine unless signals opt into allow_in_game.
    if not filtered_slugs and slugs and not allow_in_game:
        logger.warning(
            "No markets after slug-date filtering; falling back to allow_in_game=True for subscriptions",
            total_before=len(slugs),
        )
        filtered_slugs = [s for s in slugs if is_tradeable_slug(s, now, allow_in_game=True)]

    slugs = filtered_slugs
    logger.info(
        "Discovered markets",
        total=len(slugs),
        leagues={l.value: len([m for m in markets if m.league == l]) for l in leagues},
    )
    
    return slugs


def build_risk_config(app_settings: Settings) -> RiskConfig:
    return RiskConfig(
        kelly_fraction=app_settings.kelly_fraction,
        min_edge=app_settings.min_edge,
        max_position_per_market=app_settings.max_position_per_market,
        max_portfolio_exposure=app_settings.max_portfolio_exposure,
        max_portfolio_exposure_pct=app_settings.max_portfolio_exposure_pct,
        max_correlated_exposure=app_settings.max_correlated_exposure,
        max_positions=app_settings.max_positions,
        max_daily_loss=app_settings.max_daily_loss,
        max_drawdown_pct=app_settings.max_drawdown_pct,
        max_total_pnl_drawdown_pct_for_new_buys=app_settings.max_total_pnl_drawdown_pct_for_new_buys,
        min_trade_size=app_settings.min_trade_size,
    )


def build_components(app_settings: Settings, client: Optional[PolymarketClient] = None) -> AppComponents:
    state_manager = StateManager(initial_balance=app_settings.initial_balance)
    orderbook = OrderBookTracker()
    
    # Use live executor if trading_mode is live and we have a client
    if app_settings.trading_mode == "live" and client is not None:
        executor = LiveExecutor(
            client,
            state_manager,
            orderbook,
            initial_balance=app_settings.initial_balance,
            settings=app_settings,
        )
        logger.info("Using LIVE executor")
    else:
        executor = AsyncPaperExecutor(PaperExecutor(state_manager, orderbook))
        logger.info("Using PAPER executor")
    risk_manager = RiskManager(build_risk_config(app_settings), state_manager)
    event_bus = EventBus()
    feed_monitor = FeedMonitor(stale_after_seconds=app_settings.feed_stale_seconds)
    metrics = MetricsRegistry()

    engine = StrategyEngine(
        state_manager=state_manager,
        orderbook=orderbook,
        executor=executor,
        risk_manager=risk_manager,
    )
    engine.add_strategy(MarketMakerStrategy())

    if app_settings.enable_live_arbitrage:
        live_config = LiveArbitrageConfig(
            min_edge=app_settings.live_arb_min_edge,
            order_size=app_settings.live_arb_order_size,
            cooldown_seconds=app_settings.live_arb_cooldown_seconds,
            enabled_markets=_parse_patterns(app_settings.live_arb_markets),
        )
        engine.add_strategy(
            LiveArbitrageStrategy(
                config=live_config,
                event_bus=event_bus,
                metrics=metrics,
            )
        )

    if app_settings.enable_statistical_edge:
        edge_config = StatisticalEdgeConfig(
            min_edge=app_settings.stat_edge_min_edge,
            order_size=app_settings.stat_edge_order_size,
            cooldown_seconds=app_settings.stat_edge_cooldown_seconds,
            enabled_markets=_parse_patterns(app_settings.stat_edge_markets),
        )
        engine.add_strategy(
            StatisticalEdgeStrategy(
                config=edge_config,
                event_bus=event_bus,
                metrics=metrics,
            )
        )

    return AppComponents(
        state_manager=state_manager,
        orderbook=orderbook,
        executor=executor,
        risk_manager=risk_manager,
        engine=engine,
        event_bus=event_bus,
        feed_monitor=feed_monitor,
        metrics=metrics,
    )


async def market_refresh_loop(
    client: PolymarketClient,
    ws: PolymarketWebSocket,
    leagues: List[League],
    products: Optional[List[MarketProduct]],
    subscribed: Set[str],
    *,
    allow_in_game: bool,
) -> None:
    """
    Periodically check for new markets and subscribe to them.
    """
    while True:
        await asyncio.sleep(MARKET_REFRESH_INTERVAL)
        
        try:
            current_slugs = await discover_markets(client, leagues, products, allow_in_game=allow_in_game)
            new_slugs = [s for s in current_slugs if s not in subscribed]
            
            if new_slugs:
                logger.info("Found new markets", count=len(new_slugs), slugs=new_slugs[:5])
                await ws.subscribe(SubscriptionType.MARKET_DATA, new_slugs)
                subscribed.update(new_slugs)
        except Exception as e:
            logger.warning("Market refresh failed", error=str(e))


async def main() -> None:
    configure_logging(
        log_level=settings.log_level,
        log_file=settings.log_file,
        log_json=settings.log_json,
    )
    logger.info("Bot starting...", mode=settings.trading_mode)

    if not settings.pm_api_key_id or not settings.pm_private_key:
        logger.error("Missing API credentials; set PM_API_KEY_ID and PM_PRIVATE_KEY")
        return

    auth = PolymarketAuth(settings.pm_api_key_id, settings.pm_private_key)
    
    # Create client for live trading
    live_client: Optional[PolymarketClient] = None
    if settings.trading_mode == "live":
        from src.api.client import PolymarketClient
        live_client = PolymarketClient(auth=auth)
        logger.info("Created API client for LIVE trading")

    components = build_components(settings, live_client)

    # Live-mode initial state sync (avoid starting blind).
    if settings.trading_mode == "live" and hasattr(components.executor, "initialize"):
        try:
            logger.info("Performing initial live state sync...")
            await components.executor.initialize()  # type: ignore[attr-defined]
            # Reset risk breaker baseline now that state has real balance/positions.
            components.risk_manager.reset_starting_equity()
            logger.info("Initial live state sync complete")
        except Exception as exc:
            logger.warning("Initial live state sync failed", error=str(exc))
    
    # Determine market slugs: manual or auto-discovery
    market_slugs: List[str] = []
    leagues: List[League] = []
    products: List[MarketProduct] = []
    
    manual_slugs = _parse_market_slugs(settings.market_slugs)
    
    if manual_slugs:
        # Use manually configured slugs
        market_slugs = manual_slugs
        logger.info("Using configured markets", count=len(market_slugs))
    else:
        # Auto-discover markets
        leagues = _parse_leagues(settings.leagues)
        products = _parse_products(settings.market_types)
        
        if not leagues:
            leagues = [League.NBA, League.CBB]  # Default to basketball
            
        logger.info(
            "Auto-discovery enabled",
            leagues=[l.value for l in leagues],
            products=[p.value for p in products] if products else "all",
        )
        
        async with PolymarketClient(auth) as client:
            allow_in_game = bool(settings.enable_live_arbitrage or settings.enable_statistical_edge)
            market_slugs = await discover_markets(client, leagues, products, allow_in_game=allow_in_game)
        
        if not market_slugs:
            logger.error("No markets found for configured leagues")
            return

    ws = PolymarketWebSocket(auth, base_url=settings.pm_ws_url)
    ws_private: Optional[PolymarketWebSocket] = None
    if settings.trading_mode == "live":
        ws_private = PolymarketWebSocket(auth, base_url=settings.pm_ws_url)

    # Wire handlers in order: state updates -> order book -> strategy engine.
    ws.on("MARKET_DATA", components.state_manager.create_market_handler())
    ws.on("MARKET_DATA", create_orderbook_handler(components.orderbook))
    ws.on("MARKET_DATA", components.engine.create_market_handler())

    # Live mode: also subscribe to private updates (fills/positions/balance).
    if ws_private is not None and settings.trading_mode == "live":
        if hasattr(components.executor, "create_order_update_handler"):
            ws_private.on("ORDER_UPDATE", components.executor.create_order_update_handler())  # type: ignore[attr-defined]
        if hasattr(components.executor, "create_position_update_handler"):
            ws_private.on("POSITION_UPDATE", components.executor.create_position_update_handler())  # type: ignore[attr-defined]
        if hasattr(components.executor, "create_balance_update_handler"):
            ws_private.on("ACCOUNT_BALANCE_UPDATE", components.executor.create_balance_update_handler())  # type: ignore[attr-defined]

    # Track subscribed markets for refresh loop
    subscribed: Set[str] = set(market_slugs)

    # ---------------------------------------------------------------------
    # Shutdown handling (SIGTERM/SIGINT)
    # ---------------------------------------------------------------------
    shutdown_event = asyncio.Event()
    shutdown_signal: Optional[str] = None

    def _handle_shutdown(sig: signal.Signals) -> None:
        nonlocal shutdown_signal
        if shutdown_signal is None:
            shutdown_signal = sig.name
        shutdown_event.set()

    try:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, _handle_shutdown, signal.SIGTERM)
        loop.add_signal_handler(signal.SIGINT, _handle_shutdown, signal.SIGINT)
    except NotImplementedError:
        # Some platforms/event loops may not support signal handlers.
        pass

    async with ws:
        await ws.connect(Endpoint.MARKETS)
        await ws.subscribe(SubscriptionType.MARKET_DATA, market_slugs)

        # If live mode, also connect the private WebSocket and subscribe.
        if ws_private is not None:
            async with ws_private:
                await ws_private.connect(Endpoint.PRIVATE)
                await ws_private.subscribe(SubscriptionType.ORDER)
                await ws_private.subscribe(SubscriptionType.POSITION)
                await ws_private.subscribe(SubscriptionType.ACCOUNT_BALANCE)

                logger.info("Bot ready", markets=len(market_slugs), mode=settings.trading_mode)

                # Build task list
                tasks = [
                    asyncio.create_task(ws.run(), name="ws_markets"),
                    asyncio.create_task(ws_private.run(), name="ws_private"),
                    asyncio.create_task(components.engine.run(), name="engine"),
                    asyncio.create_task(
                        run_health_server(
                        settings.health_host,
                        settings.health_port,
                        feed_monitor=components.feed_monitor,
                        metrics=components.metrics,
                        engine=components.engine,
                        executor=components.executor,
                        ),
                        name="health_server",
                    ),
                ]

                # Optional feed tasks for live strategies (mock by default)
                if settings.enable_live_arbitrage or settings.enable_statistical_edge:
                    if settings.use_mock_feeds:
                        if settings.enable_live_arbitrage:
                            sports_feed = MockSportsFeed(
                                components.event_bus,
                                market_slugs,
                                update_interval=settings.mock_sports_interval,
                                feed_monitor=components.feed_monitor,
                                metrics=components.metrics,
                            )
                            tasks.append(asyncio.create_task(sports_feed.run(), name="sports_feed"))
                        if settings.enable_statistical_edge:
                            odds_feed = MockOddsFeed(
                                components.event_bus,
                                market_slugs,
                                update_interval=settings.mock_odds_interval,
                                feed_monitor=components.feed_monitor,
                                metrics=components.metrics,
                            )
                            tasks.append(asyncio.create_task(odds_feed.run(), name="odds_feed"))
                    else:
                        logger.warning(
                            "Live strategies enabled but feeds are disabled; set USE_MOCK_FEEDS or configure provider",
                            live_arbitrage=settings.enable_live_arbitrage,
                            statistical_edge=settings.enable_statistical_edge,
                        )

                # Add market refresh loop if auto-discovery is enabled
                if not manual_slugs and leagues:
                    async with PolymarketClient(auth) as discovery_client:
                        tasks.append(asyncio.create_task(
                            market_refresh_loop(
                                discovery_client,
                                ws,
                                leagues,
                                products,
                                subscribed,
                                allow_in_game=bool(settings.enable_live_arbitrage or settings.enable_statistical_edge),
                            ),
                            name="market_refresh",
                        ))

                        async def _shutdown_sequence() -> None:
                            await shutdown_event.wait()
                            logger.warning(
                                "Shutdown signal received, cancelling all open orders...",
                                signal=shutdown_signal,
                            )
                            try:
                                cancelled = await asyncio.wait_for(
                                    components.executor.cancel_all_orders(),
                                    timeout=10.0,
                                )
                                logger.warning("Cancel all orders complete", cancelled=cancelled)
                            except asyncio.TimeoutError:
                                logger.error("Timeout cancelling open orders")
                            except Exception as exc:
                                logger.error("Failed cancelling open orders", error=str(exc))
                            try:
                                components.engine.stop()
                            except Exception:
                                pass
                            try:
                                await asyncio.wait_for(ws.disconnect(), timeout=5.0)
                            except Exception:
                                pass
                            try:
                                await asyncio.wait_for(ws_private.disconnect(), timeout=5.0)
                            except Exception:
                                pass
                            for t in tasks:
                                if not t.done():
                                    t.cancel()

                        shutdown_task = asyncio.create_task(_shutdown_sequence(), name="shutdown")
                        try:
                            done, pending = await asyncio.wait(
                                [*tasks, shutdown_task],
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            # If something finished unexpectedly, trigger shutdown.
                            if not shutdown_event.is_set():
                                shutdown_event.set()
                            await shutdown_task
                        finally:
                            for t in tasks:
                                t.cancel()
                            await asyncio.gather(*tasks, return_exceptions=True)
                            if discovery_client is not None:
                                try:
                                    await discovery_client.close()
                                except Exception:
                                    pass
                            if live_client is not None:
                                try:
                                    await live_client.close()
                                except Exception:
                                    pass
                else:
                    async def _shutdown_sequence() -> None:
                        await shutdown_event.wait()
                        logger.warning(
                            "Shutdown signal received, cancelling all open orders...",
                            signal=shutdown_signal,
                        )
                        try:
                            cancelled = await asyncio.wait_for(
                                components.executor.cancel_all_orders(),
                                timeout=10.0,
                            )
                            logger.warning("Cancel all orders complete", cancelled=cancelled)
                        except asyncio.TimeoutError:
                            logger.error("Timeout cancelling open orders")
                        except Exception as exc:
                            logger.error("Failed cancelling open orders", error=str(exc))
                        try:
                            components.engine.stop()
                        except Exception:
                            pass
                        try:
                            await asyncio.wait_for(ws.disconnect(), timeout=5.0)
                        except Exception:
                            pass
                        try:
                            await asyncio.wait_for(ws_private.disconnect(), timeout=5.0)
                        except Exception:
                            pass
                        for t in tasks:
                            if not t.done():
                                t.cancel()

                    shutdown_task = asyncio.create_task(_shutdown_sequence(), name="shutdown")
                    try:
                        done, pending = await asyncio.wait(
                            [*tasks, shutdown_task],
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if not shutdown_event.is_set():
                            shutdown_event.set()
                        await shutdown_task
                    finally:
                        for t in tasks:
                            t.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        if live_client is not None:
                            try:
                                await live_client.close()
                            except Exception:
                                pass
        else:
            logger.info("Bot ready", markets=len(market_slugs), mode=settings.trading_mode)

            # Build task list
            tasks = [
                asyncio.create_task(ws.run(), name="ws_markets"),
                asyncio.create_task(components.engine.run(), name="engine"),
                asyncio.create_task(
                    run_health_server(
                    settings.health_host,
                    settings.health_port,
                    feed_monitor=components.feed_monitor,
                    metrics=components.metrics,
                    engine=components.engine,
                    executor=components.executor,
                    ),
                    name="health_server",
                ),
            ]

            # Optional feed tasks for live strategies (mock by default)
            if settings.enable_live_arbitrage or settings.enable_statistical_edge:
                if settings.use_mock_feeds:
                    if settings.enable_live_arbitrage:
                        sports_feed = MockSportsFeed(
                            components.event_bus,
                            market_slugs,
                            update_interval=settings.mock_sports_interval,
                            feed_monitor=components.feed_monitor,
                            metrics=components.metrics,
                        )
                        tasks.append(asyncio.create_task(sports_feed.run(), name="sports_feed"))
                    if settings.enable_statistical_edge:
                        odds_feed = MockOddsFeed(
                            components.event_bus,
                            market_slugs,
                            update_interval=settings.mock_odds_interval,
                            feed_monitor=components.feed_monitor,
                            metrics=components.metrics,
                        )
                        tasks.append(asyncio.create_task(odds_feed.run(), name="odds_feed"))
                else:
                    logger.warning(
                        "Live strategies enabled but feeds are disabled; set USE_MOCK_FEEDS or configure provider",
                        live_arbitrage=settings.enable_live_arbitrage,
                        statistical_edge=settings.enable_statistical_edge,
                    )

            # Add market refresh loop if auto-discovery is enabled
            if not manual_slugs and leagues:
                async with PolymarketClient(auth) as discovery_client:
                    tasks.append(asyncio.create_task(
                        market_refresh_loop(
                            discovery_client,
                            ws,
                            leagues,
                            products,
                            subscribed,
                            allow_in_game=bool(settings.enable_live_arbitrage or settings.enable_statistical_edge),
                        ),
                        name="market_refresh",
                    ))

                    async def _shutdown_sequence() -> None:
                        await shutdown_event.wait()
                        logger.warning("Shutdown signal received, cancelling all open orders...", signal=shutdown_signal)
                        try:
                            cancelled = await asyncio.wait_for(
                                components.executor.cancel_all_orders(),
                                timeout=10.0,
                            )
                            logger.warning("Cancel all orders complete", cancelled=cancelled)
                        except asyncio.TimeoutError:
                            logger.error("Timeout cancelling open orders")
                        except Exception as exc:
                            logger.error("Failed cancelling open orders", error=str(exc))
                        try:
                            components.engine.stop()
                        except Exception:
                            pass
                        try:
                            await asyncio.wait_for(ws.disconnect(), timeout=5.0)
                        except Exception:
                            pass
                        for t in tasks:
                            if not t.done():
                                t.cancel()

                    shutdown_task = asyncio.create_task(_shutdown_sequence(), name="shutdown")
                    try:
                        done, pending = await asyncio.wait(
                            [*tasks, shutdown_task],
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if not shutdown_event.is_set():
                            shutdown_event.set()
                        await shutdown_task
                    finally:
                        for t in tasks:
                            t.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        if discovery_client is not None:
                            try:
                                await discovery_client.close()
                            except Exception:
                                pass
                        if live_client is not None:
                            try:
                                await live_client.close()
                            except Exception:
                                pass
            else:
                async def _shutdown_sequence() -> None:
                    await shutdown_event.wait()
                    logger.warning("Shutdown signal received, cancelling all open orders...", signal=shutdown_signal)
                    try:
                        cancelled = await asyncio.wait_for(
                            components.executor.cancel_all_orders(),
                            timeout=10.0,
                        )
                        logger.warning("Cancel all orders complete", cancelled=cancelled)
                    except asyncio.TimeoutError:
                        logger.error("Timeout cancelling open orders")
                    except Exception as exc:
                        logger.error("Failed cancelling open orders", error=str(exc))
                    try:
                        components.engine.stop()
                    except Exception:
                        pass
                    try:
                        await asyncio.wait_for(ws.disconnect(), timeout=5.0)
                    except Exception:
                        pass
                    for t in tasks:
                        if not t.done():
                            t.cancel()

                shutdown_task = asyncio.create_task(_shutdown_sequence(), name="shutdown")
                try:
                    done, pending = await asyncio.wait(
                        [*tasks, shutdown_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if not shutdown_event.is_set():
                        shutdown_event.set()
                    await shutdown_task
                finally:
                    for t in tasks:
                        t.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    if live_client is not None:
                        try:
                            await live_client.close()
                        except Exception:
                            pass


if __name__ == "__main__":
    asyncio.run(main())
