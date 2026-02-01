"""
Live executor for Polymarket US.

This executor is designed to behave like PaperExecutor from the StrategyEngine's
perspective, but it places real orders and drives fills/positions/balance from
private WebSocket + REST reconciliation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

import structlog

from ..api.client import APIError, PolymarketClient
from ..data.models import OrderIntent, OrderStatus, OrderType, Price, OrderRequest, Side
from ..data.orderbook import OrderBookTracker
from ..state.state_manager import OrderState, StateManager
from .paper_executor import ExecutionResult, PaperOrderRequest

logger = structlog.get_logger()


@dataclass(frozen=True)
class _ReconcileSnapshot:
    ts: datetime
    open_order_ids: set[str]


class LiveExecutor:
    """
    Async executor for live trading.

    Contract:
    - accepts PaperOrderRequest
    - returns ExecutionResult (same as paper)
    - updates StateManager (orders/positions/balance)
    - supports fill listeners (same signature as paper)
    """

    def __init__(
        self,
        client: PolymarketClient,
        state: StateManager,
        orderbook: OrderBookTracker,
        initial_balance: Optional[Decimal] = None,
        settings: Any = None,
    ):
        self.client = client
        self.state = state
        self.orderbook = orderbook
        self.settings = settings

        if initial_balance is not None:
            self.state.update_balance(initial_balance)
        self._initial_balance = self.state.get_balance()

        self._fill_listeners: List[Callable[[str], None]] = []

        self._lock = asyncio.Lock()

        # Basic counters for monitoring/health endpoints.
        self.total_trades = 0
        self.successful_trades = 0
        self.failed_trades = 0

        # For detecting newly-filled deltas during reconciliation.
        self._order_last_filled: Dict[str, int] = {}
        self._order_market: Dict[str, str] = {}
        self._estimated_fees: Dict[str, Decimal] = {}
        self._last_reconcile: Optional[_ReconcileSnapshot] = None

        logger.info("LiveExecutor initialized", initial_balance=float(self._initial_balance))

    # ------------------------------------------------------------------
    # Fill listeners
    # ------------------------------------------------------------------
    def add_fill_listener(self, listener: Callable[[str], None]) -> None:
        if listener not in self._fill_listeners:
            self._fill_listeners.append(listener)

    def remove_fill_listener(self, listener: Callable[[str], None]) -> None:
        try:
            self._fill_listeners.remove(listener)
        except ValueError:
            return

    def _notify_fill_listeners(self, market_slug: str) -> None:
        for listener in list(self._fill_listeners):
            try:
                listener(market_slug)
            except Exception as exc:
                logger.warning(
                    "Fill listener error",
                    market_slug=market_slug,
                    listener=getattr(listener, "__name__", str(listener)),
                    error=str(exc),
                )

    # ------------------------------------------------------------------
    # Normalization helpers (mirror PaperExecutor semantics where safe)
    # ------------------------------------------------------------------
    def _normalize_order(self, order: PaperOrderRequest) -> PaperOrderRequest:
        if order.intent not in (OrderIntent.SELL_LONG, OrderIntent.SELL_SHORT):
            return order

        current = self.state.get_position(order.market_slug)

        if order.intent == OrderIntent.SELL_LONG:
            if current and current.side == Side.YES:
                return order
            return self._convert_sell_to_buy(order, OrderIntent.BUY_SHORT)

        if order.intent == OrderIntent.SELL_SHORT:
            if current and current.side == Side.NO:
                return order
            return self._convert_sell_to_buy(order, OrderIntent.BUY_LONG)

        return order

    def _convert_sell_to_buy(self, order: PaperOrderRequest, target_intent: OrderIntent) -> PaperOrderRequest:
        converted_price = None
        if order.price is not None:
            converted_price = Decimal("1") - order.price
        return PaperOrderRequest(
            market_slug=order.market_slug,
            intent=target_intent,
            quantity=order.quantity,
            price=converted_price,
            order_type=order.order_type,
            post_only=order.post_only,
        )

    def _get_post_only_price(self, order: PaperOrderRequest) -> Optional[Decimal]:
        price = order.price
        if price is None:
            return price

        book = self.orderbook.get(order.market_slug)
        if book is not None:
            if order.intent == OrderIntent.BUY_LONG and book.yes_best_bid is not None:
                return min(price, book.yes_best_bid)
            if order.intent == OrderIntent.BUY_SHORT and book.no_best_bid is not None:
                return min(price, book.no_best_bid)
            if order.intent == OrderIntent.SELL_LONG and book.yes_best_ask is not None:
                return max(price, book.yes_best_ask)
            if order.intent == OrderIntent.SELL_SHORT and book.no_best_ask is not None:
                return max(price, book.no_best_ask)

        market = self.state.get_market(order.market_slug)
        if market is None:
            return price

        if order.intent == OrderIntent.BUY_LONG and market.yes_bid is not None:
            return min(price, market.yes_bid)
        if order.intent == OrderIntent.BUY_SHORT and market.no_bid is not None:
            return min(price, market.no_bid)
        if order.intent == OrderIntent.SELL_LONG and market.yes_ask is not None:
            return max(price, market.yes_ask)
        if order.intent == OrderIntent.SELL_SHORT and market.no_ask is not None:
            return max(price, market.no_ask)

        return price

    # ------------------------------------------------------------------
    # Core async contract
    # ------------------------------------------------------------------
    async def execute_order(self, order: PaperOrderRequest) -> ExecutionResult:
        order = self._normalize_order(order)

        # Live executor uses exchange order IDs.
        market_slug = order.market_slug

        # Side flip behavior: close-then-open (selected).
        is_buy = order.intent in (OrderIntent.BUY_LONG, OrderIntent.BUY_SHORT)
        side = Side.YES if order.intent in (OrderIntent.BUY_LONG, OrderIntent.SELL_LONG) else Side.NO
        current_position = self.state.get_position(market_slug)
        if is_buy and current_position is not None and current_position.side != side:
            try:
                await self.client.close_position(market_slug)
                # Reconciliation will pick up position/balance changes.
                await self._reconcile_state(force=True)
                self._notify_fill_listeners(market_slug)
            except Exception as exc:
                return ExecutionResult(
                    order_id="",
                    status=OrderStatus.REJECTED,
                    error=f"Failed to close existing position before side flip: {exc}",
                )

        # Post-only: best-effort by adjusting the limit price to avoid crossing.
        submit_order = order
        if order.post_only and order.order_type == OrderType.LIMIT and order.price is not None:
            post_price = self._get_post_only_price(order)
            submit_order = PaperOrderRequest(
                market_slug=order.market_slug,
                intent=order.intent,
                quantity=order.quantity,
                price=post_price,
                order_type=order.order_type,
                post_only=order.post_only,
            )

        # Build typed API order request
        api_price = None
        if submit_order.order_type == OrderType.LIMIT:
            if submit_order.price is None:
                return ExecutionResult(
                    order_id="",
                    status=OrderStatus.REJECTED,
                    error="Live LIMIT orders require a price",
                )
            api_price = Price(value=str(submit_order.price))

        api_req = OrderRequest(
            marketSlug=submit_order.market_slug,
            type=submit_order.order_type,
            price=api_price,
            quantity=submit_order.quantity,
            intent=submit_order.intent,
        )

        estimated_fee = Decimal("0")
        try:
            preview = await self.client.preview_order(api_req)
            if preview.estimated_fee is not None:
                estimated_fee = Decimal(str(preview.estimated_fee))
        except Exception:
            # Preview is best-effort; order placement is authoritative.
            pass

        try:
            api_order = await self.client.create_order(api_req)
        except APIError as exc:
            self.total_trades += 1
            self.failed_trades += 1
            return ExecutionResult(order_id="", status=OrderStatus.REJECTED, error=str(exc))
        except Exception as exc:
            self.total_trades += 1
            self.failed_trades += 1
            return ExecutionResult(order_id="", status=OrderStatus.REJECTED, error=f"Execution error: {exc}")

        order_id = api_order.order_id
        raw_status = api_order.status
        try:
            status = OrderStatus(raw_status)
        except Exception:
            # Polymarket may use non-enum strings in some contexts; map conservatively.
            status = OrderStatus.OPEN if raw_status not in ("REJECTED", "CANCELLED") else OrderStatus.REJECTED

        filled_qty = int(api_order.filled_quantity or 0)
        avg_fill_price = api_order.avg_fill_price

        # Track order mapping and fee estimate.
        async with self._lock:
            self._order_market[order_id] = market_slug
            self._order_last_filled[order_id] = filled_qty
            self._estimated_fees[order_id] = estimated_fee

        # Monitoring counters: treat a successfully-submitted order as a successful trade.
        self.total_trades += 1
        if status != OrderStatus.REJECTED:
            self.successful_trades += 1
        else:
            self.failed_trades += 1

        # Mirror paper: store open/partial orders in state; remove when filled/cancelled.
        state_price = submit_order.price or avg_fill_price or Decimal("0")
        self.state.add_order(
            OrderState(
                order_id=order_id,
                market_slug=market_slug,
                intent=submit_order.intent,
                price=state_price,
                quantity=submit_order.quantity,
                filled_quantity=filled_qty,
                status=status,
            )
        )
        if status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
            self.state.remove_order(order_id)

        # If we got any fill immediately, reconcile positions/balance and notify.
        if filled_qty > 0 or status == OrderStatus.FILLED:
            await self._reconcile_state(force=True)
            self._notify_fill_listeners(market_slug)

        return ExecutionResult(
            order_id=order_id,
            status=status,
            filled_quantity=filled_qty,
            avg_fill_price=avg_fill_price,
            fee=estimated_fee,
            error=None,
            trade=None,
        )

    async def cancel_order(self, order_id: str) -> bool:
        try:
            await self.client.cancel_order(order_id)
        except Exception as exc:
            logger.warning("Cancel order failed", order_id=order_id, error=str(exc))
            return False

        self.state.update_order(order_id, status=OrderStatus.CANCELLED)
        self.state.remove_order(order_id)
        return True

    async def cancel_all_orders(self, market_slug: Optional[str] = None) -> int:
        try:
            await self.client.cancel_all_orders(market_slug=market_slug)
        except Exception as exc:
            logger.warning("Cancel all orders failed", market_slug=market_slug, error=str(exc))
            return 0

        cancelled = 0
        for order in self.state.get_open_orders(market_slug):
            self.state.update_order(order.order_id, status=OrderStatus.CANCELLED)
            self.state.remove_order(order.order_id)
            cancelled += 1
        return cancelled

    async def check_resting_orders(self) -> List[ExecutionResult]:
        """
        Live-mode equivalent of PaperExecutor.check_resting_orders().

        In live mode, this runs a reconciliation poll and returns ExecutionResults
        for any orders that advanced their filled quantity since the last check.
        """
        return await self._reconcile_state(force=False)

    # ------------------------------------------------------------------
    # Private WS handlers (wired in a later step)
    # ------------------------------------------------------------------
    def create_order_update_handler(self):
        async def handler(data: Dict[str, Any]) -> None:
            if data.get("type") != "ORDER_UPDATE":
                return
            order_id = data.get("orderId") or data.get("order_id")
            if not order_id:
                return

            market_slug = data.get("marketSlug") or self._order_market.get(order_id)
            status_raw = data.get("status")
            filled_raw = data.get("filledQuantity")

            try:
                status = OrderStatus(status_raw) if status_raw else None
            except Exception:
                status = None

            filled_qty: Optional[int] = None
            if filled_raw is not None:
                try:
                    filled_qty = int(Decimal(str(filled_raw)))
                except Exception:
                    filled_qty = None

            if status is not None or filled_qty is not None:
                self.state.update_order(order_id, status=status, filled_quantity=filled_qty)

            if market_slug and filled_qty is not None:
                prev = self._order_last_filled.get(order_id, 0)
                if filled_qty > prev:
                    self._order_last_filled[order_id] = filled_qty
                    self._notify_fill_listeners(market_slug)

            if status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
                self.state.remove_order(order_id)

        return handler

    def create_position_update_handler(self):
        async def handler(data: Dict[str, Any]) -> None:
            if data.get("type") != "POSITION_UPDATE":
                return
            market_slug = data.get("marketSlug")
            if not market_slug:
                return
            try:
                side = Side(data.get("side"))
            except Exception:
                return
            try:
                qty = int(Decimal(str(data.get("quantity", "0"))))
                avg = Decimal(str(data.get("avgPrice", "0")))
            except Exception:
                return

            self.state.update_position(market_slug, side=side, quantity=qty, avg_price=avg)
            self._notify_fill_listeners(market_slug)

        return handler

    def create_balance_update_handler(self):
        async def handler(data: Dict[str, Any]) -> None:
            if data.get("type") != "ACCOUNT_BALANCE_UPDATE":
                return
            raw = data.get("availableBalance") or data.get("balance")
            if raw is None:
                return
            try:
                bal = Decimal(str(raw))
            except Exception:
                return
            self.state.update_balance(bal)

        return handler

    # ------------------------------------------------------------------
    # Reconciliation (REST polling fallback)
    # ------------------------------------------------------------------
    async def _reconcile_state(self, *, force: bool) -> List[ExecutionResult]:
        """
        Poll REST for open orders/positions/balance and reconcile into StateManager.

        Returns ExecutionResults for any order that increased filled quantity since
        the last reconciliation tick.
        """
        now = datetime.now(timezone.utc)
        if not force and self._last_reconcile is not None:
            # Throttle to avoid hammering the API in tight loops.
            if (now - self._last_reconcile.ts).total_seconds() < 1.0:
                return []

        results: List[ExecutionResult] = []

        try:
            balance = await self.client.get_balance()
            self.state.update_balance(balance.available_balance)
        except Exception as exc:
            logger.debug("Reconcile balance failed", error=str(exc))

        # Positions: reconcile by overwriting known positions for markets we see.
        try:
            positions = await self.client.get_positions()
            seen_slugs = set()
            for p in positions:
                seen_slugs.add(p.market_slug)
                self.state.update_position(
                    p.market_slug,
                    side=p.side,
                    quantity=p.quantity,
                    avg_price=p.avg_price,
                )
            # NOTE: we intentionally do not clear missing positions here; WS/close flow
            # will clear via qty<=0 updates or subsequent reconciles with explicit slugs.
        except Exception as exc:
            logger.debug("Reconcile positions failed", error=str(exc))

        # Orders: reconcile open orders and detect filled deltas for tracked orders.
        try:
            open_orders = await self.client.get_open_orders()
            open_ids = set()
            for o in open_orders:
                open_ids.add(o.order_id)
                try:
                    status = OrderStatus(o.status)
                except Exception:
                    status = OrderStatus.OPEN
                filled_qty = int(o.filled_quantity or 0)
                self._order_market.setdefault(o.order_id, o.market_slug)
                self.state.add_order(
                    OrderState(
                        order_id=o.order_id,
                        market_slug=o.market_slug,
                        intent=OrderIntent(o.intent) if isinstance(o.intent, str) else o.intent,
                        price=o.price or Decimal("0"),
                        quantity=o.quantity,
                        filled_quantity=filled_qty,
                        status=status,
                    )
                )

                prev = self._order_last_filled.get(o.order_id, 0)
                if filled_qty > prev:
                    self._order_last_filled[o.order_id] = filled_qty
                    fee = self._estimated_fees.get(o.order_id, Decimal("0"))
                    results.append(
                        ExecutionResult(
                            order_id=o.order_id,
                            status=status,
                            filled_quantity=filled_qty - prev,
                            avg_fill_price=o.avg_fill_price,
                            fee=fee,
                            error=None,
                            trade=None,
                        )
                    )
                    self._notify_fill_listeners(o.market_slug)

            # If we previously tracked orders that are no longer open, try fetching
            # their final status (best-effort) and clean them up.
            for tracked_id, market_slug in list(self._order_market.items()):
                if tracked_id in open_ids:
                    continue
                # If the order is still in state as open, attempt final status fetch.
                maybe = self.state.get_order(tracked_id)
                if maybe and maybe.is_open:
                    try:
                        final = await self.client.get_order(tracked_id)
                        try:
                            final_status = OrderStatus(final.status)
                        except Exception:
                            final_status = OrderStatus.FILLED if final.is_filled else OrderStatus.CANCELLED
                        self.state.update_order(
                            tracked_id,
                            status=final_status,
                            filled_quantity=int(final.filled_quantity or 0),
                        )
                        if final_status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
                            self.state.remove_order(tracked_id)
                    except Exception:
                        # If we can't fetch, leave it; the next reconcile may succeed.
                        pass

            self._last_reconcile = _ReconcileSnapshot(ts=now, open_order_ids=open_ids)

        except Exception as exc:
            logger.debug("Reconcile orders failed", error=str(exc))

        return results

    # ------------------------------------------------------------------
    # Health endpoint metrics
    # ------------------------------------------------------------------
    def get_performance(self) -> Dict[str, Any]:
        equity = self.state.get_total_equity()
        cash = self.state.get_balance()
        position_value = self.state.get_total_position_value()
        pnl = equity - self._initial_balance
        pnl_pct = float((pnl / self._initial_balance) * 100) if self._initial_balance > 0 else 0.0

        return {
            "mode": "live",
            # Legacy fields used by discord_bot + health output.
            "total_trades": self.total_trades,
            "successful_trades": self.successful_trades,
            "failed_trades": self.failed_trades,
            "active_orders": len(self.state.get_open_orders()),
            "success_rate": round(self.successful_trades / max(self.total_trades, 1) * 100, 2),
            "initial_balance": float(self._initial_balance),
            "current_balance": float(cash),
            "position_value": float(position_value),
            "total_equity": float(equity),
            "total_pnl": float(pnl),
            "pnl_percent": pnl_pct,
            "open_positions": len(self.state.get_all_positions()),
        }

    def get_positions_report(self, limit: int = 50) -> List[Dict[str, Any]]:
        # Reuse the paper-style report shape (best-effort).
        report: List[Dict[str, Any]] = []
        for position in self.state.get_all_positions():
            book = self.orderbook.get(position.market_slug)
            best_bid = None
            best_ask = None
            if book is not None:
                side_book = book.yes if position.side == Side.YES else book.no
                if side_book.bids:
                    best_bid = side_book.bids[0].price
                if side_book.asks:
                    best_ask = side_book.asks[0].price
            report.append(
                {
                    "market_slug": position.market_slug,
                    "side": position.side.value,
                    "quantity": position.quantity,
                    "avg_price": float(position.avg_price),
                    "best_bid": float(best_bid) if best_bid is not None else None,
                    "best_ask": float(best_ask) if best_ask is not None else None,
                }
            )
        return report[: max(0, int(limit))]
