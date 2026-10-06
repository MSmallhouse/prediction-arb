# prediction-arb

Cross-platform prediction-market arbitrage scanner + **Strategy B** executor.
Kalshi vs Polymarket US on MLB / NBA / NHL / CFB game-winner markets.

**This file is the index. It stays lean — long-form knowledge lives in [`docs/`](docs/).**
Read [`docs/README.md`](docs/README.md) first; it maps every doc and defines the
end-of-session documentation protocol.

| Need | Doc |
|---|---|
| **Why it was shut down, final P&L, revival condition — read first** | [docs/shutdown-review.md](docs/shutdown-review.md) |
| How the code is laid out, run loop, discovery, arb math, fees | [docs/architecture.md](docs/architecture.md) |
| Kalshi / Polymarket API traps and undocumented behaviour | [docs/platforms.md](docs/platforms.md) |
| Per-sport support, slug derivation, CFB matching, capacity | [docs/sports.md](docs/sports.md) |
| The strategy, every gate, every tunable, exit paths | [docs/strategy-b.md](docs/strategy-b.md) |
| VPS, systemd, logs, alerting, key rotation, runbook | [docs/operations.md](docs/operations.md) |
| Measured results, P&L, latency, arb stats | [docs/performance-log.md](docs/performance-log.md) |
| What worked / what didn't, with evidence | [docs/findings-validated.md](docs/findings-validated.md) · [docs/findings-rejected.md](docs/findings-rejected.md) |
| Outage post-mortems | [docs/incidents.md](docs/incidents.md) |
| Unresolved research, each with a protocol | [docs/open-questions.md](docs/open-questions.md) |
| Ranked roadmap | [docs/future-work.md](docs/future-work.md) |

## The thesis, in three lines

Kalshi and Polymarket US price the same game differently for tens to hundreds of
milliseconds. We detect the gap and trade **one leg only**: buy the cheap side on
Polymarket, sell into the convergence. The arb is the entry trigger, not the position.

**What killed it (2026-10-06):** neither sport mix nor fill rate. The replay convergence
rates (70.5% CFB, 62.2% MLB, 67.9% overall) were measured on arbs a faster participant
takes; the arbs we fill converged 9-17% and hold to ≈0 at settlement. The bottleneck is
**speed**, and it is not purchasable at our scale.
→ [docs/shutdown-review.md](docs/shutdown-review.md)

## Invariants — violating any of these has cost real money

These governed the live system and still apply to any revival.

- **Every gate in this system fails closed and quiet.** A readiness flag that never sets
  looks exactly like a market with no opportunities. Six silent failures so far; process
  liveness would have caught none. Health checks must assert on *meaning*.
  → [docs/incidents.md](docs/incidents.md#the-silent-failure-pattern)
- **`Stores: N Kalshi markets, M Poly markets`** — `M == 0` with `N > 0` means discovery is
  broken, not that markets are quiet. That signature ran for four months undetected.
- **`executions.csv` is intent, not truth.** Run `./deploy/ops.sh reconcile` after every
  session. When it disagrees with `portfolio.activities()`, the API wins — and so does the
  user's lived experience in the Polymarket app.
- **Never start the scanner by hand on the VPS.** The systemd service is `enabled` and
  auto-restarts; a manual run is a second executor trading the same Polymarket account.
  `ops.sh status` before anything.
- **Polymarket SHORT prices are inverted** — send `1 - yes_ask`. Silent and expensive when
  wrong.
- **The arb tracker needs the flattened FULL set of arbs**, not just the changed ones —
  `ArbTracker.update()` treats anything missing as CLOSED.
- **SIGTERM must drain the CSV writer queue.** `systemctl restart` sends SIGTERM, which
  does not raise `KeyboardInterrupt`.
- **`_in_flight` must stay global** — it is the only risk coordination against one ~$70
  balance.
- **Kalshi API key is `read`-scoped by design.** "Full access" includes `write::transfer`
  (withdrawals). Never grant it. The repo is PUBLIC; `.env` and `.env.*` are gitignored.
- **`quantity` cannot rise above 1** until the maker-sell/taker-exit scaling bug is fixed.
  → [docs/open-questions.md](docs/open-questions.md#known-unfixed-bug-quantity-scaling)
- **`rss` in the heartbeat is a lower bound, not memory.** `VmRSS` excludes swapped-out
  pages, so a flat RSS curve can mean memory is growing into swap. memguard now reads
  `VmRSS + VmSwap` (fixed 2026-09-22, after it sat at "no action" through a second OOM
  kill), but **the heartbeat still reports RSS alone.** Every systemd metric is blind
  here too: at the 2026-09-22 kill `MemoryCurrent` read 431MB and `MemoryPeak` 579MB,
  both under `MemoryMax=700M`, while swap was 961MB and anon was 1375MB — **it dies of
  swap exhaustion, not the cgroup cap.**
  → [docs/incidents.md](docs/incidents.md#2026-09-20-2050-utc-oom-kill-that-memguard-could-not-see)
- **CFB is tradeable as of 2026-09-22** (`excluded_sports` is now empty). Enabled on a
  70.5% within-15s convergence replay vs NHL's 32.8%. The velocity threshold is still
  unvalidated for football — the first Saturday slate is the measurement, not a rollout.
  → [docs/open-questions.md](docs/open-questions.md#is-cfb-tradeable)

## Current state (2026-10-06): SHUT DOWN

**Trading stopped 2026-10-06 20:55 UTC. EC2 instance `i-0923ce83c9a4b7047` terminated, its
volume deleted; `ops.sh` and every SSH command in the docs now fail by design.** Nothing
billable remains on AWS for this project. → [docs/shutdown-review.md](docs/shutdown-review.md)

Final real P&L 2026-09-19 → 10-06: **≈ −$2.96** over 109 fills (lifetime trading ≈ −$5.6).
The verdict: the signal is real and predicts game winners (+5.9c/side held to settlement,
n=428 games), but **92% of gated arbs are taken by a faster participant within 200ms, and
those carry all the edge; the arbs we fill carry ≈0.** The edge's half-life is ~60-80ms
against our 94ms round trip. Every "fill rate is the bottleneck" claim in older docs was
measured on the population we lose.

**Revival condition (the only one):** a measured Polymarket US order round trip of p50 ≤
30ms. Without it, nothing in [docs/future-work.md](docs/future-work.md) is worth building.

## Operating model (historical)

While live, Claude ran the infrastructure via `./deploy/ops.sh`; see
[docs/operations.md](docs/operations.md). There is no infrastructure now. Final data lives
in `vps_pull_20261006_final/` (local-only, gitignored — the repo is PUBLIC).

## Analysing the archive

Analyse a `vps_pull_*/` directory (the final one is `vps_pull_20261006_final/`), never the
stale repo-root CSVs. Read CLOSE rows only; quote medians, never means; bootstrap by game.
For P&L use buying power or `poly_activities.json`, not `executions.csv` — and transfer
amounts in the ledger are unsigned.
→ [docs/performance-log.md](docs/performance-log.md#how-to-read-the-data-files-without-getting-it-wrong) ·
[docs/platforms.md](docs/platforms.md#polymarket-us-activity-ledger-traps)
