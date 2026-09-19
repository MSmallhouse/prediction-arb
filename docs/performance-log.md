# Performance log

Every number here carries its `n`, its date window, and the conditions it was measured
under. A number without those is an opinion, not a measurement.

**Canonical data source:** `vps_pull_20260919/` — the local `executions.csv` and
`arb_durations_*.csv` in the repo root are strict **subsets** of it (verified by key-join:
69/69, 855/855, 1377/1377 rows present). Use the VPS pull for anything analytical. The
exception is `convergence_log.csv`, which exists only locally and was never re-pulled.

---

## How to read the data files without getting it wrong

These traps have each corrupted an analysis at least once:

1. **Every arb writes TWO rows** — an OPEN row (`closed_at` empty) and a CLOSE row
   (`closed_at` + `duration_seconds` filled). **Analyse CLOSE rows only.** A naive
   `wc -l` doubles every count.
2. **`arb_durations_3.csv` and `_4.csv` are independent logs, not nested.** Each records
   an arb at the moment it crossed *that* threshold. Of 2081 closed 3%+ arbs, only 531
   share a `first_seen` with the 1098 closed 4%+ arbs, and 567 of the 4%+ arbs appear in
   no 3%+ row at all. **Never union or subtract them.**
3. **Always quote medians, never means.** 81 arbs lasted >60s and 23 lasted >600s; the max
   is KC@CWS at **11,082s (3h05m) at 5% gross**. MLB mean duration is 70.75s against a
   median of 0.087s.
4. **`historical-data/executions_apr30-may01_vps_baseline.csv` changes schema mid-file** —
   the header declares 21 columns, rows from line 25 on carry 22 (`poly_depth` was
   appended live). A default `pd.read_csv` **silently discards 34 of 57 rows**. Read with
   explicit 22-column names.
5. All three CSVs join on `arb_id` = the `first_seen` ISO timestamp.

---

## Data coverage

| File | Data rows | First | Last |
|---|---|---|---|
| `vps_pull_20260919/executions.csv` | 144 | 2026-05-12 22:57Z | 2026-05-15 03:54Z |
| `vps_pull_20260919/arb_durations_4.csv` | 2197 (1099 OPEN / 1098 CLOSE) | 2026-04-30 04:55Z | 2026-05-15 12:01Z |
| `vps_pull_20260919/arb_durations_3.csv` | 4166 (2085 / 2081) | 2026-04-30 04:46Z | 2026-05-15 13:56Z |
| `convergence_log.csv` (local only) | 36,017 ticks / **362 unique arbs** | 2026-04-30 04:46Z | 2026-05-13 04:43Z |

**The files end where the process died.** The last 3%+ row is 2026-05-15 13:56:15Z; the
OOM kill was 14:08:43Z. See [incidents.md](incidents.md#2026-05-15--2026-09-19-the-four-month-outage).

Threshold confirmed from the data itself: `_3` min gross = 0.0300, `_4` min = 0.0400, max
observed 0.4100.

---

## Execution results — 2026-05-12 → 05-15 (n=144 rows, 111 attempts, 33 fills)

**Fill rate 29.7%. Net P&L −$0.9744 across 33 closed trades. 13 W / 20 L (39.4%).**

Action totals: `BUY_FAILED` 78, `BUY` 33, `SELL_TIMEOUT` 19, `SELL_CONVERGED` 8,
`SELL_PRICE_DROP` 6. Zero `BUY_ERROR`, zero `SELL_EXIT_FAILED`. Every BUY has exactly one
SELL (33 = 19+8+6).

### By session

| Date | Attempts | Fills | Fill rate | Net P&L | W/L | Exits |
|---|---|---|---|---|---|---|
| 2026-05-12 | 17 | 3 | 17.6% | −$0.0177 | 2/1 | conv 2, drop 1 |
| 2026-05-13 | 43 | 14 | 32.6% | −$0.4086 | 6/8 | timeout 6, conv 5, drop 3 |
| 2026-05-14 | 45 | 15 | 33.3% | −$0.5657 | 4/11 | timeout 12, drop 2, conv 1 |
| 2026-05-15 | 6 | 1 | 16.7% | +$0.0176 | 1/0 | timeout 1 |
| **Total** | **111** | **33** | **29.7%** | **−$0.9744** | **13/20** | |

### The P&L decomposition is the whole story

| Exit reason | n | Sum P&L | Median | Min | Max | W/L |
|---|---|---|---|---|---|---|
| **converged** | 8 | **+$0.3069** | +$0.0384 | +$0.0375 | +$0.0389 | **8 W / 0 L** |
| timeout | 19 | −$0.4767 | −$0.0244 | −$0.0771 | +$0.0229 | 5 W / 14 L |
| **price_drop** | 6 | **−$0.8046** | −$0.1106 | −$0.2321 | −$0.0942 | **0 W / 6 L** |

**Converged exits are 8/8 profitable and remarkably tight** (+3.75c to +3.89c — the 5c
target net of the 1.1-1.25c taker buy fee). The signal works when the market behaves.

**All the loss is in price_drop (−$0.80) plus timeout bleed (−$0.48).** Six trades produced
more loss than 33 trades produced total gross. This is the entire case for the velocity
filter ([findings-validated.md](findings-validated.md#kalshi-velocity-filter)) and it means
**the improvement lever is loss truncation, not win rate.**

Fees: buy_fee $1.5733 total, sell_fee $0.2481 total — i.e. **fees alone exceed the gross
edge** at this trade count and size.

⚠️ **The timeout P&L is an upper bound.** All 19 `SELL_TIMEOUT` rows have `sell_price`
exactly equal to `poly_bid_at_exit` — the assumed-fill signature. That is consistent with
*both* the pre-05-14 unverified-FOK bug **and** the post-fix IOC retry loop (which logs
`target_bid`), so this file **cannot distinguish verified from assumed fills**. There are
zero `SELL_EXIT_FAILED` rows, so the unsold positions found by reconciliation are invisible
here. See [incidents.md](incidents.md#2026-05-14-fok-taker-exits-logged-fictional-fills).

### Latency (ms)

| Group | n | Median | Mean | p90 | Max |
|---|---|---|---|---|---|
| Fills | 33 | **74** | 109 | 224 | 323 |
| Non-fills | 78 | 86 | 154 | 293 | 1071 |

Per date (median fills / median non-fills): 05-12 60/73 · 05-13 63/79 · **05-14 97/109** ·
05-15 323/86 (n=1).

⚠️ **Unexplained regression on 05-14**: fill median went 63ms → 97ms. Cause unknown; see
[open-questions.md](open-questions.md#unexplained-latency-regression-on-2026-05-14).

### Fill rate cuts

| Cut | n | Fill rate |
|---|---|---|
| buy_price ≤ 0.30 | 16 | 37.5% |
| 0.30 – 0.50 | 41 | 34.1% |
| 0.50 – 0.70 | 38 | 28.9% |
| **0.70 – 1.00** | 16 | **12.5%** |
| gross 4-5% | 78 | 30.8% |
| gross > 10% | 4 | 25.0% |
| BUY_LONG | 47 | 31.9% |
| BUY_SHORT | 64 | 28.1% |
| MLB | 96 | 29.2% |
| NHL | 11 | 27.3% |
| NBA | 4 | 50.0% |

Two things worth keeping: **expensive buys barely fill** (12.5% above 70c), and **bigger
arbs are not easier to catch** (25% above 10% gross vs 30.8% at 4-5%) — a wide spread means
a fast-moving book, not a slow one.

P&L by sport: MLB −$0.8426 (n=28), NBA −$0.0845 (n=2), NHL −$0.0473 (n=3).

### WS staleness at fire time

**Kalshi WS age is 1ms at every quartile** for both fills and non-fills (max 3ms) — the
Kalshi feed is never stale. Polymarket is: **median 157ms for fills vs 296ms for
non-fills**, non-fill max 159 seconds. This is the one staleness signal in the data that
correlates with outcome. (It is ambiguous causally: stale *may* mean quiet rather than
wrong — BUF@MON won with a 71s-stale price.)

Error column: `ORDER_STATE_EXPIRED` 76, `WS_TIMEOUT_NO_POSITION` 2, blank 66.

---

## Arb detection — 4%+ CLOSE rows (n=1098, 2026-04-30 → 05-15)

| Sport | n | Median dur | p75 | p90 | p99 | Median gross | Median net pretax | Kalshi opener |
|---|---|---|---|---|---|---|---|---|
| MLB | 652 | 0.087s | 0.223s | 18.0s | 1343s | 0.04 | 0.0204 | **50.6%** |
| NHL | 410 | 0.045s | 1.062s | 30.0s | 448s | 0.04 | 0.0102 | **84.4%** |
| NBA | 36 | 0.035s | 0.168s | 1.44s | 1621s | 0.04 | 0.0210 | 55.6% |

`opener` takes only two values: `kalshi` (696) and `poly` (402). No nulls.

Excluding the 81 arbs that lasted >60s, medians tighten: MLB 0.074s (n=605), NHL 0.022s
(n=378), NBA 0.027s (n=34).

### Duration distribution

| Sport | n | <85ms | <150ms | <500ms | <1s |
|---|---|---|---|---|---|
| MLB | 652 | 49.2% | 68.1% | 82.7% | 85.7% |
| NHL | 410 | 56.6% | 60.2% | 66.3% | 71.0% |
| NBA | 36 | 69.4% | 75.0% | 83.3% | 88.9% |
| **All** | **1098** | **52.6%** | **65.4%** | **76.6%** | **80.3%** |

Overall median 0.073s. **52.6% of 4%+ arbs close in under 85ms** — faster than our fastest
round trip. That is the structural taker ceiling.

Thinner arbs are **not** faster: the 3-4% band (n=1550 CLOSE) has 46.8% under 85ms, median
0.065s MLB / 0.088s NHL.

---

## Three corrections to previously-quoted figures (found 2026-09-19)

### 1. ⚠️ MLB Kalshi-opener share is 50.6%, not 77%

The long-quoted "77% MLB / 77% NBA / 83% NHL" (n=801) **does not reproduce** on the
1098-arb set. It is also wildly unstable by day for MLB: 78.4% (04-30), 100% (05-01), 54.3%
(05-12), **31.6% (05-13)**, 62.1% (05-14), 65.2% (05-15). NHL is stable and high (79-100%,
84.4% overall).

This matters because **`only_kalshi_opener=True` gates every trade**. An MLB opener share
near a coin flip means the filter is discarding roughly half of MLB arbs on a signal that
is not stable across days. Re-baseline before quoting it again.

(Counterweight: the convergence re-analysis below still shows the filter has strong
predictive value — 52.6% vs 15.0% hit rate. The *rate* was wrong; the *filter* is not.)

### 2. ⚠️ `minutes_to_first_pitch` is unusable for NHL

NHL CLOSE rows have only **10 distinct `game_datetime` values**, ranging out to
2026-05-17, with a median `minutes_to_first_pitch` of **+4742 min (3.3 days)**. Only 31.7%
of NHL 4%+ arbs are at or after puck drop.

So **NHL arb counts are heavily inflated by pre-game markets** the executor's ≤180min
filter rejects anyway. This explains NHL's fat ≥1s tail (29%) and why NHL produced 410
detections but only 11 execution attempts and 3 fills. Any NHL frequency claim built on
this column is wrong.

### 3. Fill rate is 29.7% (n=111), not 27.8% (n=54)

The documented 27.8% was computed on a 54-attempt subset. The fuller VPS pull gives 29.7%.
Directionally identical, still a ~5x improvement over the 5.5% pre-private-WS baseline.

---

## Convergence re-analysis (local `convergence_log.csv`, 362 arbs, 04-30 → 05-13)

Simulating the Strategy B maker exit as "did `poly_bid` reach `poly_ask@t0 + 5c` within
15s":

| Cohort | n | Hit rate |
|---|---|---|
| All tracked arbs | 362 | 33.1% |
| **Kalshi opener** | 175 | **52.6%** |
| Poly opener | 187 | 15.0% |
| Entry ≥15c AND gross ≥4% | 128 | 53.9% |
| **+ Kalshi opener (the full executor filter)** | 84 | **67.9%** |

Two conclusions:

- **The `only_kalshi_opener` filter is validated** — 52.6% vs 15.0% is a real edge, and the
  stacked filter reaching 67.9% roughly corroborates the 77% counterfactual on a different
  window.
- **The 15s timeout is too aggressive.** Median time to first +5c bid touch is 4256ms, but
  **p75 is 11.3s** (n=154) — a meaningful slice of eventual winners is being cut off at 15s.
  See [open-questions.md](open-questions.md#is-the-15s-timeout-too-short).

Caveat: entry price here is the logged `poly_ask` at t=0, not an actual fill price.

---

## Historical baselines

### Pre-private-WS (Apr 30 – May 1, n=55 attempts)

Fill rate **5.5%** (3 fills). Net −$0.045. Fills ~70-85ms; non-fills 60-460ms (server
holds via `synchronousExecution`). **Counterfactual win rate 77%** — 30 of 39 `BUY_FAILED`
arbs with convergence data would have hit `buy_price + 5c` within 15s. Detection-to-fire
latency in our own code: **1ms median** — network and server are the entire budget.

### Post-private-WS (2026-05-13, n=54 attempts, 15 fills)

Fill rate 27.8%. 6/15 converged (all wins), 4/15 price_drop (all losses), 5/15 timeout (1
win). Logged net −$0.44.

⚠️ **All taker-exit P&L before 2026-05-14 is unreliable** — exits were logged as filled
without verification. Reconciliation found 6 unsold positions held to expiry across
5/12-5/13 (SF@LAD −$0.40, SEA@HOU −$0.46, plus 4 mixed-outcome SHORT holds). Adjusted
05-13 net is likely **~−$1.00**, not −$0.44.

### Latency by location

| Leg | Home | VPS |
|---|---|---|
| Polymarket | 5-6 ms | ~1.2 ms |
| Kalshi | 18-19 ms | ~0.8 ms |
| Viable arbs (buy could fill) | 37% | **66%** |

---

## First live trades after the four-month outage (2026-09-19 evening)

| Game | Buy | Sell | Exit | Hold | P&L |
|---|---|---|---|---|---|
| BOS @ TB | 0.2500 | 0.3000 | SELL_CONVERGED | 464ms | **+$0.0406** |
| MIL @ BAL | 0.3950 | 0.3000 | SELL_PRICE_DROP | 5133ms | **−$0.1174** |

Net −$0.077 on two fills. The converged exit hit its +5c target in 464ms — the strategy
working exactly as designed.

⚠️ **The loss gapped ~9.5c through the 5c stop DESPITE passing the Kalshi velocity
filter.** First counter-example to the filter's effectiveness. Feeds directly into
[open-questions.md § Velocity filter effectiveness](open-questions.md#velocity-filter-effectiveness).

---

## Resource baselines

| Condition | RSS | Notes |
|---|---|---|
| MLB+NBA+NHL, fresh start (2026-09-19 19:48Z) | **275 MB** | 134 Kalshi + 114 Poly markets, 8 tasks |
| + CFB at 3h lookahead | 444 MB | 39 CFB games |
| + CFB at 6h lookahead | 512 MB | 73 CFB games |
| Leak growth rate | ≈38 MB/day | On top of ~265MB steady state |
| OOM kill point | 528 MB | 2026-05-15, pre-swap |

CPU, same CFB load, before/after the hot-path rework: **8.1-8.3% → 6.7-7.1%** (t3.micro
baseline is 10%).

Event-loop lag: steady-state windows 32.2ms and 72.0ms max; startup/discovery window
2245ms. GC: 12-13 collections and 755-898ms max pause in discovery windows, **zero
collections in steady state**.

---

## Health snapshot — 2026-09-19 23:58 UTC

First undisturbed post-revival window, ~1h uptime, 0 restarts, 0 `HEALTH ALERT` lines.

| Metric | Value |
|---|---|
| Markets | `K: 204/204 confirmed`, `P: 186/186 confirmed` (93 games) |
| RSS | **236 MB**, flat across three consecutive heartbeats |
| Tasks | 10 |
| Loop lag (max per 5-min window) | 16.8 / 37.6 / 37.9 ms |
| GC | **0 collections** in all three windows |
| CSV queue | 0 |

Two things worth noting against the earlier baselines:

- **236MB at 204 Kalshi markets is BELOW the 275MB baseline taken at 134 markets.** More
  markets, less memory — consistent with the per-page extraction change and the discovery
  rewrite, and the first real evidence for the "the leak may already be fixed" hypothesis
  in [open-questions.md](open-questions.md#memory-leak-source-unidentified-only-mitigated).
  **Not conclusive** — this is a ~1h window, and the protocol calls for 72h minimum.
- **Loop lag has improved to 16.8-37.9ms** from the 32-72ms measured earlier the same day,
  with zero GC collections in every window. Consistent with the finding that steady-state
  lag is burst WS work rather than GC.

CFB had aged out of the 3h lookahead by this hour (Saturday evening slate finished), so
this window is MLB/NHL only and is **not** comparable to the CFB RSS figures.
