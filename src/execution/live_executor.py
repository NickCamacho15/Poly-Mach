"""
Live Executor for Polymarket Trading Bot
Fixed version with proper API response handling
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict, Any
from decimal import Decimal
import time

logger = logging.getLogger(__name__)


@dataclass
class LiveOrderRequest:
    """Request to place an order on Polymarket"""
    token_id: str
    side: str  # "BUY" or "SELL"
    size: float
    price: float
    market_slug: str = ""
    order_type: str = "GTC"  # Good Till Cancelled


@dataclass
class LiveExecutionResult:
    """Result of an order execution"""
    success: bool
    order_id: Optional[str] = None
    filled_size: float = 0.0
    filled_price: float = 0.0
    status: str = "UNKNOWN"
    error_message: Optional[str] = None
    raw_response: Optional[Dict[str, Any]] = None
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            "success": self.success,
            "order_id": self.order_id,
            "filled_size": self.filled_size,
            "filled_price": self.filled_price,
            "status": self.status,
            "error_message": self.error_message,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None
        }


class LiveExecutor:
    """
    Executor for live trading on Polymarket.
    Sends real orders to the Polymarket API.
    """

    def __init__(self, client, settings=None):
        self.client = client
        self.settings = settings
        self.active_orders: Dict[str, LiveExecutionResult] = {}
        self.order_history: List[LiveExecutionResult] = []
        self.total_trades = 0
        self.successful_trades = 0
        self.failed_trades = 0
        self._lock = asyncio.Lock()
        logger.info("LiveExecutor initialized for REAL trading")

    async def execute_order(self, order_request: LiveOrderRequest) -> LiveExecutionResult:
        """Execute a live order on Polymarket."""
        try:
            logger.info(f"LIVE ORDER: {order_request.side} {order_request.size} @ {order_request.price}")

            order_params = {
                "tokenID": order_request.token_id,
                "side": order_request.side,
                "size": order_request.size,
                "price": order_request.price,
            }

            response = await self._execute_with_retry(order_params)
            result = self._parse_order_response(response)

            async with self._lock:
                self.total_trades += 1
                if result.success:
                    self.successful_trades += 1
                    if result.order_id:
                        self.active_orders[result.order_id] = result
                else:
                    self.failed_trades += 1
                self.order_history.append(result)

            logger.info(f"Order result: {result.status} - ID: {result.order_id}")
            return result

        except Exception as e:
            logger.error(f"Order execution failed: {e}")
            result = LiveExecutionResult(
                success=False,
                status="ERROR",
                error_message=str(e)
            )
            async with self._lock:
                self.total_trades += 1
                self.failed_trades += 1
                self.order_history.append(result)
            return result

    async def _execute_with_retry(self, order_params: Dict, max_retries: int = 3) -> Dict:
        """Execute order with retry logic"""
        last_error = None
        for attempt in range(max_retries):
            try:
                if hasattr(self.client, 'create_order'):
                    response = await self.client.create_order(**order_params)
                elif hasattr(self.client, 'post_order'):
                    response = await self.client.post_order(**order_params)
                else:
                    response = await self.client.post("/order", order_params)
                return response
            except Exception as e:
                last_error = e
                logger.warning(f"Order attempt {attempt + 1} failed: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.5 * (attempt + 1))
        raise last_error

    def _parse_order_response(self, response: Any) -> LiveExecutionResult:
        """Parse Polymarket API order response."""
        try:
            if response is None:
                return LiveExecutionResult(success=False, status="NO_RESPONSE", error_message="No response from API")

            if isinstance(response, dict):
                order_id = response.get('id') or response.get('orderId') or response.get('order_id')
                status = response.get('status', 'SUBMITTED')
                executions = response.get('executions', [])

                filled_size = 0.0
                filled_price = 0.0
                if executions:
                    for exec_item in executions:
                        exec_size = float(exec_item.get('size', 0) or exec_item.get('fillSize', 0))
                        exec_price = float(exec_item.get('price', 0) or exec_item.get('fillPrice', 0))
                        filled_size += exec_size
                        if exec_price > 0:
                            filled_price = exec_price

                success = order_id is not None and status not in ['REJECTED', 'FAILED', 'ERROR']

                return LiveExecutionResult(
                    success=success,
                    order_id=order_id,
                    filled_size=filled_size,
                    filled_price=filled_price,
                    status=status,
                    raw_response=response
                )

            if isinstance(response, str):
                return LiveExecutionResult(success=True, order_id=response, status="SUBMITTED")

            return LiveExecutionResult(success=False, status="PARSE_ERROR", error_message=f"Unknown response format: {type(response)}")

        except Exception as e:
            logger.error(f"Error parsing order response: {e}")
            return LiveExecutionResult(success=False, status="PARSE_ERROR", error_message=str(e))

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an active order."""
        try:
            logger.info(f"Cancelling order: {order_id}")
            if hasattr(self.client, 'cancel_order'):
                await self.client.cancel_order(order_id)
            elif hasattr(self.client, 'delete_order'):
                await self.client.delete_order(order_id)
            else:
                await self.client.delete(f"/order/{order_id}")

            async with self._lock:
                if order_id in self.active_orders:
                    del self.active_orders[order_id]
            logger.info(f"Order {order_id} cancelled")
            return True
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False

    async def cancel_all_orders(self) -> int:
        """Cancel all active orders."""
        cancelled = 0
        order_ids = list(self.active_orders.keys())
        for order_id in order_ids:
            if await self.cancel_order(order_id):
                cancelled += 1
        logger.info(f"Cancelled {cancelled} orders")
        return cancelled

    def cancel_all_orders_sync(self) -> int:
        """Synchronous version of cancel_all_orders"""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                future = asyncio.run_coroutine_threadsafe(self.cancel_all_orders(), loop)
                return future.result(timeout=30)
            else:
                return loop.run_until_complete(self.cancel_all_orders())
        except Exception as e:
            logger.error(f"Error in cancel_all_orders_sync: {e}")
            return 0

    def check_resting_orders(self) -> List[Dict[str, Any]]:
        """Check for resting (unfilled) orders."""
        resting = []
        for order_id, result in self.active_orders.items():
            if result.status in ['SUBMITTED', 'OPEN', 'RESTING', 'LIVE']:
                resting.append({
                    "order_id": order_id,
                    "status": result.status,
                    "timestamp": result.timestamp.isoformat() if result.timestamp else None
                })
        return resting

    async def get_open_orders(self) -> List[Dict[str, Any]]:
        """Fetch open orders from the API"""
        try:
            if hasattr(self.client, 'get_orders'):
                orders = await self.client.get_orders()
                return orders if orders else []
            return []
        except Exception as e:
            logger.error(f"Failed to get open orders: {e}")
            return []

    def get_stats(self) -> Dict[str, Any]:
        """Get execution statistics"""
        return {
            "total_trades": self.total_trades,
            "successful_trades": self.successful_trades,
            "failed_trades": self.failed_trades,
            "success_rate": self.successful_trades / max(self.total_trades, 1) * 100,
            "active_orders": len(self.active_orders)
        }

    def get_performance(self) -> Dict[str, Any]:
        """Get performance metrics for health endpoint"""
        return {
            "mode": "live",
            "total_trades": self.total_trades,
            "successful_trades": self.successful_trades,
            "failed_trades": self.failed_trades,
            "active_orders": len(self.active_orders),
            "success_rate": round(self.successful_trades / max(self.total_trades, 1) * 100, 2)
        }
