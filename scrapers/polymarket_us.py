"""
Polymarket US market scraper.

Discovers moneyline (game winner) markets from polymarket.us API.
Matches to Kalshi events by slug derivation.

polymarket.us is a separate CFTC-regulated platform (QCX LLC) from polymarket.com.
Auth: ED25519 (keys generated at polymarket.us/developer after KYC via iOS app).
SDK: polymarket-us (pip package).

Series IDs (pass as "seriesId": [int] — a snake_case "series_id" is ignored by
the gateway and silently returns every series):
  MLB 2026: 15
  NBA 2025: 4  (covers 2025-26 season through April 2026)
  NHL 2025: 6  (covers 2025-26 season through May 2026)
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from polymarket_us import PolymarketUS
from config import POLY_US_CFB_SERIES_ID, normalize_cfb_team
from scrapers.polymarket import _normalize_poly_team

log = logging.getLogger(__name__)

# Series IDs for each sport on polymarket.us. Seeded with known values and
# refreshed from /v1/series at discovery: each season gets a NEW series (slug
# "<sport>-<year>"), so a hardcoded id silently stops matching when the season
# rolls over. A sport whose new-season series does not exist yet keeps its old
# id and simply matches nothing until Polymarket creates it.
POLY_US_SERIES = {
    "mlb": "15",
    "nba": "4",
    "nhl": "6",
}

_SERIES_SLUG_RE = re.compile(r"^(mlb|nba|nhl)-(\d{4})$")


def refresh_series_ids(client: PolymarketUS) -> dict[str, str]:
    """
    Re-resolve each sport's series id to the newest season Polymarket lists.
    Mutates and returns POLY_US_SERIES. Failures leave the current ids intact.
    """
    try:
        resp = client.series.list()
    except Exception as exc:
        log.warning("polymarket.us series list failed (%s) — keeping series ids %s",
                    exc, POLY_US_SERIES)
        return POLY_US_SERIES

    items = resp.get("series", []) if isinstance(resp, dict) else (resp or [])
    newest: dict[str, tuple[int, str]] = {}
    for entry in items:
        match = _SERIES_SLUG_RE.match(str(entry.get("slug", "")))
        if not match:
            continue
        sport, year = match.group(1), int(match.group(2))
        series_id = str(entry.get("id", ""))
        if not series_id:
            continue
        if sport not in newest or year > newest[sport][0]:
            newest[sport] = (year, series_id)

    for sport, (year, series_id) in newest.items():
        if POLY_US_SERIES.get(sport) != series_id:
            log.info("polymarket.us %s series -> %s (%s-%d)", sport, series_id, sport, year)
            POLY_US_SERIES[sport] = series_id
    return POLY_US_SERIES

# The gateway ignores an unknown "series_id" query param and returns events from
# every series (NFL included). The parameter it honours is "seriesId" as a list
# of ints, alongside ISO-8601 startDateMin/startDateMax. Discovery now queries
# the exact date window instead of paging blindly through history — the old
# offset-scan silently matched zero markets once the season moved past its
# hardcoded offsets.
PAGE_SIZE = 200
MAX_PAGES = 10
CFB_PAGE_SIZE = 50
DATE_PAD_DAYS = 1


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


# The game-winner market used to be tagged sportsMarketType == "moneyline".
# It is now sport-specific ("baseball_team_full_game_winner",
# "basketball_team_full_game_winner", ...), and sits alongside first-five and
# per-inning winner markets that share sportsMarketTypeV2 == MONEYLINE. Match on
# the full-game suffix, keeping the legacy value as a fallback.
_LEGACY_MONEYLINE = "moneyline"
_FULL_GAME_WINNER_SUFFIX = "_full_game_winner"


def _pick_full_game_moneyline(event: dict) -> Optional[dict]:
    """The full-game winner market for an event, or None."""
    fallback = None
    for m in event.get("markets", []):
        mtype = m.get("sportsMarketType") or ""
        if mtype.endswith(_FULL_GAME_WINNER_SUFFIX):
            return m
        if mtype == _LEGACY_MONEYLINE:
            fallback = m
    return fallback


def _extract_markets(
    events: list[dict],
    kalshi_event_slugs: set[str],
) -> list[PolymarketUSMarket]:
    """Pull the full-game moneyline out of one page of events."""
    markets_out: list[PolymarketUSMarket] = []

    for event in events:
        moneyline = _pick_full_game_moneyline(event)
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

    return markets_out


def discover_moneyline_markets(
    client: PolymarketUS,
    sport: str,
    kalshi_event_slugs: set[str],
) -> list[PolymarketUSMarket]:
    """
    Fetch moneyline markets from polymarket.us for a given sport.

    Queries the gateway for the exact date window covering the open Kalshi
    games, then matches by slug. One page is normally enough.
    """
    series_id = POLY_US_SERIES.get(sport)
    if series_id is None:
        log.warning("Unknown sport for polymarket.us: %s", sport)
        return []

    sport_slugs = [s for s in kalshi_event_slugs if s.startswith(sport + "-")]
    if not sport_slugs:
        return []

    pad = timedelta(days=DATE_PAD_DAYS)
    earliest = datetime.strptime(min(s[-10:] for s in sport_slugs), "%Y-%m-%d") - pad
    latest = datetime.strptime(max(s[-10:] for s in sport_slugs), "%Y-%m-%d") + pad

    # Extract per page and drop the raw payload immediately. Accumulating every
    # page first kept hundreds of full event dicts live at once — each carries
    # ~444 markets (props, per-inning, spreads) of which we want exactly one —
    # and that peak live set is what made gen-2 collections cost 755-1219ms on
    # this box. Refcounting frees each page as soon as the loop rebinds `batch`.
    markets_out: list[PolymarketUSMarket] = []
    pages_fetched = 0
    for page in range(MAX_PAGES):
        try:
            resp = client.events.list({
                "seriesId": [int(series_id)],
                "startDateMin": earliest.strftime("%Y-%m-%dT00:00:00Z"),
                "startDateMax": latest.strftime("%Y-%m-%dT00:00:00Z"),
                "limit": PAGE_SIZE,
                "offset": page * PAGE_SIZE,
            })
            batch = resp.get("events", [])
        except Exception as exc:
            log.error("polymarket.us fetch failed (series=%s page=%d): %s", series_id, page, exc)
            break
        pages_fetched += 1
        markets_out.extend(_extract_markets(batch, kalshi_event_slugs))
        if len(batch) < PAGE_SIZE:
            break
    else:
        log.warning("polymarket.us %s: hit %d-page cap — events may be missing", sport, MAX_PAGES)

    log.info(
        "polymarket.us %s: %d markets matched (%d pages, %s..%s)",
        sport.upper(), len(markets_out), pages_fetched,
        earliest.strftime("%Y-%m-%d"), latest.strftime("%Y-%m-%d"),
    )
    return markets_out


def discover_cfb_markets(
    client: PolymarketUS,
    window_start: datetime,
    window_end: datetime,
) -> list[PolymarketUSMarket]:
    """
    College football moneylines in a kickoff window.

    Unlike the pro leagues this canNOT filter by slug: Kalshi's CFB tickers
    (`KXNCAAFGAME-26SEP19UNCCLEM`) do not derive Polymarket's slugs
    (`cfb-ncar-clmsn-2026-09-19`). We pull every CFB event in the window and
    let `main` join them to Kalshi on (team pair + kickoff date). Team names
    are the normalised school name on BOTH sides so the join key lines up.
    """
    out: list[PolymarketUSMarket] = []
    pages = 0
    for page in range(MAX_PAGES):
        try:
            resp = client.events.list({
                "seriesId": [int(POLY_US_CFB_SERIES_ID)],
                "startDateMin": window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "startDateMax": window_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "limit": CFB_PAGE_SIZE,
                "offset": page * CFB_PAGE_SIZE,
            })
            batch = resp.get("events", [])
        except Exception as exc:
            log.error("polymarket.us CFB fetch failed (page=%d): %s", page, exc)
            break
        pages += 1
        for event in batch:
            market = _pick_full_game_moneyline(event)
            if market is None:
                continue
            sides = market.get("marketSides", [])
            if len(sides) != 2:
                continue
            long_side = next((x for x in sides if x.get("long")), None)
            short_side = next((x for x in sides if not x.get("long")), None)
            if long_side is None or short_side is None:
                continue
            long_team = normalize_cfb_team((long_side.get("team") or {}).get("safeName", ""))
            short_team = normalize_cfb_team((short_side.get("team") or {}).get("safeName", ""))
            if not long_team or not short_team:
                continue

            best_bid = market.get("bestBidQuote", {}).get("value") if market.get("bestBidQuote") else None
            best_ask = market.get("bestAskQuote", {}).get("value") if market.get("bestAskQuote") else None
            yes_bid = float(best_bid) if best_bid else 0.0
            yes_ask = float(best_ask) if best_ask else 1.0

            game_start = market.get("gameStartTime") or event.get("startDate", "")
            out.append(PolymarketUSMarket(
                event_slug=_strip_aec_prefix(event.get("slug", "")),
                market_slug=market.get("slug", ""),
                market_id=str(market.get("id", "")),
                team=long_team,
                opposing_team=short_team,
                game_datetime=_parse_datetime(game_start),
                yes_ask=yes_ask,
                yes_bid=yes_bid,
                opposing_ask=round(1.0 - yes_bid, 4) if yes_bid > 0 else 1.0,
                opposing_bid=round(1.0 - yes_ask, 4) if yes_ask < 1 else 0.0,
            ))
        if len(batch) < CFB_PAGE_SIZE:
            break
    else:
        log.warning("polymarket.us CFB: hit %d-page cap — events may be missing", MAX_PAGES)

    log.info("polymarket.us CFB: %d moneyline markets (%d pages, %s..%s)",
             len(out), pages, window_start.strftime("%Y-%m-%d %H:%M"),
             window_end.strftime("%Y-%m-%d %H:%M"))
    return out


def discover_all_sports(
    client: PolymarketUS,
    kalshi_event_slugs: set[str],
) -> list[PolymarketUSMarket]:
    """Discover moneyline markets for MLB + NBA + NHL."""
    refresh_series_ids(client)
    markets = []
    for sport in ["mlb", "nba", "nhl"]:
        markets.extend(discover_moneyline_markets(client, sport, kalshi_event_slugs))
    return markets
