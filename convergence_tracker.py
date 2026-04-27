"""
Convergence tracker for Strategy B analysis.

When an arb opens, tracks the Poly price on every subsequent WS tick
for TRACKING_DURATION seconds. Logs to convergence_log.csv.

This answers: after Kalshi reprices (opener), does Poly converge?
How fast? What's the bid (exit price) at each point?

Usage: call start_tracking() when arb opens, call on_poly_tick() on
every Poly WS update. Expired trackers auto-flush to CSV.
"""

import csv
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

CONVERGENCE_LOG = Path("convergence_log.csv")
TRACKING_DURATION = 60.0  # seconds to track after arb opens

_FIELDNAMES = [
    "arb_id",           # first_seen ISO timestamp (links to arb_durations CSVs)
    "game",
    "sport",
    "opener",           # "kalshi" or "poly"
    "arb_gross",        # gross spread at detection
    "kalshi_ask",       # Kalshi price at arb detection (fixed reference)
    "kalshi_side",      # YES or NO
    "poly_team",        # which Poly team we'd buy
    "t_offset_ms",      # milliseconds since arb detection
    "poly_ask",         # Poly ask at this tick
    "poly_bid",         # Poly bid at this tick (our exit price)
    "poly_depth",       # contracts at best ask
    "source",           # "poly" or "kalshi" — which WS fired this tick
]


@dataclass
class _ActiveTracker:
    arb_id: str              # first_seen ISO string
    game: str
    sport: str
    opener: str
    arb_gross: float
    kalshi_ask: float        # fixed at detection time
    kalshi_side: str
    poly_team: str
    poly_token_id: str       # which poly token to watch
    market_slug: str         # which WS slug to watch
    start_time: datetime
    rows: list = field(default_factory=list)


# Active trackers keyed by poly_token_id for fast lookup on Poly ticks
_active: dict[str, _ActiveTracker] = {}

# Also index by market_slug (one slug can have two tokens — long and short)
_active_by_slug: dict[str, list[str]] = {}  # slug → [token_ids]


def start_tracking(
    arb_id: str,
    game: str,
    sport: str,
    opener: str,
    arb_gross: float,
    kalshi_ask: float,
    kalshi_side: str,
    poly_team: str,
    poly_token_id: str,
    market_slug: str,
    poly_ask: float,
    poly_bid: float,
    poly_depth: float,
    now: datetime,
) -> None:
    """Start tracking convergence for a new arb. Called on arb OPEN."""
    # Don't double-track the same arb
    if poly_token_id in _active:
        return

    tracker = _ActiveTracker(
        arb_id=arb_id,
        game=game,
        sport=sport,
        opener=opener,
        arb_gross=arb_gross,
        kalshi_ask=kalshi_ask,
        kalshi_side=kalshi_side,
        poly_team=poly_team,
        poly_token_id=poly_token_id,
        market_slug=market_slug,
        start_time=now,
    )

    # Log initial state at t=0
    tracker.rows.append({
        "t_offset_ms": 0,
        "poly_ask": poly_ask,
        "poly_bid": poly_bid,
        "poly_depth": poly_depth,
        "source": "initial",
    })

    _active[poly_token_id] = tracker
    _active_by_slug.setdefault(market_slug, []).append(poly_token_id)


def on_poly_tick(
    market_slug: str,
    long_ask: float,
    long_bid: float,
    short_ask: float,
    short_bid: float,
    long_ask_size: float,
    short_ask_size: float,
    now: datetime,
) -> None:
    """Called on every Poly WS tick. Logs data for any active trackers on this slug."""
    token_ids = _active_by_slug.get(market_slug)
    if not token_ids:
        return

    for token_id in list(token_ids):
        tracker = _active.get(token_id)
        if tracker is None:
            continue

        elapsed_ms = (now - tracker.start_time).total_seconds() * 1000

        # Determine which side this tracker cares about
        if token_id.endswith(":long"):
            poly_ask = long_ask
            poly_bid = long_bid
            poly_depth = long_ask_size
        else:
            poly_ask = short_ask
            poly_bid = short_bid
            poly_depth = short_ask_size

        tracker.rows.append({
            "t_offset_ms": round(elapsed_ms),
            "poly_ask": poly_ask,
            "poly_bid": poly_bid,
            "poly_depth": poly_depth,
            "source": "poly",
        })


def on_kalshi_tick(
    poly_token_id: str,
    kalshi_ask: float,
    now: datetime,
) -> None:
    """Called on Kalshi tick for a market with an active tracker.
    Logs the Kalshi price so we can see if Kalshi reverted."""
    tracker = _active.get(poly_token_id)
    if tracker is None:
        return

    elapsed_ms = (now - tracker.start_time).total_seconds() * 1000

    # Get current Poly prices from the last row
    last = tracker.rows[-1] if tracker.rows else {}

    tracker.rows.append({
        "t_offset_ms": round(elapsed_ms),
        "poly_ask": last.get("poly_ask", 0),
        "poly_bid": last.get("poly_bid", 0),
        "poly_depth": last.get("poly_depth", 0),
        "source": "kalshi",
    })
    # Update the reference kalshi_ask so we can see if it moved
    tracker.kalshi_ask = kalshi_ask


def flush_expired(now: datetime) -> int:
    """Flush trackers that have exceeded TRACKING_DURATION. Returns count flushed."""
    expired = []
    for token_id, tracker in _active.items():
        elapsed = (now - tracker.start_time).total_seconds()
        if elapsed >= TRACKING_DURATION:
            expired.append(token_id)

    for token_id in expired:
        _flush_one(token_id)

    return len(expired)


def flush_all() -> int:
    """Flush all active trackers (shutdown)."""
    count = len(_active)
    for token_id in list(_active):
        _flush_one(token_id)
    return count


def _flush_one(token_id: str) -> None:
    """Write tracker rows to CSV and clean up."""
    tracker = _active.pop(token_id, None)
    if tracker is None:
        return

    # Clean up slug index
    slug_list = _active_by_slug.get(tracker.market_slug, [])
    if token_id in slug_list:
        slug_list.remove(token_id)
    if not slug_list:
        _active_by_slug.pop(tracker.market_slug, None)

    if not tracker.rows:
        return

    write_header = not CONVERGENCE_LOG.exists()
    with CONVERGENCE_LOG.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_FIELDNAMES)
        if write_header:
            writer.writeheader()

        sport = "MLB" if "mlb" in tracker.game.lower() or "mlb" in tracker.arb_id else ""
        if not sport:
            sport = tracker.sport

        for row in tracker.rows:
            writer.writerow({
                "arb_id": tracker.arb_id,
                "game": tracker.game,
                "sport": sport,
                "opener": tracker.opener,
                "arb_gross": f"{tracker.arb_gross:.4f}",
                "kalshi_ask": f"{tracker.kalshi_ask:.4f}",
                "kalshi_side": tracker.kalshi_side,
                "poly_team": tracker.poly_team,
                "t_offset_ms": row["t_offset_ms"],
                "poly_ask": f"{row['poly_ask']:.4f}",
                "poly_bid": f"{row['poly_bid']:.4f}",
                "poly_depth": f"{row['poly_depth']:.0f}",
                "source": row["source"],
            })

    log.debug(
        "Convergence: flushed %d ticks for %s (%s)",
        len(tracker.rows), tracker.game, tracker.arb_id[:19],
    )


@property
def active_count() -> int:
    return len(_active)
