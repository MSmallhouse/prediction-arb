"""
Polymarket order placement client.

Uses py-clob-client SDK for EIP-712 order signing and CLOB REST submission.
Requires Ethereum private key (for signing) + derived API credentials (for REST auth).

Production: https://clob.polymarket.com
Test:        https://clob-v2.polymarket.com
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

log = logging.getLogger(__name__)

POLY_PROD_HOST = "https://clob.polymarket.com"
POLY_TEST_HOST = "https://clob-v2.polymarket.com"
POLYGON_CHAIN_ID = 137


@dataclass
class PolyOrderResult:
    success: bool
    order_id: str = ""
    status: str = ""        # "matched" (filled), "live" (resting), "delayed"
    error: str = ""
    latency_ms: float = 0.0


def create_client(
    eth_private_key: str,
    funder_address: str = "",
    signature_type: int = 2,    # 2 = GNOSIS_SAFE (most common for Polymarket app)
    test: bool = False,
) -> ClobClient:
    """
    Create an authenticated ClobClient.

    First call derives API credentials from the ETH private key.
    Subsequent calls reuse cached creds.

    signature_type:
      0 = EOA (standalone wallet)
      1 = POLY_PROXY (Magic Link)
      2 = GNOSIS_SAFE (browser/app wallet — most common)
    """
    host = POLY_TEST_HOST if test else POLY_PROD_HOST

    # Derive API credentials from the ETH private key
    temp_client = ClobClient(host, chain_id=POLYGON_CHAIN_ID, key=eth_private_key)
    api_creds = temp_client.create_or_derive_api_creds()
    log.info("Polymarket API creds derived (key=%s...)", api_creds.api_key[:8])

    client = ClobClient(
        host,
        chain_id=POLYGON_CHAIN_ID,
        key=eth_private_key,
        creds=api_creds,
        signature_type=signature_type,
        funder=funder_address or "",
    )
    return client


def place_fok_order(
    client: ClobClient,
    token_id: str,
    price: float,       # 0.01 - 0.99
    size: float = 10.0,  # dollar amount
    tick_size: str = "0.01",
    neg_risk: bool = False,
) -> PolyOrderResult:
    """
    Place a Fill-or-Kill BUY order on Polymarket.

    FOK: fills completely at the specified price or cancels immediately.
    size = dollar amount (not share count).
    """
    t_start = time.monotonic()
    try:
        resp = client.create_and_post_order(
            OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=BUY,
            ),
            options={
                "tick_size": tick_size,
                "neg_risk": neg_risk,
            },
            order_type=OrderType.FOK,
        )
        latency = (time.monotonic() - t_start) * 1000

        status = resp.get("status", "")
        order_id = resp.get("orderID", "")
        success = status == "matched"

        if not success:
            log.info(
                "Poly FOK order not matched: status=%s token=%s... price=%.2f",
                status, token_id[:12], price,
            )

        return PolyOrderResult(
            success=success,
            order_id=order_id,
            status=status,
            latency_ms=latency,
        )
    except Exception as exc:
        latency = (time.monotonic() - t_start) * 1000
        log.error("Poly order failed: %s", exc)
        return PolyOrderResult(
            success=False,
            error=str(exc),
            latency_ms=latency,
        )


def check_balance(client: ClobClient) -> Optional[dict]:
    """Check balance and allowance status."""
    try:
        return client.get_balance_allowance()
    except Exception as exc:
        log.error("Poly balance check failed: %s", exc)
        return None


if __name__ == "__main__":
    """
    Standalone test.
    Usage: python -m scrapers.poly_orders

    Requires POLYMARKET_ETH_PRIVATE_KEY in .env (Ethereum wallet private key, hex with 0x prefix).
    Optionally POLYMARKET_FUNDER_ADDRESS (your proxy wallet address).
    """
    import os
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    eth_key = os.environ.get("POLYMARKET_ETH_PRIVATE_KEY", "")
    funder = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "")

    if not eth_key:
        print("Missing POLYMARKET_ETH_PRIVATE_KEY in .env")
        print("This should be your Ethereum wallet private key (hex, starting with 0x)")
        print("Export it from your Polymarket mobile app (fat.lobster account)")
        exit(1)

    print("Creating Polymarket client...")
    client = create_client(eth_key, funder_address=funder)

    print("Checking balance...")
    bal = check_balance(client)
    print(f"Balance/allowance: {bal}")

    print("\nClient ready. To test order placement, add code below.")
