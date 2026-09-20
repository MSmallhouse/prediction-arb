# prediction-arb

Cross-platform prediction-market arbitrage scanner + **Strategy B** executor.
Kalshi vs Polymarket US on MLB / NBA / NHL / CFB game-winner markets.

**This file is the index. It stays lean — long-form knowledge lives in [`docs/`](docs/).**
Read [`docs/README.md`](docs/README.md) first; it maps every doc and defines the
end-of-session documentation protocol.

| Need | Doc |
|---|---|
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

Signal quality is validated (77% counterfactual win rate, 67.9% convergence hit rate under
the full filter stack). **Fill rate is the bottleneck** — 52.6% of 4%+ arbs close in under
85ms, faster than our fastest round trip.

## Invariants — violating any of these has cost real money

- **Every gate in this system fails closed and quiet.** A readiness flag that never sets
  looks exactly like a market with no opportunities. Six silent failures so far; process
  liveness would have caught none. Health checks must assert on *meaning*.
  → [docs/incidents.md](docs/incidents.md#the-silent-failure-pattern)
- **`Stores: N Kalshi markets, M Poly markets`** — `M == 0` with `N > 0` means discovery is
  broken, not that markets are quiet. That signature ran for four months undetected.
- **`executions.csv` is intent, not truth.** Run `python3 reconcile.py executions.csv`
  after every session. When it disagrees with `portfolio.activities()`, the API wins — and
  so does the user's lived experience in the Polymarket app.
- **Never start the scanner by hand on the VPS.** The systemd service is `enabled` and
  auto-restarts; a manual run is a second executor on the same account. Check
  `systemctl is-active arb-scanner` first.
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
- **CFB is detect-only** (`excluded_sports = {"CFB"}`) — thresholds were fitted on baseball
  and hockey.

## Current state (2026-09-19)

Live on AWS EC2 t3.micro under systemd, trading MLB + NHL, detecting CFB. Revived
2026-09-19 after a 127-day outage caused by four independent silent breakages. Net P&L over
the last measured window (2026-05-12→15, n=33 closed trades) is **−$0.97**, dominated by
six gap losses; converged exits are 8/8 profitable. Exits logged before 2026-05-14 were
not fill-verified, so that figure is an upper bound.

Highest-leverage open items: the memory leak's true source, event-loop lag at 32-72ms, and
whether the velocity filter generalises. See
[docs/open-questions.md](docs/open-questions.md).

## Quick reference

```bash
./deploy/ops.sh status      # live health: service, commit, heartbeat, alerts, stuck positions
./deploy/ops.sh deploy      # the ONLY deploy path: push → pull → syntax check → restart → verify
./deploy/ops.sh restart     # refuses if a position is open; --force to override
./deploy/ops.sh reconcile
```

Never `scp` code or restart by hand — `ops.sh` encodes the safety checks, and scp
desynchronizes the box's git state. Nothing deploys automatically; pushing does nothing
until `ops.sh deploy` runs. SSH is IP-locked, so a timeout ≠ a dead box.

Analyse `vps_pull_20260919/`, not the stale repo-root CSVs. Read CLOSE rows only; quote
medians, never means. → [docs/performance-log.md](docs/performance-log.md#how-to-read-the-data-files-without-getting-it-wrong)
