# BULL CASE — working + numbers (2026-10-06)

Scripts (scratchpad): fetch.py (ESPN results, 124 sport-date files in espn/), outcomes.py (team matcher),
hold.py (builds hold.pkl), stat.py (game-clustered bootstrap, 2000 resamples), bull_load.py (exec join), fills.pkl.

## 0. Method
- Ran the open-questions.md "Does the arb signal predict the outcome?" protocol, which had never been executed.
- Data: arb_durations_4.csv (pull 20261006) + historical arb_durations_4_apr23/apr27 (apr28-29 has no sport/game_datetime; apr30 files didn't match). OPEN rows, deduped on (first_seen, game, poly_team).
- Winners from ESPN scoreboard (MLB/NHL/NBA/CFB FBS+FCS), matched on sport + team name + start time within 8h.
  Coverage: NHL 99.8%, MLB 98.7%, CFB 89.8%, NBA 79.5% of OPEN rows. Unmatched dropped.
- Mapping sanity (calibration, all 8,902 rows): ask 0.13 -> win 0.15; 0.29 -> 0.37; 0.43 -> 0.45; 0.56 -> 0.57; 0.72 -> 0.74; 0.86 -> 0.94. A broken mapping would flatten to 0.5.
- hold_pnl = won - poly_ask - 0.05*p*(1-p) (fee formula verified against executions buy_fee: p=.75 -> .0094).
- Gate stack replayed: opener==kalshi, gross>=0.04, depth>=1, ask>=0.15, minutes_to_first_pitch<=180.
- Stats: CIs are bootstrap over GAMES (outcomes within a game are perfectly correlated). "first-per-side" = first arb per (game, date, poly_team) only.

## 1. Hold-to-maturity EV on detected arbs (gated)
| subset | rows | games | mean/row | first-per-side n | mean | 95% CI |
|---|---|---|---|---|---|---|
| ALL gated | 4403 | 428 | +6.97c | 724 | +5.86c | [+4.43, +7.34] |
| MLB | 1744 | 175 | +6.15c | 306 | +5.46c | [+3.15, +7.89] |
| NHL | 1309 | 91 | +7.68c | 158 | +7.22c | [+4.69, +9.83] |
| CFB | 1207 | 152 | +7.70c | 241 | +5.35c | [+2.82, +7.93] |
| NBA | 143 | 11 | +4.38c | 19 | +7.75c | [+1.32, +15.37] |
| since revive (09-19+) | 3481 | 319 | +7.04c | 531 | +5.82c | [+4.23, +7.43] |
| Apr-May | 922 | 109 | +6.71c | 193 | +6.00c | [+2.96, +9.06] |
| slow (dur > median 29ms) | 2161 | 388 | +7.88c | 631 | +5.74c | [+4.07, +7.42] |
| fast (dur <= median) | 2242 | 337 | +6.10c | 534 | +4.63c | [+2.54, +6.68] |
| dur > 85ms ("fillable") | 858 | 288 | +7.07c | 410 | +6.77c | [+3.90, +9.59] |
| PLACEBO opener==poly | 3225 | 317 | +0.34c | 469 | -0.39c | [-2.97, +2.44] |

Kalshi-implied edge on gated rows (1 - kalshi mid - ask - fee): mean 6.03c, median 4.87c — realized hold EV (5.9-7.0c) matches it. "Kalshi is fair value" holds empirically; the protocol's adverse-selection test (slow subset) does NOT show degradation.

Dose-response by gross spread (first-per-side): 4-5%: +3.88c [2.15,5.59] n=626 | 5-7%: +6.52c [4.01,9.05] n=457 | 7-10%: +10.49c [6.84,14.15] n=293 | 10%+: +19.01c [14.40,23.97] n=196.
Positive in every ask band (0.15-0.35 +6.8c; 0.35-0.5 +7.1c; 0.5-0.65 +5.0c; 0.65-1 +6.5c) and every depth band (1-5 +5.1c ... 20-100 +8.1c).

## 2. Our actual fills, counterfactual hold (n=104 of 109 with outcome, since revive)
- Realized (logged) on these 104: -$3.39 (-3.26c/trade). Held to settlement instead: +$2.50 (+2.40c/trade). Swing +$5.89.
- Game-clustered CI on hold mean: [-7.3c, +11.9c], P(mean<0)=30%, 64 games. NOT significant on its own.
- By exit: timeouts n=78 realized -$2.41 vs hold +$0.02; converged n=18 realized +$0.71 vs hold +$2.63; price_drop n=6 -$0.64 vs +$0.90; exit_failed n=2 -$1.05 both ways.
- By sport (hold): NHL +$3.72 (n=52), MLB +$2.20 (n=21), CFB -$2.41 (n=29), NBA -$1.01 (n=2).
- Hybrid (keep converged maker exits, hold everything else): +$0.58.
- Failed attempts (n=473) at their intended price: hold +4.21c/trade -> some adverse selection on fills (2.4c vs 4.2c vs 5.9c), but within noise.

## 3. Why live loses: friction, not signal
Timeouts are 74% of exits; median -3.3c with price essentially unchanged — that's bid/ask spread + 2 taker fees. Their hold value was ~0 (+$0.02 over 78). The 15s exit converts a ~zero/positive-EV position into a guaranteed -3c friction loss.

## 4. Capacity / capital (hold model)
- Fills 6.1/day (104/17). Distinct gated sides/day since revive: 31.2; median depth at OPEN 21.
- Held to game end (+3.5h from start): max 11 concurrent, peak capital $5.77 at qty 1, median hold 1.6h -> qty 10 needs ~$58, fits ~$75 withdrawable.
- Daily hold P&L on fills at qty1: mean +$0.15, SD $1.29 (17 days).
- qty-scaling bug lives in the maker-sell / taker-exit path; a hold-only executor deletes that path (buy is FOK), so it stops blocking qty>1.

## 5. Profit estimate
| scenario | edge/trade | trades/mo | qty | $/mo | 1-SD $/mo |
|---|---|---|---|---|---|
| floor (fills-based) | 2.4c | 183 | 1 | +$4.4 | ±$6.8 |
| detected-arb based | 5.9c | 183 | 1 | +$10.8 | ±$6.8 |
| qty 10, edge haircut 3c | 3.0c | 183 | 10 | +$55 | ±$68 |
| qty 10, edge 5.9c | 5.9c | 183 | 10 | +$108 | ±$68 |
| raise gate to 5%+, qty 10 | ~8c | ~90 | 10 | ~+$72 | ±$48 |
Realistic: $0-$15/mo at qty 1; $30-$100/mo at qty 10 if fills hold at size. Capital ceiling ($75) caps it near there.

## 6. Weakest points
- Fills-only evidence is n=104, CI [-7.3,+11.9]c; 30% chance the true fill-level hold EV is negative.
- CFB fills held would have lost $2.41 (n=29) even though detected CFB arbs show +5.4c.
- NHL since revive = preseason; MLB season ending. Regime/sample risk; 17 days + a few Apr days.
- Binary variance: at qty 10 a losing month is ~20-40% likely even if the edge is real.
- Fill rate at qty 10 FOK unknown (depth median 21 but we fill 18% at qty 1).

## Rebuttal (after reading bear_case.md)
Scripts: reb.py, reb2.py -> paths.pkl. Joined convergence_log to gated arbs with ESPN outcomes: 1226 arbs, 274 games (since revive). Game-clustered bootstrap.

### Test 1 — is the settlement edge a property of arbs we cannot fill?  YES.
| subset (entry at t=0 ask) | n | games | hold EV | 95% CI |
|---|---|---|---|---|
| all | 1226 | 274 | +7.62c | [+5.05, +10.09] |
| ask lifted by someone within 200ms (lost-race-like) | 1103 | 268 | +8.39c | [+5.70, +11.10] |
| ask NOT lifted within 200ms (fill-like) | 123 | 86 | +0.72c | [-7.42, +8.40] |
Consistent with our actual fills held: +2.40c [-7.3, +11.9] (n=104). The +5.9c detected-arb number is the edge of the arbs a faster participant takes. Concede bear (a).

### Test 2 — can we enter later (no race) and still capture it?  NO.
Hold EV entering at the poly ask at delay T (all 1226 arbs):
200ms +2.02c [-0.64, +4.54] | 1s +0.83c [-1.78, +3.34] | 5s -0.17c [-2.85, +2.54] | 15s -0.84c | 60s -0.81c.
Residual Kalshi-vs-Poly gap at 1s: median -1.0c; only 16.6% still >= 2c. The settlement edge is the same ~100ms latency edge, not a slow "Kalshi follows the score" signal. A resting/maker entry after the fact would be buying at fair. Not double-counting exactly, but the hold EV and the convergence EV are one edge seen two ways, and both belong to the race winner.

### What survives
- Hold-to-settlement still beats the 15s exit on OUR fills: -3.26c realized vs +2.40c held (n=104) — the exit costs ~3c of pure friction (timeouts: -$2.41 realized vs +$0.02 held, n=78). It turns a bleeding bot into ~break-even, not a profitable one.
- The signal is real and outcome-predictive (placebo opener=poly -0.4c; dose-response by gross spread). Value exists only for a faster client.

### Variance
SD ~50c/trade. n for 95% CI to exclude zero: edge 2.4c -> (1.96*0.5/0.024)^2 ~ 1,670 trades (~9 months at 6.1/day); edge 0.7c -> ~19,600 (never). Live validation at qty 1 is not achievable on a useful timescale.

### Revised $/month
Hold-only, qty 1, current latency: 0.7-2.4c x ~183/mo = +$1 to +$4.5/mo gross, ±$7/mo 1-SD; minus ~$8/mo AWS => about -$7 to -$3/mo net. Qty 10 would multiply an edge that is statistically indistinguishable from zero.
Only upside path: win races (sub-40ms ack) — unproven and possibly impossible on this venue.
