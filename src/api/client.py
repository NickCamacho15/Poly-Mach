"""
Async REST API client for Polymarket US.

This module provides a fully async HTTP client with rate limiting,
retry logic, and proper error handling.
"""

import asyncio
from decimal import Decimal
from typing import Any, Dict, List, Optional, Type, TypeVar

import httpx
from asyncio_throttle import Throttler
import structlog

from .auth import PolymarketAuth, AuthenticationError
from ..data.models import (
    Balance,
    CreateOrderResponse,
    Market,
    Order,
    OrderBook,
    OrderBookSide,
    OrderRequest,
    OrderPreview,
    Position,
    PriceLevel,
)

logger = structlog.get_logger()

T = TypeVar("T")


# =============================================================================
# Exceptions
# =============================================================================

class APIError(Exception):
    """Base exception for API errors."""
    
    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        error_code: Optional[str] = None,
        response: Optional[Dict] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.response = response


class RateLimitError(APIError):
    """Raised when rate limit is exceeded."""
    pass


class InsufficientBalanceError(APIError):
    """Raised when account has insufficient funds."""
    pass


class MarketClosedError(APIError):
    """Raised when trying to trade on a closed market."""
    pass


class InvalidOrderError(APIError):
    """Raised when order parameters are invalid."""
    pass


# =============================================================================
# Client
# =============================================================================

class PolymarketClient:
    """
    Async REST API client for Polymarket US.
    
    Features:
    - Ed25519 authentication
    - Rate limiting (configurable, default 10 req/sec)
    - Automatic retries with exponential backoff
    - Typed responses using Pydantic models
    
    Example:
        >>> auth = PolymarketAuth(api_key_id, private_key)
        >>> async with PolymarketClient(auth) as client:
        ...     balance = await client.get_balance()
        ...     print(f"Balance: ${balance.available_balance}")
    """
    
    DEFAULT_BASE_URL = "https://api.polymarket.us"
    DEFAULT_RATE_LIMIT = 10  # requests per second
    DEFAULT_MAX_RETRIES = 3
    DEFAULT_TIMEOUT = 30.0
    
    # Map API error codes to exceptions
    ERROR_MAP = {
        "INSUFFICIENT_BALANCE": InsufficientBalanceError,
        "MARKET_CLOSED": MarketClosedError,
        "INVALID_PRICE": InvalidOrderError,
        "INVALID_QUANTITY": InvalidOrderError,
        "RATE_LIMITED": RateLimitError,
    }
    
    def __init__(
        self,
        auth: PolymarketAuth,
        base_url: str = DEFAULT_BASE_URL,
        rate_limit: int = DEFAULT_RATE_LIMIT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        """
        Initialize the API client.
        
        Args:
            auth: PolymarketAuth instance for request signing
            base_url: API base URL (default: https://api.polymarket.us)
            rate_limit: Maximum requests per second
            max_retries: Maximum retry attempts for transient errors
            timeout: Request timeout in seconds
        """
        self.auth = auth
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.timeout = timeout
        
        # Rate limiter
        self._throttler = Throttler(rate_limit=rate_limit, period=1.0)
        
        # HTTP client (created on context enter)
        self._client: Optional[httpx.AsyncClient] = None
    
    async def __aenter__(self) -> "PolymarketClient":
        """Async context manager entry."""
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            follow_redirects=True,
        )
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        if self._client:
            await self._client.aclose()
            self._client = None
    
    async def _ensure_client(self) -> httpx.AsyncClient:
        """Ensure HTTP client is initialized."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                follow_redirects=True,
            )
        return self._client
    
    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None
    
    def _parse_error(self, response: httpx.Response) -> APIError:
        """Parse error response and return appropriate exception."""
        try:
            data = response.json()
            error = data.get("error", {})
            code = error.get("code", "UNKNOWN")
            message = error.get("message", response.text)
        except Exception:
            code = "UNKNOWN"
            message = response.text
        
        exception_class = self.ERROR_MAP.get(code, APIError)
        return exception_class(
            message=message,
            status_code=response.status_code,
            error_code=code,
            response=data if "data" in dir() else None,
        )
    
    async def _request(
        self,
        method: str,
        path: str,
        data: Optional[Dict] = None,
        params: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """
        Make an authenticated API request with retry logic.
        
        Args:
            method: HTTP method (GET, POST, etc.)
            path: API path starting with /
            data: JSON body for POST/PUT requests
            params: Query parameters
            
        Returns:
            JSON response as dictionary
            
        Raises:
            APIError: On API errors
            AuthenticationError: On authentication failures
        """
        client = await self._ensure_client()
        url = f"{self.base_url}{path}"
        
        last_exception: Optional[Exception] = None
        
        for attempt in range(self.max_retries):
            try:
                # Rate limiting
                async with self._throttler:
                    # Generate fresh auth headers for each attempt
                    headers = self.auth.sign_request(method, path)
                    
                    logger.debug(
                        "API request",
                        method=method,
                        path=path,
                        attempt=attempt + 1,
                    )
                    
                    response = await client.request(
                        method=method,
                        url=url,
                        headers=headers,
                        json=data,
                        params=params,
                    )
                    
                    # Success
                    if response.is_success:
                        return response.json()
                    
                    # Rate limit - always retry with backoff
                    if response.status_code == 429:
                        retry_after = int(response.headers.get("Retry-After", 1))
                        logger.warning(
                            "Rate limited",
                            retry_after=retry_after,
                            attempt=attempt + 1,
                        )
                        await asyncio.sleep(retry_after)
                        continue
                    
                    # Server errors - retry with exponential backoff
                    if response.status_code >= 500:
                        delay = (2 ** attempt) * 0.5  # 0.5, 1, 2 seconds
                        logger.warning(
                            "Server error, retrying",
                            status_code=response.status_code,
                            delay=delay,
                            attempt=attempt + 1,
                        )
                        await asyncio.sleep(delay)
                        continue
                    
                    # Client errors - don't retry
                    raise self._parse_error(response)
                    
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                last_exception = e
                delay = (2 ** attempt) * 0.5
                logger.warning(
                    "Network error, retrying",
                    error=str(e),
                    delay=delay,
                    attempt=attempt + 1,
                )
                await asyncio.sleep(delay)
                continue
        
        # All retries exhausted
        if last_exception:
            raise APIError(f"Request failed after {self.max_retries} attempts: {last_exception}")
        raise APIError(f"Request failed after {self.max_retries} attempts")
    
    # =========================================================================
    # Account Endpoints
    # =========================================================================
    
    async def get_balance(self) -> Balance:
        """
        Get account balance.
        
        Returns:
            Balance object with available and total balance
        """
        data = await self._request("GET", "/v1/account/balance")
        return Balance.model_validate(data)
    
    # =========================================================================
    # Portfolio Endpoints
    # =========================================================================
    
    async def get_positions(self) -> List[Position]:
        """
        Get all current positions.
        
        Returns:
            List of Position objects
        """
        data = await self._request("GET", "/v1/portfolio/positions")
        positions = data.get("positions", [])
        return [Position.model_validate(p) for p in positions]
    
    async def get_activity(self, limit: int = 100) -> List[Dict[str, Any]]:
        """
        Get account activity (trades, deposits, etc.).
        
        Args:
            limit: Maximum number of activities to return
            
        Returns:
            List of activity records
        """
        data = await self._request(
            "GET",
            "/v1/portfolio/activity",
            params={"limit": limit},
        )
        return data.get("activity", [])
    
    # =========================================================================
    # Order Endpoints
    # =========================================================================
    
    async def create_order(self, order: OrderRequest) -> CreateOrderResponse:
        """
        Create a new order.
        
        Args:
            order: OrderRequest with order details
            
        Returns:
            Order object with order ID and status
            
        Raises:
            InsufficientBalanceError: If account has insufficient funds
            MarketClosedError: If market is not accepting orders
            InvalidOrderError: If order parameters are invalid
        """
        payload = order.to_api_payload()
        data = await self._request("POST", "/v1/orders", data=payload)
        return CreateOrderResponse.model_validate(data)
    
    async def preview_order(self, order: OrderRequest) -> OrderPreview:
        """
        Preview an order before submitting.
        
        Args:
            order: OrderRequest with order details
            
        Returns:
            OrderPreview with estimated fill, fees, etc.
        """
        payload = order.to_api_payload()
        data = await self._request("POST", "/v1/order/preview", data=payload)
        return OrderPreview.model_validate(data.get("estimatedFill", data))
    
    async def get_open_orders(
        self,
        market_slug: Optional[str] = None,
    ) -> List[Order]:
        """
        Get all open orders.
        
        Args:
            market_slug: Optional filter by market
            
        Returns:
            List of open Order objects
        """
        params = {}
        if market_slug:
            params["marketSlug"] = market_slug
        
        data = await self._request("GET", "/v1/orders/open", params=params)
        orders = data.get("orders", [])
        return [Order.model_validate(o) for o in orders]
    
    async def get_order(self, order_id: str) -> Order:
        """
        Get order details by ID.
        
        Args:
            order_id: The order UUID
            
        Returns:
            Order object with full details
        """
        data = await self._request("GET", f"/v1/order/{order_id}")
        return Order.model_validate(data)
    
    async def cancel_order(self, order_id: str) -> Order:
        """
        Cancel a specific order.
        
        Args:
            order_id: The order UUID to cancel
            
        Returns:
            Order object with updated status
        """
        data = await self._request("POST", f"/v1/order/{order_id}/cancel")
        return Order.model_validate(data)
    
    async def cancel_all_orders(
        self,
        market_slug: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Cancel all open orders.
        
        Args:
            market_slug: Optional filter to cancel only orders in this market
            
        Returns:
            Response with cancellation details
        """
        params = {}
        if market_slug:
            params["marketSlug"] = market_slug
        
        return await self._request("POST", "/v1/orders/open/cancel", params=params)
    
    async def modify_order(
        self,
        order_id: str,
        price: Optional[Decimal] = None,
        quantity: Optional[int] = None,
    ) -> Order:
        """
        Modify an existing order.
        
        Args:
            order_id: The order UUID to modify
            price: New price (optional)
            quantity: New quantity (optional)
            
        Returns:
            Order object with updated details
        """
        payload = {}
        if price is not None:
            payload["price"] = {"value": str(price), "currency": "USD"}
        if quantity is not None:
            payload["quantity"] = quantity
        
        data = await self._request(
            "POST",
            f"/v1/order/{order_id}/modify",
            data=payload,
        )
        return Order.model_validate(data)
    
    async def close_position(self, market_slug: str) -> Dict[str, Any]:
        """
        Close entire position in a market.
        
        Args:
            market_slug: The market to close position in
            
        Returns:
            Response with close details
        """
        return await self._request(
            "POST",
            "/v1/order/close-position",
            data={"marketSlug": market_slug},
        )
    
    # =========================================================================
    # Market Endpoints
    # =========================================================================
    
    async def get_markets(
        self,
        status: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Market]:
        """
        Get list of available markets.
        
        Args:
            status: Filter by status (OPEN, CLOSED, RESOLVED)
            category: Filter by category (NBA, NFL, etc.)
            limit: Maximum results (default 100)
            offset: Pagination offset
            
        Returns:
            List of Market objects
        """
        params = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status
        if category:
            params["category"] = category
        
        data = await self._request("GET", "/v1/markets", params=params)
        markets = data.get("markets", [])
        return [Market.model_validate(m) for m in markets]
    
    async def get_market(self, market_slug: str) -> Market:
        """
        Get market details by slug.
        
        Args:
            market_slug: The market slug/identifier
            
        Returns:
            Market object with full details
        """
        data = await self._request("GET", f"/v1/market/{market_slug}")
        return Market.model_validate(data)
    
    async def get_market_sides(self, market_slug: str) -> OrderBook:
        """
        Get order book for a market.
        
        Args:
            market_slug: The market slug/identifier
            
        Returns:
            OrderBook with YES and NO sides
        """
        data = await self._request("GET", f"/v1/market/{market_slug}/sides")
        
        # Parse order book sides
        def parse_side(side_data: Dict) -> OrderBookSide:
            bids = [
                PriceLevel(price=Decimal(p["price"]), quantity=int(p["quantity"]))
                for p in side_data.get("bids", [])
            ]
            asks = [
                PriceLevel(price=Decimal(p["price"]), quantity=int(p["quantity"]))
                for p in side_data.get("asks", [])
            ]
            return OrderBookSide(bids=bids, asks=asks)
        
        return OrderBook(
            market_slug=data.get("marketSlug", market_slug),
            yes=parse_side(data.get("yes", {})),
            no=parse_side(data.get("no", {})),
        )
    
    # =========================================================================
    # Helper Methods
    # =========================================================================
    
    async def buy_yes(
        self,
        market_slug: str,
        quantity: int,
        price: Decimal,
    ) -> Order:
        """
        Convenience method to buy YES shares.
        
        Args:
            market_slug: The market to trade
            quantity: Number of contracts
            price: Limit price (0.01 to 0.99)
            
        Returns:
            Order object
        """
        from ..data.models import Price, OrderIntent
        
        order = OrderRequest(
            market_slug=market_slug,
            quantity=quantity,
            price=Price(value=str(price)),
            intent=OrderIntent.BUY_LONG,
        )
        return await self.create_order(order)
    
    async def buy_no(
        self,
        market_slug: str,
        quantity: int,
        price: Decimal,
    ) -> Order:
        """
        Convenience method to buy NO shares.
        
        Args:
            market_slug: The market to trade
            quantity: Number of contracts
            price: Limit price (0.01 to 0.99)
            
        Returns:
            Order object
        """
        from ..data.models import Price, OrderIntent
        
        order = OrderRequest(
            market_slug=market_slug,
            quantity=quantity,
            price=Price(value=str(price)),
            intent=OrderIntent.BUY_SHORT,
        )
        return await self.create_order(order)
    
    async def sell_yes(
        self,
        market_slug: str,
        quantity: int,
        price: Decimal,
    ) -> Order:
        """
        Convenience method to sell YES shares.
        
        Args:
            market_slug: The market to trade
            quantity: Number of contracts
            price: Limit price (0.01 to 0.99)
            
        Returns:
            Order object
        """
        from ..data.models import Price, OrderIntent
        
        order = OrderRequest(
            market_slug=market_slug,
            quantity=quantity,
            price=Price(value=str(price)),
            intent=OrderIntent.SELL_LONG,
        )
        return await self.create_order(order)
    
    async def sell_no(
        self,
        market_slug: str,
        quantity: int,
        price: Decimal,
    ) -> Order:
        """
        Convenience method to sell NO shares.
        
        Args:
            market_slug: The market to trade
            quantity: Number of contracts
            price: Limit price (0.01 to 0.99)
            
        Returns:
            Order object
        """
        from ..data.models import Price, OrderIntent
        
        order = OrderRequest(
            market_slug=market_slug,
            quantity=quantity,
            price=Price(value=str(price)),
            intent=OrderIntent.SELL_SHORT,
        )
        return await self.create_order(order)
