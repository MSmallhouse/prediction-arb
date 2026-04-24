"""
Polymarket US WebSocket client.

Subscribes to market_data channel by market slug. Fires callback with
best ask/bid for both the long (Team A) and short (Team B) sides.

Uses raw websockets with ED25519 auth (same signing as SDK but bypasses
the SDK's event emitter for clean asyncio integration).

From one slug we derive both teams' prices:
  long_ask  = offers[0].px   (cost to buy "Team A wins")
  short_ask = 1 - bids[0].px (cost to buy "Team B wins")
"""

import asyncio
import base64
import json
import logging
import time
from typing import Awaitable, Callable

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed
from nacl.signing import SigningKey

log = logging.getLogger(__name__)

POLY_US_WS_URL = "wss://api.polymarket.us/v1/ws/markets"
POLY_US_WS_PATH = "/v1/ws/markets"
MAX_BACKOFF = 60.0


def _build_auth_headers(key_id: str, secret_key: str) -> dict[str, str]:
    """ED25519 signature for WS auth."""
    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}GET{POLY_US_WS_PATH}"

    secret_bytes = base64.b64decode(secret_key)
    if len(secret_bytes) == 64:
        secret_bytes = secret_bytes[:32]

    signing_key = SigningKey(secret_bytes)
    signed = signing_key.sign(message.encode())
    signature_b64 = base64.b64encode(signed.signature).decode()

    return {
        "X-PM-Access-Key": key_id,
        "X-PM-Timestamp": timestamp,
        "X-PM-Signature": signature_b64,
    }


class PolymarketUSWSClient:
    def __init__(
        self,
        key_id: str,
        secret_key: str,
        on_price_update: Callable[[str, float, float, float, float, float, float], Awaitable[None]],
    ) -> None:
        """
        on_price_update(market_slug, long_ask, long_bid, short_ask, short_bid, long_ask_size, short_ask_size)
        """
        self._key_id = key_id
        self._secret_key = secret_key
        self._on_price_update = on_price_update
        self._subscribed: set[str] = set()
        self._ws = None
        self._running = False
        self._backoff = 1.0
        self._last_prices: dict[str, tuple[float, float, float, float]] = {}

    async def start(self, initial_slugs: list[str]) -> None:
        """Run forever; reconnects on drop. Call via asyncio.create_task()."""
        self._running = True
        self._subscribed.update(initial_slugs)
        while self._running:
            try:
                await self._connect_and_run()
                self._backoff = 1.0
            except Exception as exc:
                log.warning("PolyUS WS: %s — reconnect in %.1fs", exc, self._backoff)
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, MAX_BACKOFF)

    async def subscribe(self, slugs: list[str]) -> None:
        """Dynamically subscribe new slugs on existing connection."""
        new = [s for s in slugs if s not in self._subscribed]
        if not new:
            return
        self._subscribed.update(new)
        if self._ws is not None:
            try:
                await self._send_subscribe(self._ws, new)
                log.info("PolyUS WS: dynamically subscribed %d new slugs", len(new))
            except ConnectionClosed:
                log.warning("PolyUS WS: dynamic subscribe failed; will resubscribe on reconnect")

    async def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            await self._ws.close()

    async def _connect_and_run(self) -> None:
        headers = _build_auth_headers(self._key_id, self._secret_key)
        async with connect(POLY_US_WS_URL, additional_headers=headers) as ws:
            self._ws = ws
            log.info("PolyUS WS: connected (%d slugs)", len(self._subscribed))
            if self._subscribed:
                await self._send_subscribe(ws, list(self._subscribed))
            await self._read_loop(ws)

    async def _send_subscribe(self, ws, slugs: list[str]) -> None:
        msg = {
            "subscribe": {
                "requestId": f"sub-{int(time.time())}",
                "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
                "marketSlugs": slugs,
            }
        }
        await ws.send(json.dumps(msg))

    async def _read_loop(self, ws) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue

            if "marketData" in msg:
                await self._handle_market_data(msg)
            elif "heartbeat" in msg:
                pass  # server heartbeat, no response needed
            elif "error" in msg:
                log.warning("PolyUS WS error: %s", msg.get("error"))

    async def _handle_market_data(self, msg: dict) -> None:
        """Extract best ask/bid from full book snapshot and fire callback."""
        data = msg.get("marketData", {})
        slug = data.get("marketSlug", "")
        if not slug:
            return

        bids = data.get("bids", [])
        offers = data.get("offers", [])

        if not offers:
            long_ask = 1.0
            long_ask_size = 0.0
        else:
            try:
                long_ask = float(offers[0]["px"]["value"])
                long_ask_size = float(offers[0]["qty"])
            except (KeyError, ValueError, TypeError, IndexError):
                return

        if not bids:
            long_bid = 0.0
            short_ask_size = 0.0
        else:
            try:
                long_bid = float(bids[0]["px"]["value"])
                short_ask_size = float(bids[0]["qty"])  # short ask depth = long bid depth
            except (KeyError, ValueError, TypeError, IndexError):
                long_bid = 0.0
                short_ask_size = 0.0

        short_ask = round(1.0 - long_bid, 4) if long_bid > 0 else 1.0
        short_bid = round(1.0 - long_ask, 4) if long_ask < 1 else 0.0

        # Only fire callback if prices changed
        new_prices = (long_ask, long_bid, short_ask, short_bid)
        if new_prices == self._last_prices.get(slug):
            return
        self._last_prices[slug] = new_prices

        await self._on_price_update(
            slug, long_ask, long_bid, short_ask, short_bid,
            long_ask_size, short_ask_size,
        )


if __name__ == "__main__":
    """
    Standalone test.
    Usage: python -m scrapers.polymarket_us_ws <slug> [slug ...]
    """
    import os
    import sys
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    slugs = sys.argv[1:] if len(sys.argv) > 1 else []
    if not slugs:
        print("Usage: python -m scrapers.polymarket_us_ws <slug> [slug ...]")
        sys.exit(1)

    key_id = os.environ.get("POLYMARKET_API_KEY_ID", "")
    secret_key = os.environ.get("POLYMARKET_PRIVATE_KEY", "")

    async def _test() -> None:
        tick_count = 0

        async def on_tick(
            slug: str,
            long_ask: float, long_bid: float,
            short_ask: float, short_bid: float,
            long_ask_size: float, short_ask_size: float,
        ) -> None:
            nonlocal tick_count
            tick_count += 1
            parts = slug.split("-")
            away = parts[2] if len(parts) > 2 else "?"
            home = parts[3] if len(parts) > 3 else "?"
            print(
                f"  #{tick_count:3d}  {away}@{home}  "
                f"LONG ask={long_ask:.3f}({long_ask_size:.0f}) bid={long_bid:.3f}  "
                f"SHORT ask={short_ask:.3f}({short_ask_size:.0f}) bid={short_bid:.3f}"
            )

        client = PolymarketUSWSClient(key_id, secret_key, on_price_update=on_tick)
        task = asyncio.create_task(client.start(slugs))
        print(f"Listening on {len(slugs)} slug(s) for 30s...")
        await asyncio.sleep(30)
        await client.stop()
        task.cancel()
        print(f"Done. {tick_count} ticks.")

    asyncio.run(_test())
