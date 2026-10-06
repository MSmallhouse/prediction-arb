# Judge's working — 2026-10-06
Own code: scratchpad/judge/{j_outcomes.py (independent ESPN matcher), j1_fills.py, j2_conv.py, j3_an.py, j4_surv.py}; outputs j_fills.pkl, j_paths.pkl.
Bootstrap = game-clustered, 3-4k resamples. "fps" = first arb per (game, day, poly_team).

## 0. Ground truth sanity
- Raw poly_activities.json: 09-20 $5 = ISSUE_REMEDIATION_CREDIT (in), 09-23 $5 = "clawback ... $5 reversal" (out). poly_ledger.csv drops the sign (both show +5). Brief's -$2.96 is right; with the naive ledger signs it would read -$12.96.
- Lifetime: cash deposited $60 (50 Apr + 10 Sep). Bonuses/credits net +$46.30 (referral 20+25, remediation +10/-10, liquidity program 1.12, taker rebates 0.18). Balance 100.67 => lifetime TRADING P&L ~ -$5.6. Withdrawable ~$75.7 vs $60 deposited: promos, not the strategy, are why the account is up.
- Fees: ledger taker fees since 09-19 = $3.17 over 210 taker legs (1.5c/leg). 0.0695*p(1-p) rounded to cent reproduces it ($3.15); 0.05 does not ($1.96 rounded, $3.48 ceil). executions.csv buy_fee uses 0.05 -> understates by ~0.3-0.5c/leg. Bull's hold path (0.05) is ~0.3-0.5c optimistic; bear's sim (0.0695) is right.

## 1. Spot-check (b): our fills, realized vs held
Matcher agrees with bull's outcomes on 3451/3451 overlapping arbs (independent code).
- 109 fills, 105 with outcome, 65 games. buy_price == poly_ask(t0) within 1c on 99.1%.
- Realized (logged) -3.43 = -3.27c/trade, CI [-5.0,-2.0].
- Held to settlement +2.17 = +2.06c/trade, CI [-7.4,+11.4], P(<0)=34%. Fee-corrected ~+1.7c.
- Hold minus realized: +5.3c, CI [-3.8,+14.9], P(hold worse)=13%. NOT significant at 95% — both sides conceded "hold beats the 15s exit" as fact; it is ~87% likely, supported mainly by the structural argument (timeout exit = spread + 2nd taker fee ~3c of certain friction).
- By exit: timeout n=79 realized -2.45 vs hold -0.31; converged n=18 +0.71 vs +2.63.
- By sport (hold): NHL +3.72 (52), MLB +2.20 (21), CFB -2.74 (30), NBA -1.01 (2). All noise-level.

## 2. Spot-check (c): decay with entry delay (gated, since 09-19, conv-logged, n=989 arbs / 429 fps, 248 games)
| entry at ask at T | all arbs | first-per-side |
|---|---|---|
| 0 | +7.12 [+4.5,+9.7] | +5.53 [+3.2,+8.0] |
| 50ms | +5.70 | +4.04 |
| 100ms | +3.44 [+0.6,+6.2] | +2.02 [-0.3,+4.5] |
| 200ms | +1.68 [-1.0,+4.4] | +0.38 [-2.0,+2.8] |
| 1s | +0.84 | -0.46 |
| 15s | -0.60 | -2.06 |
Our order RTT median is 94ms -> the realistic unconditional entry is the T=100ms row: ~+2-3c, before the selection effect below. Half-life ~60-80ms.
Survivorship of convergence_log: logged gated arbs hold +7.3c (n=1018) vs unlogged +6.8c (n=2466); logging rate is 8-39%/day, no regime pattern. No survivorship bias of consequence.

## 3. Spot-check (a): adverse selection, settlement-based
| split (entry a0) | lifted | not lifted |
|---|---|---|
| ask up within 100ms, all | +8.05 (683) | +5.04 [-0.3,+10.3] (306) |
| ask up within 200ms, all | +7.74 (912) | -0.26 [-10.4,+9.7] (77) |
| ask up within 200ms, fps | +7.23 (388) | -10.51 [-22.6,+1.4] (41) |
- 92% of gated arbs have the ask move up within 200ms. The fillable 8% have ~zero settlement edge.
- Depth confound: lift rate is 94/94/88% across depth terciles (median depth 4/22/190); within mid and high terciles the gap persists (mid +11.0 vs -6.4; hi +5.8 vs +0.1). OLS h0 ~ lift200 + gross + log(depth): lift coefficient +7.2c, depth coefficient ~0. Depth does NOT explain it.
- Methodological note on bear §1: "ask lifted" vs "bid reaches +5c" is partly mechanical (a lifted ask IS the book moving up). The settlement split is the clean test because outcomes are independent of book mechanics; it confirms bear.
- "Lifted" can't distinguish a taker hitting from the MM requoting. Irrelevant to the conclusion: either way someone faster removes the stale price.

## 4. Rulings
1. Signal real & outcome-predictive (bull): RIGHT, high confidence (placebo ~0, dose-response, my +5.5c fps).
2. Adverse selection: our fills are the arbs with no edge (bear; bull conceded): RIGHT, high confidence. Depth is not the confound.
3. Edge is latency info, gone in ~200ms (bear; bull conceded): RIGHT, high confidence.
4. Hold beats 15s exit (both): PROBABLY right (~87%), not proven; structural friction argument is the real support.
5. Fill-level hold EV ~ +2c (bull) vs ~0 (bear): both within noise; best estimate +1 to +2c after fee correction, CI ~[-8,+11]. Not provable in under ~1,700-9,000 trades.
6. qty-10 hold is capital-feasible (both): yes on capital, but SD ~$65/mo against a $75 bankroll is a real drawdown/ruin risk neither weighed.
7. Bear's "maker-bid backtest on research/poly_com_tape" as a flip test: FLAWED — that tape is polymarket.com (international), not Polymarket US where we trade. Different book, different participants.
8. Bear's fee 0.0695 vs bull's 0.05: bear right (ledger).

## 5. $/month (forward: MLB over; CFB Saturdays ~80-130 attempts, weekdays ~5-15 NHL/NBA; ~180 fills/mo at 18%)
| option | gross $/mo | net of $8 AWS | notes |
|---|---|---|---|
| status quo (15s exit) | -$5 (observed) | -$13 | measured |
| A shut down | $0 | $0 (saves $8) | withdraw ~$75; $25 bonus likely forfeited |
| B hold-only qty 1 | -$2 to +$5 | -$10 to -$3 | 2-4h dev; unverifiable in a season |
| B' hold-only qty 10 | -$20 to +$50, SD ±$65 | -$28 to +$42 | FOK depth at size untested; ruin risk on $75 |
| C race-winning infra | upper bound ~$100-140 if 100% of races won | negative realistically | must beat competitor p10 25ms with 94ms RTT |
Owner time: 1-2 h/month of monitoring (six silent failures, two OOMs) at any reasonable rate ($30-50/h) = $30-100/mo, exceeding every central estimate above.
Non-monetary (labelled): research value is largely already captured — the open "does the signal predict outcome" question is answered (yes, but it belongs to the faster player).

## 6. What would flip it
Measured Polymarket US order-ack RTT p50 <= 30ms (vs 94ms) through some available path at <= $10/mo extra. Then run hold-only qty 1 for 4 weeks; kill if fill rate on arbs that are later lifted (beat-the-lifter rate) < 30% OR fill-level hold EV < +2c after 300 fills.

## 7. Missed by both
- Ledger sign bug on transfers (brief got it right by hand).
- Fee coefficient: executions.csv/bull use 0.05; ledger says ~0.0695.
- Promotions > strategy: lifetime trading ~-$5.6 vs +$46 bonuses. Incentive release on trading ("Releasing 0.35 in incentives due to position change") — check whether the $25 bonus is released by volume before abandoning it; if so, a few manual trades may unlock it, worth more than months of the bot.
- Liquidity program $1.12 (May) came while we had maker SELL exits; hold-only would forfeit it. Market making with Kalshi as fair-value anchor is what the people beating us are doing — the actual business here — but it needs exactly the speed we lack (bear's maker proxy -7.1c).
- Hold-vs-exit difference is not significant (CI [-3.8,+14.9]).
- Bankroll ruin at qty 10.
- Seasonality: weekday volume already ~5-15 attempts/day; winter = NHL+NBA weekdays, CFB Saturdays to early Jan; then NBA/NHL only until MLB in April.
