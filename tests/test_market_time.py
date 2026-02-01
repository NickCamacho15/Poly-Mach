"""
Tests for slug date parsing + tradeability guardrails.
"""

from datetime import datetime, timezone

from src.utils.market_time import is_tradeable_slug, parse_slug_date


def test_parse_slug_date_parses_trailing_date():
    assert str(parse_slug_date("aec-nba-dal-mil-2026-01-25")) == "2026-01-25"


def test_parse_slug_date_returns_none_without_date():
    assert parse_slug_date("nba-lakers-vs-celtics") is None


def test_is_tradeable_slug_blocks_past_dates():
    now = datetime(2026, 2, 1, 0, 0, 0, tzinfo=timezone.utc)
    assert is_tradeable_slug("aec-nba-dal-mil-2026-01-25", now, allow_in_game=False) is False
    assert is_tradeable_slug("aec-nba-dal-mil-2026-01-25", now, allow_in_game=True) is False


def test_is_tradeable_slug_blocks_today_unless_allow_in_game():
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    slug = f"aec-nba-dal-mil-{today}"
    assert is_tradeable_slug(slug, now, allow_in_game=False) is False
    assert is_tradeable_slug(slug, now, allow_in_game=True) is True


def test_is_tradeable_slug_allows_future_dates():
    now = datetime(2026, 2, 1, 0, 0, 0, tzinfo=timezone.utc)
    assert is_tradeable_slug("aec-nba-dal-mil-2026-02-02", now, allow_in_game=False) is True

