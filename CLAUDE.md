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
  pages, and `MemoryHigh=600M` guarantees swapping starts before memguard's 500MB
  threshold. A flat RSS curve can mean memory is growing into swap — it OOM-killed us at
  ~1.6GB with RSS reading 368MB. Read `VmSwap` too.
  → [docs/incidents.md](docs/incidents.md#2026-09-20-2050-utc-oom-kill-that-memguard-could-not-see)
- **CFB is detect-only** (`excluded_sports = {"CFB"}`) — thresholds were fitted on baseball
  and hockey.

## Current state (2026-09-20)

Live on AWS EC2 t3.micro under systemd, trading MLB + NHL, detecting CFB. Revived
2026-09-19 after a 127-day outage caused by four independent silent breakages. OOM-killed
and auto-restarted 2026-09-20 20:50 UTC, 19h into the leak measurement window. Net P&L over
the last measured window (2026-05-12→15, n=33 closed trades) is **−$0.97**, dominated by
six gap losses; converged exits are 8/8 profitable. Exits logged before 2026-05-14 were
not fill-verified, so that figure is an upper bound.

Highest-leverage open items: the memory leak's true source, event-loop lag at 32-72ms, and
whether the velocity filter generalises. All three are **blocked on elapsed runtime, not
work** — the leak protocol alone needs 72h uninterrupted, and our own deploys reset it.
Batch changes while a measurement window is open. See
[docs/open-questions.md](docs/open-questions.md).

## Operating model

**Claude runs the infrastructure.** Building, deploying, restarting, stopping and health
checks on the VPS and AWS are Claude's to execute via `./deploy/ops.sh`; the user directs
rather than types. Do not hand the user a command to paste as the default answer — run it,
report what happened, and surface the decision instead of the keystrokes.

Still check in before acting: anything destructive, anything that stops trading, and
anything with an open position at risk. Approval for one action is not approval for the
next. Manual terminal work remains a valid fallback when tooling cannot do it (or when the
user asks), and a few things genuinely require the user — anything needing an interactive
login, or a new third-party account. → [docs/operations.md](docs/operations.md)

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

Analyse a `vps_pull_*/` directory (`ops.sh pull` makes a dated one), never the stale
repo-root CSVs. Read CLOSE rows only; quote
medians, never means. → [docs/performance-log.md](docs/performance-log.md#how-to-read-the-data-files-without-getting-it-wrong)
