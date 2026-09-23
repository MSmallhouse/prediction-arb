# Approaches that worked

Each entry has the measurement that justified keeping it. If a change is here without
numbers, it is in the wrong file.

---

## The signal itself: Strategy B is validated

**Counterfactual win rate 77%** (Apr 30 – May 1 VPS run). Of 39 `BUY_FAILED` arbs that had
convergence data, **30 would have reached `buy_price + 5c` within 15s** if they had
filled. The arbs we missed and would have lost on would have lost only a few cents each;
the winners showed dramatic upside (0.30 → 0.51, 0.53 → 0.99 within 15s).

**Implication:** the strategy is not the bottleneck — **fill rate is**. Do not spend
optimization effort re-tuning `min_gross_spread`, `sell_target_offset` or
`only_kalshi_opener` until fill rate is solved. See
[strategy-b.md](strategy-b.md) for the parameters and
[future-work.md](future-work.md) for the fill-rate roadmap.

---

## Private order WebSocket

Replaced `synchronousExecution` (2026-05-12). `client.orders.create()` now fires async and
`PolymarketUSPrivateWSClient.await_terminal(order_id, timeout=1.0)` resolves the outcome.

**Measured: fill rate 5.5% → 27.8%** (n=54 attempts, 15 fills, 2026-05-13). Fill latency
median 70-85ms → **60ms**, max 460ms → **226ms**.

The unlock was not speed — it was **unblocking retries**. `_in_flight` no longer sits
locked for 100-460ms on a failed order, so the executor can attempt the next open of the
same arb. Buffers events that arrive before the executor registers its waiter (race fix).
Falls back to `portfolio.positions()` if no terminal event lands within 1s.

---

## Incremental detection

`_check_arbs()` used to rebuild both index dicts and rescan every game on every tick.
Because tick rate *also* scales with slate size, total CPU grew roughly **quadratically**.
`arb_detector.evaluate_event()` now re-prices only the game that ticked, into
`main._arb_cache` (event_ticker → opportunities).

Per-tick detection cost (dev machine; t3.micro is slower, ratios hold):

| Games | Old full scan | New per-event | Speedup |
|---|---|---|---|
| 67 | 0.099 ms | 0.003 ms | 29x |
| 96 | 0.142 ms | 0.004 ms | 35x |
| 291 | 0.433 ms | 0.009 ms | 47x |

**The point is the flatness**, not the ratio: cost barely grows with slate size, so adding
sports is now close to free.

⚠️ **Invariant:** the tracker must still receive the **flattened full set** of arbs.
`ArbTracker.update()` treats any arb missing from the list as CLOSED. Pass
`changed_events=None` for a full rescan (startup, post-discovery).

---

## CSV writes moved off the event loop

`log_arb_duration()` and the convergence flush were doing synchronous `open()`/`write()`
from inside the tick path — one blocking call stalls every other market.

`csv_writer.py` owns a daemon thread and a queue. Rows are still **built on the loop** (so
they capture prices at the instant they were true) and only the I/O is handed off.
`convergence_tracker.flush_expired()` also moved off the tick path onto
`_housekeeping_loop()` (5s cadence; trackers expire on a 60s horizon).

⚠️ **SIGTERM must drain the queue.** Rows now live briefly in a daemon-thread queue, and
`systemctl restart` sends SIGTERM, which does **not** raise `KeyboardInterrupt`. Without
`_install_sigterm_handler()` every restart — including a memguard restart —
silently discards queued rows.

---

## Combined hot-path result

Same CFB load, before vs after the three changes above: total CPU **8.1-8.3% → 6.7-7.1%**.

Much smaller than the 30-47x detection speedup, because detection was never the bulk of
CPU — WS parsing, JSON decode, TLS and logging dominate. **The real win is that CPU no
longer scales quadratically with sport count.**

---

## VPS colocation

Moving from home to AWS EC2 us-east-1, co-located with Polymarket origin:

| Leg | From home | From VPS |
|---|---|---|
| Polymarket | 5-6 ms | **~1.2 ms** |
| Kalshi | 18-19 ms | **~0.8 ms** |

Viable-arb share (arbs alive long enough for a buy to fill) went **37% → 66%**.

---

## Kalshi velocity filter

`KalshiWSClient` records `yes_ask` history in a 5s deque per ticker on every
price-changing tick. `yes_ask_velocity(ticker, window_s=2.0)` returns the max absolute
change in the window. The executor skips the arb if velocity ≥ 5c.

**Justification:** all 4 price_drop losses on 2026-05-13 gapped straight through the 5c
stop in a single WS tick — CHC@ATL -21c, DET@NYM -11c, KC@CWS -8c. These are
**game-scoring events**, where the "arb" is a speed difference between platforms, not a
mispricing. CHC@ATL moved 5c in 1s between two consecutive attempts; the filter trips
exactly there.

Checked **before** `_in_flight.add`, so filtered tickers remain retryable. Logs `k_vel=Xc`
on every fire line for post-hoc analysis.

⚠️ **Not yet confirmed effective at scale**, and explicitly unvalidated for football —
see [open-questions.md](open-questions.md#velocity-filter-effectiveness) and the first
counter-example (MIL@BAL gapped 9.5c *despite passing* the filter) in
[performance-log.md](performance-log.md).

---

## Pre-buy gross recheck

Immediately before `create()`, re-read live Kalshi + Poly prices from the WS stores and
recompute gross. Skip if the arb has evaporated since detection. **Zero latency cost** —
dict lookups only. Prevents buying on stale signals.

---

## IOC + verified taker exits

See [incidents.md](incidents.md#2026-05-14-fok-taker-exits-logged-fictional-fills) for why.
IOC, capture `order_id`, `await_terminal()` for real `cumQuantity`, up to 3 attempts
chasing the bid by 1c. Establishes the invariant that a logged exit is a real exit.

---

## Health checks that assert on meaning

Process liveness would have caught **none** of the six silent failures. The checks that
would have are listed in [operations.md § Alerting](operations.md#alerting): confirmed
Poly market count, executor armed, arbs-with-zero-attempts, feed staleness, CSV backlog,
stuck positions.

---

## Generating the Kalshi API key locally

Kalshi's "Create API key" dialog accepts an **optional RSA public key**. Supplying one
means Kalshi never generates or displays a private key — it never leaves the machine.
Use this flow on every rotation. Details: [operations.md](operations.md#kalshi-api-key-rotation).

---

## Convergence rate is a property of the sport, not of the filter stack

Measured 2026-09-22 by replaying every arb window since the revive through the live gate
stack (`only_kalshi_opener`, `gross >= 4%`, `buy >= 0.15`, target `entry + 5c` per
`sell_target_offset`) and asking whether `poly_bid` ever reached target inside the 15s
timeout. Mislabelling window 2026-09-19 20:32-21:07 UTC excluded.

| Sport | Converges within 15s | Median Poly depth | Median time to converge |
|---|---|---|---|
| CFB | **70.5%** (86/122) | 51 | 457ms |
| MLB | 62.2% (51/82) | 18 | 661ms |
| NHL | **32.8%** (22/67) | 11 | 4480ms |

Two things this settles:

**The documented 67.9% reproduces — for MLB and CFB.** It was never a whole-system
constant. NHL runs at half that rate on a third the depth, and its median time to converge
(4.5s) is 7x MLB's, with p75 at 21.2s — past the 15s timeout entirely.

**This is the dominant P&L term, ahead of fill rate and latency.** Realized results track
the replay: NHL was 24 of 33 closed trades and −$0.73 of the −$0.94, converging 1/24;
MLB was 4/9 converged at −$0.21. We were putting 73% of volume into the worst sport.

Note this inverts the standing "fill rate is the bottleneck" thesis. That held while
per-trade EV was positive. At −2.85c/trade a higher fill rate loses money faster — sport
mix binds first.

---

## Discovery silently reset two WS-derived fields every hour

`_populate_stores` rebuilds each `PolymarketMarket` from REST on the hourly discovery pass
and copied only `yes_ask`/`yes_bid` onto the new object. Two fields fell back to their
dataclass defaults every cycle (fixed 2026-09-22):

- **`yes_ask_size` -> 0.0.** Trips the `min_poly_depth` gate in `executor.py`, blocking
  every trade on a market until its next WS tick. Fails closed and quiet — the house
  pattern. Impact was small in practice only because active markets re-tick in ~203ms
  median; fires are evenly spread across the hour, so it self-healed before it could bite.
- **`fetched_at` -> now.** Made `poly_ws_age_ms` read *fresh* for a market that had not
  ticked in an hour. **Every staleness figure measured before 2026-09-22 is a floor, not
  an age** — including the "median 157ms for fills vs 296ms for non-fills" result that
  [open-questions.md § WS staleness vs fill rate](open-questions.md#ws-staleness-vs-fill-rate)
  is built on. Re-measure before trusting it as a gate.

Neither was found by reading the tick path, which is correct. Both are in the *discovery*
path, which only runs hourly and leaves no log line when it clobbers live state.
