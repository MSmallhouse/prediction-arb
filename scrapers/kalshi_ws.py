"""
Kalshi WebSocket client.

Subscribes to the ticker channel per market_ticker. Fires on_price_update(market_ticker, ask)
on every ticker event. Auth required (RSA-PSS) even for public data.

Heartbeat: server sends WS-protocol Ping frames; websockets auto-responds with Pong.
No application-level heartbeat needed.
"""

import asyncio
import base64
import json
import logging
import time
from typing import Awaitable, Callable

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
    """
    RSA-PSS signature over f"{timestamp_ms}GET{path}".
    timestamp_ms is milliseconds since epoch as a decimal string.
    """
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


class KalshiWSClient:
    def __init__(
        self,
        api_key_id: str,
        private_key_pem: str,
        on_price_update: Callable[[str, float, float], Awaitable[None]],
    ) -> None:
        self._api_key_id = api_key_id
        self._private_key_pem = private_key_pem
        self._on_price_update = on_price_update
        self._subscribed: set[str] = set()  # market_tickers — source of truth for reconnect
        self._ws = None
        self._running = False
        self._backoff = 1.0

    async def start(self, initial_market_tickers: list[str]) -> None:
        """Run forever; reconnects on drop. Call via asyncio.create_task()."""
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
        """Dynamically subscribe new tickers on existing connection."""
        new = [t for t in market_tickers if t not in self._subscribed]
        if not new:
            return
        self._subscribed.update(new)
        if self._ws is not None:
            for ticker in new:
                try:
                    await self._send_subscribe(self._ws, ticker)
                except ConnectionClosed:
                    log.warning("Kalshi WS: dynamic subscribe failed (disconnected); will resubscribe on reconnect")
                    return
            log.info("Kalshi WS: dynamically subscribed %d new tickers", len(new))

    async def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            await self._ws.close()

    async def _connect_and_run(self) -> None:
        headers = _build_auth_headers(self._api_key_id, self._private_key_pem)
        async with connect(KALSHI_WS_URL, additional_headers=headers) as ws:
            self._ws = ws
            log.info("Kalshi WS: connected (%d tickers)", len(self._subscribed))
            for ticker in self._subscribed:
                await self._send_subscribe(ws, ticker)
            await self._read_loop(ws)

    async def _send_subscribe(self, ws, market_ticker: str) -> None:
        msg = {
            "id": _next_id(),
            "cmd": "subscribe",
            "params": {
                "channels": ["ticker"],
                "market_ticker": market_ticker,
            },
        }
        await ws.send(json.dumps(msg))

    async def _read_loop(self, ws) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            await self._dispatch(msg)

    async def _dispatch(self, msg: dict) -> None:
        """
        Kalshi tick: {"type": "ticker", "msg": {"market_ticker": "...", "yes_ask_dollars": 0.55, ...}}
        Subscribe ACK: {"type": "subscribed", ...} — ignored.
        """
        if msg.get("type") != "ticker":
            return
        data = msg.get("msg", {})
        ticker = data.get("market_ticker")
        raw_ask = data.get("yes_ask_dollars")
        if ticker is None or raw_ask is None:
            return
        try:
            ask = float(raw_ask)
        except (ValueError, TypeError):
            return

        def _safe_float(val) -> float:
            try:
                return float(val) if val is not None else 0.0
            except (ValueError, TypeError):
                return 0.0

        bid    = _safe_float(data.get("yes_bid_dollars"))
        no_ask = _safe_float(data.get("no_ask_dollars"))
        no_bid = _safe_float(data.get("no_bid_dollars"))

        if 0.0 < ask < 1.0:
            await self._on_price_update(ticker, ask, bid, no_ask, no_bid)


if __name__ == "__main__":
    """
    Standalone test. Usage:
        python -m scrapers.kalshi_ws <market_ticker>

    Example market_ticker: KXMLBGAME-26APR221410BALKC-KC
    Get live tickers from:
        python -c "
    import asyncio, aiohttp
    from scrapers.kalshi import fetch_all_prices, discover_mlb_events
    async def main():
        async with aiohttp.ClientSession() as s:
            tickers = await discover_mlb_events(s)
            markets = await fetch_all_prices(s, tickers[:2])
            for m in markets:
                print(m.market_ticker, m.team, m.yes_ask)
    asyncio.run(main())
    "
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

        async def on_tick(market_ticker: str, ask: float, bid: float) -> None:
            nonlocal tick_count
            tick_count += 1
            print(f"  TICK #{tick_count}  ticker={market_ticker}  ask={ask:.4f}  bid={bid:.4f}")

        client = KalshiWSClient(api_key, private_key, on_price_update=on_tick)
        task = asyncio.create_task(client.start(market_tickers))
        print(f"Listening on {len(market_tickers)} ticker(s) for 60s...")
        await asyncio.sleep(60)
        await client.stop()
        task.cancel()
        print(f"Done. Received {tick_count} ticks.")

    asyncio.run(_test())
