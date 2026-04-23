"""
Arb duration tracker.

Detects when arbitrage opportunities open and close by diffing consecutive
find_arbs() results. Logs OPEN and CLOSE events with timestamps to arb_durations.csv.

Primary research output: duration_seconds column measures how long each arb window stays open.
"""

import csv
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from arb_detector import ArbOpportunity

log = logging.getLogger(__name__)

DURATION_LOG_FILE = Path("arb_durations.csv")
_FIELDNAMES = [
    "event",                 # OPEN or CLOSE
    "game",
    "sport",
    "gross_spread",
    "net_pretax",
    "first_seen",            # ISO-8601 UTC, set on OPEN
    "closed_at",             # ISO-8601 UTC, set on CLOSE (empty on OPEN row)
    "duration_seconds",      # float, set on CLOSE (empty on OPEN row)
    "peak_gross",            # max gross_spread seen while open (set on CLOSE)
    "opener",                # which platform repriced first: "kalshi" or "poly"
    "minutes_to_first_pitch", # minutes until game start at first_seen (negative = in-game)
    "kalshi_team",
    "poly_team",
    "kalshi_ask",            # YES ask on Kalshi
    "kalshi_bid",            # YES bid on Kalshi
    "poly_ask",              # YES ask on Polymarket
    "poly_bid",              # YES bid on Polymarket (0 if not yet received from WS)
    "kalshi_oi",             # Kalshi open interest at first_seen
    "kalshi_vol_24h",        # Kalshi 24h volume at first_seen
    "poly_liquidity",        # Polymarket liquidity (USDC) at first_seen
    "game_datetime",         # scheduled game start (ISO-8601 UTC)
    "verified",              # REST verification result: "real", "phantom", or "" (not checked)
    "rest_kalshi_ask",       # REST-verified Kalshi ask (empty if not checked)
    "rest_poly_ask",         # REST-verified Poly ask (empty if not checked)
    "rest_gross",            # gross spread from REST prices (empty if not checked)
]

# Unique key per directional arb: (event_slug, kalshi_market_ticker, poly_token_id)
ArbKey = tuple[str, str, str]


def _arb_key(opp: ArbOpportunity) -> ArbKey:
    return (
        opp.poly_market.event_slug,
        opp.kalshi_market.market_ticker,
        opp.poly_market.token_id,
    )


def _sport(opp: ArbOpportunity) -> str:
    ticker = opp.kalshi_market.event_ticker
    if ticker.startswith("KXNBA"):
        return "NBA"
    if ticker.startswith("KXNHL"):
        return "NHL"
    return "MLB"


@dataclass
class TrackedArb:
    opportunity: ArbOpportunity  # snapshot of prices at first_seen
    first_seen: datetime
    last_seen: datetime
    peak_gross: float = field(init=False)
    opener: str = field(init=False)  # "kalshi" or "poly" — which platform repriced first
    verified: str = ""               # "real", "phantom", or "" (set by main.py after REST check)
    rest_kalshi_ask: float = 0.0
    rest_poly_ask: float = 0.0
    rest_gross: float = 0.0

    def __post_init__(self) -> None:
        self.peak_gross = self.opportunity.gross_spread
        k_ts = self.opportunity.kalshi_price_ts
        p_ts = self.opportunity.poly_market.fetched_at
        self.opener = "kalshi" if k_ts >= p_ts else "poly"

    def update(self, opp: ArbOpportunity, now: datetime) -> None:
        self.last_seen = now
        if opp.gross_spread > self.peak_gross:
            self.peak_gross = opp.gross_spread

    @property
    def duration_seconds(self) -> float:
        return (self.last_seen - self.first_seen).total_seconds()


class ArbTracker:
    def __init__(self) -> None:
        self._active: dict[ArbKey, TrackedArb] = {}

    def update(
        self,
        current_arbs: list[ArbOpportunity],
        now: Optional[datetime] = None,
    ) -> tuple[list[TrackedArb], list[TrackedArb]]:
        """
        Diff current arb list against active state.

        Returns:
            new_arbs:    TrackedArbs that just opened (log as OPEN)
            closed_arbs: TrackedArbs that just closed (log as CLOSE)
        """
        if now is None:
            now = datetime.now(timezone.utc)

        current = {_arb_key(opp): opp for opp in current_arbs}

        new_arbs: list[TrackedArb] = []
        for key, opp in current.items():
            if key not in self._active:
                tracked = TrackedArb(opportunity=opp, first_seen=now, last_seen=now)
                self._active[key] = tracked
                new_arbs.append(tracked)
            else:
                self._active[key].update(opp, now)

        closed_arbs: list[TrackedArb] = []
        for key in list(self._active):
            if key not in current:
                closed_arbs.append(self._active.pop(key))

        return new_arbs, closed_arbs

    def force_close_all(self, now: Optional[datetime] = None) -> list[TrackedArb]:
        """Flush all active arbs as closed. Call on clean shutdown only."""
        if now is None:
            now = datetime.now(timezone.utc)
        closed = list(self._active.values())
        for t in closed:
            t.last_seen = now
        self._active.clear()
        return closed

    @property
    def active_count(self) -> int:
        return len(self._active)


def log_arb_duration(tracked: TrackedArb, event: str, filepath: Path = DURATION_LOG_FILE) -> None:
    """
    Append one row to filepath (defaults to arb_durations.csv).
    event: "OPEN" or "CLOSE"
    """
    opp = tracked.opportunity
    write_header = not filepath.exists()
    with filepath.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_FIELDNAMES)
        if write_header:
            writer.writeheader()
        mins_to_pitch = (opp.poly_market.game_datetime - tracked.first_seen).total_seconds() / 60
        writer.writerow({
            "event": event,
            "game": opp.game_label,
            "sport": _sport(opp),
            "gross_spread": f"{opp.gross_spread:.4f}",
            "net_pretax": f"{opp.net_pretax:.4f}",
            "first_seen": tracked.first_seen.isoformat(),
            "closed_at": tracked.last_seen.isoformat() if event == "CLOSE" else "",
            "duration_seconds": f"{tracked.duration_seconds:.3f}" if event == "CLOSE" else "",
            "peak_gross": f"{tracked.peak_gross:.4f}" if event == "CLOSE" else "",
            "opener": tracked.opener,
            "minutes_to_first_pitch": f"{mins_to_pitch:.1f}",
            "kalshi_team": opp.kalshi_market.team,
            "poly_team": opp.poly_market.team,
            "kalshi_ask": f"{opp.kalshi_ask:.4f}",
            "kalshi_bid": f"{opp.kalshi_market.yes_bid:.4f}",
            "poly_ask": f"{opp.poly_market.yes_ask:.4f}",
            "poly_bid": f"{opp.poly_market.yes_bid:.4f}",
            "kalshi_oi": f"{opp.kalshi_market.open_interest:.0f}",
            "kalshi_vol_24h": f"{opp.kalshi_market.volume_24h:.0f}",
            "poly_liquidity": f"{opp.poly_market.liquidity:.0f}",
            "game_datetime": opp.game_datetime.isoformat(),
            "verified": tracked.verified,
            "rest_kalshi_ask": f"{tracked.rest_kalshi_ask:.4f}" if tracked.verified else "",
            "rest_poly_ask": f"{tracked.rest_poly_ask:.4f}" if tracked.verified else "",
            "rest_gross": f"{tracked.rest_gross:.4f}" if tracked.verified else "",
        })
