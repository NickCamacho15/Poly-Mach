"""
Tests for authentication and API client modules.

Run with: pytest tests/test_auth.py -v
"""

import os
import base64
from decimal import Decimal
from unittest.mock import patch, MagicMock

import pytest
from dotenv import load_dotenv

from cryptography.hazmat.primitives.asymmetric import ed25519

# Load environment variables
load_dotenv()


# =============================================================================
# Auth Module Tests
# =============================================================================

class TestPolymarketAuth:
    """Tests for PolymarketAuth class."""
    
    def test_auth_init_success(self):
        """Test successful auth initialization."""
        from src.api.auth import PolymarketAuth
        
        # Generate a test key pair
        private_key = ed25519.Ed25519PrivateKey.generate()
        private_key_bytes = private_key.private_bytes_raw()
        private_key_b64 = base64.b64encode(private_key_bytes).decode()
        
        auth = PolymarketAuth(
            api_key_id="test-api-key-uuid",
            private_key_base64=private_key_b64,
        )
        
        assert auth.api_key_id == "test-api-key-uuid"
    
    def test_auth_init_missing_api_key(self):
        """Test auth fails with missing API key."""
        from src.api.auth import PolymarketAuth, AuthenticationError
        
        with pytest.raises(AuthenticationError, match="API key ID is required"):
            PolymarketAuth(api_key_id="", private_key_base64="somekey")
    
    def test_auth_init_missing_private_key(self):
        """Test auth fails with missing private key."""
        from src.api.auth import PolymarketAuth, AuthenticationError
        
        with pytest.raises(AuthenticationError, match="Private key is required"):
            PolymarketAuth(api_key_id="test-key", private_key_base64="")
    
    def test_auth_init_invalid_private_key(self):
        """Test auth fails with invalid private key."""
        from src.api.auth import PolymarketAuth, AuthenticationError
        
        with pytest.raises(AuthenticationError, match="Failed to load private key"):
            PolymarketAuth(api_key_id="test-key", private_key_base64="not-valid-base64!")
    
    def test_auth_init_short_private_key(self):
        """Test auth fails with too-short private key."""
        from src.api.auth import PolymarketAuth, AuthenticationError
        
        # Only 16 bytes (need 32)
        short_key = base64.b64encode(b"x" * 16).decode()
        
        with pytest.raises(AuthenticationError, match="Private key too short"):
            PolymarketAuth(api_key_id="test-key", private_key_base64=short_key)
    
    def test_sign_request_generates_all_headers(self):
        """Test that sign_request generates all required headers."""
        from src.api.auth import PolymarketAuth
        
        # Generate test key
        private_key = ed25519.Ed25519PrivateKey.generate()
        private_key_b64 = base64.b64encode(private_key.private_bytes_raw()).decode()
        
        auth = PolymarketAuth(
            api_key_id="test-api-key-uuid",
            private_key_base64=private_key_b64,
        )
        
        headers = auth.sign_request("GET", "/v1/account/balance")
        
        assert "X-PM-Access-Key" in headers
        assert "X-PM-Timestamp" in headers
        assert "X-PM-Signature" in headers
        assert "Content-Type" in headers
        
        assert headers["X-PM-Access-Key"] == "test-api-key-uuid"
        assert headers["Content-Type"] == "application/json"
    
    def test_sign_request_deterministic_with_fixed_timestamp(self):
        """Test that signature is deterministic given same inputs."""
        from src.api.auth import PolymarketAuth
        
        # Generate test key
        private_key = ed25519.Ed25519PrivateKey.generate()
        private_key_b64 = base64.b64encode(private_key.private_bytes_raw()).decode()
        
        auth = PolymarketAuth(
            api_key_id="test-api-key-uuid",
            private_key_base64=private_key_b64,
        )
        
        # Use fixed timestamp for determinism
        timestamp = "1705420800000"
        
        headers1 = auth.sign_request("GET", "/v1/orders", timestamp=timestamp)
        headers2 = auth.sign_request("GET", "/v1/orders", timestamp=timestamp)
        
        assert headers1["X-PM-Signature"] == headers2["X-PM-Signature"]
    
    def test_sign_request_different_for_different_paths(self):
        """Test that signature differs for different paths."""
        from src.api.auth import PolymarketAuth
        
        private_key = ed25519.Ed25519PrivateKey.generate()
        private_key_b64 = base64.b64encode(private_key.private_bytes_raw()).decode()
        
        auth = PolymarketAuth(
            api_key_id="test-api-key-uuid",
            private_key_base64=private_key_b64,
        )
        
        timestamp = "1705420800000"
        
        headers1 = auth.sign_request("GET", "/v1/orders", timestamp=timestamp)
        headers2 = auth.sign_request("GET", "/v1/account/balance", timestamp=timestamp)
        
        assert headers1["X-PM-Signature"] != headers2["X-PM-Signature"]
    
    def test_sign_request_different_for_different_methods(self):
        """Test that signature differs for different HTTP methods."""
        from src.api.auth import PolymarketAuth
        
        private_key = ed25519.Ed25519PrivateKey.generate()
        private_key_b64 = base64.b64encode(private_key.private_bytes_raw()).decode()
        
        auth = PolymarketAuth(
            api_key_id="test-api-key-uuid",
            private_key_base64=private_key_b64,
        )
        
        timestamp = "1705420800000"
        
        headers_get = auth.sign_request("GET", "/v1/orders", timestamp=timestamp)
        headers_post = auth.sign_request("POST", "/v1/orders", timestamp=timestamp)
        
        assert headers_get["X-PM-Signature"] != headers_post["X-PM-Signature"]
    
    def test_get_ws_headers(self):
        """Test WebSocket header generation."""
        from src.api.auth import PolymarketAuth
        
        private_key = ed25519.Ed25519PrivateKey.generate()
        private_key_b64 = base64.b64encode(private_key.private_bytes_raw()).decode()
        
        auth = PolymarketAuth(
            api_key_id="test-api-key-uuid",
            private_key_base64=private_key_b64,
        )
        
        headers = auth.get_ws_headers("/v1/ws/markets")
        
        assert "X-PM-Access-Key" in headers
        assert "X-PM-Timestamp" in headers
        assert "X-PM-Signature" in headers
    
    def test_get_public_key(self):
        """Test public key extraction."""
        from src.api.auth import PolymarketAuth
        
        private_key = ed25519.Ed25519PrivateKey.generate()
        private_key_b64 = base64.b64encode(private_key.private_bytes_raw()).decode()
        expected_public_key = private_key.public_key().public_bytes_raw()
        
        auth = PolymarketAuth(
            api_key_id="test-api-key-uuid",
            private_key_base64=private_key_b64,
        )
        
        assert auth.get_public_key() == expected_public_key
    
    def test_signature_is_verifiable(self):
        """Test that generated signature can be verified."""
        from src.api.auth import PolymarketAuth
        
        private_key = ed25519.Ed25519PrivateKey.generate()
        private_key_b64 = base64.b64encode(private_key.private_bytes_raw()).decode()
        public_key = private_key.public_key()
        
        auth = PolymarketAuth(
            api_key_id="test-api-key-uuid",
            private_key_base64=private_key_b64,
        )
        
        timestamp = "1705420800000"
        method = "GET"
        path = "/v1/account/balance"
        
        headers = auth.sign_request(method, path, timestamp=timestamp)
        
        # Reconstruct and verify the signature
        message = f"{timestamp}{method}{path}"
        signature = base64.b64decode(headers["X-PM-Signature"])
        
        # This will raise InvalidSignature if verification fails
        public_key.verify(signature, message.encode("utf-8"))


# =============================================================================
# Data Models Tests
# =============================================================================

class TestDataModels:
    """Tests for Pydantic data models."""
    
    def test_market_model(self):
        """Test Market model parsing."""
        from src.data.models import Market, MarketStatus
        
        data = {
            "slug": "nba-lakers-vs-celtics-2025-01-25",
            "title": "Lakers vs Celtics",
            "status": "OPEN",
            "category": "NBA",
            "yesBid": "0.47",
            "yesAsk": "0.49",
            "noBid": "0.51",
            "noAsk": "0.53",
        }
        
        market = Market.model_validate(data)
        
        assert market.slug == "nba-lakers-vs-celtics-2025-01-25"
        assert market.status == MarketStatus.OPEN
        assert market.yes_bid == Decimal("0.47")
        assert market.yes_ask == Decimal("0.49")
    
    def test_market_mid_price(self):
        """Test Market mid_price property."""
        from src.data.models import Market
        
        data = {
            "slug": "test-market",
            "title": "Test",
            "status": "OPEN",
            "yesBid": "0.40",
            "yesAsk": "0.50",
        }
        
        market = Market.model_validate(data)
        
        assert market.mid_price == Decimal("0.45")
    
    def test_market_spread(self):
        """Test Market spread property."""
        from src.data.models import Market
        
        data = {
            "slug": "test-market",
            "title": "Test",
            "status": "OPEN",
            "yesBid": "0.40",
            "yesAsk": "0.50",
        }
        
        market = Market.model_validate(data)
        
        assert market.spread == Decimal("0.10")
    
    def test_order_model(self):
        """Test Order model parsing."""
        from src.data.models import Order
        
        data = {
            "orderId": "order-123",
            "marketSlug": "test-market",
            "intent": "ORDER_INTENT_BUY_LONG",
            "price": "0.50",
            "quantity": 100,
            "filledQuantity": 50,
            "status": "PARTIALLY_FILLED",
        }
        
        order = Order.model_validate(data)
        
        assert order.order_id == "order-123"
        assert order.quantity == 100
        assert order.filled_quantity == 50
        assert order.is_open is True
        assert order.is_filled is False
    
    def test_position_model(self):
        """Test Position model parsing."""
        from src.data.models import Position, Side
        
        data = {
            "marketSlug": "test-market",
            "side": "YES",
            "quantity": 100,
            "avgPrice": "0.45",
            "currentPrice": "0.52",
            "unrealizedPnl": "7.00",
        }
        
        position = Position.model_validate(data)
        
        assert position.market_slug == "test-market"
        assert position.side == Side.YES
        assert position.quantity == 100
        assert position.cost_basis == Decimal("45")

    def test_position_model_portfolio_positions_schema(self):
        """Test Position normalization for GET /v1/portfolio/positions schema (map values)."""
        from src.data.models import Position, Side

        data = {
            "netPosition": "100",
            "qtyBought": "100",
            "qtySold": "0",
            "cost": {"value": "60.00", "currency": "USD"},
            "cashValue": {"value": "65.00", "currency": "USD"},
            "qtyAvailable": "100",
            "expired": False,
            "updateTime": "2024-01-15T10:30:00Z",
            "marketMetadata": {
                "slug": "will-x-happen",
                "title": "Will X happen?",
                "outcome": "Yes",
            },
        }

        position = Position.model_validate(data)

        assert position.market_slug == "will-x-happen"
        assert position.side == Side.YES
        assert position.quantity == 100
        assert position.avg_price == Decimal("0.6")
        assert position.current_value == Decimal("65.00")
        assert position.unrealized_pnl == Decimal("5.00")

    def test_position_model_portfolio_positions_schema_team_outcome_uses_net_sign(self):
        """Team outcomes (e.g. DUCKS) should still parse; side derives from netPosition sign."""
        from src.data.models import Position, Side

        data = {
            "netPosition": "-50",
            "qtyBought": "50",
            "qtySold": "0",
            "cost": {"value": "12.50", "currency": "USD"},
            "cashValue": {"value": "15.00", "currency": "USD"},
            "marketMetadata": {"slug": "aec-cbb-ore-ucla-2026-02-02", "outcome": "DUCKS"},
        }

        position = Position.model_validate(data)

        assert position.market_slug == "aec-cbb-ore-ucla-2026-02-02"
        assert position.side == Side.NO
        assert position.quantity == 50
        assert position.avg_price == Decimal("0.25")
        assert position.current_value == Decimal("15.00")
    
    def test_balance_model(self):
        """Test Balance model parsing."""
        from src.data.models import Balance
        
        data = {
            "availableBalance": "1000.00",
            "totalBalance": "1245.00",
            "currency": "USD",
        }
        
        balance = Balance.model_validate(data)
        
        assert balance.available_balance == Decimal("1000.00")
        assert balance.total_balance == Decimal("1245.00")
    
    def test_order_request_to_api_payload(self):
        """Test OrderRequest conversion to API payload."""
        from src.data.models import OrderRequest, OrderIntent, Price
        
        order = OrderRequest(
            market_slug="test-market",
            quantity=100,
            price=Price(value="0.55"),
            intent=OrderIntent.BUY_LONG,
        )
        
        payload = order.to_api_payload()
        
        assert payload["marketSlug"] == "test-market"
        assert payload["quantity"] == 100
        assert payload["price"]["value"] == "0.55"
        assert payload["intent"] == "ORDER_INTENT_BUY_LONG"
        assert payload["manualOrderIndicator"] == "MANUAL_ORDER_INDICATOR_AUTOMATIC"
    
    def test_order_book_side_best_bid_ask(self):
        """Test OrderBookSide best bid/ask properties."""
        from src.data.models import OrderBookSide, PriceLevel
        
        side = OrderBookSide(
            bids=[
                PriceLevel(price=Decimal("0.45"), quantity=100),
                PriceLevel(price=Decimal("0.47"), quantity=200),
            ],
            asks=[
                PriceLevel(price=Decimal("0.50"), quantity=150),
                PriceLevel(price=Decimal("0.52"), quantity=300),
            ],
        )
        
        assert side.best_bid == Decimal("0.47")
        assert side.best_ask == Decimal("0.50")
        assert side.spread == Decimal("0.03")


# =============================================================================
# API Client Tests (Unit)
# =============================================================================

class TestPolymarketClientUnit:
    """Unit tests for PolymarketClient (mocked)."""
    
    @pytest.fixture
    def mock_auth(self):
        """Create a mock auth object."""
        private_key = ed25519.Ed25519PrivateKey.generate()
        private_key_b64 = base64.b64encode(private_key.private_bytes_raw()).decode()
        
        from src.api.auth import PolymarketAuth
        return PolymarketAuth(
            api_key_id="test-api-key",
            private_key_base64=private_key_b64,
        )
    
    @pytest.mark.asyncio
    async def test_client_context_manager(self, mock_auth):
        """Test client works as async context manager."""
        from src.api.client import PolymarketClient
        
        async with PolymarketClient(mock_auth) as client:
            assert client._client is not None
        
        assert client._client is None
    
    @pytest.mark.asyncio
    async def test_client_parse_error(self, mock_auth):
        """Test error parsing."""
        from src.api.client import PolymarketClient, InsufficientBalanceError
        import httpx
        
        client = PolymarketClient(mock_auth)
        
        # Create mock response
        response = MagicMock(spec=httpx.Response)
        response.status_code = 400
        response.text = '{"error": {"code": "INSUFFICIENT_BALANCE", "message": "Not enough funds"}}'
        response.json.return_value = {
            "error": {"code": "INSUFFICIENT_BALANCE", "message": "Not enough funds"}
        }
        
        error = client._parse_error(response)
        
        assert isinstance(error, InsufficientBalanceError)
        assert error.error_code == "INSUFFICIENT_BALANCE"


# =============================================================================
# Integration Tests (requires valid credentials)
# =============================================================================

@pytest.mark.integration
class TestIntegration:
    """
    Integration tests that require valid API credentials.
    
    Run with: pytest tests/test_auth.py -v -m integration
    
    Requires PM_API_KEY_ID and PM_PRIVATE_KEY in .env
    """
    
    @pytest.fixture
    def credentials(self):
        """Get API credentials from environment."""
        # Integration tests are opt-in so normal `pytest` runs stay fast and offline-safe.
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
    async def test_get_balance(self, auth):
        """Test getting account balance."""
        from src.api.client import PolymarketClient
        
        async with PolymarketClient(auth) as client:
            balance = await client.get_balance()
            
            assert balance.available_balance >= 0
            assert balance.currency == "USD"
            print(f"Balance: ${balance.available_balance}")
    
    @pytest.mark.asyncio
    async def test_get_markets(self, auth):
        """Test getting markets list."""
        from src.api.client import PolymarketClient
        
        async with PolymarketClient(auth) as client:
            markets = await client.get_markets(status="OPEN", limit=5)
            
            assert isinstance(markets, list)
            print(f"Found {len(markets)} open markets")
            
            for market in markets:
                print(f"  - {market.slug}: {market.title}")
    
    @pytest.mark.asyncio
    async def test_get_nba_markets(self, auth):
        """Test getting NBA markets specifically."""
        from src.api.client import PolymarketClient
        
        async with PolymarketClient(auth) as client:
            markets = await client.get_markets(
                category="NBA",
                status="OPEN",
                limit=10,
            )
            
            assert isinstance(markets, list)
            print(f"Found {len(markets)} NBA markets")
            
            for market in markets:
                print(f"  - {market.slug}")
                if market.yes_bid and market.yes_ask:
                    print(f"    YES: ${market.yes_bid} / ${market.yes_ask}")
    
    @pytest.mark.asyncio
    async def test_get_positions(self, auth):
        """Test getting current positions."""
        from src.api.client import PolymarketClient
        
        async with PolymarketClient(auth) as client:
            positions = await client.get_positions()
            
            assert isinstance(positions, list)
            print(f"Open positions: {len(positions)}")
            
            for pos in positions:
                print(f"  - {pos.market_slug}: {pos.quantity} {pos.side} @ ${pos.avg_price}")
    
    @pytest.mark.asyncio
    async def test_get_open_orders(self, auth):
        """Test getting open orders."""
        from src.api.client import PolymarketClient
        
        async with PolymarketClient(auth) as client:
            orders = await client.get_open_orders()
            
            assert isinstance(orders, list)
            print(f"Open orders: {len(orders)}")
            
            for order in orders:
                print(f"  - {order.order_id}: {order.intent} {order.quantity} @ ${order.price}")


# =============================================================================
# Pytest Configuration
# =============================================================================

def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers",
        "integration: marks tests as integration tests (require API credentials)",
    )
