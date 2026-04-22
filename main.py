"""
MLB prediction market arb scraper — Phase 2.

Polls Kalshi (REST) and Polymarket (REST/CLOB) for MLB game winner prices,
matches markets, computes spreads, and logs arbitrage opportunities above
the MIN_GROSS_SPREAD threshold.

No trading — data collection and verification only.
"""

import asyncio
import logging
import sys
from datetime import datetime, timezone

import aiohttp

from config import DISCOVERY_INTERVAL, PRICE_POLL_INTERVAL, MIN_GROSS_SPREAD
from scrapers.kalshi import discover_mlb_events, discover_nba_events, fetch_all_prices, KalshiMarket
from scrapers.polymarket import discover_and_fetch, refresh_clob_prices, PolymarketMarket
from arb_detector import find_arbs, log_arbs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def _print_kalshi_table(markets: list[KalshiMarket]) -> None:
    if not markets:
        print("  (no Kalshi markets)\n")
        return

    by_event: dict[str, list[KalshiMarket]] = {}
    for m in markets:
        by_event.setdefault(m.event_ticker, []).append(m)

    header = f"  {'Game':<30} {'Team':<18} {'ask':>6} {'bid':>6}  {'vol_24h':>8} {'OI':>8}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    for event_ticker, ms in sorted(by_event.items(), key=lambda kv: kv[1][0].game_datetime):
        game_dt = ms[0].game_datetime.astimezone()
        date_str = game_dt.strftime("%b %d %I:%M%p").lstrip("0")
        for i, m in enumerate(ms):
            label = f"  {date_str}" if i == 0 else ""
            print(f"{label:<14}  {m.team:<18} {m.yes_ask:>6.3f} {m.yes_bid:>6.3f}  {m.volume_24h:>8.0f} {m.open_interest:>8.0f}")
        print()


def _print_arb_table(
    kalshi_markets: list[KalshiMarket],
    poly_markets: list[PolymarketMarket],
) -> None:
    opps = find_arbs(kalshi_markets, poly_markets)
    profitable = [o for o in opps if o.net_pretax > 0]

    print(f"\n  Matched {len(opps)} potential arbs, {len(profitable)} profitable (net pre-tax > 0)")
    print(f"  Showing all with gross spread >= {MIN_GROSS_SPREAD:.0%}:\n")

    if not opps:
        print("  (none)\n")
        return

    header = f"  {'Game':<28} {'K-team':<14} {'K-ask':>6}  {'P-team':<14} {'P-ask':>6}  {'gross':>7}  {'net_pre':>8}  {'net_tax':>8}  {'K-OI':>7}  {'P-liq':>7}"
    print(header)
    print("  " + "-" * (len(header) - 2))

    for opp in opps:
        game_dt = opp.game_datetime.astimezone()
        date_str = game_dt.strftime("%b %d %I:%M%p").lstrip("0")
        profitable_marker = " ✓" if opp.net_pretax > 0 else "  "
        print(
            f"  {date_str:<12} {opp.game_datetime.strftime('%b%d'):<6}  "  # padding
            f"{opp.kalshi_market.team:<14} {opp.kalshi_market.yes_ask:>6.3f}  "
            f"{opp.poly_market.team:<14} {opp.poly_market.yes_ask:>6.3f}  "
            f"{opp.gross_spread:>7.1%}  "
            f"{opp.net_pretax:>8.4f}  "
            f"{opp.net_aftertax:>8.4f}  "
            f"{opp.kalshi_market.open_interest:>7.0f}  "
            f"{opp.poly_market.liquidity:>7.0f}"
            f"{profitable_marker}"
        )

    print()
    n_logged = log_arbs(opps)
    if n_logged:
        print(f"  Logged {n_logged} rows to arb_opportunities.csv ({len(profitable)} profitable)")


async def run() -> None:
    now = datetime.now(timezone.utc)
    next_discovery = now
    next_price_poll = now
    event_tickers: list[str] = []
    kalshi_markets: list[KalshiMarket] = []
    poly_markets: list[PolymarketMarket] = []

    async with aiohttp.ClientSession() as session:
        while True:
            now = datetime.now(timezone.utc)

            # ── Market discovery ────────────────────────────────────────────
            if now >= next_discovery:
                log.info("Running market discovery...")
                mlb_tickers, nba_tickers = await asyncio.gather(
                    discover_mlb_events(session),
                    discover_nba_events(session),
                )
                event_tickers = mlb_tickers + nba_tickers
                next_discovery = now + DISCOVERY_INTERVAL

                if not event_tickers:
                    log.warning("No open markets. Retrying discovery in 5 minutes.")
                    next_discovery = now + (DISCOVERY_INTERVAL / 12)
                else:
                    log.info("Discovered %d MLB + %d NBA events", len(mlb_tickers), len(nba_tickers))

            # ── Price poll ──────────────────────────────────────────────────
            if now >= next_price_poll and event_tickers:
                log.info("Fetching prices from Kalshi and Polymarket...")

                kalshi_markets, poly_markets = await asyncio.gather(
                    fetch_all_prices(session, event_tickers),
                    discover_and_fetch(session, event_tickers),
                )
                next_price_poll = now + PRICE_POLL_INTERVAL

                # CLOB refresh: Gamma outcomePrices are displayed probabilities,
                # not live ask prices. Pre-screen with Gamma, then fetch real
                # CLOB ask prices for any candidate that passes the spread filter.
                gamma_candidates = find_arbs(kalshi_markets, poly_markets)
                if gamma_candidates:
                    candidate_poly = list(
                        {o.poly_market.token_id: o.poly_market
                         for o in gamma_candidates}.values()
                    )
                    await refresh_clob_prices(session, candidate_poly)
                    log.info(
                        "CLOB-refreshed %d Poly markets for %d Gamma candidates",
                        len(candidate_poly), len(gamma_candidates),
                    )

                mlb_k = [m for m in kalshi_markets if m.event_ticker.startswith("KXMLB")]
                nba_k = [m for m in kalshi_markets if m.event_ticker.startswith("KXNBA")]
                mlb_p = [m for m in poly_markets if m.event_slug.startswith("mlb-")]
                nba_p = [m for m in poly_markets if m.event_slug.startswith("nba-")]

                ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                print(f"\n{'='*80}")
                print(f"  MLB + NBA Arb Scanner  —  {ts}")
                print(f"{'='*80}")

                print(f"\n── Kalshi MLB ({len(mlb_k)} markets) ──")
                _print_kalshi_table(mlb_k)
                print(f"\n── Kalshi NBA ({len(nba_k)} markets) ──")
                _print_kalshi_table(nba_k)

                for sport_label, sport_poly in [("MLB", mlb_p), ("NBA", nba_p)]:
                    print(f"\n── Polymarket {sport_label} ({len(sport_poly)} markets) ──")
                    if sport_poly:
                        for pm in sorted(sport_poly, key=lambda m: m.game_datetime)[:8]:
                            print(f"  {pm.team:<18} ask={pm.yes_ask:.3f}  liq=${pm.liquidity:>8.0f}  game={pm.game_datetime.strftime('%b %d %H:%M UTC')}")
                        if len(sport_poly) > 8:
                            print(f"  ... and {len(sport_poly) - 8} more")
                    else:
                        print("  (none matched)")

                print(f"\n── Arb Opportunities (gross >= {MIN_GROSS_SPREAD:.0%}) ──")
                _print_arb_table(kalshi_markets, poly_markets)

            # Sleep until next action
            now = datetime.now(timezone.utc)
            next_wake = min(next_discovery, next_price_poll)
            sleep_secs = max(0.5, (next_wake - now).total_seconds())
            await asyncio.sleep(sleep_secs)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)
