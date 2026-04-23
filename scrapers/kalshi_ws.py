"""
Kalshi WebSocket client — orderbook_delta channel.

Maintains local order books per market from snapshots + deltas.
Fires callback with YES and NO best prices on every book change.

Auth required (RSA-PSS). Server sends WS Ping frames; websockets auto-responds.

Kalshi book model:
  yes_levels[price] = resting YES buy orders (bids) at that price
  no_levels[price]  = resting NO buy orders (bids) at that price

  yes_ask = 1 - max(no_levels)   (to buy YES, match with highest NO bidder)
  yes_bid = max(yes_levels)       (best resting YES bid)
  no_ask  = 1 - max(yes_levels)  (to buy NO, match with highest YES bidder)
  no_bid  = max(no_levels)        (best resting NO bid)

Seq gap detection: if delta.seq != expected, re-subscribe for fresh snapshot.
"""

import asyncio
import base64
import json
import logging
import time
from typing import Awaitable, Callable, Optional

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

log = logging.getLogger(__name__)

KALSHI_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
KALSHI_WS_PATH = "/trade-api/ws/v2"
MAX_BACKOFF = 60.0

_msg_id = 0


def _next_id() -> int:
    global _msg_id
    _msg_id += 1
    return _msg_id


def _build_auth_headers(api_key_id: str, private_key_pem: str) -> dict[str, str]:
    pem = private_key_pem.replace("\\n", "\n")
    ts_ms = str(int(time.time() * 1000))
    message = f"{ts_ms}GET{KALSHI_WS_PATH}".encode()

    private_key = serialization.load_pem_private_key(pem.encode(), password=None)
    sig_bytes = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig_bytes).decode(),
        "KALSHI-ACCESS-TIMESTAMP": ts_ms,
    }


class _MarketBook:
    """Local order book for one market, derived from snapshot + deltas."""

    __slots__ = ("yes_levels", "no_levels", "_yes_ask", "_yes_bid", "_no_ask", "_no_bid")

    def __init__(self) -> None:
        self.yes_levels: dict[float, float] = {}  # price → size (YES bids)
        self.no_levels: dict[float, float] = {}   # price → size (NO bids)
        self._yes_ask = 1.0
        self._yes_bid = 0.0
        self._no_ask = 1.0
        self._no_bid = 0.0

    def load_snapshot(self, yes_fp: list, no_fp: list) -> None:
        """Initialize from orderbook_snapshot arrays."""
        self.yes_levels.clear()
        self.no_levels.clear()
        for price_str, size_str in yes_fp:
            size = float(size_str)
            if size > 0:
                self.yes_levels[float(price_str)] = size
        for price_str, size_str in no_fp:
            size = float(size_str)
            if size > 0:
                self.no_levels[float(price_str)] = size
        self._recompute()

    def apply_delta(self, side: str, price: float, delta: float) -> bool:
        """
        Apply an orderbook_delta. Returns True if best prices changed.
        """
        old = (self._yes_ask, self._yes_bid, self._no_ask, self._no_bid)
        levels = self.yes_levels if side == "yes" else self.no_levels
        current = levels.get(price, 0.0)
        new_size = current + delta
        if new_size > 0.001:  # float tolerance
            levels[price] = new_size
        else:
            levels.pop(price, None)
        self._recompute()
        return (self._yes_ask, self._yes_bid, self._no_ask, self._no_bid) != old

    def _recompute(self) -> None:
        if self.no_levels:
            max_no = max(self.no_levels)
            self._yes_ask = round(1.0 - max_no, 4)
            self._no_bid = max_no
        else:
            self._yes_ask = 1.0
            self._no_bid = 0.0
        if self.yes_levels:
            max_yes = max(self.yes_levels)
            self._yes_bid = max_yes
            self._no_ask = round(1.0 - max_yes, 4)
        else:
            self._yes_bid = 0.0
            self._no_ask = 1.0

    @property
    def yes_ask(self) -> float:
        return self._yes_ask

    @property
    def yes_bid(self) -> float:
        return self._yes_bid

    @property
    def no_ask(self) -> float:
        return self._no_ask

    @property
    def no_bid(self) -> float:
        return self._no_bid

    @property
    def yes_ask_size(self) -> float:
        """Contracts available at best YES ask (= size at max NO bid level)."""
        if not self.no_levels:
            return 0.0
        return self.no_levels[max(self.no_levels)]

    @property
    def no_ask_size(self) -> float:
        """Contracts available at best NO ask (= size at max YES bid level)."""
        if not self.yes_levels:
            return 0.0
        return self.yes_levels[max(self.yes_levels)]


class KalshiWSClient:
    def __init__(
        self,
        api_key_id: str,
        private_key_pem: str,
        on_price_update: Callable[[str, float, float, float, float, float, float], Awaitable[None]],
    ) -> None:
        """
        on_price_update(market_ticker, yes_ask, yes_bid, no_ask, no_bid, yes_ask_size, no_ask_size)
        """
        self._api_key_id = api_key_id
        self._private_key_pem = private_key_pem
        self._on_price_update = on_price_update
        self._subscribed: set[str] = set()
        self._ws = None
        self._running = False
        self._backoff = 1.0

        # Local order books and seq tracking
        self._books: dict[str, _MarketBook] = {}    # market_ticker → book
        self._sid_tickers: dict[int, set[str]] = {} # sid → set of market_tickers
        self._sid_seq: dict[int, int] = {}           # sid → last seq seen
        self._ticker_sid: dict[str, int] = {}        # market_ticker → sid

    async def start(self, initial_market_tickers: list[str]) -> None:
        self._running = True
        self._subscribed.update(initial_market_tickers)
        while self._running:
            try:
                await self._connect_and_run()
                self._backoff = 1.0
            except Exception as exc:
                log.warning("Kalshi WS: %s — reconnect in %.1fs", exc, self._backoff)
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, MAX_BACKOFF)

    async def subscribe(self, market_tickers: list[str]) -> None:
        new = [t for t in market_tickers if t not in self._subscribed]
        if not new:
            return
        self._subscribed.update(new)
        if self._ws is not None:
            try:
                await self._send_subscribe(self._ws, new)
            except ConnectionClosed:
                log.warning("Kalshi WS: dynamic subscribe failed (disconnected); will resubscribe on reconnect")
                return
            log.info("Kalshi WS: dynamically subscribed %d new tickers", len(new))

    async def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            await self._ws.close()

    async def _connect_and_run(self) -> None:
        # Clear book state on reconnect — fresh snapshots incoming
        self._books.clear()
        self._sid_tickers.clear()
        self._sid_seq.clear()
        self._ticker_sid.clear()

        headers = _build_auth_headers(self._api_key_id, self._private_key_pem)
        async with connect(KALSHI_WS_URL, additional_headers=headers) as ws:
            self._ws = ws
            log.info("Kalshi WS: connected (%d tickers)", len(self._subscribed))
            if self._subscribed:
                await self._send_subscribe(ws, list(self._subscribed))
            await self._read_loop(ws)

    async def _send_subscribe(self, ws, market_tickers: list[str]) -> None:
        msg = {
            "id": _next_id(),
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": market_tickers,
            },
        }
        await ws.send(json.dumps(msg))

    async def _request_snapshot(self, ws, market_tickers: list[str]) -> None:
        """Request fresh snapshot after a seq gap."""
        msg = {
            "id": _next_id(),
            "cmd": "update_subscription",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": market_tickers,
                "action": "get_snapshot",
            },
        }
        await ws.send(json.dumps(msg))

    async def _read_loop(self, ws) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            await self._dispatch(msg, ws)

    async def _dispatch(self, msg: dict, ws) -> None:
        msg_type = msg.get("type")

        if msg_type == "orderbook_snapshot":
            await self._handle_snapshot(msg)

        elif msg_type == "orderbook_delta":
            await self._handle_delta(msg, ws)

        # Ignore: "subscribed", "unsubscribed", "error", etc.

    async def _handle_snapshot(self, msg: dict) -> None:
        sid = msg.get("sid", 0)
        seq = msg.get("seq", 0)
        data = msg.get("msg", {})
        ticker = data.get("market_ticker")
        if not ticker:
            return

        book = _MarketBook()
        book.load_snapshot(
            data.get("yes_dollars_fp", []),
            data.get("no_dollars_fp", []),
        )
        self._books[ticker] = book

        # Track sid → ticker mapping and seq
        if sid not in self._sid_tickers:
            self._sid_tickers[sid] = set()
        self._sid_tickers[sid].add(ticker)
        self._ticker_sid[ticker] = sid
        self._sid_seq[sid] = seq

        log.debug(
            "Kalshi book snapshot %s: yes_ask=%.3f yes_bid=%.3f no_ask=%.3f no_bid=%.3f (%d yes levels, %d no levels)",
            ticker, book.yes_ask, book.yes_bid, book.no_ask, book.no_bid,
            len(book.yes_levels), len(book.no_levels),
        )

        await self._on_price_update(
            ticker, book.yes_ask, book.yes_bid, book.no_ask, book.no_bid,
            book.yes_ask_size, book.no_ask_size,
        )

    async def _handle_delta(self, msg: dict, ws) -> None:
        sid = msg.get("sid", 0)
        seq = msg.get("seq", 0)
        data = msg.get("msg", {})
        ticker = data.get("market_ticker")
        if not ticker:
            return

        # Seq gap detection
        expected = self._sid_seq.get(sid, 0) + 1
        if seq != expected and expected > 1:
            gap_tickers = list(self._sid_tickers.get(sid, {ticker}))
            log.warning(
                "Kalshi WS: seq gap on sid=%d (expected %d, got %d) — "
                "requesting snapshot for %d ticker(s)",
                sid, expected, seq, len(gap_tickers),
            )
            # Clear books for affected tickers
            for t in gap_tickers:
                self._books.pop(t, None)
            try:
                await self._request_snapshot(ws, gap_tickers)
            except ConnectionClosed:
                return
            self._sid_seq[sid] = seq
            return

        self._sid_seq[sid] = seq

        book = self._books.get(ticker)
        if book is None:
            # Delta before snapshot — ignore, snapshot will arrive
            return

        try:
            price = float(data.get("price_dollars", 0))
            delta = float(data.get("delta_fp", 0))
        except (ValueError, TypeError):
            return

        side = data.get("side", "")
        if side not in ("yes", "no"):
            return

        changed = book.apply_delta(side, price, delta)
        if changed:
            await self._on_price_update(
                ticker, book.yes_ask, book.yes_bid, book.no_ask, book.no_bid,
                book.yes_ask_size, book.no_ask_size,
            )


if __name__ == "__main__":
    """
    Standalone test. Usage:
        python -m scrapers.kalshi_ws <market_ticker> [market_ticker ...]
    """
    import os
    import sys
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(message)s")

    market_tickers = sys.argv[1:] if len(sys.argv) > 1 else []
    if not market_tickers:
        print("Usage: python -m scrapers.kalshi_ws <market_ticker> [market_ticker ...]")
        sys.exit(1)

    api_key = os.environ.get("KALSHI_API_KEY_ID", "")
    private_key = os.environ.get("KALSHI_PRIVATE_KEY", "")
    if not api_key or not private_key:
        print("Missing KALSHI_API_KEY_ID or KALSHI_PRIVATE_KEY in .env")
        sys.exit(1)

    async def _test() -> None:
        tick_count = 0

        async def on_tick(
            market_ticker: str,
            yes_ask: float, yes_bid: float,
            no_ask: float, no_bid: float,
            yes_ask_size: float, no_ask_size: float,
        ) -> None:
            nonlocal tick_count
            tick_count += 1
            print(
                f"  TICK #{tick_count:4d}  {market_ticker}  "
                f"YES ask={yes_ask:.3f}({yes_ask_size:.0f}) bid={yes_bid:.3f}  "
                f"NO ask={no_ask:.3f}({no_ask_size:.0f}) bid={no_bid:.3f}"
            )

        client = KalshiWSClient(api_key, private_key, on_price_update=on_tick)
        task = asyncio.create_task(client.start(market_tickers))
        print(f"Listening on {len(market_tickers)} ticker(s) for 60s...")
        await asyncio.sleep(60)
        await client.stop()
        task.cancel()
        print(f"Done. Received {tick_count} ticks.")

    asyncio.run(_test())
