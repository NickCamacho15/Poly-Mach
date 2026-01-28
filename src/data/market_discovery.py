"""
Market discovery utilities for Polymarket US sports markets.

This module provides tools to discover, filter, and organize sports markets
by league, market type, and game.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Set

import structlog

logger = structlog.get_logger()


# =============================================================================
# Enums
# =============================================================================

class League(str, Enum):
    """Supported sports leagues."""
    NBA = "nba"
    CBB = "cbb"  # College Basketball
    NFL = "nfl"
    CFB = "cfb"  # College Football
    NHL = "nhl"
    MLB = "mlb"


class MarketProduct(str, Enum):
    """Market product types (from slug prefix)."""
    MONEYLINE = "aec"  # Athletic Event Contract
    SPREAD = "asc"     # Athletic Spread Contract
    TOTAL = "tsc"      # Total Score Contract
    TITLE = "tec"      # Title Event Contract
    AWARD = "tac"      # Title Award Contract


class SportsMarketType(str, Enum):
    """Sports market type enum values."""
    MONEYLINE = "SPORTS_MARKET_TYPE_MONEYLINE"
    SPREAD = "SPORTS_MARKET_TYPE_SPREAD"
    TOTAL = "SPORTS_MARKET_TYPE_TOTAL"
    PROP = "SPORTS_MARKET_TYPE_PROP"


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class Participant:
    """Team or player in a sports market."""
    id: str
    name: str
    
    @classmethod
    def from_metadata(cls, metadata: Dict, prefix: str) -> Optional["Participant"]:
        """Create from market metadata with 'long_' or 'short_' prefix."""
        pid = metadata.get(f"{prefix}_participant_id")
        pname = metadata.get(f"{prefix}_participant_name")
        if pid and pname:
            return cls(id=pid, name=pname)
        return None


@dataclass
class SportsMarket:
    """
    Parsed sports market with easy access to key fields.
    
    Attributes:
        slug: Market identifier
        question: Market question text
        league: League enum (NBA, CBB, etc.)
        product: Market product type (MONEYLINE, SPREAD, TOTAL)
        sports_type: API sports market type
        game_id: Sports data provider game ID
        line: Spread or total line value
        long_team: Team to bet YES on
        short_team: Opposing team
        best_bid: Best bid price
        best_ask: Best ask price
        volume: 24h trading volume
        liquidity: Current liquidity
        active: Whether market is active
        closed: Whether market is closed
        raw: Original API response
    """
    slug: str
    question: str
    league: Optional[League]
    product: Optional[MarketProduct]
    sports_type: Optional[str]
    game_id: Optional[str]
    line: Optional[Decimal]
    long_team: Optional[Participant]
    short_team: Optional[Participant]
    best_bid: Optional[Decimal]
    best_ask: Optional[Decimal]
    volume: Optional[Decimal]
    liquidity: Optional[Decimal]
    active: bool
    closed: bool
    raw: Dict[str, Any] = field(repr=False)
    
    @property
    def mid_price(self) -> Optional[Decimal]:
        """Calculate mid-price."""
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return None
    
    @property
    def spread(self) -> Optional[Decimal]:
        """Calculate bid-ask spread."""
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None
    
    @property
    def event_id(self) -> Optional[str]:
        """Extract event ID from slug (game identifier without product prefix)."""
        parts = self.slug.split("-")
        if len(parts) >= 5:
            # Skip product prefix, join rest
            # aec-nba-lal-bos-2025-01-27 -> nba-lal-bos-2025-01-27
            return "-".join(parts[1:6])
        return None
    
    @property
    def is_moneyline(self) -> bool:
        return self.product == MarketProduct.MONEYLINE
    
    @property
    def is_spread(self) -> bool:
        return self.product == MarketProduct.SPREAD
    
    @property
    def is_total(self) -> bool:
        return self.product == MarketProduct.TOTAL
    
    @classmethod
    def from_api_response(cls, data: Dict[str, Any]) -> "SportsMarket":
        """
        Parse a market from API response.
        
        Args:
            data: Market dict from /v1/markets response
            
        Returns:
            Parsed SportsMarket
        """
        slug = data.get("slug", "")
        metadata = data.get("metadata", {})
        
        # Parse product and league from slug
        # Format: {product}-{league}-{away}-{home}-{date}
        # Example: aec-nfl-lac-ten-2025-11-02
        product = None
        league = None
        slug_parts = slug.split("-")
        
        if len(slug_parts) >= 2:
            # First part is product (aec, asc, tsc)
            try:
                product = MarketProduct(slug_parts[0])
            except ValueError:
                pass
            
            # Second part is league (nba, cbb, nfl, etc.)
            try:
                league = League(slug_parts[1].lower())
            except ValueError:
                pass
        
        # Fallback: check metadata if available
        if league is None:
            event_series = metadata.get("event_series")
            if event_series:
                try:
                    league = League(event_series.lower())
                except ValueError:
                    pass
        
        # Also check category field
        if league is None and data.get("category") == "sports":
            # Try to detect from sportsMarketTypeV2
            pass  # League already parsed from slug
        
        # Parse participants
        long_team = Participant.from_metadata(metadata, "long")
        short_team = Participant.from_metadata(metadata, "short")
        
        # Parse line
        line = None
        raw_line = data.get("line") or metadata.get("outcome_strike")
        if raw_line is not None:
            try:
                line = Decimal(str(raw_line))
            except:
                pass
        
        # Parse prices
        def to_decimal(val) -> Optional[Decimal]:
            if val is None:
                return None
            try:
                return Decimal(str(val))
            except:
                return None
        
        return cls(
            slug=slug,
            question=data.get("question", ""),
            league=league,
            product=product,
            sports_type=data.get("sportsMarketTypeV2"),
            game_id=data.get("gameId") or metadata.get("gameId"),
            line=line,
            long_team=long_team,
            short_team=short_team,
            best_bid=to_decimal(data.get("bestBid")),
            best_ask=to_decimal(data.get("bestAsk")),
            volume=to_decimal(data.get("volume")),
            liquidity=to_decimal(data.get("liquidity")),
            active=data.get("active", False),
            closed=data.get("closed", False),
            raw=data,
        )


@dataclass
class GameMarkets:
    """
    All markets for a single game, grouped by type.
    
    Attributes:
        event_id: Game identifier (e.g., "nba-lal-bos-2025-01-27")
        league: League enum
        long_team: Home/favored team
        short_team: Away/underdog team
        moneyline: Moneyline market if available
        spreads: All spread markets for this game
        totals: All total markets for this game
    """
    event_id: str
    league: Optional[League]
    long_team: Optional[Participant]
    short_team: Optional[Participant]
    moneyline: Optional[SportsMarket] = None
    spreads: List[SportsMarket] = field(default_factory=list)
    totals: List[SportsMarket] = field(default_factory=list)
    
    @property
    def all_markets(self) -> List[SportsMarket]:
        """Get all markets for this game."""
        markets = []
        if self.moneyline:
            markets.append(self.moneyline)
        markets.extend(self.spreads)
        markets.extend(self.totals)
        return markets
    
    @property
    def all_slugs(self) -> List[str]:
        """Get all market slugs for this game."""
        return [m.slug for m in self.all_markets]


# =============================================================================
# Market Discovery
# =============================================================================

class MarketDiscovery:
    """
    Utility class for discovering and organizing sports markets.
    
    Example:
        >>> discovery = MarketDiscovery()
        >>> 
        >>> # Parse markets from API
        >>> markets = discovery.parse_markets(api_response["markets"])
        >>> 
        >>> # Filter for NBA
        >>> nba = discovery.filter_by_league(markets, League.NBA)
        >>> 
        >>> # Get all moneylines
        >>> moneylines = discovery.filter_by_product(nba, MarketProduct.MONEYLINE)
        >>> 
        >>> # Group by game
        >>> games = discovery.group_by_game(nba)
    """
    
    def parse_markets(self, markets_data: List[Dict]) -> List[SportsMarket]:
        """
        Parse list of markets from API response.
        
        Args:
            markets_data: List of market dicts from API
            
        Returns:
            List of parsed SportsMarket objects
        """
        markets = []
        for data in markets_data:
            try:
                market = SportsMarket.from_api_response(data)
                markets.append(market)
            except Exception as e:
                logger.warning(
                    "Failed to parse market",
                    slug=data.get("slug"),
                    error=str(e),
                )
        return markets
    
    def filter_by_league(
        self,
        markets: List[SportsMarket],
        league: League,
    ) -> List[SportsMarket]:
        """Filter markets by league."""
        return [m for m in markets if m.league == league]
    
    def filter_by_leagues(
        self,
        markets: List[SportsMarket],
        leagues: List[League],
    ) -> List[SportsMarket]:
        """Filter markets by multiple leagues."""
        league_set = set(leagues)
        return [m for m in markets if m.league in league_set]
    
    def filter_by_product(
        self,
        markets: List[SportsMarket],
        product: MarketProduct,
    ) -> List[SportsMarket]:
        """Filter markets by product type."""
        return [m for m in markets if m.product == product]
    
    def filter_by_products(
        self,
        markets: List[SportsMarket],
        products: List[MarketProduct],
    ) -> List[SportsMarket]:
        """Filter markets by multiple product types."""
        product_set = set(products)
        return [m for m in markets if m.product in product_set]
    
    def filter_active(self, markets: List[SportsMarket]) -> List[SportsMarket]:
        """Filter to only active, non-closed markets."""
        return [m for m in markets if m.active and not m.closed]
    
    def filter_with_liquidity(
        self,
        markets: List[SportsMarket],
        min_liquidity: Decimal = Decimal("100"),
    ) -> List[SportsMarket]:
        """Filter markets with minimum liquidity."""
        return [
            m for m in markets
            if m.liquidity is not None and m.liquidity >= min_liquidity
        ]
    
    def get_basketball_markets(
        self,
        markets: List[SportsMarket],
    ) -> List[SportsMarket]:
        """Get all basketball markets (NBA + CBB)."""
        return self.filter_by_leagues(markets, [League.NBA, League.CBB])
    
    def group_by_game(
        self,
        markets: List[SportsMarket],
    ) -> Dict[str, GameMarkets]:
        """
        Group markets by game/event.
        
        Args:
            markets: List of sports markets
            
        Returns:
            Dict mapping event_id to GameMarkets
        """
        games: Dict[str, GameMarkets] = {}
        
        for market in markets:
            event_id = market.event_id
            if not event_id:
                continue
            
            if event_id not in games:
                games[event_id] = GameMarkets(
                    event_id=event_id,
                    league=market.league,
                    long_team=market.long_team,
                    short_team=market.short_team,
                )
            
            game = games[event_id]
            
            if market.is_moneyline:
                game.moneyline = market
            elif market.is_spread:
                game.spreads.append(market)
            elif market.is_total:
                game.totals.append(market)
        
        return games
    
    def get_all_slugs(
        self,
        markets: List[SportsMarket],
    ) -> List[str]:
        """Get list of all market slugs."""
        return [m.slug for m in markets]
    
    def summarize(self, markets: List[SportsMarket]) -> Dict[str, Any]:
        """
        Get summary statistics for a list of markets.
        
        Returns:
            Dict with counts by league, product, etc.
        """
        by_league: Dict[str, int] = {}
        by_product: Dict[str, int] = {}
        
        for market in markets:
            # Count by league
            league_key = market.league.value if market.league else "unknown"
            by_league[league_key] = by_league.get(league_key, 0) + 1
            
            # Count by product
            product_key = market.product.value if market.product else "unknown"
            by_product[product_key] = by_product.get(product_key, 0) + 1
        
        return {
            "total_markets": len(markets),
            "by_league": by_league,
            "by_product": by_product,
            "active": sum(1 for m in markets if m.active),
            "closed": sum(1 for m in markets if m.closed),
        }


# =============================================================================
# Convenience Functions
# =============================================================================

def get_basketball_slugs(markets_data: List[Dict]) -> List[str]:
    """
    Quick helper to get all basketball market slugs from API response.
    
    Args:
        markets_data: Raw markets list from API
        
    Returns:
        List of market slugs for NBA and CBB
    """
    discovery = MarketDiscovery()
    markets = discovery.parse_markets(markets_data)
    basketball = discovery.get_basketball_markets(markets)
    active = discovery.filter_active(basketball)
    return discovery.get_all_slugs(active)


def get_nba_slugs(markets_data: List[Dict]) -> List[str]:
    """Quick helper to get all NBA market slugs."""
    discovery = MarketDiscovery()
    markets = discovery.parse_markets(markets_data)
    nba = discovery.filter_by_league(markets, League.NBA)
    active = discovery.filter_active(nba)
    return discovery.get_all_slugs(active)


def get_cbb_slugs(markets_data: List[Dict]) -> List[str]:
    """Quick helper to get all College Basketball market slugs."""
    discovery = MarketDiscovery()
    markets = discovery.parse_markets(markets_data)
    cbb = discovery.filter_by_league(markets, League.CBB)
    active = discovery.filter_active(cbb)
    return discovery.get_all_slugs(active)
