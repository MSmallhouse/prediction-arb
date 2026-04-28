"""
Strategy B executor — single-leg convergence trade on Polymarket US.

When Kalshi opens an arb (reprices first), buy cheap on Poly.
Place maker sell at target. Monitor for convergence or divergence
via event-driven WS updates (not polling).

Exit conditions:
  - Converged: sell target reached
  - Timeout: configurable seconds elapsed
  - Price drop: ask drops configurable cents below buy
  - Error: any order failure

All parameters configurable. Drop threshold (5c) based on limited
convergence_log data (n=17) — re-evaluate with more data.
"""

import asyncio
import csv
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from polymarket_us import PolymarketUS

log = logging.getLogger(__name__)

EXECUTION_LOG = Path("executions.csv")

_FIELDNAMES = [
    "timestamp",
    "game",
    "sport",
    "action",               # BUY, SELL_CONVERGED, SELL_TIMEOUT, SELL_DROP, BUY_FAILED, BUY_ERROR
    "market_slug",
    "intent",
    "buy_price",
    "sell_price",
    "quantity",
    "gross_spread",
    "profit",
    "buy_fee",
    "sell_fee",
    "hold_time_ms",
    "order_id",
    "poly_bid_at_exit",
    "exit_reason",          # converged, timeout, price_drop, no_fill, buy_error
    "error",
]


@dataclass
class ExecutionConfig:
    """Tunable parameters for Strategy B."""
    enabled: bool = False               # master switch
    min_gross_spread: float = 0.04      # minimum arb gross to trigger
    max_minutes_to_pitch: float = 180   # only in-game or close pre-game
    min_poly_depth: float = 1           # minimum contracts at ask
    quantity: int = 1                   # contracts per trade
    sell_target_offset: float = 0.02    # sell at buy_price + this
    timeout_seconds: float = 30.0      # bail after this many seconds
    price_drop_threshold: float = 0.05  # bail if poly_ask drops this much below buy (5c, limited data)
    only_kalshi_opener: bool = True     # only fire when Kalshi opened the arb
    max_trades: int = 1                 # stop executing after this many completed trades (0 = unlimited)


config = ExecutionConfig()

# Active executions — prevent duplicate fires on flickering arbs
_in_flight: set[str] = set()
_trade_count: int = 0

# Event-driven price monitoring: active positions waiting for convergence
# Each entry is an asyncio.Event that gets set when WS updates the price
@dataclass
class _ActivePosition:
    poly_token_id: str
    buy_price: float
    sell_target: float
    price_event: asyncio.Event = field(default_factory=asyncio.Event)


_active_positions: dict[str, _ActivePosition] = {}  # poly_token_id → position


def on_price_update(poly_token_id: str, current_ask: float, current_bid: float) -> None:
    """Called from _on_poly_us_price on every WS tick.
    Wakes up any active position monitoring this token."""
    pos = _active_positions.get(poly_token_id)
    if pos is not None:
        pos.price_event.set()


def _log_execution(row: dict) -> None:
    """Append one row to executions.csv."""
    write_header = not EXECUTION_LOG.exists()
    with EXECUTION_LOG.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


async def maybe_execute(
    client: PolymarketUS,
    opp,
    opener: str,
    arb_key: str,
    poly_by_token: dict,
) -> None:
    """Check if this arb qualifies for execution. If so, fire as background task."""
    if not config.enabled:
        return

    if config.only_kalshi_opener and opener != "kalshi":
        return

    if opp.gross_spread < config.min_gross_spread - 1e-9:
        return

    if opp.poly_market.yes_ask_size < config.min_poly_depth:
        return

    if arb_key in _in_flight:
        return

    now = datetime.now(timezone.utc)
    minutes_to_pitch = (opp.poly_market.game_datetime - now).total_seconds() / 60
    if minutes_to_pitch > config.max_minutes_to_pitch:
        return

    poly_token = opp.poly_market.token_id
    if ":long" in poly_token:
        intent = "ORDER_INTENT_BUY_LONG"
    else:
        intent = "ORDER_INTENT_BUY_SHORT"

    market_slug = poly_token.rsplit(":", 1)[0] if ":" in poly_token else poly_token
    sport = "MLB" if "mlb" in opp.poly_market.event_slug else ("NBA" if "nba" in opp.poly_market.event_slug else "NHL")

    _in_flight.add(arb_key)

    asyncio.create_task(
        _execute_trade(
            client=client,
            arb_key=arb_key,
            game=opp.game_label,
            sport=sport,
            market_slug=market_slug,
            intent=intent,
            buy_price=opp.poly_market.yes_ask,
            gross_spread=opp.gross_spread,
            poly_token_id=poly_token,
            poly_by_token=poly_by_token,
        ),
        name=f"exec-{opp.game_label}",
    )


async def _execute_trade(
    client: PolymarketUS,
    arb_key: str,
    game: str,
    sport: str,
    market_slug: str,
    intent: str,
    buy_price: float,
    gross_spread: float,
    poly_token_id: str,
    poly_by_token: dict,
) -> None:
    """Execute the full Strategy B lifecycle: buy → monitor → sell."""
    now = datetime.now(timezone.utc)
    buy_fee = 0.05 * buy_price * (1 - buy_price)
    sell_target = round(buy_price + config.sell_target_offset, 2)

    log.info(
        "\n\n  STRATEGY B: %s  buy@%.3f  sell_target@%.3f  gross=%.1f%%\n",
        game, buy_price, sell_target, gross_spread * 100,
    )

    # ── Step 1: Buy (IOC at current ask) ────────────────────────────────
    buy_order_id = ""
    try:
        t_start = time.monotonic()
        result = await asyncio.to_thread(
            client.orders.create,
            {
                "marketSlug": market_slug,
                "intent": intent,
                "type": "ORDER_TYPE_LIMIT",
                "price": {"value": str(buy_price), "currency": "USD"},
                "quantity": config.quantity,
                "tif": "TIME_IN_FORCE_FILL_OR_KILL",
            },
        )
        buy_latency = (time.monotonic() - t_start) * 1000

        # create() returns just {id, executions} — need retrieve() for fill status
        buy_order_id = result.get("id", "")
        if buy_order_id:
            order_detail = await asyncio.to_thread(client.orders.retrieve, buy_order_id)
            order = order_detail.get("order", order_detail)
        else:
            order = result

        state = order.get("state", "")
        cum_qty = order.get("cumQuantity", 0)

        log.info("  BUY result: id=%s state=%s cumQty=%s latency=%.0fms",
                 buy_order_id, state, cum_qty, buy_latency)

        if cum_qty == 0 or state in ("ORDER_STATE_CANCELLED", "ORDER_STATE_REJECTED", "ORDER_STATE_NEW"):
            log.info("  BUY did not fill — aborting")
            _log_execution({
                "timestamp": now.isoformat(), "game": game, "sport": sport,
                "action": "BUY_FAILED", "market_slug": market_slug, "intent": intent,
                "buy_price": f"{buy_price:.4f}", "sell_price": "", "quantity": config.quantity,
                "gross_spread": f"{gross_spread:.4f}", "profit": "",
                "buy_fee": f"{buy_fee:.4f}", "sell_fee": "",
                "hold_time_ms": "", "order_id": buy_order_id,
                "poly_bid_at_exit": "", "exit_reason": "no_fill", "error": state,
            })
            _in_flight.discard(arb_key)
            return

    except Exception as exc:
        log.error("  BUY failed: %s", exc)
        _log_execution({
            "timestamp": now.isoformat(), "game": game, "sport": sport,
            "action": "BUY_ERROR", "market_slug": market_slug, "intent": intent,
            "buy_price": f"{buy_price:.4f}", "sell_price": "", "quantity": config.quantity,
            "gross_spread": f"{gross_spread:.4f}", "profit": "",
            "buy_fee": "", "sell_fee": "", "hold_time_ms": "", "order_id": "",
            "poly_bid_at_exit": "", "exit_reason": "buy_error", "error": str(exc),
        })
        _in_flight.discard(arb_key)
        return

    _log_execution({
        "timestamp": now.isoformat(), "game": game, "sport": sport,
        "action": "BUY", "market_slug": market_slug, "intent": intent,
        "buy_price": f"{buy_price:.4f}", "sell_price": "", "quantity": config.quantity,
        "gross_spread": f"{gross_spread:.4f}", "profit": "",
        "buy_fee": f"{buy_fee:.4f}", "sell_fee": "",
        "hold_time_ms": "", "order_id": buy_order_id,
        "poly_bid_at_exit": "", "exit_reason": "", "error": "",
    })

    # ── Step 2: Place maker sell at target ──────────────────────────────
    sell_intent = "ORDER_INTENT_SELL_LONG" if "LONG" in intent else "ORDER_INTENT_SELL_SHORT"
    sell_order_id = ""
    try:
        result = await asyncio.to_thread(
            client.orders.create,
            {
                "marketSlug": market_slug,
                "intent": sell_intent,
                "type": "ORDER_TYPE_LIMIT",
                "price": {"value": str(sell_target), "currency": "USD"},
                "quantity": config.quantity,
                "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
                "participateDontInitiate": True,
            },
        )
        sell_order_id = result.get("id", "")
        log.info("  SELL placed: id=%s target=%.3f (maker)", sell_order_id, sell_target)
    except Exception as exc:
        log.error("  SELL placement failed: %s — will exit at market on timeout", exc)

    # ── Step 3: Event-driven monitoring ────────────────────────────────
    position = _ActivePosition(
        poly_token_id=poly_token_id,
        buy_price=buy_price,
        sell_target=sell_target,
    )
    _active_positions[poly_token_id] = position

    buy_time = time.monotonic()
    exit_reason = "timeout"
    sell_price = 0.0
    current_bid = 0.0

    try:
        while True:
            elapsed = time.monotonic() - buy_time
            if elapsed >= config.timeout_seconds:
                exit_reason = "timeout"
                break

            # Wait for next WS price update (or timeout)
            remaining = config.timeout_seconds - elapsed
            position.price_event.clear()
            try:
                await asyncio.wait_for(position.price_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                exit_reason = "timeout"
                break

            # Check current prices from live store
            market = poly_by_token.get(poly_token_id)
            if market is None:
                continue

            current_bid = market.yes_bid
            current_ask = market.yes_ask

            # Convergence: bid reached sell target
            if current_bid >= sell_target:
                exit_reason = "converged"
                sell_price = sell_target
                break

            # Price drop: ask fell too far below buy
            if buy_price - current_ask >= config.price_drop_threshold:
                exit_reason = "price_drop"
                break
    finally:
        _active_positions.pop(poly_token_id, None)

    hold_time_ms = (time.monotonic() - buy_time) * 1000

    # ── Step 4: Exit ───────────────────────────────────────────────────
    if exit_reason == "converged" and sell_order_id:
        # Verify the maker sell actually filled
        try:
            order_detail = await asyncio.to_thread(client.orders.retrieve, sell_order_id)
            o = order_detail.get("order", order_detail)
            if o.get("cumQuantity", 0) == 0:
                log.info("  Bid reached target but maker sell didn't fill — treating as timeout")
                exit_reason = "timeout"
        except Exception:
            exit_reason = "timeout"

    if exit_reason == "converged":
        sell_fee = 0.0  # maker = free
        sell_price = sell_target
        profit = sell_target - buy_price - buy_fee
        log.info(
            "\n\n  CONVERGED  %s  profit=%.4f  hold=%dms\n",
            game, profit, hold_time_ms,
        )
    else:
        # Cancel maker sell, then market sell at bid
        if sell_order_id:
            try:
                await asyncio.to_thread(
                    client.orders.cancel, sell_order_id, {"marketSlug": market_slug},
                )
            except Exception:
                pass

        market = poly_by_token.get(poly_token_id)
        current_bid = market.yes_bid if market else 0

        if current_bid > 0:
            try:
                await asyncio.to_thread(
                    client.orders.create,
                    {
                        "marketSlug": market_slug,
                        "intent": sell_intent,
                        "type": "ORDER_TYPE_LIMIT",
                        "price": {"value": str(current_bid), "currency": "USD"},
                        "quantity": config.quantity,
                        "tif": "TIME_IN_FORCE_FILL_OR_KILL",
                    },
                )
                sell_price = current_bid
                sell_fee = 0.05 * sell_price * (1 - sell_price)
            except Exception as exc:
                log.error("  EXIT sell failed: %s", exc)
                sell_price = 0
                sell_fee = 0
        else:
            sell_price = 0
            sell_fee = 0

        profit = sell_price - buy_price - buy_fee - sell_fee if sell_price > 0 else -(buy_price + buy_fee)
        log.info(
            "\n\n  EXIT (%s)  %s  sell@%.3f  profit=%.4f  hold=%dms\n",
            exit_reason, game, sell_price, profit, hold_time_ms,
        )

    _log_execution({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "game": game, "sport": sport,
        "action": f"SELL_{exit_reason.upper()}",
        "market_slug": market_slug, "intent": sell_intent,
        "buy_price": f"{buy_price:.4f}",
        "sell_price": f"{sell_price:.4f}",
        "quantity": config.quantity,
        "gross_spread": f"{gross_spread:.4f}",
        "profit": f"{profit:.4f}",
        "buy_fee": f"{buy_fee:.4f}",
        "sell_fee": f"{sell_fee:.4f}" if sell_fee else "0.0000",
        "hold_time_ms": f"{hold_time_ms:.0f}",
        "order_id": sell_order_id or buy_order_id,
        "poly_bid_at_exit": f"{current_bid:.4f}",
        "exit_reason": exit_reason,
        "error": "",
    })

    _in_flight.discard(arb_key)

    # Trade counter — auto-disable after max_trades
    global _trade_count
    _trade_count += 1
    if config.max_trades > 0 and _trade_count >= config.max_trades:
        config.enabled = False
        print(f"\n{'='*60}")
        print(f"  EXECUTOR DISABLED — completed {_trade_count} trade(s)")
        print(f"  Result: {exit_reason}  profit={profit:.4f}")
        print(f"  Check executions.csv for full details")
        print(f"{'='*60}\n")
