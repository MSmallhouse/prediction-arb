"""
Cross-market arbitrage detector.

Pairs Kalshi and Polymarket markets for the same game, computes gross spread,
fees, and after-tax net profit. Logs opportunities above MIN_GROSS_SPREAD.

Kalshi side: YES only. Each game has 2 Kalshi markets (one per team), each
with a YES order book. We buy YES on Team A (Kalshi) + YES on Team B (Poly).
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from config import (
    KALSHI_FEE_COEFF,
    POLY_SPORTS_FEE_COEFF,
    AFTER_TAX_MULTIPLIER,
    MIN_GROSS_SPREAD,
)
from scrapers.kalshi import KalshiMarket
from scrapers.polymarket import PolymarketMarket, kalshi_ticker_to_poly_slug

log = logging.getLogger(__name__)


def kalshi_fee(p: float) -> float:
    """Kalshi taker fee per contract: 0.07 * P * (1 - P)"""
    return KALSHI_FEE_COEFF * p * (1 - p)


def poly_fee(p: float) -> float:
    """Polymarket sports taker fee per share: 0.03 * P * (1 - P), peaks 0.75% at P=0.5"""
    return POLY_SPORTS_FEE_COEFF * p * (1 - p)


@dataclass
class ArbOpportunity:
    game_datetime: datetime          # actual game start from Polymarket (not Kalshi expiry)
    away_team: str
    home_team: str
    kalshi_market: KalshiMarket      # Kalshi market we buy YES on
    poly_market: PolymarketMarket    # Poly market we buy YES on (opposing team)
    gross_spread: float
    kalshi_fee: float
    poly_fee: float
    net_pretax: float
    net_aftertax: float
    kalshi_ask: float                # Kalshi YES ask price used
    kalshi_price_ts: datetime        # fetched_at of the Kalshi market

    @property
    def game_label(self) -> str:
        return f"{self.away_team} @ {self.home_team}"

    def __str__(self) -> str:
        return (
            f"{self.game_label:<35} "
            f"Kalshi {self.kalshi_market.team} YES={self.kalshi_ask:.3f}  "
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

        poly_slug = kalshi_ticker_to_poly_slug(event_ticker)
        if poly_slug is None:
            continue

        team_a, team_b = k_markets[0], k_markets[1]

        # Two arb directions:
        #   Buy team_a YES on Kalshi + team_b YES on Polymarket
        #   Buy team_b YES on Kalshi + team_a YES on Polymarket
        for k_market, opposing_team in [
            (team_a, team_b.team),
            (team_b, team_a.team),
        ]:
            p_market = poly_by_key.get((poly_slug, opposing_team))
            if p_market is None:
                continue

            # Reject if game datetimes differ by more than 12h
            dt_diff = abs((k_market.game_datetime - p_market.game_datetime).total_seconds())
            if dt_diff > 12 * 3600:
                log.debug(
                    "Skipping %s/%s: datetime mismatch Kalshi=%s Poly=%s (diff %.1fh)",
                    k_market.team, p_market.team,
                    k_market.game_datetime.isoformat(), p_market.game_datetime.isoformat(),
                    dt_diff / 3600,
                )
                continue

            p1 = k_market.yes_ask
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
                game_datetime=p_market.game_datetime,
                away_team=p_market.event_slug.split("-")[1].upper(),
                home_team=p_market.event_slug.split("-")[2].upper(),
                kalshi_market=k_market,
                poly_market=p_market,
                gross_spread=gross,
                kalshi_fee=fk,
                poly_fee=fp,
                net_pretax=net_pre,
                net_aftertax=net_after,
                kalshi_ask=p1,
                kalshi_price_ts=k_market.fetched_at,
            )
            opps.append(opp)

    opps.sort(key=lambda o: o.gross_spread, reverse=True)
    return opps
