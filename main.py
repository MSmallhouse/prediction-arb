"""
MLB/NBA/NHL prediction market arb scanner — WebSocket edition.

REST: hourly market discovery only.
WebSocket: live price updates from Kalshi and Polymarket US.
Output: arb_durations.csv with OPEN/CLOSE events and duration_seconds.

No trading — data collection and duration research only.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

import config
from config import DISCOVERY_INTERVAL, MIN_GROSS_SPREAD
from scrapers.kalshi import discover_mlb_events, discover_nba_events, discover_nhl_events, fetch_all_prices, KalshiMarket
from scrapers.polymarket import PolymarketMarket, kalshi_ticker_to_poly_slug
from scrapers.polymarket_us import PolymarketUSMarket, discover_all_sports
from scrapers.polymarket_us_ws import PolymarketUSWSClient
from scrapers.kalshi_ws import KalshiWSClient
from arb_detector import find_arbs
from arb_tracker import ArbTracker, log_arb_duration

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ── Live price stores ─────────────────────────────────────────────────────────
kalshi_by_ticker: dict[str, KalshiMarket] = {}    # market_ticker → KalshiMarket
poly_by_token: dict[str, PolymarketMarket] = {}   # synthetic_token → PolymarketMarket

# Mapping from WS slug to the two PolymarketMarket token keys it updates
_poly_slug_to_tokens: dict[str, tuple[str, str]] = {}  # market_slug → (long_token, short_token)

# ── Arb trackers ──────────────────────────────────────────────────────────────
THRESHOLD_3 = 0.03
THRESHOLD_4 = 0.04
LOG_FILE_3 = Path("arb_durations_3.csv")
LOG_FILE_4 = Path("arb_durations_4.csv")
tracker_3 = ArbTracker()
tracker_4 = ArbTracker()

poly_ws: PolymarketUSWSClient | None = None
kalshi_ws: KalshiWSClient | None = None
_ready_after: datetime | None = None
_last_tick: datetime | None = None
_last_logged_spread: float = -999.0

# WS confirmation: only trust prices that WS has updated at least once.
_poly_ws_confirmed: set[str] = set()    # synthetic tokens confirmed by WS
_kalshi_ws_confirmed: set[str] = set()  # market_tickers confirmed by WS

WARMUP_SECONDS = 30
HEARTBEAT_INTERVAL = 300
_warmup_logged = False


# ── Arb check ─────────────────────────────────────────────────────────────────

async def _check_arbs() -> None:
    """Run on every WS price tick. Detects arb opens/closes and logs them."""
    now = datetime.now(timezone.utc)

    if _ready_after is None or now < _ready_after:
        return

    kalshi_live = [m for m in kalshi_by_ticker.values() if m.market_ticker in _kalshi_ws_confirmed]
    poly_live = [m for m in poly_by_token.values() if m.token_id in _poly_ws_confirmed]

    global _warmup_logged  # noqa: PLW0603
    if not _warmup_logged:
        _warmup_logged = True
        log.info(
            "Post-warmup: K %d/%d confirmed, P %d/%d confirmed — "
            "unconfirmed markets excluded from arb detection",
            len(kalshi_live), len(kalshi_by_ticker),
            len(poly_live), len(poly_by_token),
        )

    arbs_3 = find_arbs(kalshi_live, poly_live)
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

    for t in new_3:
        log_arb_duration(t, "OPEN", LOG_FILE_3)
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
    await _check_arbs()


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

    short_market = poly_by_token.get(short_token)
    if short_market is not None:
        _poly_ws_confirmed.add(short_token)
        short_market.yes_ask = short_ask
        short_market.yes_bid = short_bid
        short_market.yes_ask_size = short_ask_size
        short_market.fetched_at = now

    _last_tick = now
    await _check_arbs()


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


def _populate_stores(
    kalshi_markets: list[KalshiMarket],
    poly_markets: list[PolymarketMarket],
) -> None:
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


# ── Discovery loop ────────────────────────────────────────────────────────────

async def _discovery_loop(session: aiohttp.ClientSession) -> None:
    """
    Hourly REST discovery. On first run, populates stores and launches WS clients.
    On subsequent runs, subscribes new markets to existing WS connections.
    """
    global poly_ws, kalshi_ws, _ready_after

    # Initialize polymarket.us SDK client (sync, used for REST discovery)
    from polymarket_us import PolymarketUS
    poly_us_client = PolymarketUS(
        key_id=os.environ.get("POLYMARKET_API_KEY_ID", ""),
        secret_key=os.environ.get("POLYMARKET_PRIVATE_KEY", ""),
    )

    while True:
        log.info("Running market discovery...")
        try:
            mlb_tickers, nba_tickers, nhl_tickers = await asyncio.gather(
                discover_mlb_events(session),
                discover_nba_events(session),
                discover_nhl_events(session),
            )
        except Exception as exc:
            log.error("Discovery failed: %s — retrying in 5 minutes", exc)
            await asyncio.sleep(300)
            continue

        event_tickers = mlb_tickers + nba_tickers + nhl_tickers
        if not event_tickers:
            log.warning("No open markets. Retrying in 5 minutes.")
            await asyncio.sleep(300)
            continue

        log.info("Discovered %d MLB + %d NBA + %d NHL Kalshi events",
                 len(mlb_tickers), len(nba_tickers), len(nhl_tickers))

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
        except Exception as exc:
            log.error("Price fetch failed: %s — retrying in 5 minutes", exc)
            await asyncio.sleep(300)
            continue

        # Convert polymarket.us markets to PolymarketMarket objects (2 per game)
        poly_markets = _us_markets_to_poly_markets(us_markets)

        _populate_stores(kalshi_markets, poly_markets)
        log.info(
            "Stores: %d Kalshi markets, %d Poly markets (%d games)",
            len(kalshi_by_ticker), len(poly_by_token), len(us_markets),
        )

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

            # Kalshi WS
            if kalshi_api_key and kalshi_private_key:
                kalshi_ws = KalshiWSClient(
                    api_key_id=kalshi_api_key,
                    private_key_pem=kalshi_private_key,
                    on_price_update=_on_kalshi_price,
                )
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
            "3%%: %d active | 4%%: %d active | %s",
            warmup,
            k_conf, k_total,
            p_conf, p_total,
            tracker_3.active_count,
            tracker_4.active_count,
            tick_str,
        )
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

async def run() -> None:
    print(f"Arb scanner starting — 3% threshold → {LOG_FILE_3}  |  4% threshold → {LOG_FILE_4}")
    print()
    async with aiohttp.ClientSession() as session:
        asyncio.create_task(_heartbeat_loop(), name="heartbeat")
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
        print("Stopped.")
        sys.exit(0)
