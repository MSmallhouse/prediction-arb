"""
Cross-market arbitrage detector.

Pairs Kalshi and Polymarket markets for the same game, computes gross spread,
fees, and after-tax net profit. Logs opportunities above MIN_GROSS_SPREAD.
"""

import csv
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from config import (
    KALSHI_FEE_COEFF,
    POLY_SPORTS_FEE_COEFF,
    AFTER_TAX_MULTIPLIER,
    MIN_GROSS_SPREAD,
    LOG_GROSS_THRESHOLD,
)
from scrapers.kalshi import KalshiMarket
from scrapers.polymarket import PolymarketMarket, kalshi_ticker_to_poly_slug

log = logging.getLogger(__name__)

LOG_FILE = Path("arb_opportunities.csv")
_FIELDNAMES = [
    "timestamp", "sport", "game_datetime", "home_team", "away_team",
    "kalshi_team", "kalshi_ask",
    "poly_team", "poly_ask",
    "gross_spread", "kalshi_fee", "poly_fee", "total_fees",
    "net_pretax", "net_aftertax", "profitable",
    "kalshi_ticker", "poly_slug",
    "kalshi_open_interest", "kalshi_volume_24h", "poly_liquidity",
]


def kalshi_fee(p: float) -> float:
    """Kalshi taker fee per contract: 0.07 * P * (1 - P)"""
    return KALSHI_FEE_COEFF * p * (1 - p)


def poly_fee(p: float) -> float:
    """Polymarket sports taker fee per share: 0.03 * P * (1 - P), peaks 0.75% at P=0.5"""
    return POLY_SPORTS_FEE_COEFF * p * (1 - p)


@dataclass
class ArbOpportunity:
    game_datetime: datetime
    away_team: str
    home_team: str
    kalshi_market: KalshiMarket      # market for the team we want to win (for OI/vol/key)
    poly_market: PolymarketMarket
    gross_spread: float
    kalshi_fee: float
    poly_fee: float
    net_pretax: float
    net_aftertax: float
    kalshi_side: str                 # "YES" or "NO" — which side of which Kalshi market to buy
    kalshi_effective_ask: float      # actual price used (min of YES ask or opposing NO ask)
    kalshi_order_ticker: str         # market_ticker to place the Kalshi order on

    @property
    def game_label(self) -> str:
        return f"{self.away_team} @ {self.home_team}"

    def __str__(self) -> str:
        return (
            f"{self.game_label:<35} "
            f"Kalshi {self.kalshi_market.team} {self.kalshi_side}={self.kalshi_effective_ask:.3f}  "
            f"Poly {self.poly_market.team} YES={self.poly_market.yes_ask:.3f}  "
            f"spread={self.gross_spread:.1%}  "
            f"net_after_tax={self.net_aftertax:.4f}/contract"
        )


def find_arbs(
    kalshi_markets: list[KalshiMarket],
    poly_markets: list[PolymarketMarket],
) -> list[ArbOpportunity]:
    """
    Match Kalshi and Polymarket markets for the same game, then check arb.

    Matching strategy: derive Polymarket slug from Kalshi event_ticker
    (same derivation used during discovery), then look up by (slug, team).
    This avoids UTC date collisions where two same-team games fall on the
    same UTC calendar date (e.g. 7:40 PM ET game and 2:20 PM CT next-day game
    both appear as date 2026-04-23 UTC).

    Arb logic:
      Buy YES(Team A) on Kalshi at P1_ask
      Buy YES(Team B) on Polymarket at P2_ask
      One side always pays $1 → gross spread S = 1 - P1 - P2
      Profitable when S > total_fees (before tax)
    """
    # Index Polymarket markets by (event_slug, canonical_team)
    poly_by_key: dict[tuple[str, str], PolymarketMarket] = {}
    for pm in poly_markets:
        poly_by_key[(pm.event_slug, pm.team)] = pm

    opps: list[ArbOpportunity] = []

    # Group Kalshi markets by event
    kalshi_by_event: dict[str, list[KalshiMarket]] = {}
    for km in kalshi_markets:
        kalshi_by_event.setdefault(km.event_ticker, []).append(km)

    for event_ticker, k_markets in kalshi_by_event.items():
        if len(k_markets) != 2:
            continue

        # Derive the Polymarket slug for this specific Kalshi event
        poly_slug = kalshi_ticker_to_poly_slug(event_ticker)
        if poly_slug is None:
            continue

        team_a, team_b = k_markets[0], k_markets[1]

        # Arb: buy team_a YES on Kalshi + team_b YES on Polymarket
        #      buy team_b YES on Kalshi + team_a YES on Polymarket
        # For each pairing, also check if NO on the opposing Kalshi market is
        # cheaper than YES on the primary market — same payout, potentially lower cost.
        for k_market, opp_k_market, opposing_team in [
            (team_a, team_b, team_b.team),
            (team_b, team_a, team_a.team),
        ]:
            p_market = poly_by_key.get((poly_slug, opposing_team))
            if p_market is None:
                continue

            # Reject if game datetimes differ by more than 12h — guards against
            # a Kalshi ticker date-deriving the wrong Polymarket game in a series
            # (e.g. late-night ET game slug matching next-day UTC-dated Poly game).
            dt_diff = abs((k_market.game_datetime - p_market.game_datetime).total_seconds())
            if dt_diff > 12 * 3600:
                log.debug(
                    "Skipping %s/%s: datetime mismatch Kalshi=%s Poly=%s (diff %.1fh)",
                    k_market.team, p_market.team,
                    k_market.game_datetime.isoformat(), p_market.game_datetime.isoformat(),
                    dt_diff / 3600,
                )
                continue

            # Pick cheaper Kalshi exposure: YES on k_market vs NO on opp_k_market
            yes_ask = k_market.yes_ask
            no_ask  = opp_k_market.no_ask
            if 0 < no_ask < 1 and no_ask < yes_ask:
                p1             = no_ask
                kalshi_side    = "NO"
                kalshi_order_ticker = opp_k_market.market_ticker
            else:
                p1             = yes_ask
                kalshi_side    = "YES"
                kalshi_order_ticker = k_market.market_ticker

            p2 = p_market.yes_ask

            if p1 <= 0 or p2 <= 0 or p1 >= 1 or p2 >= 1:
                continue

            gross = 1.0 - p1 - p2
            fk = kalshi_fee(p1)
            fp = poly_fee(p2)
            net_pre = gross - fk - fp
            net_after = net_pre * AFTER_TAX_MULTIPLIER

            if gross < MIN_GROSS_SPREAD:
                continue

            opp = ArbOpportunity(
                game_datetime=k_market.game_datetime,
                away_team=p_market.event_slug.split("-")[1].upper(),
                home_team=p_market.event_slug.split("-")[2].upper(),
                kalshi_market=k_market,
                poly_market=p_market,
                gross_spread=gross,
                kalshi_fee=fk,
                poly_fee=fp,
                net_pretax=net_pre,
                net_aftertax=net_after,
                kalshi_side=kalshi_side,
                kalshi_effective_ask=p1,
                kalshi_order_ticker=kalshi_order_ticker,
            )
            opps.append(opp)

    # Sort by gross spread descending
    opps.sort(key=lambda o: o.gross_spread, reverse=True)
    return opps


def log_arbs(opps: list[ArbOpportunity]) -> int:
    """
    Append all opportunities >= LOG_GROSS_THRESHOLD to arb_opportunities.csv.
    Includes a `profitable` column (true when net_pretax > 0).
    Returns count of rows written.
    """
    loggable = [o for o in opps if o.gross_spread >= LOG_GROSS_THRESHOLD]
    if not loggable:
        return 0

    ts = datetime.now(timezone.utc).isoformat()
    write_header = not LOG_FILE.exists()

    with LOG_FILE.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_FIELDNAMES)
        if write_header:
            writer.writeheader()

        for opp in loggable:
            sport = "NBA" if opp.kalshi_market.event_ticker.startswith("KXNBA") else "MLB"
            profitable = opp.net_pretax > 0
            if profitable:
                log.info("ARB: %s", opp)
            writer.writerow({
                "timestamp": ts,
                "sport": sport,
                "game_datetime": opp.game_datetime.isoformat(),
                "home_team": opp.home_team,
                "away_team": opp.away_team,
                "kalshi_team": opp.kalshi_market.team,
                "kalshi_ask": f"{opp.kalshi_market.yes_ask:.4f}",
                "poly_team": opp.poly_market.team,
                "poly_ask": f"{opp.poly_market.yes_ask:.4f}",
                "gross_spread": f"{opp.gross_spread:.4f}",
                "kalshi_fee": f"{opp.kalshi_fee:.4f}",
                "poly_fee": f"{opp.poly_fee:.4f}",
                "total_fees": f"{opp.kalshi_fee + opp.poly_fee:.4f}",
                "net_pretax": f"{opp.net_pretax:.4f}",
                "net_aftertax": f"{opp.net_aftertax:.4f}",
                "profitable": profitable,
                "kalshi_ticker": opp.kalshi_market.market_ticker,
                "poly_slug": opp.poly_market.event_slug,
                "kalshi_open_interest": f"{opp.kalshi_market.open_interest:.0f}",
                "kalshi_volume_24h": f"{opp.kalshi_market.volume_24h:.0f}",
                "poly_liquidity": f"{opp.poly_market.liquidity:.0f}",
            })

    return len(loggable)

