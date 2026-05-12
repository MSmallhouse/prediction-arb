"""
Polymarket US private WebSocket client — order events.

Subscribes to SUBSCRIPTION_TYPE_ORDER on /v1/ws/private. Lets the executor
await terminal order state (FILLED, CANCELED, EXPIRED, REJECTED) without
blocking on synchronousExecution.

Same ED25519 auth as the market-data WS, different path.

Wire format (confirmed against polymarket-us SDK 0.1.2):

  Snapshot (initial dump of open orders, then eof):
    {"requestId", "subscriptionType": "SUBSCRIPTION_TYPE_ORDER",
     "orderSubscriptionSnapshot": {"orders": [...], "eof": bool}}

  Incremental update:
    {"requestId", "subscriptionType": "SUBSCRIPTION_TYPE_ORDER",
     "orderSubscriptionUpdate": {"execution": {"id", "order": {"id", "state", ...}, "type", ...}}}

  Legacy keys (also handled): ordersSnapshot, orderUpdate.

Usage:
    client = PolymarketUSPrivateWSClient(key_id, secret)
    asyncio.create_task(client.start())
    await client.wait_ready()           # blocks until eof received
    order_dict = await client.await_terminal(order_id, timeout=1.0)
    # order_dict["state"] == "ORDER_STATE_FILLED" / _CANCELED / _EXPIRED / _REJECTED
"""

import asyncio
import base64
import json
import logging
import time
from typing import Optional

from websockets.asyncio.client import connect
from nacl.signing import SigningKey

log = logging.getLogger(__name__)

POLY_US_PRIVATE_WS_URL = "wss://api.polymarket.us/v1/ws/private"
POLY_US_PRIVATE_WS_PATH = "/v1/ws/private"
MAX_BACKOFF = 60.0

_TERMINAL_STATES = frozenset({
    "ORDER_STATE_FILLED",
    "ORDER_STATE_CANCELED",
    "ORDER_STATE_REJECTED",
    "ORDER_STATE_EXPIRED",
})


def _build_auth_headers(key_id: str, secret_key: str) -> dict[str, str]:
    """ED25519 signature for private WS auth (same scheme as market-data WS)."""
    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}GET{POLY_US_PRIVATE_WS_PATH}"

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


class PolymarketUSPrivateWSClient:
    def __init__(self, key_id: str, secret_key: str) -> None:
        self._key_id = key_id
        self._secret_key = secret_key
        self._ws = None
        self._running = False
        self._backoff = 1.0

        # Set after we receive an OrderSubscriptionSnapshot with eof=true.
        # Cleared on disconnect. Executor gates on this.
        self._ready = asyncio.Event()

        # order_id → Future resolved with the terminal Order dict.
        self._pending: dict[str, asyncio.Future] = {}
        # order_id → terminal Order dict, for events that arrived before the
        # executor registered its waiter.
        self._early_terminals: dict[str, dict] = {}

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()

    async def wait_ready(self) -> None:
        await self._ready.wait()

    async def start(self) -> None:
        """Run forever; reconnects on drop. Call via asyncio.create_task()."""
        self._running = True
        while self._running:
            try:
                await self._connect_and_run()
                self._backoff = 1.0
            except Exception as exc:
                log.warning("PolyUS private WS: %s — reconnect in %.1fs", exc, self._backoff)
                await asyncio.sleep(self._backoff)
                self._backoff = min(self._backoff * 2, MAX_BACKOFF)

    async def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            await self._ws.close()

    async def _connect_and_run(self) -> None:
        # Drop ready flag for the duration of the disconnect/reconnect cycle.
        # Executor disables itself until we're ready again.
        self._ready.clear()
        # Old pending waiters will time out on their own; no need to fail them
        # here — the order outcome from before the drop is unknowable from this
        # connection's snapshot (we ignore snapshot per design).

        headers = _build_auth_headers(self._key_id, self._secret_key)
        async with connect(POLY_US_PRIVATE_WS_URL, additional_headers=headers) as ws:
            self._ws = ws
            log.info("PolyUS private WS: connected")
            await self._send_subscribe(ws)
            await self._read_loop(ws)

    async def _send_subscribe(self, ws) -> None:
        # Subscribe to orders first (the channel we care about) then balance.
        # The server only sends a snapshot for channels with non-empty state;
        # with zero open orders it stays silent on the order channel. Balance
        # always returns a snapshot immediately, which we use as proof the
        # connection is live and the (prior) order subscribe was processed.
        await ws.send(json.dumps({
            "subscribe": {
                "requestId": f"private-orders-{int(time.time())}",
                "subscriptionType": "SUBSCRIPTION_TYPE_ORDER",
            }
        }))
        await ws.send(json.dumps({
            "subscribe": {
                "requestId": f"private-balance-{int(time.time())}",
                "subscriptionType": "SUBSCRIPTION_TYPE_ACCOUNT_BALANCE",
            }
        }))

    async def _read_loop(self, ws) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            self._dispatch(msg)

    def _dispatch(self, msg: dict) -> None:
        # Order snapshot — server only sends this if there ARE open orders.
        # With zero open orders the channel is silent (no eof). Stale orders
        # from a prior run are logged but otherwise ignored per design.
        order_snap = msg.get("orderSubscriptionSnapshot") or msg.get("ordersSnapshot")
        if order_snap is not None:
            orders = order_snap.get("orders") or []
            if orders:
                log.info("PolyUS private WS: snapshot contains %d open order(s)", len(orders))
            return

        # Order update — extract execution.order and route terminal states.
        update = msg.get("orderSubscriptionUpdate") or msg.get("orderUpdate")
        if update is not None:
            execution = update.get("execution") or {}
            order = execution.get("order") or {}
            order_id = order.get("id")
            state = order.get("state")
            if not order_id or not state:
                return
            if state in _TERMINAL_STATES:
                self._resolve(order_id, order)
            return

        # Balance snapshot — server sends this immediately after we subscribe,
        # AFTER it has processed the earlier order subscribe. Use as readiness
        # proof for the whole connection.
        if not self._ready.is_set():
            balance_snap = (
                msg.get("accountBalanceSubscriptionSnapshot")
                or msg.get("accountBalancesSnapshot")
            )
            if balance_snap is not None:
                self._ready.set()
                log.info("PolyUS private WS: ready (balance snapshot received)")
                return

        # Heartbeats and unrelated messages are ignored silently.

    def _resolve(self, order_id: str, order: dict) -> None:
        fut = self._pending.pop(order_id, None)
        if fut is not None and not fut.done():
            fut.set_result(order)
            return
        # Event arrived before the executor registered its waiter — buffer it.
        self._early_terminals[order_id] = order

    async def await_terminal(self, order_id: str, timeout: float) -> dict:
        """
        Wait for a terminal Order event for this order_id.

        Returns the Order dict (with at minimum: id, state, cumQuantity).
        Raises asyncio.TimeoutError if no terminal event within timeout.
        """
        # Check the early-event buffer first (handles WS-arrives-before-create-returns).
        cached = self._early_terminals.pop(order_id, None)
        if cached is not None:
            return cached

        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[order_id] = fut
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(order_id, None)
            raise

    def cancel_waiter(self, order_id: str) -> None:
        """Stop waiting on an order_id (executor decided to give up early)."""
        fut = self._pending.pop(order_id, None)
        if fut is not None and not fut.done():
            fut.cancel()
        self._early_terminals.pop(order_id, None)
