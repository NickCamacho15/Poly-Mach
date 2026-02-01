"""
Helpers for reasoning about sports market time windows.

Many Polymarket sports market slugs embed a date suffix like YYYY-MM-DD:
  aec-nba-dal-mil-2026-01-25

We use this as a conservative guardrail to avoid trading stale markets.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Optional

_SLUG_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})$")


def parse_slug_date(slug: str) -> Optional[date]:
    """
    Parse a trailing YYYY-MM-DD date from a market slug.

    Returns None if the slug doesn't end with a date.
    """
    m = _SLUG_DATE_RE.search(slug or "")
    if not m:
        return None
    try:
        y, mo, d = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return date(y, mo, d)
    except Exception:
        return None


def is_tradeable_slug(slug: str, now_utc: datetime, *, allow_in_game: bool) -> bool:
    """
    Decide if a market should be tradeable based on its slug date.

    Rules:
    - If no parseable date -> allow (unknown/non-sports slug format).
    - If date < today (UTC) -> block.
    - If date == today (UTC) -> allow only if allow_in_game=True.
    - If date > today (UTC) -> allow.
    """
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    slug_dt = parse_slug_date(slug)
    if slug_dt is None:
        return True

    today = now_utc.date()
    if slug_dt < today:
        return False
    if slug_dt == today:
        return bool(allow_in_game)
    return True

