# Future work

Ranked by leverage. Nothing here is implemented. Ideas that were considered and rejected
live in [findings-rejected.md](findings-rejected.md) — check there before adding one back.

---

## 1. Pre-placed maker bids — the only structural fix

**The ceiling:** 52.6% of 4%+ arbs close in under 85ms, faster than our fastest VPS→Poly
round trip (fill median 74ms). No amount of latency work catches them as a taker. The
strategy is taker-bound by construction.

**The idea:** rest maker bids on Polymarket at expected entry prices for active games.
When the arb opens, we are already in the book — no race at all. This is a shift from
taker to maker, which also takes the Polymarket fee to zero.

**Trade-offs to design around:**
- Capital tied up across many resting orders against a ~$70 balance.
- **Adverse selection** — a resting bid fills precisely when someone wants to hit it, which
  is not necessarily when we want to be filled. This is the real risk and the reason this
  is a project, not a patch.
- Cancel/replace logic and its rate-limit budget.
- Order state must stay coherent with `_in_flight` and the private order WS.

**Why it is #1:** everything else optimizes a path with a hard 52.6% loss rate baked in.

---

## 2. Fix the quantity scaling bug, then measure P(fill) × size

Currently blocked — see
[open-questions.md § Known unfixed bug](open-questions.md#known-unfixed-bug-quantity-scaling).

Optimal order size is **not** max depth; fill rate degrades as size rises and nobody has
measured that curve for these books. Start at `quantity=1` (where we are), fix the maker
sell to use real `cumQuantity` and the taker exits to handle partials, then walk size up
and record fill rate per level.

Worth doing **after** the velocity/loss-truncation work — scaling a negative-expectancy
loop scales the losses.

---

## 3. Truncate the losses

The P&L decomposition says this plainly: **6 price_drop trades lost −$0.80 while 33 trades
netted −$0.97**. Converged exits are 8/8 profitable. The lever is not more winners, it is
smaller losers.

Candidates, cheapest first:
- **Re-tune `price_drop_threshold`** — it was fitted on n=17 and most winners never draw
  down at all, so a tighter stop may cost very little.
- **Tune or replace the velocity filter** once ~20 post-filter fires exist
  ([open-questions.md](open-questions.md#velocity-filter-effectiveness)). Note it already
  has one counter-example.
- **Extend the timeout** — p75 of winning touches is 11.3s against a 15s cutoff, so some
  timeout losses may be winners cut short
  ([open-questions.md](open-questions.md#is-the-15s-timeout-too-short)).

---

## 4. Same-team arb comparison

The detector currently pairs **opposite** teams across platforms (K:TeamA + P:TeamB). For a
single-leg strategy, comparing the **same** team across platforms (K:TeamA vs P:TeamA) may
be the more honest signal — it would prevent buying a team heading to 0c when the
cross-team math shows a "gap."

Requires rethinking the arb detection pipeline, not just a flag. Relevant to the known
weakness that `opener` misfires at game-ending moments.

---

## 5. +1c aggressive bid

Pay 1c over the ask for liquidity priority. An easy A/B once the velocity-filter baseline
is clean.

**Likely marginal** — books move wholesale during the race, not by 1c — but cheap to test
and it directly addresses the 12.5% fill rate on 70c+ buys.

---

## 6. Cheap latency and capacity wins

In the order established when sharding was rejected
([findings-rejected.md](findings-rejected.md#per-sport-sharding-across-multiple-vpss)):

1. Move off the burstable t3.micro to a fixed-performance instance **if** CPU approaches
   the 10% baseline (we run ~7%).
2. `uvloop`.
3. **Skip games whose books cannot produce 4%** — FCS books pinned at 0.01/0.00 burn WS
   subscriptions and produce only junk arbs.
4. Route `executions.csv` through `csv_writer` to get the last synchronous write off the
   event loop ([open-questions.md](open-questions.md#event-loop-lag-32-72ms-at-the-tail)).
5. Discard prop markets **during** JSON parse rather than after — attacks the discovery GC
   pause at its source (events carry ~444 markets each).

---

## 7. Close the remaining monitoring gap

`ALERT_HEARTBEAT_URL` is **still unset**. SNS cannot detect a process that never runs —
only the CloudWatch NetworkIn alarm can, and that takes 15 minutes. A dead-man's-switch
ping (healthchecks.io) is the only mechanism where the **absence** of a signal alerts.

Given this system's history of dying unnoticed for four months, this is cheap insurance.
See [operations.md § Alerting](operations.md#alerting).

---

## 8. Housekeeping worth doing on the next pass

- Make the CFB Polymarket series id dynamic — it is hardcoded `"225"` and will silently
  match zero markets at the 2027 rollover ([sports.md](sports.md)).
- Fix the `elif`-chained health check so "arbs but no attempts" can fire independently of
  private-WS readiness ([open-questions.md](open-questions.md#doccode-drift-found-2026-09-19)).
- Delete the dead modules and dead imports listed in the same section.
