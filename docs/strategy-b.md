# Strategy B — single-leg convergence

**Verified against code 2026-09-19.** Values below are what `executor.py` actually does,
not what older notes claimed. Discrepancies found in that audit are listed in
[open-questions.md § Doc/code drift](open-questions.md#doccode-drift-found-2026-09-19).

---

## The idea

We do **not** trade both legs. When Kalshi opens a gap against Polymarket, we buy the
cheap side **on Polymarket only** and sell into the convergence. It is a directional bet
with an arb-shaped signal — the arb is the *entry trigger*, not the *position*.

Why one leg: the two-leg arb closes faster than we can cross both books, and Kalshi is a
read-only key for us anyway ([platforms.md](platforms.md#kalshi)).

Validation: **77% counterfactual win rate** — see
[findings-validated.md](findings-validated.md#the-signal-itself-strategy-b-is-validated).

---

## Entry gates, in execution order

`maybe_execute()` applies these in order. **Every one is a silent `return`** — which is
exactly the failure mode that hid the 2026-09-19 evening outage
([incidents.md](incidents.md#2026-09-19-evening-the-executor-was-silently-disabled)).

| # | Gate | Value |
|---|---|---|
| 1 | Executor enabled | dataclass default `False`; forced `True` at `main.py:534` |
| 2 | Private order WS ready | else cannot resolve fills at all |
| 3 | Kalshi was the opener | `only_kalshi_opener = True` |
| 4 | Sport not excluded | `excluded_sports = {"CFB"}` |
| 5 | Gross spread | `>= 0.04` |
| 6 | Polymarket ask depth | `>= 1` (`min_poly_depth`) |
| 7 | Buy price band | `0.15 <= yes_ask <= 1.00` |
| 8 | Kalshi velocity | `< 0.05` over a 2.0s window |
| 9 | Not already in flight | `arb_key not in _in_flight` |
| 10 | In-game window | `minutes_to_pitch <= 180` |
| 11 | **Pre-buy recheck** | recompute gross from live WS stores; abort if it evaporated |

Gate 11 fires inside `_execute_trade`, immediately before `create()`, and re-derives
`buy_price` / `order_price` / `sell_target` from the *current* ask. Zero latency — dict
lookups only.

Note gate 8 is checked **before** `_in_flight.add`, so a velocity-filtered ticker stays
retryable on its next open.

---

## Order mechanics

- **Buy: FOK**, `ORDER_TYPE_LIMIT` + `TIME_IN_FORCE_FILL_OR_KILL`, quantity 1, dispatched
  via `asyncio.to_thread`. (Older notes called this IOC — it is not. At quantity 1 FOK and
  IOC are equivalent; they diverge the moment quantity rises.)
- **Terminal state** from `await_terminal(timeout=1.0)`. On timeout → `_check_position_fallback`
  REST read of `portfolio.positions()`.
- **Maker sell: GTC** limit at `buy + 0.05`, with `participateDontInitiate: True` so it
  can only rest, never cross. Polymarket maker fee is 0.
- **SHORT price inversion** applies to both the buy and the maker sell — send
  `1 - yes_ask` / `1 - sell_target`. See [platforms.md](platforms.md#traps--order-placement).
- Monitoring is **event-driven**: every Polymarket tick calls `on_price_update()`, which
  sets an `asyncio.Event` the monitor is awaiting. No polling.

---

## Exit paths

| Exit | Trigger | Mechanics |
|---|---|---|
| `SELL_CONVERGED` | `yes_bid >= sell_target` | Verified via `await_terminal(sell_order_id)`. **`cumQuantity == 0` or timeout downgrades it to `timeout`** — a converged row means a real maker fill. Profit = `sell_target - buy_price - buy_fee`; sell fee 0 |
| `SELL_TIMEOUT` | 15s elapsed | Cancel maker sell, then taker out |
| `SELL_PRICE_DROP` | `buy_price - yes_ask >= 0.05` | Cancel maker sell, then taker out |
| `SELL_EXIT_FAILED` | all taker retries failed | ERROR log, appended to `_stuck_positions`, real failure state written to the `error` column. **Position is still open — close it manually** |

**Taker exit retry loop:** up to `MAX_TAKER_EXIT_ATTEMPTS = 3` IOC attempts, each chasing
the bid down 1c (`target_bid = bid - attempt*0.01`), each verified by `await_terminal`.
Break conditions `NO_BID` / `BID_TOO_LOW`. This exists because FOK exits used to log
fictional fills — [incidents.md](incidents.md#2026-05-14-fok-taker-exits-logged-fictional-fills).

The trade counter increments **only on a converged exit**. `max_trades` is 0 (unlimited)
in production, so it never disables anything — but the dataclass default is **1**, so
anything importing `executor` without running `main.py` gets single-trade behaviour.

---

## Tunables and why they are where they are

| Parameter | Value | Rationale | Confidence |
|---|---|---|---|
| `sell_target_offset` | 5c | Optimal from n=70 convergence data. Higher = more profit per fill, lower fill rate | Medium — re-fit with more data |
| `timeout_seconds` | 15.0 | The convergence window passes by ~15s; longer lets price drift against us | Medium |
| `price_drop_threshold` | 5c | From n=17 drawdown data. Most winners never draw down at all | **Low — small n** |
| `min_gross_spread` | 0.04 | Minimum gross to trigger execution | Medium |
| `only_kalshi_opener` | True | Kalshi opens 77-83% of arbs and those converge more often | High |
| `min_buy_price` | 0.15 | Below 15c the team is heading to 0c and "convergence" means downward | High |
| `max_buy_price` | 1.00 | **No effective cap.** Buying 90c+ teams heading to 100c is profitable — asymmetric, more room up than down (5/5 profitable historically) | Medium |
| `kalshi_velocity_threshold` | 0.05 | Blocks game-scoring events that gap through the stop | **Low — unvalidated, see below** |
| `kalshi_velocity_window_s` | 2.0 | Same | Low |
| `quantity` | 1 | **Blocked from rising** by the scaling bug — [open-questions.md](open-questions.md#known-unfixed-bug-quantity-scaling) | — |
| `max_minutes_to_pitch` | 180 | Only in-game / near-game markets have the liquidity | High |

**Do not re-tune the signal parameters before fill rate is solved.** The counterfactual
analysis says the signal is fine and throughput is the constraint.

---

## Known weaknesses

- **`opener` means "which platform ticked most recently", not "which diverged from fair
  value."** Right ~88% of the time; fails at game-ending moments when both platforms race
  to 0c/100c. The 15c price floor mitigates it.
- **Gap risk cannot be fully eliminated.** The velocity filter blocks fast-moving entries,
  but MIL@BAL on 2026-09-19 gapped 9.5c through the 5c stop *despite passing the filter*.
  The only real defence is not buying into fast markets at all.
- **Cross-team, not same-team, comparison.** The detector pairs opposite teams across
  platforms (K:TeamA + P:TeamB). For a single-leg strategy, comparing the *same* team
  across platforms may be the more honest signal — see
  [future-work.md](future-work.md#4-same-team-arb-comparison).
- **Every exit path assumes we must sell.** Holding to game settlement is never
  considered, so a position stopped out at −9.5c on a team that goes on to win is booked
  as a loss that resolution would have erased. Whether that is a real opportunity depends
  on an untested premise — that Kalshi is fair value — measured by
  [open-questions.md](open-questions.md#does-the-arb-signal-predict-the-outcome-or-only-the-next-15-seconds-of-price).
- **72.9% of 4%+ arbs close in under 85ms** (n=1964, 2026-09-19→22; earlier docs said 56%
  and 52.6% — both understated it). Median arb life 36ms vs our 134ms median buy latency.
  That is a hard structural ceiling on any taker-side strategy.
- **Convergence is a property of the sport.** Within-15s convergence runs 70.5% CFB /
  62.2% MLB / **32.8% NHL**, on median book depths of 51 / 18 / 11. Treating it as one
  number hid the fact that most of our volume was going into the worst sport. →
  [findings-validated.md](findings-validated.md#convergence-rate-is-a-property-of-the-sport-not-of-the-filter-stack)
