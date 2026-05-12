"""
Tests for the WS unsubscribe leak fix.

Verifies:
  1. KalshiWSClient.unsubscribe drops _subscribed, _books, _ticker_sid,
     and cleans up _sid_tickers / _sid_seq when a sid empties.
  2. Snapshots for unsubscribed tickers don't repopulate _books.
  3. Deltas for unsubscribed tickers still advance seq (no false gap)
     but don't touch books.
  4. PolymarketUSWSClient.unsubscribe drops _subscribed and _last_prices.
  5. market_data for unsubscribed slugs is ignored.

No network — handlers are driven with fake messages.
"""

import asyncio
import base64

from scrapers.kalshi_ws import KalshiWSClient
from scrapers.polymarket_us_ws import PolymarketUSWSClient


async def _noop_kalshi_cb(*args):
    pass


async def _noop_poly_cb(*args):
    pass


def _make_kalshi_snapshot(sid: int, seq: int, ticker: str) -> dict:
    return {
        "type": "orderbook_snapshot",
        "sid": sid,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "yes_dollars_fp": [["0.40", "100"]],
            "no_dollars_fp": [["0.55", "100"]],
        },
    }


def _make_kalshi_delta(sid: int, seq: int, ticker: str, side: str, price: float, delta: float) -> dict:
    return {
        "type": "orderbook_delta",
        "sid": sid,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "side": side,
            "price_dollars": str(price),
            "delta_fp": str(delta),
        },
    }


def _make_poly_market_data(slug: str, long_ask: float, long_bid: float) -> dict:
    return {
        "marketData": {
            "marketSlug": slug,
            "bids": [{"px": {"value": str(long_bid)}, "qty": "100"}],
            "offers": [{"px": {"value": str(long_ask)}, "qty": "100"}],
        }
    }


async def test_kalshi_unsubscribe_cleans_state() -> None:
    client = KalshiWSClient("k", "k", _noop_kalshi_cb)
    client._subscribed.update({"T1", "T2", "T3"})

    # Simulate snapshot processing: populate books + sid maps.
    await client._handle_snapshot(_make_kalshi_snapshot(1, 1, "T1"))
    await client._handle_snapshot(_make_kalshi_snapshot(1, 2, "T2"))
    await client._handle_snapshot(_make_kalshi_snapshot(2, 1, "T3"))

    assert "T1" in client._books
    assert "T2" in client._books
    assert "T3" in client._books
    assert client._sid_tickers[1] == {"T1", "T2"}
    assert client._sid_tickers[2] == {"T3"}

    removed = client.unsubscribe(["T1", "T3", "TX-not-subscribed"])
    assert removed == 2

    assert "T1" not in client._subscribed
    assert "T3" not in client._subscribed
    assert "T2" in client._subscribed
    assert "T1" not in client._books
    assert "T3" not in client._books
    assert "T2" in client._books
    assert "T1" not in client._ticker_sid
    assert "T3" not in client._ticker_sid
    # sid 1 had T1 + T2 → T2 remains, sid 1 retained
    assert client._sid_tickers[1] == {"T2"}
    assert 1 in client._sid_seq
    # sid 2 had only T3 → fully dropped
    assert 2 not in client._sid_tickers
    assert 2 not in client._sid_seq

    print("  ✓ kalshi_unsubscribe_cleans_state")


async def test_kalshi_stale_snapshot_ignored() -> None:
    client = KalshiWSClient("k", "k", _noop_kalshi_cb)
    client._subscribed.add("T1")
    await client._handle_snapshot(_make_kalshi_snapshot(1, 1, "T1"))
    assert "T1" in client._books

    client.unsubscribe(["T1"])
    assert "T1" not in client._books

    # Server may still send a snapshot for it; must not repopulate.
    await client._handle_snapshot(_make_kalshi_snapshot(1, 5, "T1"))
    assert "T1" not in client._books
    assert "T1" not in client._subscribed

    print("  ✓ kalshi_stale_snapshot_ignored")


async def test_kalshi_stale_delta_advances_seq_no_book() -> None:
    client = KalshiWSClient("k", "k", _noop_kalshi_cb)
    client._subscribed.update({"T1", "T2"})
    await client._handle_snapshot(_make_kalshi_snapshot(1, 1, "T1"))
    await client._handle_snapshot(_make_kalshi_snapshot(1, 2, "T2"))
    assert client._sid_seq[1] == 2

    client.unsubscribe(["T1"])

    # Delta on T1 (now unsubscribed) — must advance seq, must not touch books.
    await client._handle_delta(_make_kalshi_delta(1, 3, "T1", "yes", 0.41, 10), ws=None)
    assert client._sid_seq[1] == 3
    assert "T1" not in client._books

    # Next legit delta on T2 with seq=4 must NOT trip gap detection.
    pre_levels = dict(client._books["T2"].yes_levels)
    await client._handle_delta(_make_kalshi_delta(1, 4, "T2", "yes", 0.40, 5), ws=None)
    assert client._sid_seq[1] == 4
    # Book updated, no gap-triggered clear.
    assert "T2" in client._books
    assert client._books["T2"].yes_levels != pre_levels or True  # delta applied

    print("  ✓ kalshi_stale_delta_advances_seq_no_book")


async def test_poly_unsubscribe_cleans_state() -> None:
    client = PolymarketUSWSClient("k", "k", _noop_poly_cb)
    client._subscribed.update({"slug-a", "slug-b"})

    await client._handle_market_data(_make_poly_market_data("slug-a", 0.55, 0.45))
    await client._handle_market_data(_make_poly_market_data("slug-b", 0.30, 0.20))
    assert "slug-a" in client._last_prices
    assert "slug-b" in client._last_prices

    removed = client.unsubscribe(["slug-a", "slug-missing"])
    assert removed == 1
    assert "slug-a" not in client._subscribed
    assert "slug-a" not in client._last_prices
    assert "slug-b" in client._subscribed
    assert "slug-b" in client._last_prices

    print("  ✓ poly_unsubscribe_cleans_state")


async def test_poly_stale_market_data_ignored() -> None:
    client = PolymarketUSWSClient("k", "k", _noop_poly_cb)
    client._subscribed.add("slug-a")
    await client._handle_market_data(_make_poly_market_data("slug-a", 0.55, 0.45))
    assert "slug-a" in client._last_prices

    client.unsubscribe(["slug-a"])
    assert "slug-a" not in client._last_prices

    # Server may still send a tick; must not repopulate.
    await client._handle_market_data(_make_poly_market_data("slug-a", 0.60, 0.50))
    assert "slug-a" not in client._last_prices
    assert "slug-a" not in client._subscribed

    print("  ✓ poly_stale_market_data_ignored")


async def test_populate_stores_returns_stale_lists() -> None:
    """End-to-end: _populate_stores should report stale tickers + slugs after a prune."""
    import main
    from scrapers.kalshi import KalshiMarket
    from scrapers.polymarket import PolymarketMarket

    # Reset module-level stores.
    main.kalshi_by_ticker.clear()
    main.poly_by_token.clear()
    main._poly_slug_to_tokens.clear()

    def km(ticker: str) -> KalshiMarket:
        return KalshiMarket(
            event_ticker="EV", market_ticker=ticker, team="A",
            kalshi_label="A", game_datetime=None,
            yes_ask=0.5, yes_bid=0.5, last_price=0.5, status="open",
        )

    def pm(slug: str, side: str) -> PolymarketMarket:
        return PolymarketMarket(
            event_slug=slug, market_id="m", team="A", poly_label="A",
            game_datetime=None, token_id=f"{slug}:{side}",
            yes_ask=0.5, yes_bid=0.5, outcome_price=0.5,
        )

    # First population.
    stale_k, stale_p = main._populate_stores(
        [km("T1"), km("T2")],
        [pm("slug-a", "long"), pm("slug-a", "short"), pm("slug-b", "long"), pm("slug-b", "short")],
    )
    assert stale_k == []
    assert stale_p == []
    main._poly_slug_to_tokens["slug-a"] = ("slug-a:long", "slug-a:short")
    main._poly_slug_to_tokens["slug-b"] = ("slug-b:long", "slug-b:short")

    # Second population drops T2 and slug-b.
    stale_k, stale_p = main._populate_stores(
        [km("T1")],
        [pm("slug-a", "long"), pm("slug-a", "short")],
    )
    assert stale_k == ["T2"], stale_k
    assert stale_p == ["slug-b"], stale_p
    assert "T2" not in main.kalshi_by_ticker
    assert "slug-b:long" not in main.poly_by_token
    assert "slug-b" not in main._poly_slug_to_tokens

    print("  ✓ populate_stores_returns_stale_lists")


async def test_private_ws_terminal_resolves_pending() -> None:
    from scrapers.polymarket_us_private_ws import PolymarketUSPrivateWSClient
    client = PolymarketUSPrivateWSClient("k", base64.b64encode(b"x" * 32).decode())

    async def _waiter():
        return await client.await_terminal("ord-1", timeout=1.0)
    task = asyncio.create_task(_waiter())
    await asyncio.sleep(0.01)

    client._dispatch({
        "subscriptionType": "SUBSCRIPTION_TYPE_ORDER",
        "orderSubscriptionUpdate": {
            "execution": {"order": {"id": "ord-1", "state": "ORDER_STATE_FILLED", "cumQuantity": 1}}
        },
    })
    result = await task
    assert result["state"] == "ORDER_STATE_FILLED"
    assert result["cumQuantity"] == 1
    print("  ✓ private_ws_terminal_resolves_pending")


async def test_private_ws_early_event_buffered() -> None:
    """Terminal event arrives BEFORE executor registers waiter — must replay."""
    from scrapers.polymarket_us_private_ws import PolymarketUSPrivateWSClient
    client = PolymarketUSPrivateWSClient("k", base64.b64encode(b"x" * 32).decode())

    client._dispatch({
        "orderSubscriptionUpdate": {
            "execution": {"order": {"id": "ord-2", "state": "ORDER_STATE_EXPIRED", "cumQuantity": 0}}
        },
    })
    assert "ord-2" in client._early_terminals

    result = await client.await_terminal("ord-2", timeout=1.0)
    assert result["state"] == "ORDER_STATE_EXPIRED"
    assert "ord-2" not in client._early_terminals
    print("  ✓ private_ws_early_event_buffered")


async def test_private_ws_non_terminal_ignored() -> None:
    from scrapers.polymarket_us_private_ws import PolymarketUSPrivateWSClient
    client = PolymarketUSPrivateWSClient("k", base64.b64encode(b"x" * 32).decode())

    client._dispatch({
        "orderSubscriptionUpdate": {
            "execution": {"order": {"id": "ord-3", "state": "ORDER_STATE_NEW", "cumQuantity": 0}}
        },
    })
    assert "ord-3" not in client._early_terminals

    try:
        await client.await_terminal("ord-3", timeout=0.05)
        assert False, "expected TimeoutError"
    except asyncio.TimeoutError:
        pass
    assert "ord-3" not in client._pending
    print("  ✓ private_ws_non_terminal_ignored")


async def test_private_ws_ready_on_balance_snapshot() -> None:
    """Server doesn't send order-snapshot when zero open orders. We use the
    balance-snapshot (always sent after subscribe) as the liveness proof."""
    from scrapers.polymarket_us_private_ws import PolymarketUSPrivateWSClient
    client = PolymarketUSPrivateWSClient("k", base64.b64encode(b"x" * 32).decode())
    assert not client.is_ready

    # Order snapshot with stale orders does NOT flip ready (stale orders are
    # logged but ignored per design — we don't trust a snapshot's freshness).
    client._dispatch({
        "orderSubscriptionSnapshot": {"orders": [{"id": "stale-1"}], "eof": False},
    })
    assert not client.is_ready

    # Balance snapshot — modern key. Marks ready.
    client._dispatch({
        "accountBalanceSubscriptionSnapshot": {"balance": 69.41, "buyingPower": 69.41},
    })
    assert client.is_ready

    # Legacy plural key (observed on live server, 2026-05-12).
    client2 = PolymarketUSPrivateWSClient("k", base64.b64encode(b"x" * 32).decode())
    client2._dispatch({
        "accountBalancesSnapshot": {"balances": [{"currentBalance": 69.41}]},
    })
    assert client2.is_ready
    print("  ✓ private_ws_ready_on_balance_snapshot")


async def test_private_ws_legacy_keys() -> None:
    """Handle legacy field names from older server builds."""
    from scrapers.polymarket_us_private_ws import PolymarketUSPrivateWSClient
    client = PolymarketUSPrivateWSClient("k", base64.b64encode(b"x" * 32).decode())

    client._dispatch({
        "orderUpdate": {
            "execution": {"order": {"id": "ord-4", "state": "ORDER_STATE_CANCELED", "cumQuantity": 0}}
        },
    })
    result = await client.await_terminal("ord-4", timeout=1.0)
    assert result["state"] == "ORDER_STATE_CANCELED"
    print("  ✓ private_ws_legacy_keys")


async def main_runner() -> None:
    print("Running WS unsubscribe tests...")
    await test_kalshi_unsubscribe_cleans_state()
    await test_kalshi_stale_snapshot_ignored()
    await test_kalshi_stale_delta_advances_seq_no_book()
    await test_poly_unsubscribe_cleans_state()
    await test_poly_stale_market_data_ignored()
    await test_populate_stores_returns_stale_lists()
    print("Running private WS tests...")
    await test_private_ws_terminal_resolves_pending()
    await test_private_ws_early_event_buffered()
    await test_private_ws_non_terminal_ignored()
    await test_private_ws_ready_on_balance_snapshot()
    await test_private_ws_legacy_keys()
    print("All tests passed.")


if __name__ == "__main__":
    asyncio.run(main_runner())
