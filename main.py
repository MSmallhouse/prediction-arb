"""
MLB/NBA prediction market arb scanner — WebSocket edition.

REST: hourly market discovery only.
WebSocket: live price updates from Kalshi and Polymarket.
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
from scrapers.polymarket import discover_and_fetch, PolymarketMarket
from scrapers.polymarket_ws import PolymarketWSClient
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

# ── Live price stores ─────────────────────────────────────────────────────────
# Populated by REST discovery; yes_ask updated in-place by WS callbacks.
kalshi_by_ticker: dict[str, KalshiMarket] = {}    # market_ticker → KalshiMarket
poly_by_token: dict[str, PolymarketMarket] = {}   # token_id → PolymarketMarket

# ── Arb trackers ──────────────────────────────────────────────────────────────
THRESHOLD_3 = 0.03
THRESHOLD_4 = 0.04
LOG_FILE_3 = Path("arb_durations_3.csv")
LOG_FILE_4 = Path("arb_durations_4.csv")
tracker_3 = ArbTracker()   # tracks ≥ 3% arbs → arb_durations_3.csv
tracker_4 = ArbTracker()   # tracks ≥ 4% arbs → arb_durations_4.csv

poly_ws: PolymarketWSClient | None = None
kalshi_ws: KalshiWSClient | None = None
_ready_after: datetime | None = None  # don't log arbs until prices have settled
_last_tick: datetime | None = None    # timestamp of most recent price update
_last_logged_spread: float = -999.0   # suppress repeated identical best-spread logs

WARMUP_SECONDS = 30
HEARTBEAT_INTERVAL = 300  # 5 minutes


# ── Arb check ─────────────────────────────────────────────────────────────────

async def _check_arbs() -> None:
    """Run on every WS price tick. Detects arb opens/closes and logs them."""
    now = datetime.now(timezone.utc)

    # Suppress logging during warmup — Gamma prices in store haven't all been
    # replaced by real CLOB prices yet, causing false arbs on startup.
    if _ready_after is None or now < _ready_after:
        return

    # Single find_arbs call at 3% threshold (config.MIN_GROSS_SPREAD = 0.03)
    arbs_3 = find_arbs(list(kalshi_by_ticker.values()), list(poly_by_token.values()))
    arbs_4 = [o for o in arbs_3 if o.gross_spread >= THRESHOLD_4]

    # Log best spread only when it changes — avoids flooding log with identical lines
    global _last_logged_spread  # noqa: PLW0603
    orig = config.MIN_GROSS_SPREAD
    config.MIN_GROSS_SPREAD = -1.0
    all_opps = find_arbs(list(kalshi_by_ticker.values()), list(poly_by_token.values()))
    config.MIN_GROSS_SPREAD = orig
    if all_opps:
        best = max(all_opps, key=lambda o: o.gross_spread)
        if abs(best.gross_spread - _last_logged_spread) >= 0.001:
            log.info("best spread: %s  gross=%.1f%%  (threshold %.0f%%)",
                     best.game_label, best.gross_spread * 100, MIN_GROSS_SPREAD * 100)
            _last_logged_spread = best.gross_spread
    elif _last_logged_spread != 0.0:
        log.info("best spread: all negative (markets efficiently priced)")
        _last_logged_spread = 0.0

    new_4, closed_4 = tracker_4.update(arbs_4, now)
    new_3, closed_3 = tracker_3.update(arbs_3, now)

    if not new_4 and not closed_4 and not new_3 and not closed_3:
        return

    ts = now.strftime("%H:%M:%S.") + f"{now.microsecond // 1000:03d}"

    # Console: print 4%+ arbs only (avoids duplicate lines for arbs that hit both thresholds)
    for t in new_4:
        opp = t.opportunity
        print(
            f"[{ts}] ARB OPENED  {opp.game_label:<28} "
            f"gross={opp.gross_spread:.1%}  net={opp.net_pretax:.2%}  "
            f"K:{opp.kalshi_market.team}@{opp.kalshi_market.yes_ask:.3f}  "
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

    # 3% tracker: CSV only (no console output — would duplicate 4% arb lines)
    for t in new_3:
        log_arb_duration(t, "OPEN", LOG_FILE_3)
    for t in closed_3:
        log_arb_duration(t, "CLOSE", LOG_FILE_3)


# ── WS price callbacks ────────────────────────────────────────────────────────

async def _on_kalshi_price(market_ticker: str, new_ask: float, new_bid: float, new_no_ask: float, new_no_bid: float) -> None:
    global _last_tick
    market = kalshi_by_ticker.get(market_ticker)
    if market is None:
        log.debug("Kalshi tick for unknown ticker %s — ignoring", market_ticker)
        return
    market.yes_ask = new_ask
    if new_bid > 0:
        market.yes_bid = new_bid
    if new_no_ask > 0:
        market.no_ask = new_no_ask
    if new_no_bid > 0:
        market.no_bid = new_no_bid
    market.fetched_at = datetime.now(timezone.utc)
    _last_tick = market.fetched_at
    await _check_arbs()


async def _on_poly_price(token_id: str, new_ask: float, new_bid: float) -> None:
    global _last_tick
    market = poly_by_token.get(token_id)
    if market is None:
        log.debug("Poly tick for unknown token %s... — ignoring", token_id[:16])
        return
    market.yes_ask = new_ask
    if new_bid > 0:
        market.yes_bid = new_bid
    market.fetched_at = datetime.now(timezone.utc)
    _last_tick = market.fetched_at
    await _check_arbs()


# ── Price store population ────────────────────────────────────────────────────

def _populate_stores(
    kalshi_markets: list[KalshiMarket],
    poly_markets: list[PolymarketMarket],
) -> None:
    """
    Merge REST discovery results into price stores.
    Preserve live WS prices for markets already in store — prevents a stale
    REST ask from overwriting a price that the WS has already updated.
    """
    for m in kalshi_markets:
        existing = kalshi_by_ticker.get(m.market_ticker)
        if existing is not None:
            m.yes_ask = existing.yes_ask
            m.yes_bid = existing.yes_bid
        kalshi_by_ticker[m.market_ticker] = m

    for m in poly_markets:
        existing = poly_by_token.get(m.token_id)
        if existing is not None:
            m.yes_ask = existing.yes_ask
        poly_by_token[m.token_id] = m


# ── Discovery loop ────────────────────────────────────────────────────────────

async def _discovery_loop(session: aiohttp.ClientSession) -> None:
    """
    Hourly REST discovery. On first run, populates stores and launches WS clients.
    On subsequent runs, subscribes new markets to existing WS connections.
    """
    global poly_ws, kalshi_ws, _ready_after

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

        log.info("Discovered %d MLB + %d NBA + %d NHL events", len(mlb_tickers), len(nba_tickers), len(nhl_tickers))

        try:
            kalshi_markets, poly_markets = await asyncio.gather(
                fetch_all_prices(session, event_tickers),
                discover_and_fetch(session, event_tickers),
            )
        except Exception as exc:
            log.error("Price fetch failed: %s — retrying in 5 minutes", exc)
            await asyncio.sleep(300)
            continue

        _populate_stores(kalshi_markets, poly_markets)
        log.info(
            "Stores: %d Kalshi markets, %d Poly markets",
            len(kalshi_by_ticker), len(poly_by_token),
        )

        if poly_ws is None:
            # First discovery: construct and launch WS clients.
            # Stores are fully populated before create_task yields, so no tick
            # can arrive for an unknown ticker during startup.
            api_key = os.environ.get("KALSHI_API_KEY_ID", "")
            private_key = os.environ.get("KALSHI_PRIVATE_KEY", "")
            if not api_key or not private_key:
                log.error("Missing KALSHI_API_KEY_ID or KALSHI_PRIVATE_KEY in .env — Kalshi WS disabled")

            poly_ws = PolymarketWSClient(on_price_update=_on_poly_price)
            asyncio.create_task(
                poly_ws.start(initial_token_ids=list(poly_by_token.keys())),
                name="polymarket-ws",
            )
            log.info("Polymarket WS task launched (%d tokens)", len(poly_by_token))

            if api_key and private_key:
                kalshi_ws = KalshiWSClient(
                    api_key_id=api_key,
                    private_key_pem=private_key,
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
            # Subsequent discovery: subscribe any new markets dynamically.
            tasks = [poly_ws.subscribe(list(poly_by_token.keys()))]
            if kalshi_ws is not None:
                tasks.append(kalshi_ws.subscribe(list(kalshi_by_ticker.keys())))
            await asyncio.gather(*tasks)

        await asyncio.sleep(DISCOVERY_INTERVAL.total_seconds())


# ── Heartbeat ────────────────────────────────────────────────────────────────

async def _heartbeat_loop() -> None:
    """Log a status line every 5 minutes so scanner.log shows the system is alive."""
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        now = datetime.now(timezone.utc)
        if _last_tick is None:
            tick_str = "no ticks yet"
        else:
            secs = (now - _last_tick).total_seconds()
            tick_str = f"last tick {secs:.0f}s ago"
        warmup = "warming up" if (_ready_after and now < _ready_after) else "live"
        log.info(
            "heartbeat — %s | %d K / %d P markets | 3%%: %d active | 4%%: %d active | %s",
            warmup,
            len(kalshi_by_ticker),
            len(poly_by_token),
            tracker_3.active_count,
            tracker_4.active_count,
            tick_str,
        )


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
        # Flush any open arbs on exit
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
