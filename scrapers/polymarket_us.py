"""
Polymarket US market scraper.

Discovers moneyline (game winner) markets from polymarket.us API.
Matches to Kalshi events by slug derivation.

polymarket.us is a separate CFTC-regulated platform (QCX LLC) from polymarket.com.
Auth: ED25519 (keys generated at polymarket.us/developer after KYC via iOS app).
SDK: polymarket-us (pip package).

Series IDs:
  MLB 2026: 15
  NBA 2025: 4  (covers 2025-26 season through April 2026)
  NHL 2025: 6  (covers 2025-26 season through May 2026)
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from polymarket_us import PolymarketUS
from scrapers.polymarket import _normalize_poly_team

log = logging.getLogger(__name__)

# Series IDs for each sport on polymarket.us
POLY_US_SERIES = {
    "mlb": "15",
    "nba": "4",
    "nhl": "6",
}

# Cached offsets: start scanning from here to find current events.
# Events are chronological (oldest first); these skip past resolved history.
# Updated automatically when the cached offset returns only old events.
_offset_cache: dict[str, int] = {
    "mlb": 400,
    "nba": 1000,
    "nhl": 800,
}


@dataclass
class PolymarketUSMarket:
    event_slug: str          # e.g. "mlb-phi-atl-2026-04-24" (stripped of aec- prefix)
    market_slug: str         # e.g. "aec-mlb-phi-atl-2026-04-24" (full polymarket.us slug)
    market_id: str           # polymarket.us market ID (integer string)
    team: str                # canonical team name (long side)
    opposing_team: str       # canonical team name (short side)
    game_datetime: datetime
    yes_ask: float           # long side best ask (offers[0])
    yes_bid: float           # long side best bid (bids[0])
    opposing_ask: float      # short side ask = 1 - yes_bid
    opposing_bid: float      # short side bid = 1 - yes_ask
    liquidity: float = 0.0   # placeholder — derive from WS book depth
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _strip_aec_prefix(slug: str) -> str:
    """Strip 'aec-' prefix from polymarket.us slug to match our derived slugs."""
    if slug.startswith("aec-"):
        return slug[4:]
    return slug


def _parse_datetime(dt_str: str) -> datetime:
    """Parse ISO datetime string to timezone-aware datetime."""
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def discover_moneyline_markets(
    client: PolymarketUS,
    sport: str,
    kalshi_event_slugs: set[str],
) -> list[PolymarketUSMarket]:
    """
    Fetch moneyline markets from polymarket.us for a given sport.

    Uses offset caching: starts scanning from a cached position that skips
    past resolved historical events. If the cached offset is stale (returns
    only old events), scans forward. If it's too far ahead, scans back.
    Typically fetches 1-2 pages (~2-4s) instead of 10+ (~30s).
    """
    series_id = POLY_US_SERIES.get(sport)
    if series_id is None:
        log.warning("Unknown sport for polymarket.us: %s", sport)
        return []

    sport_slugs = [s for s in kalshi_event_slugs if s.startswith(sport + "-")]
    if not sport_slugs:
        return []
    earliest_date = min(s[-10:] for s in sport_slugs)

    start_offset = _offset_cache.get(sport, 0)
    relevant_events = []
    pages_fetched = 0

    # Scan forward from cached offset
    for offset in range(start_offset, start_offset + 600, 200):
        try:
            resp = client.events.list({"series_id": series_id, "limit": 200, "offset": offset})
            batch = resp.get("events", [])
        except Exception as exc:
            log.error("polymarket.us fetch failed (series=%s offset=%d): %s", series_id, offset, exc)
            break
        if not batch:
            break

        pages_fetched += 1
        relevant_events.extend(batch)

        # Check if we've gone past all our dates
        latest_in_batch = max(e.get("startDate", "")[:10] for e in batch)
        latest_kalshi_date = max(s[-10:] for s in sport_slugs)
        if latest_in_batch > latest_kalshi_date:
            break  # We've covered all Kalshi dates

    # If cached offset was too high (no matches), scan backwards
    if not relevant_events and start_offset > 0:
        log.info("polymarket.us %s: cached offset %d too high, scanning back", sport, start_offset)
        for offset in range(max(0, start_offset - 400), start_offset, 200):
            try:
                resp = client.events.list({"series_id": series_id, "limit": 200, "offset": offset})
                batch = resp.get("events", [])
            except Exception as exc:
                break
            if not batch:
                break
            pages_fetched += 1
            relevant_events.extend(batch)

    # Update the offset cache: find where relevant events start
    # so next call can skip directly there
    if relevant_events:
        for i, e in enumerate(relevant_events):
            if e.get("startDate", "")[:10] >= earliest_date:
                # This event is in range — cache the offset that would include it
                new_offset = start_offset + (i // 200) * 200
                if new_offset != _offset_cache.get(sport):
                    _offset_cache[sport] = new_offset
                    log.debug("polymarket.us %s: updated offset cache to %d", sport, new_offset)
                break

    # Extract moneyline markets and match to Kalshi
    markets_out = []

    for event in relevant_events:
        moneyline = None
        for m in event.get("markets", []):
            if m.get("sportsMarketType") == "moneyline":
                moneyline = m
                break
        if moneyline is None:
            continue

        market_slug = moneyline.get("slug", "")
        event_slug = _strip_aec_prefix(event.get("slug", ""))

        if event_slug not in kalshi_event_slugs:
            continue

        sides = moneyline.get("marketSides", [])
        if len(sides) != 2:
            continue

        long_side = next((s for s in sides if s.get("long")), None)
        short_side = next((s for s in sides if not s.get("long")), None)
        if long_side is None or short_side is None:
            continue

        long_team_raw = long_side.get("team", {}).get("name", "")
        short_team_raw = short_side.get("team", {}).get("name", "")
        long_team = _normalize_poly_team(long_team_raw)
        short_team = _normalize_poly_team(short_team_raw)
        if long_team is None or short_team is None:
            log.warning("Unknown polymarket.us team: %r / %r in %s", long_team_raw, short_team_raw, event_slug)
            continue

        best_bid = moneyline.get("bestBidQuote", {}).get("value")
        best_ask = moneyline.get("bestAskQuote", {}).get("value")

        yes_bid = float(best_bid) if best_bid else 0.0
        yes_ask = float(best_ask) if best_ask else 1.0

        game_start = moneyline.get("gameStartTime") or event.get("startDate", "")
        game_dt = _parse_datetime(game_start)

        markets_out.append(PolymarketUSMarket(
            event_slug=event_slug,
            market_slug=market_slug,
            market_id=str(moneyline.get("id", "")),
            team=long_team,
            opposing_team=short_team,
            game_datetime=game_dt,
            yes_ask=yes_ask,
            yes_bid=yes_bid,
            opposing_ask=round(1.0 - yes_bid, 4) if yes_bid > 0 else 1.0,
            opposing_bid=round(1.0 - yes_ask, 4) if yes_ask < 1 else 0.0,
        ))

    log.info(
        "polymarket.us %s: %d markets matched (%d pages, offset=%d)",
        sport.upper(), len(markets_out), pages_fetched, start_offset,
    )
    return markets_out


def discover_all_sports(
    client: PolymarketUS,
    kalshi_event_slugs: set[str],
) -> list[PolymarketUSMarket]:
    """Discover moneyline markets for MLB + NBA + NHL."""
    markets = []
    for sport in ["mlb", "nba", "nhl"]:
        markets.extend(discover_moneyline_markets(client, sport, kalshi_event_slugs))
    return markets
