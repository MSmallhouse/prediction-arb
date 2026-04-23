"""
Cross-market arbitrage detector.

Pairs Kalshi and Polymarket markets for the same game, computes gross spread,
fees, and after-tax net profit. Logs opportunities above MIN_GROSS_SPREAD.

Kalshi has 4 order books per game: Team A YES/NO, Team B YES/NO.
Buying YES on Team A and buying NO on Team B have the same payout ($1 if A wins).
We pick whichever is cheaper. NO prices come from the orderbook_delta WS channel.
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
    kalshi_market: KalshiMarket      # k_market: reference market for arb key direction
    poly_market: PolymarketMarket    # Poly market we buy YES on (opposing team)
    gross_spread: float
    kalshi_fee: float
    poly_fee: float
    net_pretax: float
    net_aftertax: float
    kalshi_side: str                 # "YES" or "NO" — which Kalshi order book we buy
    kalshi_ask: float                # effective ask (YES ask or opposing NO ask, whichever cheaper)
    kalshi_order_market: KalshiMarket  # market we place the order on (opp_k for NO, k for YES)
    kalshi_price_ts: datetime        # fetched_at of the market whose price created this arb

    @property
    def game_label(self) -> str:
        return f"{self.away_team} @ {self.home_team}"

    def __str__(self) -> str:
        return (
            f"{self.game_label:<35} "
            f"Kalshi {self.kalshi_order_market.team} {self.kalshi_side}={self.kalshi_ask:.3f}  "
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

    Arb logic:
      To express "Team A wins" on Kalshi, pick cheaper of:
        - Buy YES on Team A market (yes_ask)
        - Buy NO on Team B market (no_ask) — same payout
      Then pair with "Team B wins" on Polymarket (YES ask).
      One side always pays $1 → gross spread S = 1 - P1 - P2
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

        # Two arb directions, each checking YES vs opposing NO:
        for k_market, opp_k_market, opposing_team in [
            (team_a, team_b, team_b.team),
            (team_b, team_a, team_a.team),
        ]:
            p_market = poly_by_key.get((poly_slug, opposing_team))
            if p_market is None:
                continue

            # Reject if game datetimes differ by more than 12h
            dt_diff = abs((k_market.game_datetime - p_market.game_datetime).total_seconds())
            if dt_diff > 12 * 3600:
                log.debug(
                    "Skipping %s/%s: datetime mismatch (diff %.1fh)",
                    k_market.team, p_market.team, dt_diff / 3600,
                )
                continue

            # Pick cheaper Kalshi exposure: YES on k_market vs NO on opp_k_market
            yes_ask = k_market.yes_ask
            no_ask = opp_k_market.no_ask
            if 0 < no_ask < 1 and no_ask < yes_ask:
                p1 = no_ask
                kalshi_side = "NO"
                kalshi_order_market = opp_k_market
                kalshi_price_ts = opp_k_market.fetched_at
            else:
                p1 = yes_ask
                kalshi_side = "YES"
                kalshi_order_market = k_market
                kalshi_price_ts = k_market.fetched_at

            p2 = p_market.yes_ask

            if p1 <= 0 or p2 <= 0 or p1 >= 1 or p2 >= 1:
                continue

            gross = 1.0 - p1 - p2
            fk = kalshi_fee(p1)
            fp = poly_fee(p2)
            net_pre = gross - fk - fp
            net_after = net_pre * AFTER_TAX_MULTIPLIER

            if gross < MIN_GROSS_SPREAD - 1e-9:
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
                kalshi_side=kalshi_side,
                kalshi_ask=p1,
                kalshi_order_market=kalshi_order_market,
                kalshi_price_ts=kalshi_price_ts,
            )
            opps.append(opp)

    opps.sort(key=lambda o: o.gross_spread, reverse=True)
    return opps
