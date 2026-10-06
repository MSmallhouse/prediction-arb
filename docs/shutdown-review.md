# Shutdown review — 2026-10-06

**Status: project closed. Trading stopped 2026-10-06 20:55 UTC; the EC2 instance was
terminated the same evening.** This doc is the verdict and the evidence behind it. Read it
before reviving anything.

The decision came out of a structured review: a bull advocate and a bear advocate built
cases from the data, cross-examined each other, and an independent judge re-derived the
load-bearing numbers with its own code. All three converged. Working, scripts and both
cases are in [`analysis/2026-10-06-shutdown-review/`](../analysis/2026-10-06-shutdown-review/)
(`judge.md` is the most compact). Intermediate data (pickles, parquet, ESPN outcome cache)
is local-only in `vps_pull_20261006_final/analysis/` — the repo is public and trading data
is gitignored.

---

## Verdict in one paragraph

The arb signal is **real and predicts game outcomes**, but its value belongs to a faster
participant. Of gated arbs, 92% have their Polymarket ask lifted by someone else within
200ms, and those carry the entire edge (+7.7c held to settlement). The 8% left for us —
the ones we can actually fill — carry **≈0** (−0.3c). The edge halves in ~60-80ms; our
order round trip is 94ms median from a box already ~1ms from both venues. No exit
redesign, sport mix, filter, or entry delay recovers it. Best realistic case after fixes
was −$10 to −$3/month net of AWS, against $30-100/month of owner time.

---

## Ground truth: what it actually earned

Source: Polymarket buying-power readings logged on every private-WS reconnect
(`scanner.log*`), cross-checked against `portfolio.activities()` and `account.balances()`.
**Not** `executions.csv`.

| | |
|---|---|
| Window | 2026-09-19 → 2026-10-06 (17 days, revival to shutdown) |
| Buying power | 68.63 → 100.67, flat at close, 0 open positions, 0 open orders |
| External flows in window | +$10 deposit, +$25 referral bonus, +$5 remediation credit, −$5 clawback (09-23) |
| **Real trading P&L** | **≈ −$2.96** (≈ −$0.17/day, ≈ −$5/month) |
| Logged P&L (`executions.csv`) | −$3.55 — differs by 2 `SELL_EXIT_FAILED` and held-to-expiry resolutions |
| AWS | ≈ $11.70/month for the bot (Sep: EC2 $7.49, public IPv4 $3.60, EBS $0.64) |

**Lifetime account** (Apr 2026 → close): $60 cash deposited; +$46.30 of bonuses and
credits (referrals 20+25, remediation +10/−10, liquidity program $1.12, taker rebates
$0.18); lifetime trading P&L ≈ **−$5.6**. Withdrawable ≈ $75.7 at close — the account is
up because of promotions, not the strategy. $25 of the balance is a non-withdrawable bonus
held as `bonusReservation`; the API logged "Releasing 0.35 in incentives due to position
change", so bonus release appears to be tied to trading activity.

### Execution stats, 2026-09-19 → 10-06

| | n | |
|---|---|---|
| Buy attempts | 606 | |
| Fills | 109 | **18.0%** (29.7% in May) |
| Lost races (`ORDER_STATE_EXPIRED`) | 489 | |
| Exit: timeout | 81 | 74% of exits, median −3.3c |
| Exit: converged | 19 | 17%, +3.8 to +4.1c each |
| Exit: price_drop | 7 | median −10 to −12c |
| Exit: exit_failed | 2 | −$1.05 (both CFB) |

Fill rate by sport: NHL 32.7% (n=159), NBA 28.6% (n=7), MLB 13.5% (n=156), CFB 12.0%
(n=284). Logged P&L by sport: CFB −$1.45 (n=34), NHL −$1.31 (n=52), MLB −$0.65 (n=21),
NBA −$0.14 (n=2). **Every sport lost**, including CFB — the sport enabled 2026-09-22 on a
70.5% replay convergence rate converged 8/34 live.

---

## The four findings that decided it

All CIs are bootstrapped **by game**, not by row. Outcomes from the ESPN scoreboard API;
two independent matchers (bull's and judge's) agreed on 3,451/3,451 overlapping arbs.
Coverage: NHL 99.8%, MLB 98.7%, CFB 89.8%.

### 1. The signal predicts the winner (answers the 2026-09-20 open question)

Holding gated, Kalshi-opened arbs to settlement at the t0 Polymarket ask:

| Cut | n | Hold EV | 95% CI |
|---|---|---|---|
| First arb per side, all data | 724 sides / 428 games | **+5.86c** | [+4.43, +7.34] |
| Since revive only | 531 | +5.82c | [+4.23, +7.43] |
| Apr-May, out of sample | 193 | +6.00c | [+2.96, +9.06] |
| Placebo: opener = poly | — | −0.39c | [−2.97, +2.44] |

Positive in every sport (MLB +5.5c, NHL +7.2c, CFB +5.4c, NBA +7.8c), monotone in gross
spread (4-5%: +3.9c → 5-7%: +6.5c → 7-10%: +10.5c → 10%+: +19.0c). Calibration sane
(ask 0.29 won 37%, ask 0.72 won 74%). **Kalshi behaves as fair value** at the moment it
moves first.

### 2. Adverse selection: the edge lives in arbs we cannot fill

Split by whether someone else lifted the Polymarket ask within 200ms of `first_seen`
(settlement-based, so book mechanics cannot cause it):

| Subset | n | Hold EV |
|---|---|---|
| Ask lifted within 200ms (= a race someone else won) | 912 | **+7.7c** |
| Not lifted (= the arbs we can fill) | 77 | **−0.3c** [−10.4, +9.7] |
| Not lifted, first arb per side | 41 | −10.5c [−22.6, +1.4] |

92% of gated arbs get lifted. **Depth does not explain it**: lift rate is 94/94/88%
across depth terciles, the gap persists within the mid and top terciles, and OLS of hold
EV on lift + gross + log(depth) gives lift +7.2c, depth ≈ 0.

The same split on the 15s-convergence outcome (bear's analysis, live exit rules
simulated, calibrated to −3.31c vs −3.26c realized): **lost races hit +5c within 15s
57.3% of the time (n=307); our fills 9.4% (n=32).** On lost races, another participant
lifted the ask at median **77ms** (p25 46ms, p10 25ms). This is why live convergence ran
~17% against a 60-70% replay: **the replay measured arbs we lose, the live number
measures arbs we win.**

### 3. The edge is latency information, gone in ~200ms

Hold EV if entered at the Polymarket ask T after `first_seen` (first arb per side,
n=429, 248 games):

| T | 0 | 50ms | 100ms | 200ms | 1s | 15s |
|---|---|---|---|---|---|---|
| Hold EV | +5.5c | +4.0c | +2.0c | +0.4c | −0.5c | −2.1c |

Half-life ~60-80ms. Our fill round trip is 94ms median, fastest decile ~64ms — slower
than the competitor's p25 of 46ms — from a box ~1ms from both venues, so the remaining
latency is exchange and client processing, not network. **A resting maker bid does not
escape it**: a bid 1c under the t0 ask fills 11.8% of the time when the ask trades down
to it, and those fills hold **−7.1c** (n=97); the bids that never fill would have held
+9.4c. A maker fills exactly when it is wrong.

### 4. Hold-to-settlement stops the bleeding but cannot be shown to profit

On our 105 resolvable fills (65 games): realized −3.27c/trade [−5.0, −2.0]; held to
settlement +2.06c [−7.4, +11.4], ≈ **+1.7c** after the fee correction below. Hold minus
realized +5.3c [−3.8, +14.9] — about 87% likely to be better, not proven; the real support
is structural (a timeout exit pays the spread plus a second taker fee, ~3c of certain
cost). Telling a 1-2c edge from zero at 95% with a ~50c per-share SD needs **1,700-9,000
trades — 1 to 4+ years** at ~6 fills/day.

---

## Options considered

| Option | Gross $/mo | Net of $8 AWS |
|---|---|---|
| Status quo (15s exit) | −$5 (measured) | −$13 |
| **Shut down (chosen)** | $0 | $0 |
| Hold-only, qty 1 | −$2 to +$5 | −$10 to −$3 |
| Hold-only, qty 10 | −$20 to +$50, SD ±$65/mo | −$28 to +$42 — real ruin risk on a $75 bankroll |
| Infra to win races | ~$100-140 only if every race were won | realistically negative |

Forward volume assumed ~180 fills/month: MLB over, CFB Saturdays 80-130 attempts, weekdays
5-15 (NHL/NBA). Owner monitoring time (six silent failures and two OOM kills since revival)
at 1-2h/month dominates every central estimate.

---

## What would justify a revival

**One condition:** a measured Polymarket US order round trip of **p50 ≤ 30ms** (vs 94ms),
available for ≲ $10/month extra. Without it, nothing in
[future-work.md](future-work.md) changes the outcome.

If that exists: hold-only executor (delete the 15s timeout, price-drop stop and taker
exit), qty 1, four weeks. **Kill criteria:** fill rate on arbs that someone else later
lifts < 30%, **or** fill-level hold EV < +2c after 300 fills.

The real business in this market is probably **market-making on Polymarket US with Kalshi
as the fair-value anchor** — that is plausibly what the participants beating us are
doing, and the $1.12 liquidity-program payment in May came while our exits rested as maker
orders. It needs the same speed we lack.

⚠️ `research/poly_com_tape_*` is **polymarket.com (international)**, not Polymarket US —
a different book and different participants. It cannot be used to backtest anything we
would trade.

---

## Corrections found during the review

- **Polymarket US taker fee coefficient is ≈ 0.0695, not 0.05.** Ledger taker fees since
  2026-09-19 were $3.17 over 210 taker legs; `0.0695·p·(1−p)` rounded to the cent gives
  $3.15, `0.05` gives $1.96. `executions.csv` `buy_fee` understated fees by ~0.3-0.5c per
  leg throughout. Markets expose it as `market.feeCoefficient`.
  → [platforms.md](platforms.md#polymarket-us-activity-ledger-traps)
- **`ACTIVITY_TYPE_TRANSFER` amounts are unsigned.** The 2026-09-23 $5 transfer was a
  clawback (out), but reads `+5` like the 09-20 credit. Naively summed, the ledger gives
  −$12.96 instead of −$2.96. → [platforms.md](platforms.md#polymarket-us-activity-ledger-traps)
- **`reconcile.py` misreads SHORT-side activity.** Polymarket nets YES and NO in one slug
  (`netPosition`), so "bought=N sold=0" per side and the "43 mismatched markets" summary
  are artefacts of the netting, not missing sells. Use buying power or per-trade `cost` for
  P&L, never reconcile's per-side counts.

---

## Decommissioning record (2026-10-06)

1. Verified flat via API: 0 positions, 0 open orders, balance $100.6659.
2. Disabled and stopped `arb-scanner-memguard.timer`, `poly-com-tape.timer`,
   `arb-scanner.service` (SIGTERM drained the CSV queue — last log line 20:55:31 UTC);
   removed `/etc/cron.d/arb-memsample`.
3. Archived the box's home directory (minus `.env`, `.git`, caches) to
   `vps_pull_20261006_final/` (418MB, local-only): all CSVs, all 16 rotated logs,
   research tapes, memsample, systemd journal, and stray scripts from `~`.
4. Deleted CloudWatch alarms `arb-scanner-process-dead` and `arb-scanner-cpu-credits-low`.
5. **Terminated `i-0923ce83c9a4b7047`.** Its 8GB gp3 root volume was `DeleteOnTermination`
   and is gone. Verified afterwards in us-east-1: 0 volumes, 0 Elastic IPs, 0 snapshots,
   0 ENIs, 0 alarms; no instances or volumes in any other region.

**Left in place, all $0/month:** security group `sg-0714ac951294552f4`, key pair
`arb-key`, SNS topic `arb-scanner-alerts`, IAM role `arb-scanner-role` + instance profile
`arb-scanner-profile`. Automated deletion was blocked by the session's permission
classifier; remove by hand if wanted. **Not related to this project — do not delete:** the
`aws-sam-cli-managed-default-*` S3 bucket and the DynamoDB usage (older SAM apps), and the
$15 "My Monthly Cost Budget" alert.

**Still live outside AWS:** the Polymarket US API key (trading-capable) and the Kalshi API
key (read-only). With the box gone nothing holds them except the local `.env`; revoking
both requires the owner's interactive login.
