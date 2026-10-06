# Profitability debate — shared data brief (2026-10-06)

Repo: .  (read CLAUDE.md, docs/strategy-b.md,
docs/performance-log.md, docs/findings-validated.md, docs/findings-rejected.md,
docs/open-questions.md, docs/future-work.md for context).

Fresh data pull: ./vps_pull_20261006_2027/
- executions.csv (859 rows, intent log — NOT ground truth). Since 2026-09-19: 606 attempts, 109 fills.
- arb_durations_3.csv / _4.csv (every detected arb; OPEN+CLOSE rows — use CLOSE rows only; never union the two)
- convergence_log.csv (149MB, per-tick price path after each arb opens; t_offset_ms from first_seen)
- poly_ledger.csv — GROUND TRUTH: every Polymarket fill/resolution/deposit from portfolio.activities()
- poly_activities.json — raw API dump behind the ledger
- scanner.log, research/poly_com_tape_*.csv(.gz) (full-tick polymarket.com tape, research only)
Read docs/performance-log.md "How to read the data files" before touching the CSVs. Quote medians.
DO NOT touch the VPS, deploy, or edit repo files. Write scratch files only under
vps_pull_20261006_final/analysis/

## Established facts (verified by the lead)

**Ground-truth P&L** from Polymarket buying-power readings in scanner.log across 17 days:
68.63 (2026-09-19) → +$35 deposit/bonus → +$5 transfer in → −$5 transfer out (09-24) → 100.67 now, flat, no open positions.
=> **Real trading P&L 2026-09-19 → 10-06 ≈ −$2.96** (~−$0.17/day). Logged executions.csv says −$3.55
(difference = 2 SELL_EXIT_FAILED + held-to-expiry positions that resolved).
Account: ~$100 balance, of which $25 is a non-withdrawable bonus.

**Since revive (2026-09-19 → 10-06, 17 days), executions.csv:**
- 606 buy attempts, 109 fills (18.0% fill rate; was 29.7% in May). 489 failures are ORDER_STATE_EXPIRED (taker race lost).
- Fill rate by sport: NHL 32.7% (n=159), NBA 28.6% (n=7), MLB 13.5% (n=156), CFB 12.0% (n=284).
- Exits: timeout 81 (74%), converged 19 (17%), price_drop 7, exit_failed 2.
- Logged P&L by sport: CFB −$1.45 (n=34, incl. 2 exit_failed −$1.05), NHL −$1.31 (n=52), MLB −$0.65 (n=21), NBA −$0.14 (n=2).
- Converged exits: +3.8–4.1c each, always profitable. Timeouts: median −3.3c. Price drops: median −10–12c.
- Median per-trade logged P&L −2.85c. quantity=1 on every trade (hard invariant until a scaling bug is fixed).
- Median buy price 0.50, median gross spread 4.5%, median poly_depth at fire 19 shares.
- Buy latency median 94ms (fills) / 113ms (failures). Docs: 72.9% of 4%+ arbs close in <85ms.
- Attempts/day median 17, max 120 (CFB Saturdays). Fills/day ~6.4 mean.
- Replay predicted within-15s convergence of 70.5% CFB / 62.2% MLB / 32.8% NHL; LIVE converged-exit share
  is ~17% overall (CFB 8/34, MLB 5/21, NHL 6/52). This replay→live gap is the central fact to explain.
- Fees: Polymarket taker fee ~1–2c per side at qty 1 (feeCoefficient 0.0695 on market objects); maker exits are fee-free.

**Structural constraints**: one Polymarket account (~$75 withdrawable), t3.micro, single leg (no Kalshi hedge — Kalshi key is read-only),
quantity locked at 1, US game-winner markets only (MLB season ending, NHL/NBA starting, CFB Saturdays).
