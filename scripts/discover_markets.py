#!/usr/bin/env python3
"""
Market Discovery Script

Discovers available sports markets on Polymarket US.
Use this to find market slugs for your .env configuration.

Usage:
    python scripts/discover_markets.py
    python scripts/discover_markets.py --league nba
    python scripts/discover_markets.py --league cbb --type moneyline
"""

import argparse
import asyncio
import sys
from decimal import Decimal
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.auth import PolymarketAuth
from src.api.client import PolymarketClient
from src.config import settings
from src.data.market_discovery import (
    League,
    MarketDiscovery,
    MarketProduct,
    SportsMarket,
)


def print_market(market: SportsMarket, verbose: bool = False) -> None:
    """Print market details."""
    # Price info
    if market.best_bid and market.best_ask:
        price_str = f"${market.best_bid:.2f} / ${market.best_ask:.2f}"
        spread_str = f"spread: {float(market.spread or 0)*100:.1f}¢"
    else:
        price_str = "N/A"
        spread_str = ""
    
    # Line info for spread/total
    line_str = ""
    if market.line:
        line_str = f"line: {market.line}"
    
    # Team info
    teams = ""
    if market.long_team and market.short_team:
        teams = f"{market.long_team.name} vs {market.short_team.name}"
    
    # Product type
    product = market.product.name if market.product else "?"
    
    print(f"\n  {market.slug}")
    print(f"    Type: {product} | League: {market.league.value if market.league else '?'}")
    if teams:
        print(f"    Teams: {teams}")
    print(f"    Price: {price_str} {spread_str}")
    if line_str:
        print(f"    {line_str}")
    if market.volume:
        print(f"    Volume: ${float(market.volume):,.0f} | Liquidity: ${float(market.liquidity or 0):,.0f}")
    
    if verbose:
        print(f"    Question: {market.question}")
        print(f"    Active: {market.active} | Closed: {market.closed}")


async def main():
    parser = argparse.ArgumentParser(description="Discover Polymarket sports markets")
    parser.add_argument(
        "--league", "-l",
        choices=["nba", "cbb", "nfl", "cfb", "nhl", "mlb", "all"],
        default="all",
        help="Filter by league (default: all)",
    )
    parser.add_argument(
        "--type", "-t",
        choices=["moneyline", "spread", "total", "all"],
        default="all",
        help="Filter by market type (default: all)",
    )
    parser.add_argument(
        "--min-liquidity", "-m",
        type=float,
        default=0,
        help="Minimum liquidity filter (default: 0)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Max markets to show (default: 50)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show verbose output",
    )
    parser.add_argument(
        "--slugs-only",
        action="store_true",
        help="Output only slugs (for copying to .env)",
    )
    
    args = parser.parse_args()
    
    # Check credentials
    if not settings.pm_api_key_id or not settings.pm_private_key:
        print("ERROR: Missing API credentials!")
        print("Set PM_API_KEY_ID and PM_PRIVATE_KEY in your .env file")
        sys.exit(1)
    
    print("=" * 60)
    print("Polymarket Sports Market Discovery")
    print("=" * 60)
    
    # Connect to API
    print("\nConnecting to Polymarket API...")
    auth = PolymarketAuth(settings.pm_api_key_id, settings.pm_private_key)
    
    async with PolymarketClient(auth) as client:
        # Fetch markets
        print("Fetching markets...")
        
        # Query for non-closed markets (active, tradeable)
        try:
            response = await client._request("GET", "/v1/markets", params={
                "limit": 500,
                "closed": "false",  # Only get markets that aren't closed
            })
            markets_data = response.get("markets", [])
        except Exception as e:
            print(f"Error fetching markets: {e}")
            sys.exit(1)
        
        print(f"Fetched {len(markets_data)} markets from API")
        
        # Parse and filter
        discovery = MarketDiscovery()
        markets = discovery.parse_markets(markets_data)
        
        # Filter for sports (has league OR has sports category in raw data)
        sports_markets = [
            m for m in markets 
            if m.league is not None or m.raw.get("category") == "sports"
        ]
        print(f"Found {len(sports_markets)} sports markets")
        
        # Apply league filter
        if args.league != "all":
            try:
                league = League(args.league)
                sports_markets = discovery.filter_by_league(sports_markets, league)
                print(f"Filtered to {len(sports_markets)} {args.league.upper()} markets")
            except ValueError:
                pass
        
        # Apply type filter
        if args.type != "all":
            type_map = {
                "moneyline": MarketProduct.MONEYLINE,
                "spread": MarketProduct.SPREAD,
                "total": MarketProduct.TOTAL,
            }
            if args.type in type_map:
                sports_markets = discovery.filter_by_product(sports_markets, type_map[args.type])
                print(f"Filtered to {len(sports_markets)} {args.type} markets")
        
        # Apply liquidity filter
        if args.min_liquidity > 0:
            sports_markets = discovery.filter_with_liquidity(
                sports_markets,
                Decimal(str(args.min_liquidity)),
            )
            print(f"Filtered to {len(sports_markets)} markets with >${args.min_liquidity} liquidity")
        
        # Filter out any that somehow are still closed (API already filters, but double check)
        sports_markets = [m for m in sports_markets if not m.closed]
        print(f"Open markets: {len(sports_markets)}")
        
        if not sports_markets:
            print("\nNo markets found matching your criteria!")
            print("Try different filters or check if markets are available.")
            sys.exit(0)
        
        # Show summary
        summary = discovery.summarize(sports_markets)
        print(f"\n--- Summary ---")
        print(f"Total: {summary['total_markets']}")
        print(f"By League: {summary['by_league']}")
        print(f"By Type: {summary['by_product']}")
        
        # Limit results
        display_markets = sports_markets[:args.limit]
        
        if args.slugs_only:
            # Output slugs for easy copying
            print(f"\n--- Market Slugs (copy to MARKET_SLUGS in .env) ---\n")
            slugs = ",".join(m.slug for m in display_markets)
            print(f"MARKET_SLUGS={slugs}")
        else:
            # Show detailed market info
            print(f"\n--- Markets (showing {len(display_markets)} of {len(sports_markets)}) ---")
            
            for market in display_markets:
                print_market(market, verbose=args.verbose)
            
            # Helpful output
            print(f"\n--- Quick Start ---")
            print(f"\nAdd this to your .env file:")
            
            # Show first 5 slugs
            sample_slugs = ",".join(m.slug for m in display_markets[:5])
            print(f"\nMARKET_SLUGS={sample_slugs}")
            
            if len(display_markets) > 5:
                print(f"\n(Use --slugs-only to get all {len(display_markets)} slugs)")


if __name__ == "__main__":
    asyncio.run(main())
