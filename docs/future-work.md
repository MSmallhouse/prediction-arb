# Future work

Ranked by leverage. Nothing here is implemented. Ideas that were considered and rejected
live in [findings-rejected.md](findings-rejected.md) — check there before adding one back.

---

## 1. Pre-placed maker bids — the only structural fix

**The ceiling:** **72.9%** of 4%+ arbs close in under 85ms (n=1964, 2026-09-19→22;
re-measured — the long-quoted 52.6% understated it). Median arb life is **36ms** against a
median buy latency of **134ms**, and 59% of our attempts expire. No amount of latency work
catches them as a taker. The strategy is taker-bound by construction, and more so than
this section originally claimed.

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

**Why it is #1 — with one caveat added 2026-09-22:** everything else optimizes a path with
a hard ~73% loss rate baked in. The caveat is that per-trade EV is currently **negative**
(−2.85c over n=33), so a higher fill rate would lose money faster, not slower. Sport mix
([findings-validated.md](findings-validated.md#convergence-rate-is-a-property-of-the-sport-not-of-the-filter-stack))
has to be fixed before winning more races is worth anything.

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
- **Hold to settlement instead of stopping out** — the most aggressive version of
  "smaller losers" is *no forced losers*: keep the +5c maker sell, delete the taker
  stop-out, and let anything unfilled at 15s ride to game resolution. Converts path risk
  into outcome risk, and because the two known blockers on `quantity > 1` are both
  exit-side ([open-questions.md](open-questions.md#known-unfixed-bug-quantity-scaling)),
  a no-exit path sidesteps them entirely. **Gated on** the counterfactual in
  [open-questions.md](open-questions.md#does-the-arb-signal-predict-the-outcome-or-only-the-next-15-seconds-of-price)
  — the whole idea rests on Kalshi being fair value, which is untested, and on our fills
  not being adversely selected, which is the likelier failure.

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

### 7a. Dead-man's switch

`ALERT_HEARTBEAT_URL` is **still unset**. SNS cannot detect a process that never runs —
only the CloudWatch NetworkIn alarm can, and that takes 15 minutes. A dead-man's-switch
ping (healthchecks.io) is the only mechanism where the **absence** of a signal alerts.

Given this system's history of dying unnoticed for four months, this is cheap insurance.
See [operations.md § Alerting](operations.md#alerting).

### 7b. Per-feed staleness, and stop the no-tick email flood

Two defects in the same check, found 2026-09-20 after ~37 alert emails landed overnight.
Both are known and deliberately **not** deployed yet — the memory-leak measurement window
was open and a restart resets it. Batch these with the next deploy.

**The noise.** `_health_problems()` (main.py) fires `no price tick for {N}s — feeds
stalled` at `NO_TICK_ALERT_S = 180`. `_last_tick` only advances on a real book update
(main.py:272 Kalshi, main.py:325 Poly), so between roughly 08:00 and 17:00 UTC — 4am to
1pm ET, no MLB or NHL in play — nobody quotes and the check fires every five minutes. On
2026-09-20 all 37 stall alerts fell in that window and every one was a false positive; the
scanner was healthy throughout (`NRestarts=0`, back to `last tick 1s ago` by 17:12 UTC).

**Why each stall sent its own email.** De-duplication keys on the rendered problem text:

```python
key = " | ".join(sorted(problems))     # main.py, _send_email_alert
```

The text embeds the live second count — `239s`, `364s`, `286s` — so every alert is a new
key and `ALERT_REPEAT_SUPPRESS_S = 3600` never matches. This affects **every** alert
carrying a number, not just this one. Fix: have `_health_problems()` return
`(code, text)` pairs and key suppression on the stable code.

**Do not just delete the check.** It is the only detector for a half-open market-data
socket. `_kalshi_ws_confirmed` and `_poly_ws_confirmed` are cumulative sets that are never
cleared, so `K: 128/128 confirmed` proves a tick arrived *once*, not that the feed is
alive — which also means the `p_conf == 0` discovery check cannot fire once the set is
populated. The dead-man ping (7a) sees a healthy process, and the executor checks only
watch the private trading WS. Nothing else sees a dead feed on a live process.

**The hole it does not cover.** `_last_tick` is global and satisfied by *either* feed. A
dead Polymarket socket during a busy evening is invisible: Kalshi ticks every second,
`_last_tick` stays at 0s, and we silently detect nothing all night against frozen prices.
That is the May–Sep failure shape again, and the current check sleeps through it.

| Condition | Meaning | Today |
|---|---|---|
| Both feeds silent, no games in play | quiet market | emails (noise) |
| Both feeds silent, games in play | real outage | emails (correct) |
| One feed silent, the other ticking | one socket dead | **silent** |

**What to build:** track `_last_kalshi_tick` and `_last_poly_tick` separately and alert on
*divergence* — "Kalshi ticked N times while Poly ticked zero" cannot be explained by a
quiet market, so it is near-zero false positive at any hour, which is what makes it
trustworthy at 4am. Demote global quiet-hours no-tick to log-only, or gate its threshold
on games in play.

**Why this ranks here and not lower:** 30 false alarms a night is not a cosmetic problem.
It trains the operator to ignore the channel that exists to catch
[the silent-failure pattern](incidents.md#the-silent-failure-pattern).

### 7c. memguard and the heartbeat are blind to swap

✅ **Item 1 shipped for memguard in `9846e13` (2026-09-22)** — it now thresholds on
`VmRSS + VmSwap` at 900MB and decays its breach counter instead of zeroing it (the reset
was a second, independent reason it could never fire; see
[incidents.md](incidents.md#2026-09-22-0630-utc-second-oom-kill-and-what-it-corrected)).
**The heartbeat half of item 1 is NOT done** — `main._rss_mb()` still logs RSS alone, so
every `rss` figure in the heartbeat and in these docs remains a lower bound. Items 2 and 3
are untouched.

⚠️ **The premise below is partly superseded.** A second kill on 2026-09-22 measured the
growth as a **linear ~40MB/h leak across two independent processes**, not a discovery
spike — so item 3 ("react faster than hourly") is much less urgent than it looked: an
hourly check has ~11h of headroom against a steady climb. Item 2 (`MemorySwapMax`) is
correspondingly *more* attractive, since swap is what stretches the death out to 1.4GB.

Established by the OOM kill on
[2026-09-20 20:50 UTC](incidents.md#2026-09-20-2050-utc-oom-kill-that-memguard-could-not-see):
the process died at roughly 1.6GB of anon memory (590M RSS peak + 1009M swap peak) while
memguard logged `RSS 367MB < 500MB — no action` fifty minutes earlier and the last
heartbeat read `rss 368MB`.

**Why the threshold cannot work as written.** `arb-scanner-memguard.sh` reads
`ps -o rss=` and `main._rss_mb()` reads `/proc/self/status VmRSS`. Both count only
resident pages. `MemoryHigh=600M` makes the kernel reclaim *before* the 500MB guard
threshold is plausibly reached, and `MemorySwapMax=infinity` lets the evicted pages go to
a 1GB swapfile. RSS therefore flattens out exactly when memory is growing fastest.
Raising the threshold does not fix this — the guard is measuring the wrong quantity.

**Three changes, in order of value:**

1. **Measure RSS + swap.** In memguard, `awk` `VmRSS` and `VmSwap` out of
   `/proc/$pid/status` and threshold on the sum; in `_rss_mb()`, log swap as a second
   field so the heartbeat curve stops lying. Every RSS baseline in these docs is a lower
   bound until this lands.
2. **Set `MemorySwapMax`** to something small (say 128M) in `arb-scanner.service`. Swap is
   what converts a bounded cgroup kill into a 1.6GB death spiral, and it buys nothing for
   a latency-sensitive process — swapped-in pages on the hot path are worse than a
   restart.
3. **React faster than hourly.** The kill went from "healthy" to dead inside one discovery
   cycle. An hourly timer with a two-consecutive-breach rule needs two hours to act and
   cannot see a 50-minute spike at all. Either run the check every 5 minutes (keeping the
   two-breach rule, which then costs 10 minutes) or move the guard in-process onto the
   existing heartbeat, which already samples every 5 minutes.

**Related, and possibly the actual cure:** the burst is discovery parsing markets it
immediately discards — `KXNCAAFGAME: parsed 468 markets from 234 events` to use zero of
them. That is item 5 of [§ 6](#6-cheap-latency-and-capacity-wins), which should be
re-read as a memory fix rather than a latency one.

**Also missing: nothing alerts on a restart.** `Restart=always` recovered in 15s, inside
the NetworkIn alarm's 15-minute window, and no health check asserts on `NRestarts`. The
kill was found by reading the RSS curve by hand. Add `NRestarts > 0 since last check` to
`_health_problems()`, or have `ops.sh status` diff it against a stored value.

---

## 8. Housekeeping worth doing on the next pass

- Make the CFB Polymarket series id dynamic — it is hardcoded `"225"` and will silently
  match zero markets at the 2027 rollover ([sports.md](sports.md)).
- Delete the dead modules and dead imports listed in the same section.
