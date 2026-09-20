# Open questions

Each item states **what we don't know**, **why it matters**, and **the protocol to resolve
it**. An item without a protocol is a wish, not a question.

When one is answered, move it to [findings-validated.md](findings-validated.md) or
[findings-rejected.md](findings-rejected.md) and delete it from here.

---

## Memory leak: source unidentified, only mitigated

**Opened** 2026-09-19. First re-read due 2026-09-22; ideally 2026-09-26.

The 2026-05-15 OOM proved growth to 528MB over ~14 days (**≈38 MB/day**). systemd caps and
the memguard restart make it non-fatal, but nothing has identified **what** leaks.

**Baseline to compare against:** 275MB RSS / 8 tasks at 134 Kalshi + 114 Poly markets
(2026-09-19 19:48 UTC, freshly started).

**Already ruled out — do not re-investigate without new evidence:** `arb_tracker._active`
(popped on close), `convergence_tracker` (`_flush_one` pops tracker + both slug indexes at
60s), `kalshi_ws._books` / `_price_history` / `_sid_tickers` / `_sid_seq` / `_ticker_sid`
(all pruned by `KalshiWSClient.unsubscribe()` on discovery — note: older docs called this
`prune_tickers()`, which does not exist), asyncio task accumulation (`tasks` stayed flat at
8-9 across a 7-minute dry run and live operation).

**Plausible that it is already fixed.** The pre-2026-09-19 discovery code accumulated up to
3 pages (~600) of full event payloads per hourly cycle, and events now carry ~444 markets
each. The rewrite holds one page (~50 events). **Confirm this before chasing anything.**

### Protocol

```bash
# all heartbeat samples, oldest first. delaycompress means scanner.log.1 is NOT
# gzipped, so grepping only *.gz silently skips the most recent full day.
zgrep -h "rss" ~/prediction-arb/scanner.log.*.gz 2>/dev/null
grep  -h "rss" ~/prediction-arb/scanner.log.1 2>/dev/null
grep  -h "rss" ~/prediction-arb/scanner.log
# discovery boundaries to align against (hourly)
grep "Stores:" ~/prediction-arb/scanner.log
```

**The shape is the diagnostic; the absolute number is not:**

| Shape | Means |
|---|---|
| Step up at each hourly `Stores:` line | Discovery/REST path — retained event payloads. Biggest suspect |
| Linear between discoveries | WS tick path — book state, per-tick allocations |
| Sawtooth that recovers | GC churn, not a leak |
| Flat across a full 24h window | Leak died with the discovery rewrite — **close this item** |

**Caveats when reading:** (a) since 2026-09-20 there is **no scheduled restart** — the
process runs until `arb-scanner-memguard` sees RSS >= 500MB, so the curve is continuous for
as long as memory stays flat. A drop means memguard fired (grep `memguard` in the log to
confirm) or a deploy happened; do not read either as a fix;
(b) logrotate runs ~00:49 UTC, so a rotated file can span a restart boundary mid-file;
(c) market count drives baseline RSS, so compare windows with similar `Stores:` counts, not
raw peaks (MLB slate size swings, and NHL/NBA regular seasons start in October).

**Wait at least 5 days of uninterrupted runtime** before drawing conclusions; ~10 days for
a rate you would act on. Because restarts are now memory-triggered rather than scheduled,
**whenever you check you have the whole history back to the last restart** — which, if the
leak really is gone, is the full 30 days logrotate keeps. That was the point of the change:
under the old daily/5-day recycle, checking in just after a recycle gave you hours.

`./deploy/ops.sh heartbeats` prints the curve; `./deploy/ops.sh memguard` shows the timer
and the last few memguard decisions. **The only thing that now truncates the window is our
own deploys** — see the hazard below.

**Next step only if growth is real:** hourly `gc` object-count histogram by type (cheap,
sampled) to name the culprit. `tracemalloc` top-10 by traceback **only** if the histogram
is ambiguous — it costs ~10-25% CPU on the hot path, which is the latency budget the whole
edge depends on. Enable it off-hours.

---

## Event-loop lag: 32-72ms at the tail

Measured 2026-09-19, immediately after the hot-path rework. Steady-state heartbeats
reported `loop lag max 72.0ms` then `32.2ms` per 5-minute window; the first window showed
2245ms but that includes startup/discovery.

**Why it matters:** against a ~60-74ms fill latency, a 30-70ms stall is not noise — it is
comparable to the entire round trip.

It is **not detection** any more (0.009ms/tick) and **not GC** in steady state (zero
collections). Prime remaining suspects, in order:

1. **Burst WS frame processing** — real work, not a stall. Would need less per-message work
   or true parallelism, not GC tuning.
2. Synchronous `log.info`/`print` writes to `scanner.log` on the tick path.
3. **`executions.csv` is still written synchronously on the event loop** — `executor.py`
   uses a plain `open()` in `_log_execution`, bypassing `csv_writer`. Only
   `arb_durations_*.csv` and `convergence_log.csv` were moved off-loop. This is a concrete,
   fixable instance of suspect 2. **Cheapest thing to try first.**
4. The hourly discovery cycle and the 5s convergence flush building many rows at once.

**Protocol:** log which coroutine was running when lag spikes, or sample with asyncio debug
mode. Route `executions.csv` through `csv_writer` and re-measure the tail.

**Do not add sports capacity assuming headroom until this is understood.**

GC attempts already made and their results:
[findings-rejected.md § GC tuning](findings-rejected.md#gc-tuning--three-attempts-none-fixed-the-pauses).
Note the unfinished measurement there — `gc.freeze()`'s effect on the *hourly discovery*
window has never actually been measured.

---

## Velocity filter effectiveness

`kalshi_velocity_threshold = 0.05` over a 2.0s window was fitted to the 4 gap losses of
2026-05-13, all of which it would have caught. Whether it **generalises** is unknown, and
there is already one counter-example.

**Counter-example (2026-09-19):** MIL@BAL gapped ~9.5c through the 5c stop **despite
passing** the filter. Loss −$0.1174.

**Why it matters:** price_drop exits are 0 W / 6 L and account for −$0.80 of a −$0.97 total
P&L. Loss truncation, not win rate, is the improvement lever
([performance-log.md](performance-log.md#the-pl-decomposition-is-the-whole-story)).

**Protocol:** collect ~20 post-filter fires. Compare the `k_vel=Xc` values logged at fire
time against outcomes. Tune **up** if we are blocking good trades; tune **down** only with
evidence of more gap losses. Also count *skips* — a filter that blocks everything looks
identical to a quiet market (the third silent failure of 2026-09-19).

---

## Is CFB tradeable?

Opened 2026-09-19; currently detect-only
([sports.md § Detect-only](sports.md#detect-only)). Two things to learn before enabling:

**(a) Arb frequency.** CFB arbs *do* appear — `CENCON @ MONST gross=5.0%`,
`YALE @ HOLY gross=4.0%` on 2026-09-19 ~21:00 UTC. The earlier "zero CFB arbs in 7 minutes"
was simply too short a sample. **Protocol:** count `,CFB,` rows in `arb_durations_4.csv`
over a full Saturday slate.

**(b) Velocity threshold.** 5c/2s was fitted to baseball and hockey tick sizes. A touchdown
moves a football line far more than 5c in 2s, so the filter may block **every** real CFB
signal, or fail to block gap risk. **Protocol:** compare `k_vel=` values on CFB arbs
against their subsequent price paths before trusting it.

Also check for **junk arbs against near-zero books** — many CFB books (especially FCS) sit
at 0.01/0.00 once a game is decided. `min_buy_price = 0.15` filters most, but verify.

⚠️ Exclude 2026-09-19 20:32–21:07 UTC from any analysis: CFB arbs in that window are
mislabeled `MLB` ([incident](incidents.md#2026-09-19-cfb-arbs-mislabeled-as-mlb)).

---

## Is the 15s timeout too short?

Convergence re-analysis (n=154 successful +5c touches) gives a **median time to first touch
of 4256ms but a p75 of 11.3s**. At a 15s cutoff we are trimming close to the tail of the
winning distribution.

**Why it matters:** timeout exits are 5 W / 14 L and bleed −$0.48. Some of those may be
winners cut off early rather than genuine losers.

**Protocol:** re-run the convergence simulation at 15s / 20s / 30s / 45s horizons and
compare hit rate against the adverse price drift over the extra hold time. This is a pure
data question — no live trading needed. Caveat: entry price in the simulation is the logged
`poly_ask` at t=0, not an actual fill.

---

## MLB Kalshi-opener instability

MLB's Kalshi-opener share is 50.6% overall but ranges **31.6% to 100% by day**. NHL is
stable at 79-100%. `only_kalshi_opener = True` gates every trade, so on a 31.6% day we
discard two-thirds of MLB arbs.

**Why it matters:** the filter has demonstrated predictive value (52.6% vs 15.0%
convergence hit rate), so this is not "remove the filter" — it is "understand why the rate
moves." A plausible explanation is that `opener` measures *which platform ticked most
recently*, not which diverged from fair value, and the answer depends on relative feed
activity that varies by slate.

**Protocol:** bucket opener share by slate size, time of day and game state. Check whether
the low-share days had worse or merely fewer outcomes.

---

## Unexplained latency regression on 2026-05-14

Fill latency median jumped 63ms → 97ms between 05-13 and 05-14, with non-fill median
79ms → 109ms. Never explained. Possible: the IOC retry-loop deploy that day, Polymarket-side
change, or instance CPU credit depletion.

**Protocol:** check CloudWatch `CPUCreditBalance` for 05-14 if the metric is still retained,
and diff the deploys made that day.

---

## NBA slug derivation for 2026-27

Untestable until Polymarket creates the `nba-2026` series (~Oct 20 2026). Protocol and the
specific abbreviations at risk: [sports.md](sports.md#nba-slug-derivation-is-unverified-for-2026-27).

---

## WS staleness vs fill rate

Polymarket WS age at fire is median 157ms for fills vs 296ms for non-fills (max 159s).
Correlation is real; causation is not established — stale may mean *quiet* rather than
*wrong* (BUF@MON won on a 71s-stale price).

**Protocol:** control for market activity. If stale-but-quiet fills fine, the signal is
useless as a filter; if stale predicts non-fill independently, add it as gate 12.

---

## Known unfixed bug: quantity scaling

⚠️ **Blocks every experiment that requires `quantity > 1`, including the P(fill) × size
curve.**

- The **maker sell uses `config.quantity`, not the actual `cumQuantity`** from the buy fill.
- The **taker-exit IOC retries each assume a full fill at `target_bid`**.

Both hold at `quantity = 1` and break the moment it rises. Note the buy is **FOK**, so the
buy itself cannot partially fill today — the exposure is on the exit side and on any future
switch of the buy to IOC.

**Fix before raising quantity above 1.**

---

## Doc/code drift found 2026-09-19

A code audit found twelve places where the documentation described something the code does
not do. Most are corrected in place in the docs; these remain as **code** cleanups worth
doing:

| Drift | Status |
|---|---|
| `executions.csv` written synchronously on the loop | Real gap — see event-loop lag above |
| `arb_detector.find_arbs` imported by `main.py` but never called | Dead import |
| `config.MIN_GROSS_SPREAD` imported by `main.py`, never used | Dead import |
| `main._gc_was_enabled` assigned, never read | Vestige of the reverted `gc.disable()` experiment |
| `main._gc_disabled_at` never set non-zero — the "GC left disabled" watchdog **can never fire**, and its comment describes code that no longer exists | Misleading dead watchdog |
| `csv_writer._dropped` never incremented or read; `flush()` spawns a waiter thread but never enqueues the `None` shutdown sentinel `_worker` checks for | Unfinished |
| ~~Health check "arbs at 4%+ with zero attempts" `elif`-chained behind the private-WS check~~ | **FIXED 2026-09-19** — the two checks are now independent |
| `arb_detector.poly_fee()` docstring and the `config.py` comment both say 0.03 while the constant is 0.05 | Stale comment |
| Maker-fee formulas (`kalshi_maker`, `poly_maker`) exist in docs only — no constants in code | See [architecture.md § Fee model](architecture.md#fee-model) |
| Per-sport fee coefficients (the claimed CFB 0.0695) do not exist | See [sports.md](sports.md#cfb-fees) |
| Executor hardcodes `0.05` inline rather than importing `config.POLY_SPORTS_FEE_COEFF` | Duplication risk |
| `arb_tracker.DURATION_LOG_FILE` default `arb_durations.csv` is never used (main always passes `_3`/`_4`) | Harmless |
| Dead modules: `scrapers/polymarket_ws.py`, `scrapers/kalshi_orders.py`, the gamma/CLOB half of `scrapers/polymarket.py`, `test_ws_unsubscribe.py` | Candidates for deletion |
| `reconcile.short_position()` is a no-op passthrough | Harmless |

No `TODO`/`FIXME`/`XXX` comments exist anywhere in the repo.

---

## ⚠️ Our own restarts are destroying the memory-leak measurement

Not a question so much as a standing hazard. The leak protocol above needs **72h / 3
recycle windows** of uninterrupted runtime. Every deploy restarts the service and resets
RSS to its cold baseline, so the window starts over.

On 2026-09-19/20 the service was restarted **four times in under an hour** (three deploys
plus the reconciliation verification). Useful work, but it means the leak measurement has
effectively not started yet.

**Rule going forward:** before deploying anything non-urgent, check how long the current
window has run (`./deploy/ops.sh status` shows `ActiveEnterTimestamp`). If a measurement
window is in progress and the change can wait, let it run. Batch small changes into one
deploy rather than several.

The measurement window in progress as of 2026-09-20 00:24 UTC is the first real one.

---

## Health check counted detect-only sports — FIXED 2026-09-20

Recorded because it is a good example of one fix exposing the next.

Un-chaining the `elif` (earlier the same night) let the "N arbs at 4%+ but zero execution
attempts" check fire for the first time. It immediately produced a **false positive** at
00:44:55 UTC: 34 arbs at 4%+ since restart, of which **20 were CFB** — a detect-only sport
that can never produce an execution attempt by design.

So the check was mis-specified: it counted every 4%+ arb, then complained that none of them
executed. **Adding any detect-only sport guaranteed the alert would fire on a healthy
night.** Nobody had seen it because the `elif` had been masking the check entirely.

Fixed by counting only arbs whose sport is *not* in `executor.config.excluded_sports`,
using `arb_tracker._sport()` so the health check and the CSV can never disagree about what
sport an arb is.

**The general lesson:** an alert that counts a population larger than the one the guarded
behaviour applies to will cry wolf, and a crying-wolf alert on a system with this outage
history is worse than no alert — it trains the operator to ignore the channel that matters.
When adding a health check, make the denominator match exactly what the check asserts.
