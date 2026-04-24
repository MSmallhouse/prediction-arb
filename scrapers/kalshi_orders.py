"""
Kalshi order placement client.

Places FOK (Fill-or-Kill) orders via REST API. Reuses RSA-PSS auth from kalshi_ws.py.

Production: https://api.elections.kalshi.com/trade-api/v2
Demo:       https://demo-api.kalshi.co/trade-api/v2
"""

import base64
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Optional

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

log = logging.getLogger(__name__)

KALSHI_PROD_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"
ORDER_PATH = "/trade-api/v2/portfolio/orders"


@dataclass
class KalshiOrderResult:
    success: bool
    order_id: str = ""
    status: str = ""           # "executed", "canceled", "resting"
    fill_count: float = 0.0
    fill_cost: float = 0.0     # total dollars paid
    fees: float = 0.0          # taker fees
    error: str = ""
    latency_ms: float = 0.0    # time from request sent to response received


def _sign_request(
    api_key_id: str,
    private_key_pem: str,
    method: str,
    path: str,
) -> dict[str, str]:
    """RSA-PSS signature for REST requests. Same algo as WS auth."""
    pem = private_key_pem.replace("\\n", "\n")
    ts_ms = str(int(time.time() * 1000))
    message = f"{ts_ms}{method}{path}".encode()

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
        "Content-Type": "application/json",
    }


async def place_fok_order(
    session: aiohttp.ClientSession,
    api_key_id: str,
    private_key_pem: str,
    market_ticker: str,
    side: str,              # "yes" or "no"
    price_cents: int,       # 1-99
    count: int = 1,
    demo: bool = False,
) -> KalshiOrderResult:
    """
    Place a Fill-or-Kill order on Kalshi.

    Returns KalshiOrderResult with fill status and latency.
    FOK: fills completely at the specified price or cancels immediately.
    """
    base_url = KALSHI_DEMO_BASE if demo else KALSHI_PROD_BASE
    url = f"{base_url}/portfolio/orders"

    body = {
        "ticker": market_ticker,
        "side": side,
        "action": "buy",
        "count": count,
        "type": "limit",
        "time_in_force": "fill_or_kill",
        "client_order_id": str(uuid.uuid4()),
    }

    if side == "yes":
        body["yes_price"] = price_cents
    else:
        body["no_price"] = price_cents

    headers = _sign_request(api_key_id, private_key_pem, "POST", ORDER_PATH)

    t_start = time.monotonic()
    try:
        async with session.post(url, headers=headers, json=body) as resp:
            latency = (time.monotonic() - t_start) * 1000
            data = await resp.json()

            if resp.status == 201:
                order = data.get("order", {})
                return KalshiOrderResult(
                    success=True,
                    order_id=order.get("order_id", ""),
                    status=order.get("status", ""),
                    fill_count=float(order.get("fill_count_fp", 0)),
                    fill_cost=float(order.get("taker_fill_cost_dollars", 0)),
                    fees=float(order.get("taker_fees_dollars", 0)),
                    latency_ms=latency,
                )
            else:
                error_msg = data.get("message", data.get("error", str(data)))
                log.warning(
                    "Kalshi order rejected (%d): %s — ticker=%s side=%s price=%dc",
                    resp.status, error_msg, market_ticker, side, price_cents,
                )
                return KalshiOrderResult(
                    success=False,
                    error=f"HTTP {resp.status}: {error_msg}",
                    latency_ms=latency,
                )
    except Exception as exc:
        latency = (time.monotonic() - t_start) * 1000
        log.error("Kalshi order failed: %s", exc)
        return KalshiOrderResult(
            success=False,
            error=str(exc),
            latency_ms=latency,
        )


async def get_balance(
    session: aiohttp.ClientSession,
    api_key_id: str,
    private_key_pem: str,
    demo: bool = False,
) -> Optional[float]:
    """Fetch account balance in dollars. Also useful as a keepalive ping."""
    base_url = KALSHI_DEMO_BASE if demo else KALSHI_PROD_BASE
    url = f"{base_url}/portfolio/balance"
    path = "/trade-api/v2/portfolio/balance"
    headers = _sign_request(api_key_id, private_key_pem, "GET", path)

    try:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                data = await resp.json()
                return float(data.get("balance", 0)) / 100  # API returns cents
            log.warning("Kalshi balance check returned %d", resp.status)
    except Exception as exc:
        log.error("Kalshi balance check failed: %s", exc)
    return None


if __name__ == "__main__":
    """
    Standalone test against demo environment.
    Usage: python -m scrapers.kalshi_orders [--live]
    """
    import asyncio
    import os
    import sys
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    demo = "--live" not in sys.argv
    env_label = "DEMO" if demo else "LIVE"
    print(f"=== Kalshi Order Test ({env_label}) ===\n")

    api_key = os.environ.get("KALSHI_API_KEY_ID", "")
    private_key = os.environ.get("KALSHI_PRIVATE_KEY", "")
    if not api_key or not private_key:
        print("Missing KALSHI_API_KEY_ID or KALSHI_PRIVATE_KEY in .env")
        sys.exit(1)

    async def main():
        async with aiohttp.ClientSession() as session:
            # Step 1: Check balance
            balance = await get_balance(session, api_key, private_key, demo=demo)
            print(f"Balance: ${balance:.2f}" if balance is not None else "Balance: failed to fetch")

            if demo:
                print("\nTo test order placement on demo, uncomment the order code below.")
                print("Demo uses mock funds — no real money at risk.")
                # Uncomment to test:
                # result = await place_fok_order(
                #     session, api_key, private_key,
                #     market_ticker="SOME-DEMO-TICKER",
                #     side="yes",
                #     price_cents=50,
                #     count=1,
                #     demo=True,
                # )
                # print(f"Order result: {result}")

    asyncio.run(main())
