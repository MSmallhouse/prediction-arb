"""
REST order book verification for detected arbs.

When the WS-driven arb detector finds an opportunity, this module hits both
platforms' REST APIs to confirm the prices are real and current — not stale
WS data. Only called for arbs above the verification threshold.
"""

import logging
from dataclasses import dataclass
from typing import Optional

import aiohttp

from config import KALSHI_BASE_URL

log = logging.getLogger(__name__)

CLOB_BASE = "https://clob.polymarket.com"


@dataclass
class VerificationResult:
    ws_kalshi_price: float       # price from WS (what triggered the arb)
    ws_poly_price: float
    rest_kalshi_price: float     # price from REST verification
    rest_poly_price: float
    ws_gross: float              # gross spread from WS prices
    rest_gross: float            # gross spread from REST prices
    confirmed: bool              # REST gross still above threshold

    @property
    def price_diff_kalshi(self) -> float:
        return self.rest_kalshi_price - self.ws_kalshi_price

    @property
    def price_diff_poly(self) -> float:
        return self.rest_poly_price - self.ws_poly_price


async def _fetch_kalshi_market(
    session: aiohttp.ClientSession, market_ticker: str
) -> Optional[dict]:
    """Fetch a single Kalshi market by ticker."""
    try:
        async with session.get(
            f"{KALSHI_BASE_URL}/markets/{market_ticker}"
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("market", data)
            log.warning("Kalshi /markets/%s returned %d", market_ticker, resp.status)
    except Exception as exc:
        log.error("Kalshi market fetch failed for %s: %s", market_ticker, exc)
    return None


async def _fetch_poly_price(
    session: aiohttp.ClientSession, token_id: str
) -> Optional[float]:
    """Fetch current Polymarket CLOB ask price."""
    try:
        async with session.get(
            f"{CLOB_BASE}/price",
            params={"token_id": token_id, "side": "sell"},
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return float(data.get("price", 0))
            log.warning("Poly CLOB /price returned %d", resp.status)
    except Exception as exc:
        log.error("Poly CLOB price fetch failed: %s", exc)
    return None


async def verify_arb(
    session: aiohttp.ClientSession,
    opp,  # ArbOpportunity — not imported to avoid circular dep
    min_gross: float = 0.03,
) -> Optional[VerificationResult]:
    """
    Hit both REST APIs to confirm an arb is real.

    Returns VerificationResult, or None if REST fetch failed.
    """
    import asyncio

    # Fetch both prices concurrently
    kalshi_task = _fetch_kalshi_market(session, opp.kalshi_market.market_ticker)
    poly_task = _fetch_poly_price(session, opp.poly_market.token_id)
    kalshi_data, poly_price = await asyncio.gather(kalshi_task, poly_task)

    if kalshi_data is None or poly_price is None:
        log.warning("Verification failed — REST fetch returned None")
        return None

    rest_kalshi = float(kalshi_data.get("yes_ask_dollars", 0))
    rest_poly = poly_price
    rest_gross = 1.0 - rest_kalshi - rest_poly
    ws_gross = opp.gross_spread

    confirmed = rest_gross >= min_gross and 0 < rest_kalshi < 1 and 0 < rest_poly < 1

    return VerificationResult(
        ws_kalshi_price=opp.kalshi_ask,
        ws_poly_price=opp.poly_market.yes_ask,
        rest_kalshi_price=rest_kalshi,
        rest_poly_price=rest_poly,
        ws_gross=ws_gross,
        rest_gross=rest_gross,
        confirmed=confirmed,
    )
