"""
Strategy engine for Polymarket US trading bot.

This module provides the orchestration layer that manages multiple strategies,
routes market updates, aggregates signals, and integrates with the executor.
"""

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Coroutine, Deque, Dict, List, Optional

import structlog

from ..data.models import OrderIntent
from ..data.orderbook import OrderBookTracker
from ..execution.paper_executor import PaperExecutor, PaperOrderRequest
from ..state.state_manager import MarketState, PositionState, StateManager
from ..risk.risk_manager import RiskManager
from .base_strategy import BaseStrategy, Signal, SignalAction, Urgency

logger = structlog.get_logger()


# =============================================================================
# Type Aliases
# =============================================================================

SignalHandler = Callable[[Signal], Coroutine[Any, Any, None]]


# =============================================================================
# Signal Aggregator
# =============================================================================

@dataclass
class AggregatedSignals:
    """
    Container for aggregated signals from all strategies.
    
    Attributes:
        signals: List of signals, sorted by priority
        by_market: Signals grouped by market
        timestamp: When aggregation was performed
    """
    signals: List[Signal] = field(default_factory=list)
    by_market: Dict[str, List[Signal]] = field(default_factory=lambda: defaultdict(list))
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class SignalAggregator:
    """
    Aggregates and prioritizes signals from multiple strategies.
    
    Handles:
    - Deduplication of conflicting signals
    - Priority ordering (live_arbitrage > statistical_edge > market_maker)
    - Urgency-based sorting within priorities
    """
    
    # Strategy priority (lower number = higher priority)
    STRATEGY_PRIORITY = {
        "live_arbitrage": 1,
        "statistical_edge": 2,
        "market_maker": 3,
    }
    
    # Default priority for unknown strategies
    DEFAULT_PRIORITY = 99
    
    def aggregate(self, all_signals: List[Signal]) -> AggregatedSignals:
        """
        Aggregate and prioritize signals from all strategies.
        
        Args:
            all_signals: List of all signals from all strategies
            
        Returns:
            AggregatedSignals with deduplicated, prioritized signals
        """
        if not all_signals:
            return AggregatedSignals()
        
        # Group by market
        by_market: Dict[str, List[Signal]] = defaultdict(list)
        for signal in all_signals:
            by_market[signal.market_slug].append(signal)
        
        # Process each market
        final_signals = []
        
        for market_slug, signals in by_market.items():
            # Sort by priority and urgency
            sorted_signals = sorted(
                signals,
                key=lambda s: (
                    self._get_priority(s.strategy_name),
                    self._urgency_rank(s.urgency),
                    -s.confidence,  # Higher confidence first
                ),
            )
            
            # Deduplicate by action type (keep highest priority)
            seen_actions = set()
            for signal in sorted_signals:
                # CANCEL_ALL always goes through
                if signal.action == SignalAction.CANCEL_ALL:
                    final_signals.append(signal)
                    continue
                
                # For other actions, only keep first (highest priority)
                if signal.action not in seen_actions:
                    final_signals.append(signal)
                    seen_actions.add(signal.action)
        
        # Final sort by urgency (HIGH first)
        final_signals.sort(key=lambda s: self._urgency_rank(s.urgency))
        
        # Build grouped result
        result_by_market = defaultdict(list)
        for signal in final_signals:
            result_by_market[signal.market_slug].append(signal)
        
        return AggregatedSignals(
            signals=final_signals,
            by_market=dict(result_by_market),
        )
    
    def _get_priority(self, strategy_name: str) -> int:
        """Get priority for a strategy."""
        return self.STRATEGY_PRIORITY.get(strategy_name, self.DEFAULT_PRIORITY)
    
    def _urgency_rank(self, urgency: Urgency) -> int:
        """Convert urgency to sortable rank (lower = more urgent)."""
        return {
            Urgency.HIGH: 0,
            Urgency.MEDIUM: 1,
            Urgency.LOW: 2,
        }.get(urgency, 2)


# =============================================================================
# Strategy Engine
# =============================================================================

class StrategyEngine:
    """
    Orchestration layer for managing trading strategies.
    
    The StrategyEngine:
    - Manages multiple strategy instances
    - Routes market updates to all strategies
    - Runs periodic tick loop for time-based logic
    - Aggregates signals from all strategies
    - Converts signals to orders and sends to executor
    
    Example:
        >>> state = StateManager(initial_balance=Decimal("1000"))
        >>> orderbook = OrderBookTracker()
        >>> executor = PaperExecutor(state, orderbook)
        >>> 
        >>> engine = StrategyEngine(
        ...     state_manager=state,
        ...     orderbook=orderbook,
        ...     executor=executor,
        ... )
        >>> 
        >>> # Add strategies
        >>> engine.add_strategy(MarketMakerStrategy(config))
        >>> 
        >>> # Run the engine
        >>> await engine.run()
    """
    
    def __init__(
        self,
        state_manager: StateManager,
        orderbook: OrderBookTracker,
        executor: PaperExecutor,
        risk_manager: Optional[RiskManager] = None,
        tick_interval: float = 1.0,
        enabled: bool = True,
    ):
        """
        Initialize strategy engine.
        
        Args:
            state_manager: StateManager for market/position data
            orderbook: OrderBookTracker for order book data
            executor: PaperExecutor for order execution
            tick_interval: Seconds between tick calls
            enabled: Whether engine is active
        """
        self.state_manager = state_manager
        self.orderbook = orderbook
        self.executor = executor
        self.risk_manager = risk_manager
        self.tick_interval = tick_interval
        self._enabled = enabled
        
        self._strategies: List[BaseStrategy] = []
        self._aggregator = SignalAggregator()
        self._running = False
        self._task: Optional[asyncio.Task] = None

        # Position update plumbing: PaperExecutor fills update StateManager, but
        # strategies cache positions internally. We subscribe to fill events and
        # flush position updates through process_position_update().
        self._pending_position_updates: set[str] = set()
        self._flushing_position_updates = False
        self._max_position_update_flush_loops = 50
        if hasattr(self.executor, "add_fill_listener"):
            try:
                self.executor.add_fill_listener(self._mark_position_updated)
            except Exception as exc:
                logger.warning("Failed to register fill listener", error=str(exc))
        
        # Metrics
        self._signals_generated = 0
        self._signals_executed = 0
        self._execution_errors = 0
        self._signals_rejected_by_risk = 0
        self._last_portfolio_log_at: Optional[datetime] = None

        # Execution error observability
        self._execution_error_counts: Dict[str, int] = defaultdict(int)
        self._execution_error_samples: Deque[Dict[str, Any]] = deque(maxlen=200)
        
        logger.info(
            "StrategyEngine initialized",
            tick_interval=tick_interval,
            enabled=enabled,
            risk_manager_enabled=self.risk_manager is not None,
        )

    def _categorize_execution_error(self, error: Optional[str]) -> str:
        if not error:
            return "unknown"

        text = error.lower()
        if "no liquidity" in text:
            return "no_liquidity"
        if "insufficient balance" in text:
            return "insufficient_balance"
        if "market not found" in text:
            return "market_not_found"
        if (
            "invalid order" in text
            or "cannot sell" in text
            or "quantity must be positive" in text
            or "price must be between" in text
        ):
            return "invalid_order"
        if "execution error" in text:
            return "unexpected_exception"

        return "other"

    def _record_execution_error(
        self,
        *,
        signal: Signal,
        intent: Optional[OrderIntent] = None,
        status: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        category = self._categorize_execution_error(error)
        self._execution_error_counts[category] += 1

        sample: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "error_category": category,
            "error": error,
            "status": status,
            "market_slug": signal.market_slug,
            "action": getattr(signal.action, "value", str(signal.action)),
            "strategy": signal.strategy_name,
            "price": float(signal.price) if signal.price is not None else None,
            "quantity": signal.quantity,
            "intent": intent.value if intent is not None else None,
        }

        # Keep only the most recent N.
        self._execution_error_samples.append(sample)

    def _mark_position_updated(self, market_slug: str) -> None:
        """
        Callback invoked by the executor when a fill occurs.

        We buffer market slugs and flush them in a controlled loop to avoid
        deep recursion when position-update signals themselves cause fills.
        """
        if market_slug:
            self._pending_position_updates.add(market_slug)

    def _flush_position_updates(self) -> None:
        """
        Flush any pending position updates into strategies.

        - If a position exists: call process_position_update() and execute any
          resulting signals (e.g., stop-loss / inventory reduction).
        - If no position exists: clear cached position state for that market.
        """
        if self._flushing_position_updates:
            return

        if not self._pending_position_updates:
            return

        self._flushing_position_updates = True
        try:
            loops = 0
            while self._pending_position_updates:
                loops += 1
                if loops > self._max_position_update_flush_loops:
                    logger.error(
                        "Position update flush exceeded max loops; clearing pending",
                        pending_count=len(self._pending_position_updates),
                    )
                    self._pending_position_updates.clear()
                    break

                slugs = list(self._pending_position_updates)
                self._pending_position_updates.clear()

                for market_slug in slugs:
                    position = self.state_manager.get_position(market_slug)
                    if position is not None:
                        signals = self.process_position_update(position)
                        if signals:
                            self.execute_signals(signals)
                    else:
                        for strategy in self._strategies:
                            try:
                                # Clear stale cached inventory when positions close.
                                if hasattr(strategy, "clear_position_state"):
                                    strategy.clear_position_state(market_slug)  # type: ignore[attr-defined]
                            except Exception as exc:
                                logger.error(
                                    "Strategy error clearing position state",
                                    strategy=strategy.name,
                                    market_slug=market_slug,
                                    error=str(exc),
                                )
        finally:
            self._flushing_position_updates = False
    
    # =========================================================================
    # Strategy Management
    # =========================================================================
    
    def add_strategy(self, strategy: BaseStrategy) -> None:
        """
        Add a strategy to the engine.
        
        Args:
            strategy: Strategy instance to add
        """
        self._strategies.append(strategy)
        logger.info(
            "Strategy added",
            strategy=strategy.name,
            enabled=strategy.enabled,
            total_strategies=len(self._strategies),
        )
    
    def remove_strategy(self, strategy_name: str) -> bool:
        """
        Remove a strategy by name.
        
        Args:
            strategy_name: Name of strategy to remove
            
        Returns:
            True if strategy was removed
        """
        for i, strategy in enumerate(self._strategies):
            if strategy.name == strategy_name:
                self._strategies.pop(i)
                logger.info("Strategy removed", strategy=strategy_name)
                return True
        return False
    
    def get_strategy(self, strategy_name: str) -> Optional[BaseStrategy]:
        """
        Get a strategy by name.
        
        Args:
            strategy_name: Name of strategy
            
        Returns:
            Strategy instance if found
        """
        for strategy in self._strategies:
            if strategy.name == strategy_name:
                return strategy
        return None
    
    def get_all_strategies(self) -> List[BaseStrategy]:
        """Get all registered strategies."""
        return list(self._strategies)
    
    # =========================================================================
    # Signal Processing
    # =========================================================================
    
    def process_market_update(self, market: MarketState) -> List[Signal]:
        """
        Route market update to all strategies and collect signals.
        
        Args:
            market: Updated market state
            
        Returns:
            Aggregated signals from all strategies
        """
        if not self._enabled:
            return []
        
        all_signals = []
        
        for strategy in self._strategies:
            if not strategy.enabled:
                continue
            
            try:
                # Update strategy's cached state
                strategy.update_market_state(market)
                
                # Get signals
                signals = strategy.on_market_update(market)
                if signals:
                    all_signals.extend(signals)
                    
            except Exception as e:
                logger.error(
                    "Strategy error on market update",
                    strategy=strategy.name,
                    market_slug=market.market_slug,
                    error=str(e),
                )
        
        # Aggregate signals
        if all_signals:
            aggregated = self._aggregator.aggregate(all_signals)
            self._signals_generated += len(aggregated.signals)
            return aggregated.signals
        
        return []
    
    def process_position_update(self, position: PositionState) -> List[Signal]:
        """
        Route position update to all strategies.
        
        Args:
            position: Updated position state
            
        Returns:
            Aggregated signals from all strategies
        """
        if not self._enabled:
            return []
        
        all_signals = []
        
        for strategy in self._strategies:
            if not strategy.enabled:
                continue
            
            try:
                # Update strategy's cached state
                strategy.update_position_state(position)
                
                # Get signals
                signals = strategy.on_position_update(position)
                if signals:
                    all_signals.extend(signals)
                    
            except Exception as e:
                logger.error(
                    "Strategy error on position update",
                    strategy=strategy.name,
                    market_slug=position.market_slug,
                    error=str(e),
                )
        
        if all_signals:
            aggregated = self._aggregator.aggregate(all_signals)
            self._signals_generated += len(aggregated.signals)
            return aggregated.signals
        
        return []
    
    def process_tick(self) -> List[Signal]:
        """
        Call on_tick for all strategies.
        
        Returns:
            Aggregated signals from all strategies
        """
        if not self._enabled:
            return []
        
        all_signals = []
        
        for strategy in self._strategies:
            if not strategy.enabled:
                continue
            
            try:
                signals = strategy.on_tick()
                if signals:
                    all_signals.extend(signals)
                    
            except Exception as e:
                logger.error(
                    "Strategy error on tick",
                    strategy=strategy.name,
                    error=str(e),
                )
        
        if all_signals:
            aggregated = self._aggregator.aggregate(all_signals)
            self._signals_generated += len(aggregated.signals)
            return aggregated.signals
        
        return []
    
    # =========================================================================
    # Signal Execution
    # =========================================================================
    
    def execute_signals(self, signals: List[Signal]) -> Dict[str, Any]:
        """
        Execute signals through the executor.
        
        Args:
            signals: List of signals to execute
            
        Returns:
            Execution results summary
        """
        results = {
            "executed": 0,
            "cancelled": 0,
            "risk_rejected": 0,
            "errors": 0,
            "details": [],
        }
        
        for signal in signals:
            try:
                # Risk gate (optional)
                if self.risk_manager is not None:
                    decision = self.risk_manager.evaluate_signal(signal)
                    if not decision.approved or decision.signal is None:
                        results["risk_rejected"] += 1
                        self._signals_rejected_by_risk += 1
                        logger.info(
                            "Signal rejected by risk manager",
                            signal=signal.to_dict(),
                            reason=decision.reason,
                            meta=decision.metadata,
                        )
                        results["details"].append({
                            "signal": signal.to_dict(),
                            "result": "risk_rejected",
                            "reason": decision.reason,
                            "metadata": decision.metadata,
                        })
                        if signal.metadata and signal.metadata.get("risk_exit"):
                            self._log_risk_exit_rejected(signal, decision.reason)
                        continue

                    # Use potentially resized signal.
                    if decision.signal != signal:
                        logger.info(
                            "Signal resized by risk manager",
                            original=signal.to_dict(),
                            resized=decision.signal.to_dict(),
                            reason=decision.reason,
                            meta=decision.metadata,
                        )
                    signal = decision.signal

                if signal.is_cancel:
                    # Cancel all orders for the market
                    cancelled = self.executor.cancel_all_orders(signal.market_slug)
                    results["cancelled"] += cancelled
                    results["details"].append({
                        "signal": signal.to_dict(),
                        "result": "cancelled",
                        "count": cancelled,
                    })
                else:
                    # Convert signal to order request
                    order = self._signal_to_order(signal)
                    
                    # Execute order
                    result = self.executor.execute_order(order)
                    
                    if result.is_success:
                        results["executed"] += 1
                        self._signals_executed += 1
                        
                        logger.info(
                            "Signal executed",
                            signal=signal.to_dict(),
                            order_id=result.order_id,
                            status=result.status.value,
                        )
                    else:
                        results["errors"] += 1
                        self._execution_errors += 1

                        self._record_execution_error(
                            signal=signal,
                            intent=order.intent,
                            status=getattr(result.status, "value", str(result.status)),
                            error=result.error,
                        )
                        
                        logger.warning(
                            "Signal execution failed",
                            signal=signal.to_dict(),
                            error=result.error,
                        )
                    
                    if signal.metadata and signal.metadata.get("risk_exit"):
                        self._log_risk_exit_execution(signal, result)
                    
                    results["details"].append({
                        "signal": signal.to_dict(),
                        "result": result.to_dict(),
                    })

                    # Update risk manager after execution attempt.
                    if self.risk_manager is not None:
                        self.risk_manager.on_state_update()

                    # If the order filled, the executor will have notified us via
                    # _mark_position_updated(). Flush here so strategies can react
                    # immediately (inventory/stop-loss logic).
                    self._flush_position_updates()
                    
            except Exception as e:
                results["errors"] += 1
                self._execution_errors += 1

                self._record_execution_error(
                    signal=signal,
                    intent=None,
                    status=None,
                    error=str(e),
                )
                
                logger.error(
                    "Signal execution error",
                    signal=signal.to_dict(),
                    error=str(e),
                )
                
                results["details"].append({
                    "signal": signal.to_dict(),
                    "error": str(e),
                })
        
        return results

    def _log_risk_exit_execution(self, signal: Signal, result) -> None:
        market = self.state_manager.get_market(signal.market_slug)
        payload = {
            "exit_type": signal.metadata.get("risk_exit") if signal.metadata else None,
            "status": result.status.value if hasattr(result, "status") else None,
            "error": result.error,
            "yes_bid": float(market.yes_bid) if market and market.yes_bid else None,
            "yes_ask": float(market.yes_ask) if market and market.yes_ask else None,
            "no_bid": float(market.no_bid) if market and market.no_bid else None,
            "no_ask": float(market.no_ask) if market and market.no_ask else None,
        }
        if result.is_success:
            logger.info("Risk exit executed", signal=signal.to_dict(), **payload)
        else:
            logger.warning("Risk exit failed", signal=signal.to_dict(), **payload)

    def _log_risk_exit_rejected(self, signal: Signal, reason: str) -> None:
        market = self.state_manager.get_market(signal.market_slug)
        logger.warning(
            "Risk exit rejected",
            signal=signal.to_dict(),
            reason=reason,
            yes_bid=float(market.yes_bid) if market and market.yes_bid else None,
            yes_ask=float(market.yes_ask) if market and market.yes_ask else None,
            no_bid=float(market.no_bid) if market and market.no_bid else None,
            no_ask=float(market.no_ask) if market and market.no_ask else None,
        )
    
    def _signal_to_order(self, signal: Signal) -> PaperOrderRequest:
        """
        Convert a signal to a PaperOrderRequest.
        
        Args:
            signal: Signal to convert
            
        Returns:
            PaperOrderRequest ready for execution
        """
        # Map signal action to order intent
        intent_map = {
            SignalAction.BUY_YES: OrderIntent.BUY_LONG,
            SignalAction.SELL_YES: OrderIntent.SELL_LONG,
            SignalAction.BUY_NO: OrderIntent.BUY_SHORT,
            SignalAction.SELL_NO: OrderIntent.SELL_SHORT,
        }
        
        intent = intent_map.get(signal.action)
        if intent is None:
            raise ValueError(f"Cannot convert action {signal.action} to order intent")
        
        return PaperOrderRequest(
            market_slug=signal.market_slug,
            intent=intent,
            quantity=signal.quantity,
            price=signal.price,
            post_only=bool(signal.metadata.get("post_only")) if signal.metadata else False,
        )
    
    # =========================================================================
    # Main Loop
    # =========================================================================
    
    async def run(self) -> None:
        """
        Run the strategy engine main loop.
        
        This method:
        1. Starts all strategies
        2. Runs periodic tick loop
        3. Checks for resting order fills
        """
        if self._running:
            logger.warning("StrategyEngine already running")
            return
        
        self._running = True
        
        # Start all strategies
        for strategy in self._strategies:
            strategy.start()
        
        logger.info(
            "StrategyEngine started",
            strategies=len(self._strategies),
            tick_interval=self.tick_interval,
        )
        
        try:
            while self._running:
                await self._tick()
                await asyncio.sleep(self.tick_interval)
                
        except asyncio.CancelledError:
            logger.info("StrategyEngine cancelled")
        except Exception as e:
            logger.error("StrategyEngine error", error=str(e))
            raise
        finally:
            # Stop all strategies
            for strategy in self._strategies:
                strategy.stop()
            
            logger.info("StrategyEngine stopped")
    
    async def _tick(self) -> None:
        """
        Perform one tick of the engine loop.
        """
        # Keep risk manager up to date even if there are no signals.
        if self.risk_manager is not None:
            self.risk_manager.on_state_update()

        # Periodic portfolio snapshot logging every 5 seconds.
        self._log_portfolio_snapshot()

        # Process tick for all strategies
        signals = self.process_tick()
        
        # Execute any generated signals
        if signals:
            self.execute_signals(signals)
        
        # Check for resting order fills
        filled_orders = self.executor.check_resting_orders()
        if filled_orders:
            logger.debug(
                "Resting orders filled",
                count=len(filled_orders),
            )

        # Resting fills update StateManager via PaperExecutor; flush into strategies.
        self._flush_position_updates()

    def _log_portfolio_snapshot(self) -> None:
        now = datetime.now(timezone.utc)
        if self._last_portfolio_log_at is not None:
            elapsed = (now - self._last_portfolio_log_at).total_seconds()
            if elapsed < 5.0:
                return
        self._last_portfolio_log_at = now

        total_equity = self.state_manager.get_total_equity()
        cash = self.state_manager.get_balance()
        position_value = self.state_manager.get_total_position_value()
        positions_exposure = self.state_manager.get_exposure()

        open_orders = self.state_manager.get_open_orders()
        open_orders_exposure = Decimal("0")
        for order in open_orders:
            if order.remaining_quantity <= 0:
                continue
            open_orders_exposure += order.price * order.remaining_quantity

        total_exposure = positions_exposure + open_orders_exposure
        exposure_pct = (
            float((total_exposure / total_equity) * 100)
            if total_equity > 0
            else 0.0
        )

        positions_snapshot = []
        for position in self.state_manager.get_all_positions():
            mark_price = self._get_mark_price(position)
            unrealized_pnl = (mark_price - position.avg_price) * position.quantity
            pnl_pct = (
                float((mark_price - position.avg_price) / position.avg_price)
                if position.avg_price > 0
                else 0.0
            )
            age_seconds = (now - position.created_at).total_seconds()

            positions_snapshot.append(
                {
                    "market": position.market_slug,
                    "side": position.side.value,
                    "quantity": position.quantity,
                    "avg_price": float(position.avg_price),
                    "mark_price": float(mark_price),
                    "unrealized_pnl": float(unrealized_pnl),
                    "unrealized_pnl_pct": pnl_pct,
                    "age_seconds": age_seconds,
                }
            )

        logger.info(
            "Portfolio snapshot",
            cash=float(cash),
            position_value=float(position_value),
            total_equity=float(total_equity),
            positions_exposure=float(positions_exposure),
            open_orders_exposure=float(open_orders_exposure),
            total_exposure=float(total_exposure),
            exposure_pct=round(exposure_pct, 2),
            open_positions=len(positions_snapshot),
            positions=positions_snapshot,
        )

    def _get_mark_price(self, position: PositionState) -> Decimal:
        book = self.orderbook.get(position.market_slug)
        if book is not None:
            if position.side.value == "YES":
                return book.yes_best_bid or position.avg_price
            return book.no_best_bid or position.avg_price

        market = self.state_manager.get_market(position.market_slug)
        if market is not None:
            if position.side.value == "YES":
                return market.yes_bid or position.avg_price
            return market.no_bid or position.avg_price

        return position.avg_price
    
    def stop(self) -> None:
        """Stop the strategy engine."""
        self._running = False
        
        if self._task:
            self._task.cancel()
    
    async def start_async(self) -> asyncio.Task:
        """
        Start the engine as a background task.
        
        Returns:
            Asyncio task running the engine
        """
        self._task = asyncio.create_task(self.run())
        return self._task
    
    # =========================================================================
    # WebSocket Integration
    # =========================================================================
    
    def create_market_handler(self):
        """
        Create a WebSocket message handler for market data.
        
        Returns:
            Async handler function for MARKET_DATA messages
        """
        async def handler(data: Dict[str, Any]) -> None:
            if data.get("type") != "MARKET_DATA":
                return
            
            market_slug = data.get("marketSlug")
            if not market_slug:
                return
            
            # Get market state from state manager
            market = self.state_manager.get_market(market_slug)
            if market:
                # Process update and execute signals
                signals = self.process_market_update(market)
                if signals:
                    self.execute_signals(signals)
        
        return handler
    
    # =========================================================================
    # Properties & Metrics
    # =========================================================================
    
    @property
    def enabled(self) -> bool:
        """Check if engine is enabled."""
        return self._enabled
    
    @enabled.setter
    def enabled(self, value: bool) -> None:
        """Enable or disable the engine."""
        self._enabled = value
        logger.info("StrategyEngine enabled state changed", enabled=value)
    
    @property
    def is_running(self) -> bool:
        """Check if engine is running."""
        return self._running
    
    def get_metrics(self) -> Dict[str, Any]:
        """
        Get engine metrics.
        
        Returns:
            Dictionary of engine metrics
        """
        return {
            "strategies": len(self._strategies),
            "enabled_strategies": sum(1 for s in self._strategies if s.enabled),
            "signals_generated": self._signals_generated,
            "signals_executed": self._signals_executed,
            "signals_rejected_by_risk": self._signals_rejected_by_risk,
            "execution_errors": self._execution_errors,
            "execution_error_counts": dict(self._execution_error_counts),
            "execution_error_samples": list(self._execution_error_samples),
            "running": self._running,
            "enabled": self._enabled,
        }
    
    def reset_metrics(self) -> None:
        """Reset engine metrics."""
        self._signals_generated = 0
        self._signals_executed = 0
        self._execution_errors = 0
