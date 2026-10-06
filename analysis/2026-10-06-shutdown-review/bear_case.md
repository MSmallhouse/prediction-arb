# Bear case — working + numbers (2026-10-06)
Data: vps_pull_20261006_2027. Window 2026-09-19 -> 10-06 (17 days). Scripts: scratchpad/bear/{adv.py,cf.py}; row outputs bear/adv_rows.csv, bear/cf_rows.csv.

## 1. Adverse selection: we fill exactly the arbs a faster player declines
Join executions.csv (606 attempts, 606 unique arb_ids, 199 games) to convergence_log.csv on arb_id.
Coverage: 339/606 attempts have a convergence log (32/109 fills, 307/497 failures).
Logged fills are representative: realized converged-exit share 5/32 (16%) logged vs 14/77 (18%) unlogged; median P&L -2.4c vs -3.3c.
Price fields: buy_price == poly_ask(t=0) of poly_team for both LONG and SHORT intents (median |diff| 0.0), so target = poly_bid >= buy_price+0.05.

| | Fills (n=32) | Lost races (n=307) |
|---|---|---|
| Bid reaches +5c within 15s | 9.4% | 57.3% |
| Poly ask lifted by someone else within 200ms | 22% | 96% |
| Ask lifted before our own buy_latency elapsed | 12.5% | 78% |
| Median time to ask lift | 762ms | 77ms (p10 25ms, p25 46ms, p75 107ms) |
| Median bid(15s) - entry | 0.0c | +5.0c |
| Median poly_depth at fire | 220 | 18 |
By sport, fills vs lost races converge: CFB 17% (12) vs 59% (164); MLB 0% (7) vs 63% (91); NHL 8% (12) vs 43% (51).
=> The replay's 60-70% convergence is real, but it is the convergence of the arbs someone else takes. The 17% live rate is not a sport-mix or exit-tuning problem; it is selection.

## 2. Counterfactual P&L (same exit rules: +5c maker target, 5c stop on ask, 15s timeout taker exit, fee 0.0695*p*(1-p))
Simulator calibration: on our 32 logged fills it gives mean -3.31c vs realized mean -3.26c (all 109). Good.
- Our fills: mean -3.31c, median -2.73c (n=32). Exit mix conv 9% / drop 13% / timeout 78%.
- Lost races: mean +1.77c (bootstrap 95% CI +1.39..+2.12c), median +3.76c (n=307). By sport: CFB +1.75c (164), MLB +2.19c (91), NHL +1.03c (51).
=> Removing NHL does not fix our fills: counterfactual on our own fills is negative in every sport (CFB -2.9c n=12, MLB -5.3c n=7, NHL -2.2c n=12); realized logged P&L CFB -$1.45/34, MLB -$0.65/21.

## 3. Latency ceiling
- Box already 1.2ms from Polymarket, 0.8ms from Kalshi (operations.md). Order RTT 94ms median fills / 113 fails is exchange+client processing, not network.
- Our fastest decile round trip: 64-65ms (both fills and fails). Competitor lift p25 46ms, p10 25ms after our first_seen.
- arb_durations_4 CLOSE rows, gated (kalshi opener, poly_ask>=0.15), since 09-19: n=4325, median duration 25ms, 82.2% < 85ms, 92.1% < 150ms.
=> Even a zero-overhead client is on the wrong side of the median race.

## 4. No profitable subset (multiple comparisons)
57 cells tested across sport, intent, price bucket, depth quartile, ws_age quartile, gross bucket, hour-of-day, and 3 two-way splits on 109 closed trades. Positive cells: 2, with n=2 and n=1. Nothing to select on.

## 5. Ceiling in $/month
Attempts: 606 / 17d = 35.6/day (median 17/day; Saturdays CFB 83-120).
- Status quo: -$2.96 real over 17d = -$5.2/mo trading, minus ~$8/mo AWS = ~-$13/mo.
- "Fix" sport mix / tuning only: fills remain adversely selected; expected ~-$5 to 0/mo trading, still negative after AWS.
- Win 100% of races, qty 1: +1.77c x 35.6/day x 30 = ~$19/mo gross, ~$11 net of AWS.
- Win 100% of races, qty 10 depth-capped: 307 logged lost races sum $44.52 -> scaled to all 606 attempts ~$80/17d = ~$140/mo. Upper bound; ignores that a 10-share maker exit needs 10 shares of buying at target, and requires fixing the quantity bug and beating a counterparty whose p25 reaction is faster than our p10 RTT.
- Realistic "partially winning" (30% race share, qty 5-10, edge decays to ~1c): ~$10-40/mo gross.
Seasonality: MLB attempts 15-32/day (09-20..09-27) -> 0-9/day (09-29..10-06); MLB gated arbs 141-213/day -> 3-67/day. Winter is NHL (worst lost-race edge, +1.03c) + NBA (n=7 attempts total). CFB carries volume but only on Saturdays, ending Dec/Jan.

## 6. What would change my mind
- Offline maker-bid backtest using research/poly_com_tape_*.csv (full tick tape, already collected): would a resting bid at entry price have filled, and what was the 15s P&L of those fills? Free, no VPS. If positive >= +1c over n>=200, maker approach has a case.
- Live: >= 100 fills with converged-exit share >= 40%.
- Measured order-ack floor < 40ms (e.g. a Polymarket US low-latency/FIX path).

## 7. Weakest point
The signal is real: lost races earn +1.77c (CI 1.39-2.12c, n=307). The problem is who captures it, and the race margin is tens of ms, not orders of magnitude. Fill coverage in convergence_log is 32/109 (though representative by exit mix). Competitor could leave.

## Rebuttal (to bull_case.md)
Scripts: bear/reb1.py, bear/reb2.py (use bull's hold.pkl, stat.py rep() game-clustered bootstrap, fills.pkl). Artefacts bear/gated_conv.pkl, bear/conv_gated.parquet.

### Mapping/inversion/fees: checked, no error found
- fills.pkl buy_price == poly_ask (within 0.6c) on 99.0% of fills; LONG 98.1%, SHORT 100% -> poly_team is the side bought; no inversion bug.
- Bull's calibration table and placebo (opener==poly ~0) look right. Clustering by game is appropriate. I CONCEDE the settlement result on detected arbs: +5.86c, CI [+4.43,+7.34] is real.

### The settlement edge lives only in arbs that converge within 15s
Gated (kalshi opener, gross>=4%, depth>=1, ask>=0.15, <=180min), since 09-19, joined to convergence_log: 1020 rows / 279 games (29% of 3481; subset first-per-side +5.72c matches bull's +5.86c -> representative).
| subset | first-per-side n | hold mean | 95% CI (games) |
|---|---|---|---|
| all matched | 428 | +5.72c | [+3.43,+8.04] |
| bid hits +5c within 15s | 352 | +10.36c | [+7.74,+12.92] |
| does NOT converge | 237 | -2.98c | [-7.08,+1.19] |
| ask not lifted <=100ms ("winnable") | 231 | +3.69c | [-0.95,+7.90] |
| winnable & not converged | 126 | -1.87c | [-8.24,+5.11] |
Bull's own fills split identically: converged fills hold +14.6c (n=18), timeouts +0.02c (n=78). Our fills converge 9-17% -> implied fill-level hold EV = 0.17*(+10.4) + 0.83*(-3.0..-1.5) = **-0.7c to +0.7c**; direct bull estimate +2.4c CI [-7.3,+11.9] (n=104, 64 games). Adverse selection DOES carry to settlement: the fills we win are the arbs with no settlement edge.

### Slower or maker entry does not capture it
Taker entry at the ask prevailing at later times (same 1020 rows, first-per-side n~427, game-clustered):
t0 +5.72c | <=200ms +0.50c [-1.81,+2.88] | <=1s -0.35c | <=5s -1.14c | <=15s -1.97c [-4.29,+0.27].
Edge is gone within ~200ms -> it is latency information, not slow "Kalshi is more accurate" information. Latency argument stands.
Maker proxy (rest bid at a0-1c, filled if ask trades down to it within 15s): fills 11.8% of the time; filled bids hold **-7.10c** [-14.77,+0.23] (n=97); unfilled +9.44c (n=408). Textbook adverse selection.

### Capital/variance at qty 10
Per-share SD on a ~0.5 binary ~= 49c -> ~$4.9/trade at qty 10. 183 trades/mo -> monthly SD ~ $66 (ignoring within-game correlation, which makes it worse). At a fill-level edge of 0-2.4c: mean $0-$44/mo, P(losing month) 25-50%. Distinguishing a 1c edge from 0 at 95% needs n ~ (1.96*0.49/0.01)^2 ~ 9,200 trades ~ 4 years at 6 fills/day. Capital at qty 10 fits (~$58 peak), I concede that.

### Concessions
1. The signal predicts winners on detected arbs; my "edge is captured by competitors" framing was about 15s convergence, but the settlement edge is real and robust.
2. Holding beats the 15s exit for our fills: removes ~3.3c friction on timeouts (realized -$2.41 -> hold +$0.02, n=78). Switching to hold turns -3c/trade into roughly 0 to +2c.
3. Capital fits at qty 10.

### What survives
- The settlement edge is concentrated in fast-converging arbs we systematically lose; fill-level edge ~0 (-0.7..+2.4c, CI spans 0).
- It decays to ~0 within 200ms of first_seen and is negative for resting maker bids -> no slow/maker route around the race.
- Variance swamps any plausible edge; not verifiable within a season.
Revised $/month: qty 1 hold: -$2 to +$4 (before $8 AWS). qty 10 hold: -$15 to +$45 mean, +-$66 SD. Best-case (bull's 5.9c at qty 10): ~$108, requires fills that look like detected arbs, which the data says they don't.
