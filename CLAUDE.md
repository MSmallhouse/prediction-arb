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

Signal quality is real but **sport-dependent**, which the old single-number framing hid:
within-15s convergence is 70.5% for CFB, 62.2% for MLB and **32.8% for NHL** (replay,
2026-09-22). The often-quoted 67.9% is the MLB/CFB figure, not a system constant.

**The bottleneck is sport mix, not fill rate.** "Fill rate is the bottleneck" held while
per-trade EV was positive; at −2.85c/trade a better fill rate loses money faster. Fixing
which sports we trade comes first. → [docs/findings-validated.md](docs/findings-validated.md#convergence-rate-is-a-property-of-the-sport-not-of-the-filter-stack)

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

## Current state (2026-09-23)

Live on AWS EC2 t3.micro under systemd, trading MLB + NHL + **CFB** (CFB enabled
2026-09-22, commit `9846e13`, first slate Saturday 2026-09-26). Revived 2026-09-19 after a
127-day outage caused by four independent silent breakages. OOM-killed twice since —
2026-09-20 20:50 and 2026-09-22 06:30 UTC.

Net P&L since the revive is **−$0.94** over n=33 closed trades (**−2.85c/trade**). The
loss is concentrated in one sport: NHL is 24 of those 33 trades and −$0.73 of the loss,
converging 1/24. MLB is 4/9 converged, −$0.21. Converged exits remain the only profitable
path (5/5); timeouts are 2/25 and price-drops 0/3. **NHL was deliberately left tradeable**
to keep collecting on the worst sport rather than to make money on it.

Highest-leverage open items: whether CFB's convergence edge survives contact with real
fills, the memory leak's true source, and event-loop lag. All are **blocked on elapsed
runtime, not work** — and our own deploys reset the clock, so batch changes while a
measurement window is open. See [docs/open-questions.md](docs/open-questions.md) and the
open window in
[docs/performance-log.md](docs/performance-log.md#measurement-window-opened-2026-09-23-0011-utc).

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
