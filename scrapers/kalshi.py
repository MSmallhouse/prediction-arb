"""
Kalshi MLB game-winner market scraper.

Uses REST polling (no auth required for public market data).

Key Kalshi market structure:
  - Series ticker: KXMLBGAME
  - Event ticker:  KXMLBGAME-26APR231545LADSF  (date + time + away + home abbrevs)
  - Market ticker: KXMLBGAME-26APR231545LADSF-LAD  (one per team per game)
  - Each event has exactly 2 markets (one per team); YES on each = that team wins.
  - Price fields used:
      yes_ask_dollars  — cost to buy YES (enter a long position on that team)
      yes_bid_dollars  — best bid (what you'd receive selling YES)
      yes_sub_title    — city/city+suffix team name (mapped via KALSHI_TO_CANONICAL)
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from config import (
    KALSHI_BASE_URL, KALSHI_MLB_SERIES, KALSHI_NBA_SERIES, KALSHI_NHL_SERIES,
    KALSHI_TO_CANONICAL, NBA_KALSHI_TO_CANONICAL, NHL_KALSHI_TO_CANONICAL,
)

log = logging.getLogger(__name__)


@dataclass
class KalshiMarket:
    event_ticker: str
    market_ticker: str
    team: str                    # canonical team name
    kalshi_label: str            # raw yes_sub_title from Kalshi
    game_datetime: datetime
    yes_ask: float               # cost to buy YES (taker price)
    yes_bid: float               # best bid
    last_price: float
    status: str
    volume: float = 0.0          # total contracts traded
    volume_24h: float = 0.0      # contracts traded last 24h
    open_interest: float = 0.0   # open contracts
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def midpoint(self) -> float:
        return (self.yes_ask + self.yes_bid) / 2


def _normalize_team(kalshi_label: str, event_ticker: str = "") -> Optional[str]:
    """Map Kalshi yes_sub_title to canonical team name (sport-aware)."""
    if event_ticker.startswith("KXNBAGAME"):
        lookup = NBA_KALSHI_TO_CANONICAL
        sport = "NBA"
    elif event_ticker.startswith("KXNHLGAME"):
        lookup = NHL_KALSHI_TO_CANONICAL
        sport = "NHL"
    else:
        lookup = KALSHI_TO_CANONICAL
        sport = "MLB"
    canonical = lookup.get(kalshi_label)
    if canonical is None:
        log.warning(
            "Unknown Kalshi %s team label: %r — add to %s_KALSHI_TO_CANONICAL",
            sport, kalshi_label, sport,
        )
    return canonical


def _parse_event(event: dict) -> list[KalshiMarket]:
    """Parse one game event dict (with nested markets) into KalshiMarket objects."""
    markets = event.get("markets", [])
    result = []

    for m in markets:
        if m.get("status") != "active":
            continue

        label = m.get("yes_sub_title", "")
        event_ticker = m.get("event_ticker", "")
        team = _normalize_team(label, event_ticker)
        if team is None:
            continue

        raw_dt = (
            m.get("occurrence_datetime")
            or m.get("expected_expiration_time")
        )
        if not raw_dt:
            log.warning("Skipping market %s — no datetime field available", m.get("ticker", "?"))
            continue
        try:
            game_dt = datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
        except ValueError as exc:
            log.warning("Could not parse datetime %r for %s: %s", raw_dt, m.get("ticker", "?"), exc)
            continue

        result.append(KalshiMarket(
            event_ticker=m.get("event_ticker", ""),
            market_ticker=m.get("ticker", ""),
            team=team,
            kalshi_label=label,
            game_datetime=game_dt,
            yes_ask=float(m.get("yes_ask_dollars", 0)),
            yes_bid=float(m.get("yes_bid_dollars", 0)),
            last_price=float(m.get("last_price_dollars", 0)),
            status=m.get("status", ""),
            volume=float(m.get("volume_fp", 0)),
            volume_24h=float(m.get("volume_24h_fp", 0)),
            open_interest=float(m.get("open_interest_fp", 0)),
        ))

    return result


async def _fetch_series_markets(
    session: aiohttp.ClientSession, series_ticker: str
) -> list[KalshiMarket]:
    """
    Fetch all open events + nested markets for a series in one paginated call.
    Avoids per-event requests entirely — no 429s.
    """
    markets: list[KalshiMarket] = []
    cursor = None
    page = 0
    total_events = 0

    while True:
        params = {
            "series_ticker": series_ticker,
            "status": "open",
            "with_nested_markets": "true",
            "limit": 200,
        }
        if cursor:
            params["cursor"] = cursor

        try:
            async with session.get(f"{KALSHI_BASE_URL}/events", params=params) as resp:
                resp.raise_for_status()
                data = await resp.json()
        except Exception as exc:
            log.error("Failed to fetch %s events+markets (page %d): %s", series_ticker, page, exc)
            break

        events = data.get("events", [])
        for e in events:
            markets.extend(_parse_event(e))
        total_events += len(events)

        cursor = data.get("cursor")
        page += 1

        if not cursor or not events:
            break

    expected = total_events * 2
    actual = len(markets)
    if actual < expected:
        log.info(
            "%s: parsed %d markets from %d events (skipped %d — likely settled/resolving)",
            series_ticker, actual, total_events, expected - actual,
        )
    else:
        log.info("%s: parsed %d markets from %d events", series_ticker, actual, total_events)
    return markets


async def discover_mlb_events(session: aiohttp.ClientSession) -> list[str]:
    """Return event tickers for open KXMLBGAME events (used for Polymarket slug derivation)."""
    markets = await _fetch_series_markets(session, KALSHI_MLB_SERIES)
    tickers = list({m.event_ticker for m in markets})
    log.info("Discovered %d open %s events", len(tickers), KALSHI_MLB_SERIES)
    return tickers


async def discover_nba_events(session: aiohttp.ClientSession) -> list[str]:
    """Return event tickers for open KXNBAGAME events."""
    markets = await _fetch_series_markets(session, KALSHI_NBA_SERIES)
    tickers = list({m.event_ticker for m in markets})
    log.info("Discovered %d open %s events", len(tickers), KALSHI_NBA_SERIES)
    return tickers


async def discover_nhl_events(session: aiohttp.ClientSession) -> list[str]:
    """Return event tickers for open KXNHLGAME events."""
    markets = await _fetch_series_markets(session, KALSHI_NHL_SERIES)
    tickers = list({m.event_ticker for m in markets})
    log.info("Discovered %d open %s events", len(tickers), KALSHI_NHL_SERIES)
    return tickers


async def fetch_all_prices(
    session: aiohttp.ClientSession,
    event_tickers: list[str],
) -> list[KalshiMarket]:
    """
    Fetch current prices for all events. Uses single paginated list calls
    per series — no per-event requests, no 429s.
    """
    # Determine which series are represented in the ticker list
    series_needed = set()
    for t in event_tickers:
        if t.startswith("KXMLBGAME"):
            series_needed.add(KALSHI_MLB_SERIES)
        elif t.startswith("KXNBAGAME"):
            series_needed.add(KALSHI_NBA_SERIES)
        elif t.startswith("KXNHLGAME"):
            series_needed.add(KALSHI_NHL_SERIES)

    results = await asyncio.gather(*[
        _fetch_series_markets(session, s) for s in series_needed
    ])
    markets = [m for sublist in results for m in sublist]
    log.info("Fetched prices for %d markets total", len(markets))
    return markets
