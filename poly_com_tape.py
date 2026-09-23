#!/usr/bin/env python3
"""
Record a price tape from polymarket.COM for games we track on polymarket.US.

Research tool, not part of the scanner. It exists to answer one question:
**does the deep .com book lead the thin .us book we actually trade, and by how
much?** If it does, .com is a better proxy than Kalshi — same contract, same
outcome definitions, no cross-exchange basis, and no dependence on the `opener`
field whose meaning is currently in doubt (docs/open-questions.md).

Why standalone, and why time-boxed:

  - It touches NOTHING in the scanner. No import of main.py, no shared state,
    no deploy, no restart — so it cannot reset the CFB or memory-leak windows.
  - .com is far busier than .us: measured 2026-09-23 from the VPS, ~7.2 events
    per second per game. Across a full 80-game slate that is ~580/s, which is
    real CPU on a burstable t3.micro whose baseline is 10% per vCPU. Hence
    --max-games, and hence a bounded --minutes rather than a daemon.
  - The box has 908MB RAM and memguard now trips the scanner at 900MB anon.
    A second long-lived process changes that headroom math. Run it under
    `systemd-run --scope -p MemoryMax=...` and let it exit on its own.

polymarket.com requires a browser User-Agent on every REST call — without one
every request is 403, which is what made the legacy scraper look geo-blocked.
It is not: verified reachable from AWS us-east-1.

Read-only. This places no orders and needs no credentials.

Usage (on the VPS):
  python3 poly_com_tape.py --minutes 180 --max-games 12 --out /tmp/poly_com_tape.csv
"""

import argparse
import asyncio
import csv
import json
import logging
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scrapers.polymarket import _kalshi_abbr_to_poly  # noqa: E402

log = logging.getLogger("poly_com_tape")

GAMMA = "https://gamma-api.polymarket.com"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
UA = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
}

FIELDS = [
    "ts_utc",          # OUR receive time, ISO8601 with ms
    "exch_ts",         # EXCHANGE timestamp (ms epoch) where the event carries one.
                       # Prefer this for lead/lag: it removes our own network
                       # latency from the measurement, which is precisely the
                       # confound that makes the Kalshi `opener` field untrustworthy
                       # (docs/open-questions.md).
    "recv_monotonic",  # monotonic seconds since start, for ordering within this tape
    "slug",
    "sport",
    "token_id",
    "outcome",         # team name this token pays out on
    "event_type",      # book | price_change | last_trade_price
    "best_bid",
    "best_ask",
    "bid_size",
    "ask_size",
]


def _get(url: str, timeout: float = 25.0):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _games_from_durations(path: Path, hours_back: float) -> list[tuple[str, str, str]]:
    """
    (game_label, game_date_utc, sport) for games the scanner has recently tracked.

    Using our own arb log rather than a schedule API means we collect exactly the
    games we trade, and nothing else — which is what keeps --max-games meaningful.
    """
    if not path.exists():
        log.error("no arb duration log at %s", path)
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).isoformat()
    # Keep the LAST time each game was seen, so we can prefer games that are live
    # now. Ordering matters more than it looks: --max-games fills from the front,
    # and a naive file-order pass fills it with finished games from yesterday that
    # emit no ticks at all (observed on the first run — 3 games, 0 rows).
    last_seen: dict[tuple[str, str, str], str] = {}
    with path.open() as f:
        for r in csv.DictReader(f):
            fs = r.get("first_seen", "")
            if fs < cutoff:
                continue
            if r.get("sport") not in ("MLB", "NHL", "NBA"):
                continue  # CFB slugs are not derivable — they use a name-pair join
            gd = (r.get("game_datetime") or "")[:10]
            if not gd or not r.get("game"):
                continue
            key = (r["game"], gd, r["sport"])
            if fs > last_seen.get(key, ""):
                last_seen[key] = fs
    return [k for k, _ in sorted(last_seen.items(), key=lambda kv: kv[1], reverse=True)]


def _candidate_slugs(game: str, date_utc: str, sport: str) -> list[str]:
    """
    .com slug is {sport}-{away}-{home}-{YYYY-MM-DD} on the US LOCAL date, while
    game_datetime is UTC — a 7pm ET game is already the next day in UTC. Try the
    UTC date and the day before it; one of them is the local date.
    """
    try:
        away, home = (x.strip() for x in game.split("@"))
    except ValueError:
        return []
    s = sport.lower()
    ap, hp = _kalshi_abbr_to_poly(away, s), _kalshi_abbr_to_poly(home, s)
    if not ap or not hp:
        return []
    try:
        d = datetime.strptime(date_utc, "%Y-%m-%d").date()
    except ValueError:
        return []
    return [f"{s}-{ap}-{hp}-{d}", f"{s}-{ap}-{hp}-{d - timedelta(days=1)}"]


def _game_winner_market(event: dict) -> dict | None:
    """The moneyline market: exactly two outcomes, not a derived/prop market."""
    for m in event.get("markets", []):
        raw = m.get("outcomes")
        if not raw or not m.get("clobTokenIds"):
            continue
        try:
            outcomes = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if len(outcomes) == 2 and m.get("groupItemThreshold") == "0":
            return m
    return None


def resolve_tokens(games, max_games: int) -> dict[str, dict]:
    """token_id -> {slug, sport, outcome}. Skips games .com does not list."""
    tokens: dict[str, dict] = {}
    matched = 0
    for game, date_utc, sport in games:
        if matched >= max_games:
            break
        for slug in _candidate_slugs(game, date_utc, sport):
            try:
                events = _get(f"{GAMMA}/events?slug={slug}")
            except Exception as exc:
                log.warning("gamma lookup failed for %s: %s", slug, exc)
                continue
            if not events:
                continue
            mk = _game_winner_market(events[0])
            if not mk:
                continue
            try:
                ids = json.loads(mk["clobTokenIds"])
                outcomes = json.loads(mk["outcomes"])
            except (TypeError, ValueError, KeyError):
                continue
            for tid, outcome in zip(ids, outcomes):
                tokens[tid] = {"slug": slug, "sport": sport, "outcome": outcome}
            matched += 1
            log.info("matched %-13s %s -> %s", game, sport, slug)
            break
        else:
            log.info("no .com market for %-13s %s %s", game, sport, date_utc)
    log.info("resolved %d games -> %d tokens", matched, len(tokens))
    return tokens


def _quotes(event: dict):
    """
    Yield (token_id, best_bid, best_ask, bid_size, ask_size, exch_ts) per event.

    A `price_change` event has NO top-level asset_id. It carries a `price_changes`
    ARRAY, one entry per token, each with its own asset_id/best_bid/best_ask, plus
    a top-level exchange `timestamp`. The first version of this collector looked
    for a top-level id, found none, and silently dropped every price_change — 3
    hours of recording produced book snapshots only, a median quote age of 11s,
    and a dataset far too coarse to resolve the sub-second lead this exists to
    measure. It looked exactly like a quiet feed. Fails closed and quiet, again.
    """
    et = event.get("event_type")
    exch = event.get("timestamp") or ""

    if et == "price_change":
        for ch in event.get("price_changes") or []:
            tid = ch.get("asset_id")
            if not tid:
                continue
            bb, ba = ch.get("best_bid"), ch.get("best_ask")
            yield (
                tid,
                float(bb) if bb is not None else None,
                float(ba) if ba is not None else None,
                0.0,
                0.0,
                exch,
            )
        return

    tid = event.get("asset_id") or event.get("token_id")
    if not tid:
        return

    if et == "book":
        bids, asks = event.get("bids") or [], event.get("asks") or []
        bb = ba = None
        bs = as_ = 0.0
        if bids:
            top = max(bids, key=lambda x: float(x["price"]))
            bb, bs = float(top["price"]), float(top.get("size", 0) or 0)
        if asks:
            top = min(asks, key=lambda x: float(x["price"]))
            ba, as_ = float(top["price"]), float(top.get("size", 0) or 0)
        yield tid, bb, ba, bs, as_, exch
        return

    bb, ba = event.get("best_bid"), event.get("best_ask")
    if bb is None and ba is None:
        return
    yield (
        tid,
        float(bb) if bb is not None else None,
        float(ba) if ba is not None else None,
        0.0,
        0.0,
        exch,
    )


async def record(tokens: dict[str, dict], out: Path, minutes: float) -> None:
    from websockets.asyncio.client import connect

    deadline = time.monotonic() + minutes * 60
    start = time.monotonic()
    counts: Counter = Counter()
    rows = 0
    new_file = not out.exists()

    with out.open("a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()
            fh.flush()
        # Flush on a TIMER, not a row count. A row-count flush starves at low
        # event rates — a quiet slate wrote 0 bytes for minutes because nothing
        # reached the 2000-row mark, which reads from outside exactly like a
        # collector that is silently broken.
        last_flush = time.monotonic()
        last_report = time.monotonic()

        while time.monotonic() < deadline:
            try:
                async with connect(WS_URL, open_timeout=25, ping_interval=10) as ws:
                    await ws.send(
                        json.dumps({"assets_ids": list(tokens), "operation": "subscribe"})
                    )
                    log.info("subscribed to %d tokens", len(tokens))
                    while time.monotonic() < deadline:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        except asyncio.TimeoutError:
                            log.warning("30s with no message — reconnecting")
                            break
                        msgs = json.loads(raw)
                        if not isinstance(msgs, list):
                            msgs = [msgs]
                        now = datetime.now(timezone.utc)
                        mono = time.monotonic() - start
                        for ev in msgs:
                            if not isinstance(ev, dict):
                                continue
                            et = ev.get("event_type", "?")
                            counts[et] += 1
                            for tid, bb, ba, bs, as_, exch in _quotes(ev):
                                meta = tokens.get(tid)
                                if meta is None:
                                    continue
                                if bb is None and ba is None:
                                    continue
                                counts[f"{et}:kept"] += 1
                                writer.writerow({
                                "ts_utc": now.isoformat(timespec="milliseconds"),
                                "exch_ts": exch,
                                "recv_monotonic": f"{mono:.4f}",
                                "slug": meta["slug"],
                                "sport": meta["sport"],
                                "token_id": tid,
                                "outcome": meta["outcome"],
                                "event_type": et,
                                "best_bid": "" if bb is None else f"{bb:.4f}",
                                "best_ask": "" if ba is None else f"{ba:.4f}",
                                "bid_size": f"{bs:.2f}",
                                "ask_size": f"{as_:.2f}",
                                })
                                rows += 1
                        now_m = time.monotonic()
                        if now_m - last_flush >= 10.0:
                            fh.flush()
                            last_flush = now_m
                        if now_m - last_report >= 60.0:
                            log.info(
                                "%d rows | %.0f min left | events %s",
                                rows, (deadline - now_m) / 60, dict(counts),
                            )
                            last_report = now_m
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("websocket dropped (%s) — retrying in 5s", exc)
                await asyncio.sleep(5)
        fh.flush()

    log.info("done: %d rows, event types %s", rows, dict(counts))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--minutes", type=float, default=180.0, help="how long to record")
    ap.add_argument("--max-games", type=int, default=12, help="cap on games subscribed")
    ap.add_argument("--hours-back", type=float, default=12.0,
                    help="look this far back in the arb log for tracked games")
    ap.add_argument("--out", type=Path, default=Path("/tmp/poly_com_tape.csv"))
    ap.add_argument("--durations", type=Path,
                    default=Path(__file__).resolve().parent / "arb_durations_3.csv")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    games = _games_from_durations(args.durations, args.hours_back)
    log.info("scanner tracked %d distinct games in the last %.0fh", len(games), args.hours_back)
    if not games:
        log.error("nothing to record")
        return 1

    tokens = resolve_tokens(games, args.max_games)
    if not tokens:
        log.error("no .com markets resolved — nothing to record")
        return 1

    try:
        asyncio.run(record(tokens, args.out, args.minutes))
    except KeyboardInterrupt:
        log.info("interrupted — tape flushed to %s", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
