"""
Tests for WebSocket manager and order book tracker modules.

Run with: pytest tests/test_websocket.py -v
"""

import asyncio
import base64
import json
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def mock_auth():
    """Create a mock auth object for testing."""
    private_key = ed25519.Ed25519PrivateKey.generate()
    private_key_b64 = base64.b64encode(private_key.private_bytes_raw()).decode()
    
    from src.api.auth import PolymarketAuth
    return PolymarketAuth(
        api_key_id="test-api-key",
        private_key_base64=private_key_b64,
    )


@pytest.fixture
def orderbook_tracker():
    """Create an OrderBookTracker for testing."""
    from src.data.orderbook import OrderBookTracker
    return OrderBookTracker()


@pytest.fixture
def sample_market_data():
    """Sample market data message from WebSocket."""
    return {
        "type": "MARKET_DATA",
        "marketSlug": "nba-lakers-vs-celtics-2025-01-25",
        "timestamp": "2025-01-25T12:00:00.123Z",
        "yes": {
            "bids": [["0.47", "500"], ["0.46", "1000"], ["0.45", "2000"]],
            "asks": [["0.49", "300"], ["0.50", "800"], ["0.51", "1500"]]
        },
        "no": {
            "bids": [["0.51", "400"], ["0.50", "600"]],
            "asks": [["0.53", "350"], ["0.54", "700"]]
        }
    }


@pytest.fixture
def sample_market_data_bids_offers():
    """Sample live-style market data message with top-level bids/offers."""
    return {
        "type": "MARKET_DATA",
        "marketSlug": "nba-lakers-vs-celtics-2025-01-25",
        "timestamp": "2025-01-25T12:00:00.123Z",
        "bids": [
            {"px": {"value": "0.47", "currency": "USD"}, "qty": "500.000"},
            {"px": {"value": "0.46", "currency": "USD"}, "qty": "1000.000"},
            {"px": {"value": "0.45", "currency": "USD"}, "qty": "2000.000"},
        ],
        "offers": [
            {"px": {"value": "0.49", "currency": "USD"}, "qty": "300.000"},
            {"px": {"value": "0.50", "currency": "USD"}, "qty": "800.000"},
            {"px": {"value": "0.51", "currency": "USD"}, "qty": "1500.000"},
        ],
    }


# =============================================================================
# WebSocket Manager Tests
# =============================================================================

class TestPolymarketWebSocket:
    """Tests for PolymarketWebSocket class."""
    
    def test_websocket_init(self, mock_auth):
        """Test WebSocket initialization."""
        from src.api.websocket import PolymarketWebSocket, ConnectionState
        
        ws = PolymarketWebSocket(mock_auth)
        
        assert ws.auth == mock_auth
        assert ws.state == ConnectionState.DISCONNECTED
        assert not ws.is_connected
        assert ws.subscriptions == {}
    
    def test_websocket_custom_base_url(self, mock_auth):
        """Test WebSocket with custom base URL."""
        from src.api.websocket import PolymarketWebSocket
        
        ws = PolymarketWebSocket(
            mock_auth,
            base_url="wss://custom.example.com/v1/ws/"
        )
        
        assert ws.base_url == "wss://custom.example.com/v1/ws"
    
    def test_handler_registration(self, mock_auth):
        """Test event handler registration."""
        from src.api.websocket import PolymarketWebSocket
        
        ws = PolymarketWebSocket(mock_auth)
        
        async def handler1(data):
            pass
        
        async def handler2(data):
            pass
        
        # Register handlers
        ws.on("MARKET_DATA", handler1)
        ws.on("MARKET_DATA", handler2)
        ws.on("ORDER_UPDATE", handler1)
        
        assert len(ws._handlers["MARKET_DATA"]) == 2
        assert len(ws._handlers["ORDER_UPDATE"]) == 1
    
    def test_handler_deregistration(self, mock_auth):
        """Test event handler deregistration."""
        from src.api.websocket import PolymarketWebSocket
        
        ws = PolymarketWebSocket(mock_auth)
        
        async def handler(data):
            pass
        
        ws.on("MARKET_DATA", handler)
        assert len(ws._handlers["MARKET_DATA"]) == 1
        
        ws.off("MARKET_DATA", handler)
        assert len(ws._handlers["MARKET_DATA"]) == 0
    
    def test_handler_duplicate_prevention(self, mock_auth):
        """Test that duplicate handlers are not added."""
        from src.api.websocket import PolymarketWebSocket
        
        ws = PolymarketWebSocket(mock_auth)
        
        async def handler(data):
            pass
        
        ws.on("MARKET_DATA", handler)
        ws.on("MARKET_DATA", handler)  # Duplicate
        
        assert len(ws._handlers["MARKET_DATA"]) == 1
    
    def test_clear_handlers(self, mock_auth):
        """Test clearing handlers."""
        from src.api.websocket import PolymarketWebSocket
        
        ws = PolymarketWebSocket(mock_auth)
        
        async def handler(data):
            pass
        
        ws.on("MARKET_DATA", handler)
        ws.on("ORDER_UPDATE", handler)
        
        # Clear specific type
        ws.clear_handlers("MARKET_DATA")
        assert "MARKET_DATA" not in ws._handlers
        assert "ORDER_UPDATE" in ws._handlers
        
        # Clear all
        ws.clear_handlers()
        assert ws._handlers == {}
    
    @pytest.mark.asyncio
    async def test_context_manager(self, mock_auth):
        """Test async context manager."""
        from src.api.websocket import PolymarketWebSocket
        
        async with PolymarketWebSocket(mock_auth) as ws:
            assert ws is not None
    
    @pytest.mark.asyncio
    async def test_subscribe_not_connected(self, mock_auth):
        """Test subscribe fails when not connected."""
        from src.api.websocket import (
            PolymarketWebSocket,
            SubscriptionType,
            SubscriptionError,
        )
        
        ws = PolymarketWebSocket(mock_auth)
        
        with pytest.raises(SubscriptionError, match="Not connected"):
            await ws.subscribe(SubscriptionType.MARKET_DATA, ["test-market"])
    
    @pytest.mark.asyncio
    async def test_message_dispatch(self, mock_auth):
        """Test message dispatching to handlers."""
        from src.api.websocket import PolymarketWebSocket
        
        ws = PolymarketWebSocket(mock_auth)
        
        received = []
        
        async def handler(data):
            received.append(data)
        
        ws.on("MARKET_DATA", handler)
        
        # Simulate message handling
        await ws._handle_message('{"type": "MARKET_DATA", "value": 1}')
        
        assert len(received) == 1
        assert received[0]["type"] == "MARKET_DATA"
        assert received[0]["value"] == 1

    @pytest.mark.asyncio
    async def test_message_dispatch_enveloped_market_data(self, mock_auth):
        """Test dispatching for subscriptionType-wrapped market data."""
        from src.api.websocket import PolymarketWebSocket

        ws = PolymarketWebSocket(mock_auth)
        received = []

        async def handler(data):
            received.append(data)

        ws.on("MARKET_DATA", handler)

        message = {
            "requestId": "sub_market_data_1",
            "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
            "marketData": {
                "marketSlug": "nba-lakers-vs-celtics-2025-01-25",
                "yes": {"bids": [], "asks": []},
                "no": {"bids": [], "asks": []},
            },
        }

        await ws._handle_message(json.dumps(message))

        assert len(received) == 1
        assert received[0]["type"] == "MARKET_DATA"
        assert received[0]["marketSlug"] == "nba-lakers-vs-celtics-2025-01-25"
        assert received[0]["requestId"] == "sub_market_data_1"
    
    @pytest.mark.asyncio
    async def test_wildcard_handler(self, mock_auth):
        """Test wildcard handler receives all messages."""
        from src.api.websocket import PolymarketWebSocket
        
        ws = PolymarketWebSocket(mock_auth)
        
        received = []
        
        async def wildcard_handler(data):
            received.append(data)
        
        ws.on("*", wildcard_handler)
        
        await ws._handle_message('{"type": "MARKET_DATA"}')
        await ws._handle_message('{"type": "ORDER_UPDATE"}')
        await ws._handle_message('{"type": "UNKNOWN_TYPE"}')
        
        assert len(received) == 3
    
    @pytest.mark.asyncio
    async def test_invalid_json_handling(self, mock_auth):
        """Test handling of invalid JSON messages."""
        from src.api.websocket import PolymarketWebSocket
        
        ws = PolymarketWebSocket(mock_auth)
        
        received = []
        
        async def handler(data):
            received.append(data)
        
        ws.on("*", handler)
        
        # This should not raise, just log error
        await ws._handle_message("not valid json")
        
        assert len(received) == 0
    
    @pytest.mark.asyncio
    async def test_handler_error_isolation(self, mock_auth):
        """Test that handler errors don't affect other handlers."""
        from src.api.websocket import PolymarketWebSocket
        
        ws = PolymarketWebSocket(mock_auth)
        
        results = []
        
        async def failing_handler(data):
            raise ValueError("Test error")
        
        async def good_handler(data):
            results.append(data)
        
        ws.on("TEST", failing_handler)
        ws.on("TEST", good_handler)
        
        await ws._handle_message('{"type": "TEST"}')
        
        # Good handler should still be called
        assert len(results) == 1


class TestSubscriptionType:
    """Tests for SubscriptionType enum."""
    
    def test_subscription_types(self):
        """Test subscription type values."""
        from src.api.websocket import SubscriptionType
        
        assert SubscriptionType.MARKET_DATA.value == "SUBSCRIPTION_TYPE_MARKET_DATA"
        assert SubscriptionType.ORDER.value == "SUBSCRIPTION_TYPE_ORDER"
        assert SubscriptionType.POSITION.value == "SUBSCRIPTION_TYPE_POSITION"


class TestConnectionState:
    """Tests for ConnectionState enum."""
    
    def test_connection_states(self):
        """Test connection state values."""
        from src.api.websocket import ConnectionState
        
        assert ConnectionState.DISCONNECTED.value == "disconnected"
        assert ConnectionState.CONNECTED.value == "connected"
        assert ConnectionState.RECONNECTING.value == "reconnecting"


# =============================================================================
# Order Book Tracker Tests
# =============================================================================

class TestOrderBookTracker:
    """Tests for OrderBookTracker class."""
    
    def test_tracker_init(self, orderbook_tracker):
        """Test tracker initialization."""
        assert orderbook_tracker.markets() == []
        assert orderbook_tracker.get_all() == {}
    
    def test_update_from_websocket_format(
        self,
        orderbook_tracker,
        sample_market_data,
    ):
        """Test updating from WebSocket message format."""
        orderbook_tracker.update(
            market_slug=sample_market_data["marketSlug"],
            data=sample_market_data,
        )
        
        book = orderbook_tracker.get(sample_market_data["marketSlug"])
        
        assert book is not None
        assert book.market_slug == "nba-lakers-vs-celtics-2025-01-25"
        
        # Check YES side
        assert len(book.yes.bids) == 3
        assert len(book.yes.asks) == 3
        
        # Best bid should be highest
        assert book.yes.bids[0].price == Decimal("0.47")
        assert book.yes.bids[0].quantity == 500
        
        # Best ask should be lowest
        assert book.yes.asks[0].price == Decimal("0.49")
        assert book.yes.asks[0].quantity == 300
    
    def test_best_bid_ask(self, orderbook_tracker, sample_market_data):
        """Test best bid/ask getters."""
        orderbook_tracker.update(
            market_slug=sample_market_data["marketSlug"],
            data=sample_market_data,
        )
        
        market = sample_market_data["marketSlug"]
        
        assert orderbook_tracker.best_bid(market, "YES") == Decimal("0.47")
        assert orderbook_tracker.best_ask(market, "YES") == Decimal("0.49")
        assert orderbook_tracker.best_bid(market, "NO") == Decimal("0.51")
        assert orderbook_tracker.best_ask(market, "NO") == Decimal("0.53")
    
    def test_mid_price(self, orderbook_tracker, sample_market_data):
        """Test mid-price calculation."""
        orderbook_tracker.update(
            market_slug=sample_market_data["marketSlug"],
            data=sample_market_data,
        )
        
        market = sample_market_data["marketSlug"]
        
        # (0.47 + 0.49) / 2 = 0.48
        assert orderbook_tracker.mid_price(market, "YES") == Decimal("0.48")
        
        # (0.51 + 0.53) / 2 = 0.52
        assert orderbook_tracker.mid_price(market, "NO") == Decimal("0.52")
    
    def test_spread(self, orderbook_tracker, sample_market_data):
        """Test spread calculation."""
        orderbook_tracker.update(
            market_slug=sample_market_data["marketSlug"],
            data=sample_market_data,
        )
        
        market = sample_market_data["marketSlug"]
        
        # 0.49 - 0.47 = 0.02
        assert orderbook_tracker.spread(market, "YES") == Decimal("0.02")
        
        # 0.53 - 0.51 = 0.02
        assert orderbook_tracker.spread(market, "NO") == Decimal("0.02")
    
    def test_spread_bps(self, orderbook_tracker, sample_market_data):
        """Test spread in basis points."""
        orderbook_tracker.update(
            market_slug=sample_market_data["marketSlug"],
            data=sample_market_data,
        )
        
        market = sample_market_data["marketSlug"]
        
        # spread / mid * 10000 = 0.02 / 0.48 * 10000 ≈ 416.67
        spread_bps = orderbook_tracker.spread_bps(market, "YES")
        assert spread_bps is not None
        assert Decimal("416") < spread_bps < Decimal("417")
    
    def test_nonexistent_market(self, orderbook_tracker):
        """Test queries for nonexistent market."""
        assert orderbook_tracker.get("nonexistent") is None
        assert orderbook_tracker.best_bid("nonexistent") is None
        assert orderbook_tracker.best_ask("nonexistent") is None
        assert orderbook_tracker.mid_price("nonexistent") is None
        assert orderbook_tracker.spread("nonexistent") is None
    
    def test_remove_market(self, orderbook_tracker, sample_market_data):
        """Test removing a market."""
        market = sample_market_data["marketSlug"]
        
        orderbook_tracker.update(market_slug=market, data=sample_market_data)
        assert orderbook_tracker.get(market) is not None
        
        orderbook_tracker.remove(market)
        assert orderbook_tracker.get(market) is None
    
    def test_clear(self, orderbook_tracker, sample_market_data):
        """Test clearing all data."""
        orderbook_tracker.update(
            market_slug=sample_market_data["marketSlug"],
            data=sample_market_data,
        )
        orderbook_tracker.update(market_slug="another-market", data={})
        
        assert len(orderbook_tracker.markets()) == 2
        
        orderbook_tracker.clear()
        
        assert len(orderbook_tracker.markets()) == 0
    
    def test_depth_at_price(self, orderbook_tracker, sample_market_data):
        """Test depth at specific price level."""
        orderbook_tracker.update(
            market_slug=sample_market_data["marketSlug"],
            data=sample_market_data,
        )
        
        market = sample_market_data["marketSlug"]
        
        # Check bid depth
        assert orderbook_tracker.depth_at_price(
            market, "YES", Decimal("0.47"), is_bid=True
        ) == 500
        
        assert orderbook_tracker.depth_at_price(
            market, "YES", Decimal("0.46"), is_bid=True
        ) == 1000
        
        # Check ask depth
        assert orderbook_tracker.depth_at_price(
            market, "YES", Decimal("0.49"), is_bid=False
        ) == 300
        
        # Nonexistent price
        assert orderbook_tracker.depth_at_price(
            market, "YES", Decimal("0.99"), is_bid=True
        ) == 0
    
    def test_total_depth(self, orderbook_tracker, sample_market_data):
        """Test total notional depth calculation."""
        orderbook_tracker.update(
            market_slug=sample_market_data["marketSlug"],
            data=sample_market_data,
        )
        
        market = sample_market_data["marketSlug"]
        
        # YES bids: 0.47*500 + 0.46*1000 + 0.45*2000 = 235 + 460 + 900 = 1595
        bid_depth = orderbook_tracker.total_depth(market, "YES", is_bid=True)
        assert bid_depth == Decimal("1595")
        
        # YES asks: 0.49*300 + 0.50*800 + 0.51*1500 = 147 + 400 + 765 = 1312
        ask_depth = orderbook_tracker.total_depth(market, "YES", is_bid=False)
        assert ask_depth == Decimal("1312")
    
    def test_liquidity_within_bps(self, orderbook_tracker, sample_market_data):
        """Test liquidity within basis points."""
        orderbook_tracker.update(
            market_slug=sample_market_data["marketSlug"],
            data=sample_market_data,
        )
        
        market = sample_market_data["marketSlug"]
        
        # Get liquidity within 300 bps of best bid (0.47)
        # 300 bps = 3% -> threshold = 0.47 * 0.97 = 0.4559
        # Should include 0.47 and 0.46 bids
        notional, qty = orderbook_tracker.liquidity_within_bps(
            market, "YES", 300, is_bid=True
        )
        
        # 0.47*500 + 0.46*1000 = 235 + 460 = 695
        assert notional == Decimal("695")
        assert qty == 1500


class TestOrderBookState:
    """Tests for OrderBookState dataclass."""
    
    def test_state_properties(self, orderbook_tracker, sample_market_data):
        """Test OrderBookState property accessors."""
        orderbook_tracker.update(
            market_slug=sample_market_data["marketSlug"],
            data=sample_market_data,
        )
        
        state = orderbook_tracker.get(sample_market_data["marketSlug"])
        
        assert state.yes_best_bid == Decimal("0.47")
        assert state.yes_best_ask == Decimal("0.49")
        assert state.yes_spread == Decimal("0.02")
        assert state.yes_mid_price == Decimal("0.48")
        
        assert state.no_best_bid == Decimal("0.51")
        assert state.no_best_ask == Decimal("0.53")
    
    def test_staleness(self, orderbook_tracker, sample_market_data):
        """Test staleness detection."""
        from src.data.orderbook import OrderBookState, OrderBookTracker
        
        # Create tracker with short timeout
        tracker = OrderBookTracker(stale_timeout=timedelta(milliseconds=100))
        
        tracker.update(
            market_slug=sample_market_data["marketSlug"],
            data=sample_market_data,
        )
        
        # Should not be stale immediately
        assert not tracker.is_stale(sample_market_data["marketSlug"])
        
        # Wait for staleness
        import time
        time.sleep(0.15)
        
        assert tracker.is_stale(sample_market_data["marketSlug"])
    
    def test_prune_stale(self, sample_market_data):
        """Test pruning stale order books."""
        from src.data.orderbook import OrderBookTracker
        
        tracker = OrderBookTracker(stale_timeout=timedelta(milliseconds=50))
        
        tracker.update(
            market_slug=sample_market_data["marketSlug"],
            data=sample_market_data,
        )
        
        import time
        time.sleep(0.1)
        
        pruned = tracker.prune_stale()
        
        assert pruned == 1
        assert len(tracker.markets()) == 0


class TestSequenceHandling:
    """Tests for sequence number handling."""
    
    def test_sequence_ordering(self, orderbook_tracker):
        """Test that out-of-order updates are ignored."""
        market = "test-market"
        
        # Initial update with sequence 5
        orderbook_tracker.update(
            market_slug=market,
            data={"yes": {"bids": [["0.50", "100"]], "asks": []}},
            sequence=5,
        )
        
        assert orderbook_tracker.best_bid(market) == Decimal("0.50")
        
        # Update with lower sequence should be ignored
        orderbook_tracker.update(
            market_slug=market,
            data={"yes": {"bids": [["0.40", "100"]], "asks": []}},
            sequence=3,
        )
        
        # Should still be 0.50
        assert orderbook_tracker.best_bid(market) == Decimal("0.50")
        
        # Update with higher sequence should be applied
        orderbook_tracker.update(
            market_slug=market,
            data={"yes": {"bids": [["0.60", "100"]], "asks": []}},
            sequence=10,
        )
        
        assert orderbook_tracker.best_bid(market) == Decimal("0.60")


class TestOrderBookHandler:
    """Tests for the WebSocket handler helper."""
    
    @pytest.mark.asyncio
    async def test_handler_updates_tracker(self, orderbook_tracker, sample_market_data):
        """Test that handler correctly updates tracker."""
        from src.data.orderbook import create_orderbook_handler
        
        handler = create_orderbook_handler(orderbook_tracker)
        
        await handler(sample_market_data)
        
        book = orderbook_tracker.get(sample_market_data["marketSlug"])
        assert book is not None
        assert book.yes_best_bid == Decimal("0.47")

    @pytest.mark.asyncio
    async def test_handler_updates_tracker_bids_offers(
        self, orderbook_tracker, sample_market_data_bids_offers
    ):
        """Test handler supports top-level bids/offers market data."""
        from src.data.orderbook import create_orderbook_handler

        handler = create_orderbook_handler(orderbook_tracker)
        await handler(sample_market_data_bids_offers)

        book = orderbook_tracker.get(sample_market_data_bids_offers["marketSlug"])
        assert book is not None
        assert book.yes_best_bid == Decimal("0.47")
        assert book.yes_best_ask == Decimal("0.49")
    
    @pytest.mark.asyncio
    async def test_handler_ignores_non_market_data(self, orderbook_tracker):
        """Test that handler ignores non-market-data messages."""
        from src.data.orderbook import create_orderbook_handler
        
        handler = create_orderbook_handler(orderbook_tracker)
        
        await handler({"type": "ORDER_UPDATE", "orderId": "123"})
        
        assert len(orderbook_tracker.markets()) == 0
    
    @pytest.mark.asyncio
    async def test_handler_ignores_missing_slug(self, orderbook_tracker):
        """Test that handler ignores messages without market slug."""
        from src.data.orderbook import create_orderbook_handler
        
        handler = create_orderbook_handler(orderbook_tracker)
        
        await handler({"type": "MARKET_DATA"})  # No marketSlug
        
        assert len(orderbook_tracker.markets()) == 0


class TestThreadSafety:
    """Tests for thread safety."""
    
    def test_concurrent_updates(self, orderbook_tracker):
        """Test concurrent updates from multiple threads."""
        import threading
        
        results = []
        errors = []
        
        def update_worker(market_id: int):
            try:
                for i in range(100):
                    orderbook_tracker.update(
                        market_slug=f"market-{market_id}",
                        data={
                            "yes": {"bids": [[f"0.{i:02d}", str(i * 10)]], "asks": []},
                        },
                    )
                results.append(market_id)
            except Exception as e:
                errors.append(e)
        
        threads = [
            threading.Thread(target=update_worker, args=(i,))
            for i in range(10)
        ]
        
        for t in threads:
            t.start()
        
        for t in threads:
            t.join()
        
        assert len(errors) == 0
        assert len(results) == 10
        assert len(orderbook_tracker.markets()) == 10
    
    def test_concurrent_reads_writes(self, orderbook_tracker, sample_market_data):
        """Test concurrent reads and writes."""
        import threading
        
        errors = []
        market = sample_market_data["marketSlug"]
        
        orderbook_tracker.update(market_slug=market, data=sample_market_data)
        
        def writer():
            try:
                for _ in range(100):
                    orderbook_tracker.update(
                        market_slug=market,
                        data=sample_market_data,
                    )
            except Exception as e:
                errors.append(e)
        
        def reader():
            try:
                for _ in range(100):
                    orderbook_tracker.best_bid(market)
                    orderbook_tracker.mid_price(market)
                    orderbook_tracker.spread(market)
            except Exception as e:
                errors.append(e)
        
        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
        ]
        
        for t in threads:
            t.start()
        
        for t in threads:
            t.join()
        
        assert len(errors) == 0


# =============================================================================
# Integration Tests
# =============================================================================

@pytest.mark.integration
class TestWebSocketIntegration:
    """
    Integration tests that require valid API credentials.
    
    Run with: pytest tests/test_websocket.py -v -m integration
    """
    
    @pytest.fixture
    def credentials(self):
        """Get API credentials from environment."""
        import os
        from dotenv import load_dotenv
        
        load_dotenv()

        # Integration tests are opt-in so normal `pytest` runs stay offline-safe.
        # To run: RUN_INTEGRATION_TESTS=1 pytest -m integration -v
        if os.getenv("RUN_INTEGRATION_TESTS") != "1":
            pytest.skip("Integration tests are opt-in (set RUN_INTEGRATION_TESTS=1)")
        
        api_key_id = os.getenv("PM_API_KEY_ID")
        private_key = os.getenv("PM_PRIVATE_KEY")
        
        if not api_key_id or not private_key:
            pytest.skip("API credentials not configured")
        
        return api_key_id, private_key
    
    @pytest.fixture
    def auth(self, credentials):
        """Create auth with real credentials."""
        from src.api.auth import PolymarketAuth
        
        api_key_id, private_key = credentials
        return PolymarketAuth(api_key_id, private_key)
    
    @pytest.mark.asyncio
    async def test_connect_to_markets(self, auth):
        """Test connecting to markets WebSocket."""
        from src.api.websocket import PolymarketWebSocket, Endpoint
        
        async with PolymarketWebSocket(auth) as ws:
            await ws.connect(Endpoint.MARKETS)
            
            assert ws.is_connected
            
            await ws.disconnect()
            
            assert not ws.is_connected
    
    @pytest.mark.asyncio
    async def test_subscribe_to_market(self, auth):
        """Test subscribing to market data."""
        from src.api.websocket import (
            PolymarketWebSocket,
            Endpoint,
            SubscriptionType,
        )
        from src.api.client import PolymarketClient
        
        # First get a real market slug
        async with PolymarketClient(auth) as client:
            markets = await client.get_markets(status="OPEN", limit=1)
            
            if not markets:
                pytest.skip("No open markets available")
            
            market_slug = markets[0].slug
        
        # Now test WebSocket subscription
        async with PolymarketWebSocket(auth) as ws:
            await ws.connect(Endpoint.MARKETS)
            
            request_id = await ws.subscribe(
                SubscriptionType.MARKET_DATA,
                [market_slug],
            )
            
            assert request_id in ws.subscriptions
            sub = ws.subscriptions[request_id]
            assert sub.subscription_type == SubscriptionType.MARKET_DATA
            assert market_slug in sub.market_slugs


# =============================================================================
# Pytest Configuration
# =============================================================================

def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers",
        "integration: marks tests as integration tests (require API credentials)",
    )
