"""
Polymarket CLOB WebSocket client.

Subscribes to the market channel by YES token_id. Fires on_price_update(token_id, ask)
on every price_change or best_bid_ask event. Reconnects with exponential backoff.

No auth required for market channel.
"""

import asyncio
import json
import logging
from typing import Awaitable, Callable

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

log = logging.getLogger(__name__)

POLY_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
HEARTBEAT_INTERVAL = 10.0  # seconds between client PINGs
MAX_BACKOFF = 60.0


class PolymarketWSClient:
    def __init__(self, on_price_update: Callable[[str, float, float], Awaitable[None]]) -> None:
        self._on_price_update = on_price_update
        self._subscribed: set[str] = set()  # token_ids — source of truth for reconnect
        self._ws = None
        self._running = False
        self._backoff = 1.0

    async def start(self, initial_token_ids: list[str]) -> None:
        """Run forever; reconnects on drop. Call via asyncio.create_task()."""
        self._running = True
        self._subscribed.update(initial_token_ids)
        while self._running:
            try:
                await self._connect_and_run()
                self._backoff = 1.0
            except Exception as exc:
                log.warning("Polymarket WS: %s — reconnect in %.1fs", exc, self._backoff)
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, MAX_BACKOFF)

    async def subscribe(self, token_ids: list[str]) -> None:
        """Dynamically add tokens to the live connection (hourly discovery path)."""
        new = [t for t in token_ids if t not in self._subscribed]
        if not new:
            return
        self._subscribed.update(new)
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"assets_ids": new, "operation": "subscribe"}))
                log.info("Polymarket WS: dynamically subscribed %d new tokens", len(new))
            except ConnectionClosed:
                log.warning("Polymarket WS: dynamic subscribe failed (disconnected); will resubscribe on reconnect")

    async def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            await self._ws.close()

    async def _connect_and_run(self) -> None:
        async with connect(POLY_WS_URL) as ws:
            self._ws = ws
            log.info("Polymarket WS: connected (%d tokens)", len(self._subscribed))
            if self._subscribed:
                await ws.send(json.dumps({
                    "assets_ids": list(self._subscribed),
                    "type": "market",
                }))
            await asyncio.gather(
                self._heartbeat_loop(ws),
                self._read_loop(ws),
            )

    async def _heartbeat_loop(self, ws) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            try:
                await ws.send("PING")
            except ConnectionClosed:
                return

    async def _read_loop(self, ws) -> None:
        async for raw in ws:
            if raw == "PONG":
                continue
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                log.debug("Polymarket WS: non-JSON frame: %r", str(raw)[:80])
                continue
            await self._dispatch(msg)

    async def _dispatch(self, msg) -> None:
        """
        Handle a single event dict or a list of events.

        event_type values we care about:
          "book"        — initial orderbook snapshot on subscribe; extract min(asks), max(bids)
          "price_change"— single price update; carries "price" field (no bid)
          "best_bid_ask"— carries explicit "best_ask" and "best_bid" fields
        """
        events = msg if isinstance(msg, list) else [msg]
        for event in events:
            if not isinstance(event, dict):
                continue
            etype = event.get("event_type")
            asset_id = event.get("asset_id") or event.get("token_id")
            if not asset_id:
                continue

            bid = 0.0
            if etype == "book":
                asks = event.get("asks", [])
                if not asks:
                    continue
                try:
                    ask = min(float(a["price"]) for a in asks)
                except (KeyError, ValueError, TypeError):
                    continue
                bids = event.get("bids", [])
                if bids:
                    try:
                        bid = max(float(b["price"]) for b in bids)
                    except (KeyError, ValueError, TypeError):
                        bid = 0.0
            elif etype == "best_bid_ask":
                raw_ask = event.get("best_ask")
                if raw_ask is None:
                    continue
                try:
                    ask = float(raw_ask)
                except (ValueError, TypeError):
                    continue
                raw_bid = event.get("best_bid")
                if raw_bid is not None:
                    try:
                        bid = float(raw_bid)
                    except (ValueError, TypeError):
                        bid = 0.0
            elif etype == "price_change":
                raw_ask = event.get("price")
                if raw_ask is None:
                    continue
                try:
                    ask = float(raw_ask)
                except (ValueError, TypeError):
                    continue
            else:
                continue

            if 0.0 < ask < 1.0:
                await self._on_price_update(asset_id, ask, bid)


if __name__ == "__main__":
    """
    Standalone test. Usage:
        python -m scrapers.polymarket_ws <token_id>

    Get a token_id from:
        python -c "
    import asyncio, aiohttp
    from scrapers.polymarket import discover_and_fetch
    from scrapers.kalshi import discover_mlb_events
    async def main():
        async with aiohttp.ClientSession() as s:
            tickers = await discover_mlb_events(s)
            markets = await discover_and_fetch(s, tickers[:3])
            for m in markets[:4]:
                print(m.token_id, m.team, m.yes_ask)
    asyncio.run(main())
    "
    """
    import sys

    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(message)s")

    token_ids = sys.argv[1:] if len(sys.argv) > 1 else []
    if not token_ids:
        print("Usage: python -m scrapers.polymarket_ws <token_id> [token_id ...]")
        sys.exit(1)

    async def _test() -> None:
        tick_count = 0

        async def on_tick(token_id: str, ask: float, bid: float) -> None:
            nonlocal tick_count
            tick_count += 1
            print(f"  TICK #{tick_count}  token={token_id[:12]}...  ask={ask:.4f}  bid={bid:.4f}")

        client = PolymarketWSClient(on_price_update=on_tick)
        task = asyncio.create_task(client.start(token_ids))
        print(f"Listening on {len(token_ids)} token(s) for 60s...")
        await asyncio.sleep(60)
        await client.stop()
        task.cancel()
        print(f"Done. Received {tick_count} ticks.")

    asyncio.run(_test())
