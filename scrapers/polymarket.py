"""
Polymarket MLB game-winner market scraper.

Discovery strategy: derive Polymarket event slugs directly from Kalshi event
tickers (abbreviations match with two exceptions: ATH→oak, AZ→ari).
Slug format: mlb-{away_abbr}-{home_abbr}-YYYY-MM-DD

Price source: Polymarket CLOB REST API (/price endpoint, no auth required).
Polling approach consistent with Kalshi — no WebSocket needed for Phase 2.

Polymarket game winner market identification:
  - slug == event slug (no suffix)
  - groupItemThreshold == "0"
  - outcomes == ["Team A", "Team B"] (full team names)
  - clobTokenIds == [token_a, token_b]
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, date
from typing import Optional

import aiohttp

from config import (
    CANONICAL_TO_POLY_ABBR,
    KALSHI_ABBR_SET,
    POLYMARKET_TO_CANONICAL,
    NBA_CANONICAL_TO_POLY_ABBR,
    NBA_KALSHI_ABBR_SET,
    NBA_POLYMARKET_TO_CANONICAL,
    NHL_CANONICAL_TO_POLY_ABBR,
    NHL_KALSHI_ABBR_SET,
    NHL_POLYMARKET_TO_CANONICAL,
)

log = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

# Month name → zero-padded number
_MONTH_MAP = {
    "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04",
    "MAY": "05", "JUN": "06", "JUL": "07", "AUG": "08",
    "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12",
}

# Regex to parse Kalshi MLB event ticker:
# KXMLBGAME-26APR221310NYYBOS  (has 4-digit time field)
_MLB_TICKER_RE = re.compile(
    r"KXMLBGAME-(\d{2})([A-Z]{3})(\d{2})\d{4}([A-Z]+)$"
)

# Regex to parse Kalshi NBA event ticker:
# KXNBAGAME-26APR27MINDEN  (no time field)
_NBA_TICKER_RE = re.compile(
    r"KXNBAGAME-(\d{2})([A-Z]{3})(\d{2})([A-Z]+)$"
)

# Regex to parse Kalshi NHL event ticker:
# KXNHLGAME-26APR26EDMANA  (no time field)
_NHL_TICKER_RE = re.compile(
    r"KXNHLGAME-(\d{2})([A-Z]{3})(\d{2})([A-Z]+)$"
)


def _split_kalshi_abbrs(combined: str, abbr_set: list[str]) -> Optional[tuple[str, str]]:
    """
    Split a concatenated Kalshi away+home abbreviation string.
    E.g. "NYYBOS" → ("NYY", "BOS"), "MINDEN" → ("MIN", "DEN").
    Uses greedy longest-first match from left.
    """
    abbr_lookup = set(abbr_set)
    for abbr in abbr_set:
        if combined.startswith(abbr):
            rest = combined[len(abbr):]
            if rest in abbr_lookup:
                return abbr, rest
    return None


def _kalshi_abbr_to_poly(kalshi_abbr: str, sport: str) -> Optional[str]:
    """
    Convert a Kalshi team abbreviation to the Polymarket slug abbreviation.
    Sport-aware: uses NHL, NBA or MLB lookup tables.
    """
    if sport == "nhl":
        from config import NHL_KALSHI_TO_CANONICAL
        canonical = NHL_KALSHI_TO_CANONICAL.get(kalshi_abbr)
        poly_abbr = NHL_CANONICAL_TO_POLY_ABBR.get(canonical) if canonical else None
    elif sport == "nba":
        from config import NBA_KALSHI_TO_CANONICAL
        canonical = NBA_KALSHI_TO_CANONICAL.get(kalshi_abbr)
        poly_abbr = NBA_CANONICAL_TO_POLY_ABBR.get(canonical) if canonical else None
    else:
        from config import KALSHI_TO_CANONICAL
        canonical = KALSHI_TO_CANONICAL.get(kalshi_abbr)
        poly_abbr = CANONICAL_TO_POLY_ABBR.get(canonical) if canonical else None

    if canonical is None:
        log.warning("Unknown Kalshi %s abbr: %r", sport.upper(), kalshi_abbr)
        return kalshi_abbr.lower()
    if poly_abbr is None:
        log.warning("No Polymarket abbr for %s canonical: %r", sport.upper(), canonical)
        return kalshi_abbr.lower()
    return poly_abbr


def kalshi_ticker_to_poly_slug(event_ticker: str) -> Optional[str]:
    """
    Derive Polymarket event slug from a Kalshi event ticker.
    Handles MLB (KXMLBGAME-...), NBA (KXNBAGAME-...), and NHL (KXNHLGAME-...).

    MLB: "KXMLBGAME-26APR221845NYYBOS" → "mlb-nyy-bos-2026-04-22"
    NBA: "KXNBAGAME-26APR27MINDEN"     → "nba-min-den-2026-04-27"
    NHL: "KXNHLGAME-26APR26EDMANA"     → "nhl-edm-ana-2026-04-26"
    """
    if event_ticker.startswith("KXNHLGAME"):
        m = _NHL_TICKER_RE.match(event_ticker)
        sport = "nhl"
        abbr_set = NHL_KALSHI_ABBR_SET
    elif event_ticker.startswith("KXNBAGAME"):
        m = _NBA_TICKER_RE.match(event_ticker)
        sport = "nba"
        abbr_set = NBA_KALSHI_ABBR_SET
    else:
        m = _MLB_TICKER_RE.match(event_ticker)
        sport = "mlb"
        abbr_set = KALSHI_ABBR_SET

    if not m:
        return None

    yy, mon, dd, combined = m.groups()
    mm = _MONTH_MAP.get(mon)
    if mm is None:
        return None

    parts = _split_kalshi_abbrs(combined, abbr_set)
    if parts is None:
        log.warning("Could not split abbreviations: %r (from %s)", combined, event_ticker)
        return None

    away_k, home_k = parts
    away_p = _kalshi_abbr_to_poly(away_k, sport)
    home_p = _kalshi_abbr_to_poly(home_k, sport)

    if away_p is None or home_p is None:
        return None

    return f"{sport}-{away_p}-{home_p}-20{yy}-{mm}-{dd}"


@dataclass
class PolymarketMarket:
    event_slug: str
    market_id: str
    team: str                    # canonical team name
    poly_label: str              # raw outcome label from Polymarket
    game_datetime: datetime
    token_id: str                # CLOB token ID for this outcome
    yes_ask: float               # cost to buy YES (taker buy price from CLOB)
    yes_bid: float               # best bid (updated by WS; 0.0 until first WS update)
    outcome_price: float         # price from Gamma API (used before CLOB poll)
    liquidity: float = 0.0       # USDC in order book (from Gamma API)
    yes_ask_size: float = 0.0   # contracts available at best ask (from WS)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _normalize_poly_team(label: str) -> Optional[str]:
    # Check MLB full names first
    canonical = POLYMARKET_TO_CANONICAL.get(label)
    if canonical is not None:
        return canonical
    # Check NBA short names
    canonical = NBA_POLYMARKET_TO_CANONICAL.get(label)
    if canonical is not None:
        return canonical
    # Check NHL short names / nicknames
    canonical = NHL_POLYMARKET_TO_CANONICAL.get(label)
    if canonical is not None:
        return canonical
    # Fallback: match by last word of MLB full name (e.g. "New York Yankees" → "Yankees")
    last_word = label.split()[-1]
    for k, v in POLYMARKET_TO_CANONICAL.items():
        if k.endswith(last_word):
            return v
    log.warning("Unknown Polymarket team label: %r", label)
    return None


async def _fetch_event_by_slug(
    session: aiohttp.ClientSession, slug: str
) -> Optional[dict]:
    """Fetch a Polymarket event dict from Gamma API by slug."""
    try:
        async with session.get(
            f"{GAMMA_BASE}/events", params={"slug": slug}
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            if isinstance(data, list) and data:
                return data[0]
    except Exception as exc:
        log.error("Failed to fetch Polymarket event %s: %s", slug, exc)
    return None


def _extract_game_winner_market(event: dict) -> Optional[dict]:
    """
    Find the game winner binary market within an event.
    Identified by: slug == event slug (no suffix), groupItemThreshold == "0".
    """
    event_slug = event.get("ticker", "")
    for m in event.get("markets", []):
        if (
            m.get("slug") == event_slug
            and str(m.get("groupItemThreshold", "")) == "0"
        ):
            return m
    # Fallback: find market whose question is just "TeamA vs. TeamB"
    for m in event.get("markets", []):
        if " vs. " in m.get("question", "") and "?" not in m.get("question", ""):
            return m
    return None


async def _fetch_clob_buy_price(
    session: aiohttp.ClientSession, token_id: str
) -> Optional[float]:
    """Fetch current taker buy price for a CLOB token."""
    try:
        async with session.get(
            f"{CLOB_BASE}/price",
            params={"token_id": token_id, "side": "sell"},
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return float(data.get("price", 0))
            log.warning("CLOB /price returned %d for token %s...", resp.status, token_id[:16])
    except Exception as exc:
        log.error("CLOB price fetch failed for %s...: %s", token_id[:16], exc)
    return None


async def refresh_clob_prices(
    session: aiohttp.ClientSession,
    markets: list[PolymarketMarket],
    max_concurrency: int = 20,
) -> None:
    """
    Fetch live CLOB ask prices and update yes_ask in-place for each market.
    Called only for arb candidates after a Gamma pre-screen — typically 0-10 calls.
    If CLOB returns no price (empty orderbook), yes_ask is set to 1.0 so the
    market can't pass any spread check.
    """
    sem = asyncio.Semaphore(max_concurrency)

    async def update_one(market: PolymarketMarket) -> None:
        async with sem:
            price = await _fetch_clob_buy_price(session, market.token_id)
        if price is not None:
            market.yes_ask = price
        else:
            log.warning(
                "CLOB returned no price for %s %s (token %s...) — no orderbook",
                market.event_slug, market.team, market.token_id[:16],
            )
            market.yes_ask = 1.0  # ensures this market can't form a profitable arb

    await asyncio.gather(*[update_one(m) for m in markets])
    log.info("CLOB refresh: updated %d Poly markets", len(markets))


async def discover_and_fetch(
    session: aiohttp.ClientSession,
    kalshi_event_tickers: list[str],
    max_concurrency: int = 5,
) -> list[PolymarketMarket]:
    """
    For each Kalshi event ticker, derive the Polymarket slug, fetch the event,
    extract the game winner market, and fetch current CLOB prices.
    Returns flat list of PolymarketMarket objects (2 per matched game).
    """
    sem = asyncio.Semaphore(max_concurrency)
    results: list[PolymarketMarket] = []

    async def process_one(kalshi_ticker: str) -> list[PolymarketMarket]:
        slug = kalshi_ticker_to_poly_slug(kalshi_ticker)
        if slug is None:
            return []

        async with sem:
            event = await _fetch_event_by_slug(session, slug)
            await asyncio.sleep(0.1)

        if event is None:
            log.warning("Polymarket event not found for slug: %s", slug)
            return []

        market = _extract_game_winner_market(event)
        if market is None:
            log.warning("No game winner market found in %s", slug)
            return []

        try:
            outcomes = json.loads(market.get("outcomes", "[]"))
            token_ids = json.loads(market.get("clobTokenIds", "[]"))
            outcome_prices = json.loads(market.get("outcomePrices", "[]"))
        except Exception as exc:
            log.error("Failed to parse market fields for %s: %s", slug, exc)
            return []

        game_start = market.get("gameStartTime") or event.get("endDate", "")
        try:
            game_dt = datetime.fromisoformat(
                game_start.replace(" ", "T").replace("+00", "+00:00")
            ).replace(tzinfo=timezone.utc)
        except Exception:
            game_dt = datetime.now(timezone.utc)

        market_liquidity = float(market.get("liquidityNum") or market.get("liquidity") or 0)

        markets_out = []
        for i, (label, token_id, op_str) in enumerate(
            zip(outcomes, token_ids, outcome_prices)
        ):
            team = _normalize_poly_team(label)
            if team is None:
                continue

            # Gamma outcomePrices = displayed probability (midpoint), NOT real CLOB ask.
            # Used as a seed price only; caller should CLOB-refresh before arb detection.
            yes_ask = float(op_str)

            markets_out.append(PolymarketMarket(
                event_slug=slug,
                market_id=market.get("id", ""),
                team=team,
                poly_label=label,
                game_datetime=game_dt,
                token_id=token_id,
                yes_ask=yes_ask,
                yes_bid=0.0,
                outcome_price=yes_ask,
                liquidity=market_liquidity,
            ))

        return markets_out

    tasks = [process_one(t) for t in kalshi_event_tickers]
    all_results = await asyncio.gather(*tasks)
    for r in all_results:
        results.extend(r)

    log.info(
        "Polymarket: fetched %d markets across %d Kalshi events",
        len(results),
        len(kalshi_event_tickers),
    )
    return results
