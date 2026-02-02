"""
Order book tracker for Polymarket US markets.

This module provides a thread-safe local order book manager that maintains
order book state for subscribed markets and provides price calculations.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Lock
from typing import Dict, List, Optional, Tuple

import structlog

from .models import OrderBookSide, PriceLevel, Side

logger = structlog.get_logger()


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class OrderBookState:
    """
    Current state of an order book for a market.
    
    Attributes:
        market_slug: Market identifier
        yes: YES side order book
        no: NO side order book
        last_update: Timestamp of last update
        sequence: Sequence number for ordering (if provided by API)
    """
    market_slug: str
    yes: OrderBookSide = field(default_factory=lambda: OrderBookSide(bids=[], asks=[]))
    no: OrderBookSide = field(default_factory=lambda: OrderBookSide(bids=[], asks=[]))
    last_update: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    sequence: int = 0
    
    @property
    def yes_best_bid(self) -> Optional[Decimal]:
        """Get best bid price for YES side."""
        return self.yes.best_bid
    
    @property
    def yes_best_ask(self) -> Optional[Decimal]:
        """Get best ask price for YES side."""
        return self.yes.best_ask
    
    @property
    def yes_spread(self) -> Optional[Decimal]:
        """Get bid-ask spread for YES side."""
        return self.yes.spread
    
    @property
    def yes_mid_price(self) -> Optional[Decimal]:
        """Get mid-price for YES side."""
        if self.yes.best_bid is not None and self.yes.best_ask is not None:
            return (self.yes.best_bid + self.yes.best_ask) / 2
        return None
    
    @property
    def no_best_bid(self) -> Optional[Decimal]:
        """Get best bid price for NO side."""
        return self.no.best_bid
    
    @property
    def no_best_ask(self) -> Optional[Decimal]:
        """Get best ask price for NO side."""
        return self.no.best_ask
    
    @property
    def no_spread(self) -> Optional[Decimal]:
        """Get bid-ask spread for NO side."""
        return self.no.spread
    
    @property
    def no_mid_price(self) -> Optional[Decimal]:
        """Get mid-price for NO side."""
        if self.no.best_bid is not None and self.no.best_ask is not None:
            return (self.no.best_bid + self.no.best_ask) / 2
        return None
    
    def is_stale(self, max_age: timedelta = timedelta(seconds=30)) -> bool:
        """
        Check if order book data is stale.
        
        Args:
            max_age: Maximum age before considered stale
            
        Returns:
            True if data is older than max_age
        """
        now = datetime.now(timezone.utc)
        # Handle timezone-naive datetimes for backwards compatibility
        if self.last_update.tzinfo is None:
            last = self.last_update.replace(tzinfo=timezone.utc)
        else:
            last = self.last_update
        return now - last > max_age


# =============================================================================
# Order Book Tracker
# =============================================================================

class OrderBookTracker:
    """
    Thread-safe order book manager for multiple markets.
    
    Maintains local order book state updated from WebSocket messages
    and provides price/depth calculations.
    
    Example:
        >>> tracker = OrderBookTracker()
        >>> 
        >>> # Update from WebSocket message
        >>> tracker.update("nba-lakers-vs-celtics", {
        ...     "yes": {
        ...         "bids": [["0.47", "500"], ["0.46", "1000"]],
        ...         "asks": [["0.49", "300"], ["0.50", "800"]]
        ...     },
        ...     "no": {
        ...         "bids": [["0.51", "400"]],
        ...         "asks": [["0.53", "350"]]
        ...     }
        ... })
        >>> 
        >>> # Get prices
        >>> print(tracker.best_bid("nba-lakers-vs-celtics"))  # Decimal("0.47")
        >>> print(tracker.mid_price("nba-lakers-vs-celtics"))  # Decimal("0.48")
    """
    
    def __init__(self, stale_timeout: timedelta = timedelta(seconds=30)):
        """
        Initialize order book tracker.
        
        Args:
            stale_timeout: How long before order book data is considered stale
        """
        self._books: Dict[str, OrderBookState] = {}
        self._lock = Lock()
        self._async_lock = asyncio.Lock()
        self._stale_timeout = stale_timeout
    
    # =========================================================================
    # State Management
    # =========================================================================
    
    def update(
        self,
        market_slug: str,
        data: Dict,
        sequence: Optional[int] = None,
    ) -> None:
        """
        Update order book state from WebSocket message data.
        
        Args:
            market_slug: Market identifier
            data: Order book data dict with 'yes' and 'no' sides
            sequence: Optional sequence number for ordering
        """
        with self._lock:
            self._update_internal(market_slug, data, sequence)
    
    async def update_async(
        self,
        market_slug: str,
        data: Dict,
        sequence: Optional[int] = None,
    ) -> None:
        """
        Async version of update for use in async contexts.
        
        Args:
            market_slug: Market identifier
            data: Order book data dict with 'yes' and 'no' sides
            sequence: Optional sequence number for ordering
        """
        async with self._async_lock:
            self._update_internal(market_slug, data, sequence)
    
    def _update_internal(
        self,
        market_slug: str,
        data: Dict,
        sequence: Optional[int] = None,
    ) -> None:
        """
        Internal update logic (must be called with lock held).
        """
        current = self._books.get(market_slug)
        
        # Check sequence to avoid out-of-order updates
        if current and sequence is not None:
            if sequence <= current.sequence:
                logger.debug(
                    "Ignoring out-of-order update",
                    market_slug=market_slug,
                    current_seq=current.sequence,
                    received_seq=sequence,
                )
                return
        
        # Parse order book sides.
        #
        # Documented format is {"yes": {"bids": ..., "asks": ...}, "no": ...}
        # but live sports feeds can emit top-level {"bids": ..., "offers": ...}.
        yes_data = data.get("yes")
        no_data = data.get("no")
        if not yes_data and ("bids" in data or "offers" in data or "asks" in data):
            yes_data = {
                "bids": data.get("bids", []),
                "asks": data.get("offers", data.get("asks", [])),
            }
        else:
            yes_data = yes_data or {}
        no_data = no_data or {}

        yes_side = self._parse_side(yes_data)
        no_side = self._parse_side(no_data)
        
        # Create or update state
        self._books[market_slug] = OrderBookState(
            market_slug=market_slug,
            yes=yes_side,
            no=no_side,
            last_update=datetime.now(timezone.utc),
            sequence=sequence or (current.sequence + 1 if current else 0),
        )
        
        logger.debug(
            "Order book updated",
            market_slug=market_slug,
            yes_bids=len(yes_side.bids),
            yes_asks=len(yes_side.asks),
        )
    
    def _parse_side(self, side_data: Dict) -> OrderBookSide:
        """
        Parse order book side from API format.
        
        Args:
            side_data: Dict with 'bids' and 'asks' as lists of [price, quantity]
            
        Returns:
            OrderBookSide with parsed PriceLevel objects
        """
        bids = []
        asks = []
        
        bids_levels = side_data.get("bids", [])
        asks_levels = side_data.get("asks", side_data.get("offers", []))

        def _parse_qty(raw: object) -> Optional[int]:
            try:
                return int(Decimal(str(raw)))
            except Exception:
                return None

        def _parse_level(level: object) -> Optional[PriceLevel]:
            if isinstance(level, (list, tuple)) and len(level) >= 2:
                price, quantity = level[0], level[1]
                qty = _parse_qty(quantity)
                if qty is None:
                    return None
                return PriceLevel(
                    price=Decimal(str(price)),
                    quantity=qty,
                )
            if isinstance(level, dict):
                if "px" in level:
                    px = level.get("px")
                    if isinstance(px, dict):
                        price = px.get("value", "0")
                    else:
                        price = px
                    quantity = level.get("qty", 0)
                else:
                    price = level.get("price", "0")
                    quantity = level.get("quantity", level.get("size", 0))
                qty = _parse_qty(quantity)
                if qty is None:
                    return None
                return PriceLevel(
                    price=Decimal(str(price)),
                    quantity=qty,
                )
            return None

        # Parse bids: [[price, quantity], ...]
        for level in bids_levels:
            parsed = _parse_level(level)
            if parsed is not None:
                bids.append(parsed)
        
        # Parse asks
        for level in asks_levels:
            parsed = _parse_level(level)
            if parsed is not None:
                asks.append(parsed)
        
        # Sort: bids descending (best first), asks ascending (best first)
        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)
        
        return OrderBookSide(bids=bids, asks=asks)
    
    def get(self, market_slug: str) -> Optional[OrderBookState]:
        """
        Get order book state for a market.
        
        Args:
            market_slug: Market identifier
            
        Returns:
            OrderBookState if exists, None otherwise
        """
        with self._lock:
            return self._books.get(market_slug)
    
    def get_all(self) -> Dict[str, OrderBookState]:
        """
        Get all order book states.
        
        Returns:
            Dictionary of market_slug to OrderBookState
        """
        with self._lock:
            return dict(self._books)
    
    def remove(self, market_slug: str) -> None:
        """
        Remove order book for a market.
        
        Args:
            market_slug: Market identifier
        """
        with self._lock:
            self._books.pop(market_slug, None)
    
    def clear(self) -> None:
        """Clear all order book data."""
        with self._lock:
            self._books.clear()
    
    def markets(self) -> List[str]:
        """
        Get list of tracked market slugs.
        
        Returns:
            List of market slugs
        """
        with self._lock:
            return list(self._books.keys())
    
    # =========================================================================
    # Price Helpers
    # =========================================================================
    
    def best_bid(
        self,
        market_slug: str,
        side: str = "YES",
    ) -> Optional[Decimal]:
        """
        Get best bid price for a market side.
        
        Args:
            market_slug: Market identifier
            side: "YES" or "NO"
            
        Returns:
            Best bid price or None if not available
        """
        with self._lock:
            book = self._books.get(market_slug)
            if not book:
                return None
            
            order_side = book.yes if side.upper() == "YES" else book.no
            return order_side.best_bid
    
    def best_ask(
        self,
        market_slug: str,
        side: str = "YES",
    ) -> Optional[Decimal]:
        """
        Get best ask price for a market side.
        
        Args:
            market_slug: Market identifier
            side: "YES" or "NO"
            
        Returns:
            Best ask price or None if not available
        """
        with self._lock:
            book = self._books.get(market_slug)
            if not book:
                return None
            
            order_side = book.yes if side.upper() == "YES" else book.no
            return order_side.best_ask
    
    def mid_price(
        self,
        market_slug: str,
        side: str = "YES",
    ) -> Optional[Decimal]:
        """
        Get mid-price for a market side.
        
        Args:
            market_slug: Market identifier
            side: "YES" or "NO"
            
        Returns:
            Mid-price or None if not available
        """
        with self._lock:
            book = self._books.get(market_slug)
            if not book:
                return None
            
            if side.upper() == "YES":
                return book.yes_mid_price
            else:
                return book.no_mid_price
    
    def spread(
        self,
        market_slug: str,
        side: str = "YES",
    ) -> Optional[Decimal]:
        """
        Get bid-ask spread for a market side.
        
        Args:
            market_slug: Market identifier
            side: "YES" or "NO"
            
        Returns:
            Spread or None if not available
        """
        with self._lock:
            book = self._books.get(market_slug)
            if not book:
                return None
            
            order_side = book.yes if side.upper() == "YES" else book.no
            return order_side.spread
    
    def spread_bps(
        self,
        market_slug: str,
        side: str = "YES",
    ) -> Optional[Decimal]:
        """
        Get bid-ask spread in basis points.
        
        Args:
            market_slug: Market identifier
            side: "YES" or "NO"
            
        Returns:
            Spread in bps or None if not available
        """
        mid = self.mid_price(market_slug, side)
        spread = self.spread(market_slug, side)
        
        if mid and spread and mid > 0:
            return (spread / mid) * Decimal("10000")
        return None
    
    # =========================================================================
    # Depth Analysis
    # =========================================================================
    
    def depth_at_price(
        self,
        market_slug: str,
        side: str,
        price: Decimal,
        is_bid: bool = True,
    ) -> int:
        """
        Get total quantity available at a specific price level.
        
        Args:
            market_slug: Market identifier
            side: "YES" or "NO"
            price: Price level to check
            is_bid: True for bid side, False for ask side
            
        Returns:
            Quantity at that price level
        """
        with self._lock:
            book = self._books.get(market_slug)
            if not book:
                return 0
            
            order_side = book.yes if side.upper() == "YES" else book.no
            levels = order_side.bids if is_bid else order_side.asks
            
            for level in levels:
                if level.price == price:
                    return level.quantity
            
            return 0
    
    def total_depth(
        self,
        market_slug: str,
        side: str = "YES",
        is_bid: bool = True,
    ) -> Decimal:
        """
        Get total notional depth (price * quantity) on one side.
        
        Args:
            market_slug: Market identifier
            side: "YES" or "NO"
            is_bid: True for bid side, False for ask side
            
        Returns:
            Total notional depth
        """
        with self._lock:
            book = self._books.get(market_slug)
            if not book:
                return Decimal("0")
            
            order_side = book.yes if side.upper() == "YES" else book.no
            levels = order_side.bids if is_bid else order_side.asks
            
            total = Decimal("0")
            for level in levels:
                total += level.price * level.quantity
            
            return total
    
    def liquidity_within_bps(
        self,
        market_slug: str,
        side: str,
        bps: int,
        is_bid: bool = True,
    ) -> Tuple[Decimal, int]:
        """
        Get total liquidity within a certain number of basis points from best price.
        
        Args:
            market_slug: Market identifier
            side: "YES" or "NO"
            bps: Basis points from best price
            is_bid: True for bid side, False for ask side
            
        Returns:
            Tuple of (notional_value, quantity) within range
        """
        with self._lock:
            book = self._books.get(market_slug)
            if not book:
                return Decimal("0"), 0
            
            order_side = book.yes if side.upper() == "YES" else book.no
            levels = order_side.bids if is_bid else order_side.asks
            
            if not levels:
                return Decimal("0"), 0
            
            best_price = levels[0].price
            if best_price == 0:
                return Decimal("0"), 0
            
            # Calculate price threshold
            bps_decimal = Decimal(str(bps)) / Decimal("10000")
            
            if is_bid:
                # For bids, we look for prices >= (best - threshold)
                threshold = best_price * (Decimal("1") - bps_decimal)
                matching = [l for l in levels if l.price >= threshold]
            else:
                # For asks, we look for prices <= (best + threshold)
                threshold = best_price * (Decimal("1") + bps_decimal)
                matching = [l for l in levels if l.price <= threshold]
            
            total_notional = sum(l.price * l.quantity for l in matching)
            total_quantity = sum(l.quantity for l in matching)
            
            return total_notional, total_quantity
    
    # =========================================================================
    # Staleness
    # =========================================================================
    
    def is_stale(self, market_slug: str) -> bool:
        """
        Check if order book data is stale.
        
        Args:
            market_slug: Market identifier
            
        Returns:
            True if data is stale or doesn't exist
        """
        with self._lock:
            book = self._books.get(market_slug)
            if not book:
                return True
            return book.is_stale(self._stale_timeout)
    
    def get_stale_markets(self) -> List[str]:
        """
        Get list of markets with stale data.
        
        Returns:
            List of market slugs with stale data
        """
        with self._lock:
            return [
                slug for slug, book in self._books.items()
                if book.is_stale(self._stale_timeout)
            ]
    
    def prune_stale(self) -> int:
        """
        Remove all stale order books.
        
        Returns:
            Number of order books removed
        """
        with self._lock:
            stale = [
                slug for slug, book in self._books.items()
                if book.is_stale(self._stale_timeout)
            ]
            
            for slug in stale:
                del self._books[slug]
            
            if stale:
                logger.info("Pruned stale order books", count=len(stale))
            
            return len(stale)


# =============================================================================
# WebSocket Handler Helper
# =============================================================================

def create_orderbook_handler(tracker: OrderBookTracker):
    """
    Create a WebSocket message handler that updates an OrderBookTracker.
    
    Args:
        tracker: OrderBookTracker instance to update
        
    Returns:
        Async handler function for WebSocket messages
        
    Example:
        >>> tracker = OrderBookTracker()
        >>> handler = create_orderbook_handler(tracker)
        >>> ws.on("MARKET_DATA", handler)
    """
    async def handler(data: Dict) -> None:
        if data.get("type") != "MARKET_DATA":
            return
        
        market_slug = data.get("marketSlug")
        if not market_slug:
            return

        # Live feeds may emit:
        # - top-level "bids"/"offers" rather than nested {"yes": {"bids":..., "asks":...}}
        # - top-of-book scalars like yesBid/yesAsk/noBid/noAsk rather than full depth arrays
        # Normalize to the tracker format so best bid/ask populate correctly.
        normalized = data
        if "yes" not in data and ("bids" in data or "offers" in data):
            normalized = dict(data)
            normalized["yes"] = {
                "bids": data.get("bids", []),
                "asks": data.get("offers", []),
            }
            normalized.setdefault("no", {"bids": [], "asks": []})
        elif "yes" not in data and (
            "yesBid" in data
            or "yesAsk" in data
            or "noBid" in data
            or "noAsk" in data
            or "yes_bid" in data
            or "yes_ask" in data
            or "no_bid" in data
            or "no_ask" in data
        ):
            # Synthesize a one-level book from top-of-book fields.
            def _px(raw):
                if raw is None:
                    return None
                if isinstance(raw, dict) and "value" in raw:
                    return raw.get("value")
                return raw

            yes_bid = _px(data.get("yesBid", data.get("yes_bid")))
            yes_ask = _px(data.get("yesAsk", data.get("yes_ask")))
            no_bid = _px(data.get("noBid", data.get("no_bid")))
            no_ask = _px(data.get("noAsk", data.get("no_ask")))

            yes_bid_size = data.get("yesBidSize", data.get("yes_bid_size", 0))
            yes_ask_size = data.get("yesAskSize", data.get("yes_ask_size", 0))
            no_bid_size = data.get("noBidSize", data.get("no_bid_size", 0))
            no_ask_size = data.get("noAskSize", data.get("no_ask_size", 0))

            normalized = dict(data)
            normalized["yes"] = {
                "bids": [[yes_bid, yes_bid_size]] if yes_bid is not None else [],
                "asks": [[yes_ask, yes_ask_size]] if yes_ask is not None else [],
            }
            normalized["no"] = {
                "bids": [[no_bid, no_bid_size]] if no_bid is not None else [],
                "asks": [[no_ask, no_ask_size]] if no_ask is not None else [],
            }

        await tracker.update_async(
            market_slug=market_slug,
            data=normalized,
            sequence=normalized.get("sequence"),
        )
    
    return handler
