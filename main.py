"""
MLB/NBA/NHL prediction market arb scanner — WebSocket edition.

REST: hourly market discovery only.
WebSocket: live price updates from Kalshi and Polymarket US.
Output: arb_durations.csv with OPEN/CLOSE events and duration_seconds.

No trading — data collection and duration research only.
"""

import asyncio
import gc
import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

import config
from config import DISCOVERY_INTERVAL, MIN_GROSS_SPREAD
from scrapers.kalshi import (
    discover_cfb_events, discover_mlb_events, discover_nba_events,
    discover_nhl_events, fetch_all_prices, KalshiMarket,
)
from scrapers.polymarket import PolymarketMarket, kalshi_ticker_to_poly_slug, register_cfb_slug_map
from scrapers.polymarket_us import PolymarketUSMarket, discover_all_sports, discover_cfb_markets
from scrapers.polymarket_us_ws import PolymarketUSWSClient
from scrapers.polymarket_us_private_ws import PolymarketUSPrivateWSClient
from scrapers.kalshi_ws import KalshiWSClient
from arb_detector import evaluate_event, find_arbs
from arb_tracker import ArbTracker, log_arb_duration, _sport as _arb_sport
import convergence_tracker
import csv_writer
import executor

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ── College football ─────────────────────────────────────────────────────────
# DETECT-ONLY while we gather data: arbs are logged but `executor.config
# .excluded_sports` blocks execution, because the velocity/price-drop filters
# were tuned on baseball and hockey. Football scores in 7-point chunks, so those
# thresholds are unvalidated here.
# The lookahead is a memory guard, not a trading rule: a Saturday slate is 200+
# games and each costs 2 Kalshi WS subscriptions + 2 Polymarket tokens.
CFB_ENABLED = True
CFB_LOOKAHEAD_HOURS = 3.0   # measured 2026-09-19: 6h = 73 games = +237MB RSS
CFB_JOIN_MAX_HOURS = 36.0   # max kickoff difference when joining the two feeds


async def _no_events() -> list[str]:
    """Placeholder for a disabled sport in the discovery gather()."""
    return []


# ── Live price stores ─────────────────────────────────────────────────────────
kalshi_by_ticker: dict[str, KalshiMarket] = {}    # market_ticker → KalshiMarket
poly_by_token: dict[str, PolymarketMarket] = {}   # synthetic_token → PolymarketMarket

# Mapping from WS slug to the two PolymarketMarket token keys it updates
_poly_slug_to_tokens: dict[str, tuple[str, str]] = {}  # market_slug → (long_token, short_token)

# ── Incremental arb detection state ──────────────────────────────────────────
# A WS tick changes ONE game's prices, so only that game is re-priced. The
# cache holds the current opportunities per Kalshi event; `ArbTracker.update()`
# still receives the complete set (anything absent is treated as closed), so the
# cache must stay authoritative for every game, not just the one that ticked.
_arb_cache: dict[str, list] = {}                      # event_ticker → opportunities
_kalshi_by_event: dict[str, list[KalshiMarket]] = {}  # event_ticker → its 2 markets
_poly_by_key: dict[tuple[str, str], PolymarketMarket] = {}   # (event_slug, team) → market
_events_by_event_slug: dict[str, list[str]] = {}      # poly event_slug → event_tickers

# ── Arb trackers ──────────────────────────────────────────────────────────────
THRESHOLD_3 = 0.03
THRESHOLD_4 = 0.04
LOG_FILE_3 = Path("arb_durations_3.csv")
LOG_FILE_4 = Path("arb_durations_4.csv")
tracker_3 = ArbTracker()
tracker_4 = ArbTracker()

poly_ws: PolymarketUSWSClient | None = None
poly_private_ws: PolymarketUSPrivateWSClient | None = None
kalshi_ws: KalshiWSClient | None = None
_poly_us_client = None  # PolymarketUS client, set in _discovery_loop
_ready_after: datetime | None = None
_last_tick: datetime | None = None
_last_logged_spread: float = -999.0

# WS confirmation: only trust prices that WS has updated at least once.
_poly_ws_confirmed: set[str] = set()    # synthetic tokens confirmed by WS
_kalshi_ws_confirmed: set[str] = set()  # market_tickers confirmed by WS

WARMUP_SECONDS = 30
HEARTBEAT_INTERVAL = 300
HOUSEKEEPING_INTERVAL = 5   # convergence flush cadence (was: every poly tick)
_warmup_logged = False


# ── Arb check ─────────────────────────────────────────────────────────────────

async def _check_arbs(changed_events: Optional[list[str]] = None) -> None:
    """
    Detect arb opens/closes and log them.

    `changed_events` names the games whose prices just moved; only those are
    re-priced, and the rest are read from `_arb_cache`. Pass None for a full
    rescan (startup and after discovery). The tracker still sees the complete
    opportunity set either way — that is what makes a CLOSE a real close and
    not an artefact of only looking at one game.
    """
    now = datetime.now(timezone.utc)

    if _ready_after is None or now < _ready_after:
        return

    global _warmup_logged  # noqa: PLW0603
    if not _warmup_logged:
        _warmup_logged = True
        log.info(
            "Post-warmup: K %d/%d confirmed, P %d/%d confirmed — "
            "unconfirmed markets excluded from arb detection",
            len(_kalshi_ws_confirmed), len(kalshi_by_ticker),
            len(_poly_ws_confirmed), len(poly_by_token),
        )
        changed_events = None  # force a full scan on the first post-warmup tick

    if changed_events is None:
        _arb_cache.clear()
        for event_ticker in list(_kalshi_by_event):
            _recompute_event(event_ticker)
    else:
        for event_ticker in changed_events:
            _recompute_event(event_ticker)

    arbs_3 = [o for opps in _arb_cache.values() for o in opps]
    arbs_3.sort(key=lambda o: o.gross_spread, reverse=True)
    arbs_4 = [o for o in arbs_3 if o.gross_spread >= THRESHOLD_4 - 1e-9]

    global _last_logged_spread  # noqa: PLW0603
    if arbs_3:
        best = arbs_3[0]
        if abs(best.gross_spread - _last_logged_spread) >= 0.001:
            opp = best
            log.info(
                "\n\n  prices: K: %s %s %dc   P: %s %dc   gross=%.1f%%\n",
                opp.kalshi_order_market.team, opp.kalshi_side,
                round(opp.kalshi_ask * 100),
                opp.poly_market.team,
                round(opp.poly_market.yes_ask * 100),
                opp.gross_spread * 100,
            )
            _last_logged_spread = best.gross_spread
    elif _last_logged_spread != 0.0:
        log.info("best spread: all negative (markets efficiently priced)")
        _last_logged_spread = 0.0

    new_4, closed_4 = tracker_4.update(arbs_4, now)
    new_3, closed_3 = tracker_3.update(arbs_3, now)

    if not new_4 and not closed_4 and not new_3 and not closed_3:
        return

    ts = now.strftime("%H:%M:%S.") + f"{now.microsecond // 1000:03d}"

    # Count only arbs the executor could ACT on. Detect-only sports (CFB) can
    # never produce an attempt, so counting them makes the "zero execution
    # attempts" health check fire on a perfectly healthy quiet night — it tripped
    # 2026-09-20 00:44 UTC on 20 CFB arbs out of 34. Adding any detect-only sport
    # would otherwise guarantee a false alert.
    global _arb4_count  # noqa: PLW0603
    _arb4_count += sum(
        1 for t in new_4
        if _sport_of(t) not in executor.config.excluded_sports
    )

    for t in new_4:
        opp = t.opportunity
        print(
            f"[{ts}] ARB OPENED  {opp.game_label:<28} "
            f"gross={opp.gross_spread:.1%}  net={opp.net_pretax:.2%}  "
            f"K:{opp.kalshi_order_market.team} {opp.kalshi_side}@{opp.kalshi_ask:.3f}  "
            f"P:{opp.poly_market.team}@{opp.poly_market.yes_ask:.3f}"
        )
        log_arb_duration(t, "OPEN", LOG_FILE_4)

    for t in closed_4:
        opp = t.opportunity
        print(
            f"[{ts}] ARB CLOSED  {opp.game_label:<28} "
            f"duration={t.duration_seconds:.3f}s  peak={t.peak_gross:.1%}"
        )
        log_arb_duration(t, "CLOSE", LOG_FILE_4)

    # Start convergence tracking for all new 3%+ arbs
    for t in new_3:
        opp = t.opportunity
        # Derive market_slug from poly token_id (e.g. "aec-mlb-phi-atl-2026-04-24:long" → "aec-mlb-phi-atl-2026-04-24")
        poly_token = opp.poly_market.token_id
        market_slug = poly_token.rsplit(":", 1)[0] if ":" in poly_token else poly_token
        sport = executor._sport_from_slug(opp.poly_market.event_slug)
        convergence_tracker.start_tracking(
            arb_id=t.first_seen.isoformat(),
            game=opp.game_label,
            sport=sport,
            opener=t.opener,
            arb_gross=opp.gross_spread,
            kalshi_ask=opp.kalshi_ask,
            kalshi_side=opp.kalshi_side,
            poly_team=opp.poly_market.team,
            poly_token_id=poly_token,
            market_slug=market_slug,
            kalshi_market_ticker=opp.kalshi_order_market.market_ticker,
            poly_ask=opp.poly_market.yes_ask,
            poly_bid=opp.poly_market.yes_bid,
            poly_depth=opp.poly_market.yes_ask_size,
            now=now,
        )
        log_arb_duration(t, "OPEN", LOG_FILE_3)
        # Strategy B execution
        if _poly_us_client is not None:
            from arb_tracker import _arb_key
            await executor.maybe_execute(
                client=_poly_us_client,
                opp=opp,
                opener=t.opener,
                arb_key=str(_arb_key(opp)),
                arb_id=t.first_seen.isoformat(),
                poly_by_token=poly_by_token,
            )
    for t in closed_3:
        log_arb_duration(t, "CLOSE", LOG_FILE_3)


# ── WS price callbacks ────────────────────────────────────────────────────────

async def _on_kalshi_price(
    market_ticker: str,
    yes_ask: float,
    yes_bid: float,
    no_ask: float,
    no_bid: float,
    yes_ask_size: float,
    no_ask_size: float,
) -> None:
    global _last_tick
    market = kalshi_by_ticker.get(market_ticker)
    if market is None:
        log.debug("Kalshi tick for unknown ticker %s — ignoring", market_ticker)
        return
    _kalshi_ws_confirmed.add(market_ticker)
    market.yes_ask = yes_ask
    market.yes_bid = yes_bid
    market.no_ask = no_ask
    market.no_bid = no_bid
    market.yes_ask_size = yes_ask_size
    market.no_ask_size = no_ask_size
    market.fetched_at = datetime.now(timezone.utc)
    _last_tick = market.fetched_at

    # Feed convergence tracker with Kalshi price updates
    convergence_tracker.on_kalshi_tick(market_ticker, yes_ask, market.fetched_at)

    await _check_arbs([market.event_ticker])


async def _on_poly_us_price(
    market_slug: str,
    long_ask: float,
    long_bid: float,
    short_ask: float,
    short_bid: float,
    long_ask_size: float,
    short_ask_size: float,
) -> None:
    """Update both long and short PolymarketMarket objects from one WS message."""
    global _last_tick
    token_pair = _poly_slug_to_tokens.get(market_slug)
    if token_pair is None:
        log.debug("PolyUS tick for unknown slug %s — ignoring", market_slug)
        return

    long_token, short_token = token_pair
    now = datetime.now(timezone.utc)

    long_market = poly_by_token.get(long_token)
    if long_market is not None:
        _poly_ws_confirmed.add(long_token)
        long_market.yes_ask = long_ask
        long_market.yes_bid = long_bid
        long_market.yes_ask_size = long_ask_size
        long_market.fetched_at = now
        executor.on_price_update(long_token, long_ask, long_bid)

    short_market = poly_by_token.get(short_token)
    if short_market is not None:
        _poly_ws_confirmed.add(short_token)
        short_market.yes_ask = short_ask
        short_market.yes_bid = short_bid
        short_market.yes_ask_size = short_ask_size
        executor.on_price_update(short_token, short_ask, short_bid)
        short_market.fetched_at = now

    # Feed convergence tracker
    convergence_tracker.on_poly_tick(
        market_slug, long_ask, long_bid, short_ask, short_bid,
        long_ask_size, short_ask_size, now,
    )
    # NOTE: convergence_tracker.flush_expired() used to run here, on every poly
    # tick. It writes CSV, so it is now on the periodic _housekeeping_loop().

    _last_tick = now
    event_slug = (long_market or short_market).event_slug if (long_market or short_market) else None
    await _check_arbs(_events_by_event_slug.get(event_slug, []) if event_slug else [])


def _join_cfb(
    kalshi_markets: list[KalshiMarket],
    cfb_markets: list[PolymarketUSMarket],
) -> list[PolymarketUSMarket]:
    """
    Join Kalshi CFB events to Polymarket CFB events on (team pair + kickoff).

    Registers the resulting ticker→slug map so `kalshi_ticker_to_poly_slug()`
    resolves CFB, then returns only the Polymarket markets that found a Kalshi
    counterpart — an unmatched Polymarket game would otherwise sit in the store
    burning a WS subscription with nothing to arb against.

    Team names are already normalised to the same canonical form on both sides
    (`config.normalize_cfb_team`), so the pair is the join key. Kickoff times
    are compared with a wide tolerance because Kalshi timestamps the scheduled
    start while Polymarket sometimes carries the broadcast window.
    """
    # Kalshi side: event_ticker → (team pair, kickoff)
    k_by_ticker: dict[str, tuple[frozenset[str], datetime]] = {}
    grouped: dict[str, list[KalshiMarket]] = {}
    for m in kalshi_markets:
        if not m.event_ticker.startswith("KXNCAAFGAME"):
            continue
        grouped.setdefault(m.event_ticker, []).append(m)
    for ticker, legs in grouped.items():
        if len(legs) != 2:
            continue
        k_by_ticker[ticker] = (frozenset(leg.team for leg in legs), legs[0].game_datetime)

    p_by_pair: dict[frozenset[str], list[PolymarketUSMarket]] = {}
    for pm in cfb_markets:
        p_by_pair.setdefault(frozenset((pm.team, pm.opposing_team)), []).append(pm)

    slug_map: dict[str, str] = {}
    matched: list[PolymarketUSMarket] = []
    unmatched: list[str] = []
    for ticker, (pair, kickoff) in k_by_ticker.items():
        candidates = p_by_pair.get(pair, [])
        best = None
        best_gap = None
        for pm in candidates:
            gap = abs((pm.game_datetime - kickoff).total_seconds()) / 3600.0
            if gap <= CFB_JOIN_MAX_HOURS and (best_gap is None or gap < best_gap):
                best, best_gap = pm, gap
        if best is None:
            unmatched.append(ticker)
            continue
        slug_map[ticker] = best.event_slug
        matched.append(best)

    register_cfb_slug_map(slug_map)
    log.info("CFB join: %d Kalshi events matched, %d unmatched, %d Poly markets in window",
             len(matched), len(unmatched), len(cfb_markets))
    if unmatched:
        log.info("CFB unmatched (first 5): %s", ", ".join(unmatched[:5]))
    return matched


# ── Price store population ────────────────────────────────────────────────────

def _us_markets_to_poly_markets(us_markets: list[PolymarketUSMarket]) -> list[PolymarketMarket]:
    """
    Convert PolymarketUSMarket objects (one per game) into PolymarketMarket
    objects (two per game, one per team) for compatibility with find_arbs().
    Also populates _poly_slug_to_tokens mapping for WS updates.
    """
    poly_markets = []
    for um in us_markets:
        long_token = f"{um.market_slug}:long"
        short_token = f"{um.market_slug}:short"
        _poly_slug_to_tokens[um.market_slug] = (long_token, short_token)

        # Long side (Team A)
        poly_markets.append(PolymarketMarket(
            event_slug=um.event_slug,
            market_id=um.market_id,
            team=um.team,
            poly_label=um.team,
            game_datetime=um.game_datetime,
            token_id=long_token,
            yes_ask=um.yes_ask,
            yes_bid=um.yes_bid,
            outcome_price=um.yes_ask,
            liquidity=um.liquidity,
        ))

        # Short side (Team B)
        poly_markets.append(PolymarketMarket(
            event_slug=um.event_slug,
            market_id=um.market_id,
            team=um.opposing_team,
            poly_label=um.opposing_team,
            game_datetime=um.game_datetime,
            token_id=short_token,
            yes_ask=um.opposing_ask,
            yes_bid=um.opposing_bid,
            outcome_price=um.opposing_ask,
            liquidity=um.liquidity,
        ))

    return poly_markets


def _rebuild_indexes() -> None:
    """
    Rebuild the lookup tables the incremental tick path uses. Called after every
    discovery, since pruning, new games and the CFB slug registry all change the
    mapping. Values are the same objects held in the stores, so WS mutations
    stay visible without rebuilding.
    """
    _kalshi_by_event.clear()
    _poly_by_key.clear()
    _events_by_event_slug.clear()

    for m in kalshi_by_ticker.values():
        _kalshi_by_event.setdefault(m.event_ticker, []).append(m)

    for pm in poly_by_token.values():
        _poly_by_key[(pm.event_slug, pm.team)] = pm

    for event_ticker in _kalshi_by_event:
        slug = kalshi_ticker_to_poly_slug(event_ticker)
        if slug:
            _events_by_event_slug.setdefault(slug, []).append(event_ticker)

    # Drop cached arbs for games that no longer exist.
    for stale in [e for e in _arb_cache if e not in _kalshi_by_event]:
        _arb_cache.pop(stale, None)


def _recompute_event(event_ticker: str) -> None:
    """Re-price one game into the arb cache, honouring the WS-confirmed filter."""
    k_markets = [
        m for m in _kalshi_by_event.get(event_ticker, [])
        if m.market_ticker in _kalshi_ws_confirmed
    ]
    if len(k_markets) != 2:
        _arb_cache.pop(event_ticker, None)
        return

    slug = kalshi_ticker_to_poly_slug(event_ticker)
    if slug is None:
        _arb_cache.pop(event_ticker, None)
        return

    poly_lookup = {}
    for team in (k_markets[0].team, k_markets[1].team):
        pm = _poly_by_key.get((slug, team))
        if pm is not None and pm.token_id in _poly_ws_confirmed:
            poly_lookup[(slug, team)] = pm

    opps = evaluate_event(event_ticker, k_markets, poly_lookup)
    if opps:
        _arb_cache[event_ticker] = opps
    else:
        _arb_cache.pop(event_ticker, None)


def _populate_stores(
    kalshi_markets: list[KalshiMarket],
    poly_markets: list[PolymarketMarket],
) -> tuple[list[str], list[str]]:
    """
    Merge REST discovery results into price stores.
    Preserve live WS prices for markets already in store.
    Prunes markets that have left the active list.
    """
    fresh_kalshi = {m.market_ticker for m in kalshi_markets}
    fresh_poly   = {m.token_id      for m in poly_markets}

    for m in kalshi_markets:
        existing = kalshi_by_ticker.get(m.market_ticker)
        if existing is not None:
            m.yes_ask = existing.yes_ask
            m.yes_bid = existing.yes_bid
            m.no_ask = existing.no_ask
            m.no_bid = existing.no_bid
            m.yes_ask_size = existing.yes_ask_size
            m.no_ask_size = existing.no_ask_size
        kalshi_by_ticker[m.market_ticker] = m

    for m in poly_markets:
        existing = poly_by_token.get(m.token_id)
        if existing is not None:
            m.yes_ask = existing.yes_ask
            if existing.yes_bid > 0:
                m.yes_bid = existing.yes_bid
        poly_by_token[m.token_id] = m

    # Prune stale markets
    stale_k = [t for t in list(kalshi_by_ticker) if t not in fresh_kalshi]
    stale_p = [t for t in list(poly_by_token)    if t not in fresh_poly]
    for t in stale_k:
        del kalshi_by_ticker[t]
        _kalshi_ws_confirmed.discard(t)
    for t in stale_p:
        del poly_by_token[t]
        _poly_ws_confirmed.discard(t)
    # Clean up slug mapping for pruned poly markets
    stale_slugs = [slug for slug, (lt, st) in _poly_slug_to_tokens.items()
                   if lt not in poly_by_token and st not in poly_by_token]
    for slug in stale_slugs:
        del _poly_slug_to_tokens[slug]
    if stale_k or stale_p:
        log.info("Pruned %d stale Kalshi + %d stale Poly markets", len(stale_k), len(stale_p))

    return stale_k, stale_slugs


# ── Discovery loop ────────────────────────────────────────────────────────────

async def _discovery_loop(session: aiohttp.ClientSession) -> None:
    """
    Hourly REST discovery. On first run, populates stores and launches WS clients.
    On subsequent runs, subscribes new markets to existing WS connections.
    """
    global poly_ws, poly_private_ws, kalshi_ws, _ready_after, _poly_us_client

    # Initialize polymarket.us SDK client (sync, used for REST discovery + order placement)
    from polymarket_us import PolymarketUS
    poly_us_client = PolymarketUS(
        key_id=os.environ.get("POLYMARKET_API_KEY_ID", ""),
        secret_key=os.environ.get("POLYMARKET_PRIVATE_KEY", ""),
    )
    _poly_us_client = poly_us_client

    # Strategy B execution — run indefinitely
    executor.config.enabled = True
    executor.config.max_trades = 0  # 0 = unlimited
    log.info("Strategy B executor ENABLED (unlimited trades, min_gross=%.1f%%, timeout=%ds, drop=%.0fc)",
             executor.config.min_gross_spread * 100,
             executor.config.timeout_seconds,
             executor.config.price_drop_threshold * 100)

    while True:
        log.info("Running market discovery...")
        # NOTE: gc.disable() around this block was tried on 2026-09-19 and
        # REVERTED — it did not avoid the pause, it concentrated it. Deferred
        # collections simply ran together once re-enabled: 9 collections with a
        # 1219ms max, versus 12 collections at 817ms max when left alone. Same
        # total GC time either way. The garbage is real and must be collected;
        # the fix is to create less of it (see per-page extraction in
        # scrapers/polymarket_us.py), not to postpone the bill.
        _gc_was_enabled = False
        try:
            mlb_tickers, nba_tickers, nhl_tickers, cfb_tickers = await asyncio.gather(
                discover_mlb_events(session),
                discover_nba_events(session),
                discover_nhl_events(session),
                discover_cfb_events(session, within_hours=CFB_LOOKAHEAD_HOURS)
                if CFB_ENABLED else _no_events(),
            )
        except Exception as exc:
            log.error("Discovery failed: %s — retrying in 5 minutes", exc)
            await asyncio.sleep(300)
            continue

        event_tickers = mlb_tickers + nba_tickers + nhl_tickers + cfb_tickers
        if not event_tickers:
            log.warning("No open markets. Retrying in 5 minutes.")
            await asyncio.sleep(300)
            continue

        log.info("Discovered %d MLB + %d NBA + %d NHL + %d CFB Kalshi events",
                 len(mlb_tickers), len(nba_tickers), len(nhl_tickers), len(cfb_tickers))

        # Derive slugs and fetch Kalshi prices concurrently with Poly US discovery
        kalshi_slugs = set()
        for t in event_tickers:
            slug = kalshi_ticker_to_poly_slug(t)
            if slug:
                kalshi_slugs.add(slug)

        try:
            # Kalshi prices via REST (async)
            kalshi_markets = await fetch_all_prices(session, event_tickers)
            # Polymarket US discovery via SDK (sync — runs in thread to not block)
            us_markets = await asyncio.to_thread(
                discover_all_sports, poly_us_client, kalshi_slugs,
            )
            # `fetch_all_prices` pulls a whole Kalshi series, so CFB comes back
            # with every open event (291 on a Saturday) regardless of the
            # lookahead window. Drop the out-of-window ones before they reach the
            # stores and cost a WS subscription each.
            if CFB_ENABLED:
                in_window = set(cfb_tickers)
                kalshi_markets = [
                    m for m in kalshi_markets
                    if not m.event_ticker.startswith("KXNCAAFGAME")
                    or m.event_ticker in in_window
                ]

            # CFB is matched by team-pair, not slug, so it discovers separately.
            if cfb_tickers:
                cfb_markets = await asyncio.to_thread(
                    discover_cfb_markets, poly_us_client,
                    datetime.now(timezone.utc) - timedelta(hours=6),
                    datetime.now(timezone.utc) + timedelta(hours=CFB_LOOKAHEAD_HOURS + 6),
                )
                matched = _join_cfb(kalshi_markets, cfb_markets)
                us_markets = us_markets + matched
        except Exception as exc:
            log.error("Price fetch failed: %s — retrying in 5 minutes", exc)
            await asyncio.sleep(300)
            continue

        # Convert polymarket.us markets to PolymarketMarket objects (2 per game)
        poly_markets = _us_markets_to_poly_markets(us_markets)

        stale_kalshi_tickers, stale_poly_slugs = _populate_stores(kalshi_markets, poly_markets)
        _rebuild_indexes()

        _gc_discovery_cycles = globals().get("_gc_discovery_cycles", 0) + 1
        globals()["_gc_discovery_cycles"] = _gc_discovery_cycles
        log.info(
            "Stores: %d Kalshi markets, %d Poly markets (%d games)",
            len(kalshi_by_ticker), len(poly_by_token), len(us_markets),
        )

        if kalshi_ws is not None and stale_kalshi_tickers:
            dropped = kalshi_ws.unsubscribe(stale_kalshi_tickers)
            if dropped:
                log.info("Kalshi WS: unsubscribed %d stale tickers", dropped)
        if poly_ws is not None and stale_poly_slugs:
            dropped = poly_ws.unsubscribe(stale_poly_slugs)
            if dropped:
                log.info("PolyUS WS: unsubscribed %d stale slugs", dropped)

        if poly_ws is None:
            # First discovery: launch WS clients.
            kalshi_api_key = os.environ.get("KALSHI_API_KEY_ID", "")
            kalshi_private_key = os.environ.get("KALSHI_PRIVATE_KEY", "")
            if not kalshi_api_key or not kalshi_private_key:
                log.error("Missing KALSHI_API_KEY_ID or KALSHI_PRIVATE_KEY — Kalshi WS disabled")

            poly_key_id = os.environ.get("POLYMARKET_API_KEY_ID", "")
            poly_secret = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
            if not poly_key_id or not poly_secret:
                log.error("Missing POLYMARKET_API_KEY_ID or POLYMARKET_PRIVATE_KEY — Poly WS disabled")

            # Polymarket US WS — subscribe by market slug
            ws_slugs = list(_poly_slug_to_tokens.keys())
            if poly_key_id and poly_secret:
                poly_ws = PolymarketUSWSClient(
                    key_id=poly_key_id,
                    secret_key=poly_secret,
                    on_price_update=_on_poly_us_price,
                )
                asyncio.create_task(
                    poly_ws.start(initial_slugs=ws_slugs),
                    name="polymarket-us-ws",
                )
                log.info("Polymarket US WS task launched (%d slugs)", len(ws_slugs))

                # Private WS for order events (replaces synchronousExecution).
                poly_private_ws = PolymarketUSPrivateWSClient(
                    key_id=poly_key_id,
                    secret_key=poly_secret,
                )
                executor.set_private_ws(poly_private_ws)
                asyncio.create_task(
                    poly_private_ws.start(),
                    name="polymarket-us-private-ws",
                )
                log.info("Polymarket US private WS task launched")

            # Kalshi WS
            if kalshi_api_key and kalshi_private_key:
                kalshi_ws = KalshiWSClient(
                    api_key_id=kalshi_api_key,
                    private_key_pem=kalshi_private_key,
                    on_price_update=_on_kalshi_price,
                )
                executor.set_kalshi_ws(kalshi_ws)
                asyncio.create_task(
                    kalshi_ws.start(initial_market_tickers=list(kalshi_by_ticker.keys())),
                    name="kalshi-ws",
                )
                log.info("Kalshi WS task launched (%d tickers)", len(kalshi_by_ticker))

            _ready_after = datetime.now(timezone.utc) + timedelta(seconds=WARMUP_SECONDS)
            log.info("Warmup: arb logging suppressed for %ds while prices settle", WARMUP_SECONDS)
        else:
            # Subsequent discovery: subscribe new markets dynamically.
            new_slugs = list(_poly_slug_to_tokens.keys())
            tasks = [poly_ws.subscribe(new_slugs)]
            if kalshi_ws is not None:
                tasks.append(kalshi_ws.subscribe(list(kalshi_by_ticker.keys())))
            await asyncio.gather(*tasks)
            await _check_arbs()
        await asyncio.sleep(DISCOVERY_INTERVAL.total_seconds())


# ── Heartbeat ────────────────────────────────────────────────────────────────

def _rss_mb() -> float:
    """Resident set size in MB (Linux); -1.0 where /proc is unavailable."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return -1.0


# ── Health checks / alerting ─────────────────────────────────────────────────
# Every failure this system has had was SILENT: it kept logging healthy
# heartbeats while matching zero markets (May–Sep), and it detected 103 arbs in
# one evening while a readiness flag blocked every trade (2026-09-19). Process
# liveness alone would have caught NEITHER, so these checks assert on meaning,
# not on "is the process running".
#
# Both URLs are optional and no-op when unset:
#   ALERT_WEBHOOK_URL   — POSTed {"text": ...} when something is wrong
#   ALERT_HEARTBEAT_URL — pinged on every HEALTHY heartbeat (dead man's switch:
#                         the absence of a ping is what alerts, so this also
#                         covers process death and the box disappearing)
ALERT_WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "")
ALERT_HEARTBEAT_URL = os.environ.get("ALERT_HEARTBEAT_URL", "")
# Email alerts go out via SNS. The EC2 instance carries `arb-scanner-role`
# (sns:Publish on this topic only), so there are no credentials in .env — boto3
# picks them up from IMDS. Unset the ARN to disable email alerting.
ALERT_SNS_TOPIC_ARN = os.environ.get(
    "ALERT_SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:828841719603:arb-scanner-alerts"
)
ALERT_SNS_REGION = os.environ.get("ALERT_SNS_REGION", "us-east-1")
ALERT_REPEAT_SUPPRESS_S = 3600.0   # don't re-send an identical alert this often

_sns_client = None
_alerts_sent: dict[str, float] = {}   # alert text → monotonic time last sent
NO_TICK_ALERT_S = 180.0
NOT_READY_ALERT_S = 600.0
ARB4_WITHOUT_ATTEMPT_ALERT = 25
CSV_BACKLOG_ALERT = 100

_arb4_count = 0            # 4%+ arbs opened since start, EXECUTABLE sports only


def _sport_of(t) -> str:
    """Sport label for a tracked arb, using arb_tracker's own classification so
    the health check and the CSV can never disagree about what sport an arb is."""
    try:
        return _arb_sport(t.opportunity)
    except Exception:
        return "UNKNOWN"
_process_start = datetime.now(timezone.utc)


def _health_problems(now: datetime) -> list[str]:
    """Conditions that mean 'alive but not working'. Empty list = healthy."""
    problems: list[str] = []
    uptime_s = (now - _process_start).total_seconds()

    k_conf, p_conf = len(_kalshi_ws_confirmed), len(_poly_ws_confirmed)
    if uptime_s > NOT_READY_ALERT_S:
        # The May–Sep outage: Kalshi fine, Polymarket matching zero markets.
        if k_conf > 0 and p_conf == 0:
            problems.append(f"no Polymarket markets confirmed ({k_conf} Kalshi) — discovery or WS broken")
        if not kalshi_by_ticker:
            problems.append("no Kalshi markets in store — discovery broken")

    if _last_tick is not None:
        since_tick = (now - _last_tick).total_seconds()
        if since_tick > NO_TICK_ALERT_S:
            problems.append(f"no price tick for {since_tick:.0f}s — feeds stalled")
    elif uptime_s > NO_TICK_ALERT_S:
        problems.append("never received a price tick")

    # Tonight's outage: executor enabled, arbs flowing, nothing ever fired.
    # These two are deliberately independent, not chained: a dead private WS is
    # only ONE of the reasons attempts can be zero, and chaining them meant the
    # "a filter is blocking everything" signal could never fire while the WS was
    # down — two silent-failure detectors sharing one slot.
    if executor.config.enabled and uptime_s > NOT_READY_ALERT_S:
        ws = getattr(executor, "_private_ws", None)
        if ws is None or not ws.is_ready:
            problems.append("executor enabled but private WS not ready — cannot trade")
        if _arb4_count >= ARB4_WITHOUT_ATTEMPT_ALERT and executor.attempt_count == 0:
            problems.append(
                f"{_arb4_count} arbs at 4%+ but zero execution attempts — a filter is blocking everything"
            )

    if csv_writer.pending() > CSV_BACKLOG_ALERT:
        problems.append(f"CSV writer backlog {csv_writer.pending()} batches — disk or writer thread stuck")

    if _stuck_positions_count() > 0:
        problems.append(f"{_stuck_positions_count()} position(s) left open by failed taker exits")

    return problems


def _stuck_positions_count() -> int:
    try:
        return len(getattr(executor, "_stuck_positions", []) or [])
    except Exception:
        return 0


def _publish_sns(subject: str, body: str) -> None:
    """Blocking SNS publish — always call via asyncio.to_thread."""
    global _sns_client  # noqa: PLW0603
    import boto3

    if _sns_client is None:
        _sns_client = boto3.client("sns", region_name=ALERT_SNS_REGION)
    _sns_client.publish(TopicArn=ALERT_SNS_TOPIC_ARN, Subject=subject[:100], Message=body)


async def _send_email_alert(problems: list[str]) -> None:
    """
    Email the operator about an unhealthy scanner, de-duplicated.

    This box runs unattended for months, so a failure that only lands in a log
    file is effectively invisible — that is how a dead scanner went unnoticed
    from May to September. Repeats of the same problem are suppressed for an
    hour so an ongoing outage doesn't become an inbox flood.
    """
    if not ALERT_SNS_TOPIC_ARN:
        return
    key = " | ".join(sorted(problems))
    now = time.monotonic()
    last = _alerts_sent.get(key)
    if last is not None and (now - last) < ALERT_REPEAT_SUPPRESS_S:
        return
    _alerts_sent[key] = now

    body = (
        "arb-scanner is running but not working correctly.\n\n"
        + "\n".join(f"  - {p}" for p in problems)
        + f"\n\nhost: 98.82.172.44 (i-0923ce83c9a4b7047)"
        + f"\nrss: {_rss_mb():.0f}MB"
        + f"\nkalshi confirmed: {len(_kalshi_ws_confirmed)}/{len(kalshi_by_ticker)}"
        + f"\npoly confirmed: {len(_poly_ws_confirmed)}/{len(poly_by_token)}"
        + f"\narbs at 4%+ this run: {_arb4_count}"
        + f"\nexecution attempts this run: {executor.attempt_count}"
        + "\n\nlogs: ssh ubuntu@98.82.172.44 'tail -100 ~/prediction-arb/scanner.log'"
    )
    try:
        await asyncio.to_thread(_publish_sns, f"arb-scanner ALERT: {problems[0][:80]}", body)
        log.info("alert email sent (%d problem(s))", len(problems))
    except Exception as exc:
        log.error("SNS alert publish FAILED: %s", exc)


async def _notify(url: str, payload: Optional[dict] = None) -> None:
    """Fire-and-forget alert. Never raises into the caller."""
    if not url:
        return
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if payload is None:
                await session.get(url)
            else:
                await session.post(url, json=payload)
    except Exception as exc:
        log.warning("alert delivery failed (%s): %s", url.split("/")[2] if "//" in url else url, exc)


# ── GC pause instrumentation ─────────────────────────────────────────────────
# A forced gen-2 collection measured 36-54ms on this box (2026-09-19) — the same
# order as the observed event-loop lag. That is suggestive, NOT proof, so record
# real pauses here and report them next to the lag figure. If gc max tracks loop
# lag max, GC is the cause and `gc.freeze()` after warmup is the fix; if it does
# not, look elsewhere (synchronous log writes, discovery, to_thread hops).
# Python 3.14 defaults to thresholds (2000, 10, 0) with an incremental collector,
# so pauses may appear as many small ones rather than one long stop.
_gc_pause_max_ms = 0.0
_gc_pause_total_ms = 0.0
_gc_collections = 0
_gc_t0 = 0.0
_gc_frozen = False
_gc_disabled_at = 0.0   # monotonic time GC was suspended for discovery (0 = not suspended)
GC_DISABLED_MAX_S = 120.0


def _gc_callback(phase: str, info: dict) -> None:
    global _gc_t0, _gc_pause_max_ms, _gc_pause_total_ms, _gc_collections  # noqa: PLW0603
    if phase == "start":
        _gc_t0 = time.perf_counter()
        return
    if not _gc_t0:
        return
    pause_ms = (time.perf_counter() - _gc_t0) * 1000
    _gc_t0 = 0.0
    _gc_collections += 1
    _gc_pause_total_ms += pause_ms
    if pause_ms > _gc_pause_max_ms:
        _gc_pause_max_ms = pause_ms


gc.callbacks.append(_gc_callback)


# ── Event loop lag watchdog ──────────────────────────────────────────────────
# The whole system is one asyncio loop: while any coroutine runs, every other
# tick waits. Lag is the gap between when a sleep should have woken and when it
# did — i.e. how long the loop was busy or blocked. Reported in the heartbeat so
# queuing shows up as a number instead of as mysteriously missed fills.
LOOP_LAG_SAMPLE_S = 0.25
_loop_lag_max_ms = 0.0


async def _loop_lag_monitor() -> None:
    global _loop_lag_max_ms  # noqa: PLW0603
    loop = asyncio.get_running_loop()
    while True:
        before = loop.time()
        await asyncio.sleep(LOOP_LAG_SAMPLE_S)
        lag_ms = (loop.time() - before - LOOP_LAG_SAMPLE_S) * 1000
        if lag_ms > _loop_lag_max_ms:
            _loop_lag_max_ms = lag_ms


async def _housekeeping_loop() -> None:
    """
    Periodic work that used to run inline on the WS tick path.

    `convergence_tracker.flush_expired()` writes CSV, so calling it from
    `_on_poly_us_price` put synchronous file I/O on the hot path. Trackers
    expire on a 60s horizon, so a 5s cadence here loses nothing.
    """
    while True:
        await asyncio.sleep(HOUSEKEEPING_INTERVAL)
        try:
            convergence_tracker.flush_expired(datetime.now(timezone.utc))
        except Exception:
            log.exception("housekeeping: convergence flush failed")

        # Safety net: discovery disables the cyclic collector and re-enables it
        # on every exit path, but an unexpected exception could escape them all
        # and leave it off forever — which would let cyclic garbage grow without
        # bound on a box that already has an unresolved leak. Discovery takes
        # well under 30s, so anything past 120s means something went wrong.
        if _gc_disabled_at and (time.monotonic() - _gc_disabled_at) > GC_DISABLED_MAX_S:
            gc.enable()
            globals()["_gc_disabled_at"] = 0.0
            log.error("GC was left disabled for >%.0fs — re-enabled by watchdog", GC_DISABLED_MAX_S)

        # Freeze the steady-state heap out of GC scanning, once, after warmup.
        # Measured 2026-09-19: startup/discovery triggers gen-2 collections with
        # a 755-817ms max pause — long enough to sleep through entire arb
        # windows (median arb life is 85ms). gc.freeze() moves everything
        # currently live into the permanent generation so later collections stop
        # walking it. Steady-state lag (~42ms) is NOT GC — zero collections were
        # recorded in a quiet window — so this targets the discovery stall only.
        # COST: frozen objects are never collected again, so the heap cannot
        # shrink below this point. It does not shrink in practice anyway, but if
        # RSS growth accelerates after this change, suspect it first.
        global _gc_frozen  # noqa: PLW0603
        if not _gc_frozen and (datetime.now(timezone.utc) - _process_start).total_seconds() > 120:
            gc.collect()
            gc.freeze()
            _gc_frozen = True
            log.info("GC: froze %d objects out of future collections", gc.get_freeze_count())


async def _heartbeat_loop() -> None:
    """Log a status line every 5 minutes."""
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        now = datetime.now(timezone.utc)
        if _last_tick is None:
            tick_str = "no ticks yet"
        else:
            secs = (now - _last_tick).total_seconds()
            tick_str = f"last tick {secs:.0f}s ago"
        warmup = "warming up" if (_ready_after and now < _ready_after) else "live"
        k_conf = len(_kalshi_ws_confirmed)
        p_conf = len(_poly_ws_confirmed)
        k_total = len(kalshi_by_ticker)
        p_total = len(poly_by_token)
        log.info(
            "heartbeat — %s | K: %d/%d confirmed | P: %d/%d confirmed | "
            "3%%: %d active | 4%%: %d active | %s | rss %.0fMB tasks %d | loop lag max %.1fms | gc %d cols max %.1fms tot %.0fms | csvq %d",
            warmup,
            k_conf, k_total,
            p_conf, p_total,
            tracker_3.active_count,
            tracker_4.active_count,
            tick_str,
            _rss_mb(),
            len(asyncio.all_tasks()),
            _loop_lag_max_ms,
            _gc_collections,
            _gc_pause_max_ms,
            _gc_pause_total_ms,
            csv_writer.pending(),
        )
        globals()["_loop_lag_max_ms"] = 0.0   # peak is per-heartbeat-window
        globals()["_gc_pause_max_ms"] = 0.0
        globals()["_gc_pause_total_ms"] = 0.0
        globals()["_gc_collections"] = 0
        problems = _health_problems(now)
        if problems:
            for problem in problems:
                log.error("HEALTH ALERT: %s", problem)
            await _send_email_alert(problems)
            await _notify(ALERT_WEBHOOK_URL, {
                "text": "arb-scanner unhealthy: " + "; ".join(problems),
                "host": "98.82.172.44",
                "rss_mb": round(_rss_mb()),
            })
        else:
            await _notify(ALERT_HEARTBEAT_URL)   # dead man's switch

        if k_conf < k_total or p_conf < p_total:
            unconf_k = [t for t in kalshi_by_ticker if t not in _kalshi_ws_confirmed]
            unconf_p = [t for t in poly_by_token if t not in _poly_ws_confirmed]
            if unconf_k:
                log.warning("Kalshi unconfirmed (%d): %s", len(unconf_k),
                            ", ".join(kalshi_by_ticker[t].team for t in unconf_k[:5]))
            if unconf_p:
                log.warning("Poly unconfirmed (%d): %s", len(unconf_p),
                            ", ".join(f"{poly_by_token[t].event_slug}" for t in unconf_p[:5]))


# ── Entry point ───────────────────────────────────────────────────────────────

def _install_sigterm_handler() -> None:
    """
    Drain queued CSV rows on SIGTERM.

    systemd stop/restart sends SIGTERM, which does NOT raise KeyboardInterrupt,
    so the shutdown path above never runs for it. Since rows are now written by
    a daemon thread, an unhandled SIGTERM would discard whatever is still queued
    — every restart, including the daily 10:00 UTC recycle.
    """
    def _handler(signum, frame):  # noqa: ARG001
        log.info("SIGTERM received — draining CSV queue (%d batches)", csv_writer.pending())
        convergence_tracker.flush_all()
        csv_writer.flush(5.0)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handler)


async def run() -> None:
    print(f"Arb scanner starting — 3% threshold → {LOG_FILE_3}  |  4% threshold → {LOG_FILE_4}")
    print()
    _install_sigterm_handler()
    async with aiohttp.ClientSession() as session:
        asyncio.create_task(_heartbeat_loop(), name="heartbeat")
        asyncio.create_task(_housekeeping_loop(), name="housekeeping")
        asyncio.create_task(_loop_lag_monitor(), name="loop-lag")
        await _discovery_loop(session)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        now = datetime.now(timezone.utc)
        closed_3 = tracker_3.force_close_all(now)
        closed_4 = tracker_4.force_close_all(now)
        total = len(closed_3) + len(closed_4)
        if total > 0:
            print(f"\nFlushing {len(closed_3)} open arb(s) [3%] and {len(closed_4)} [4%] on exit...")
            for t in closed_3:
                log_arb_duration(t, "CLOSE", LOG_FILE_3)
            for t in closed_4:
                log_arb_duration(t, "CLOSE", LOG_FILE_4)
        conv_flushed = convergence_tracker.flush_all()
        if conv_flushed:
            print(f"Flushed {conv_flushed} convergence tracker(s).")
        if not csv_writer.flush(5.0):
            print("WARNING: CSV writer did not drain in 5s — some rows may be lost.")
        print("Stopped.")
        sys.exit(0)
