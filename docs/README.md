# prediction-arb documentation

Working memory for a cross-platform prediction-market arbitrage scanner + executor
(Kalshi vs Polymarket US, game-winner markets). These files exist so that research
already paid for is never paid for twice.

`CLAUDE.md` in the repo root is the always-loaded index. It stays lean and points here.
Everything long-form lives in this directory.

## Map

| Doc | Contains | Read it when |
|---|---|---|
| [architecture.md](architecture.md) | Module map, run loop, discovery flow, arb math, fee model, data files | Changing any code |
| [platforms.md](platforms.md) | Kalshi + Polymarket US wire details, auth, API traps, undocumented behaviour | Touching a scraper or the API |
| [sports.md](sports.md) | Per-sport support, slug derivation, team-name maps, CFB matching + capacity | Adding a sport, or a sport matches 0 markets |
| [strategy-b.md](strategy-b.md) | The trading strategy, every filter, every tunable, exit paths | Changing execution behaviour |
| [operations.md](operations.md) | Operating model, `ops.sh`, VPS, systemd, deploy, logs, alerting, key rotation | Deploying, or the box looks dead |
| [performance-log.md](performance-log.md) | Chronological results: fills, P&L, latency, arb stats, per-session baselines | Judging whether a change helped |
| [findings-validated.md](findings-validated.md) | Approaches that measurably worked, with the evidence | Before re-litigating a solved question |
| [findings-rejected.md](findings-rejected.md) | Dead ends, reverted changes, rejected designs — and why | Before proposing an idea that "sounds obvious" |
| [incidents.md](incidents.md) | Post-mortems of every outage and silent failure | Something broke; check if it broke before |
| [open-questions.md](open-questions.md) | Unresolved investigations, each with a measurement protocol | Picking up research |
| [future-work.md](future-work.md) | Unimplemented ideas, ranked by leverage | Choosing what to build next |

## Operating the live system

Claude runs the infrastructure; the user directs. Everything goes through one script —
`./deploy/ops.sh status | deploy | restart | stop | start | logs | pull | reconcile |
heartbeats | timer`. It encodes the safety checks (open-position guard, syntax check before
restart, wait for a genuinely new heartbeat) so they are executed rather than remembered.
Details and the escalation rules: [operations.md](operations.md#operating-model).

## The one-line thesis

Kalshi and Polymarket US price the same game differently for tens to hundreds of
milliseconds. We detect the gap and trade **one leg** (Strategy B): buy the cheap side
on Polymarket, sell into the convergence. Not a true two-leg arb — a directional bet
with an arb-shaped signal. See [strategy-b.md](strategy-b.md).

## Documentation protocol (do this at the end of every session)

Docs are only worth what the last update made them worth. Before a session ends:

1. **Record measurements, not impressions.** Any number that came from a real run goes
   into [performance-log.md](performance-log.md) with its `n`, its date, and the
   conditions it was measured under. A number without an `n` is an opinion.
2. **Route the finding.** Worked → [findings-validated.md](findings-validated.md).
   Didn't → [findings-rejected.md](findings-rejected.md), *with the reason*, so it is
   not retried. Still unknown → [open-questions.md](open-questions.md) **with the exact
   protocol** to resolve it (what to run, what to compare against, how long to wait).
3. **Close the loop.** When an open question is answered, move it out of
   open-questions.md — do not leave an answered question open. When a future-work item
   ships, move it to findings-validated or -rejected.
4. **Check CLAUDE.md is still lean.** It should hold: the thesis, the invariants that
   prevent expensive mistakes, and links. If a section there grew past a few lines,
   it belongs in a doc with a pointer left behind.
5. **Fix cross-links.** Every doc that names another doc links it. Every memory file in
   `~/.claude/projects/.../memory/` that covers a topic owned by a doc points at that
   doc rather than restating it.
6. **Date every claim that can go stale.** Series IDs, balances, thresholds, prices,
   and "as of" statements all rot. Write `(2026-09-19)` next to them.

### Where a fact belongs

- **Invariant / trap that would cause real loss** → CLAUDE.md, one line + link.
- **How something works** → architecture / platforms / sports / strategy-b.
- **What happened** → performance-log (numbers) or incidents (failures).
- **What we concluded** → findings-validated / findings-rejected.
- **What we don't know yet** → open-questions, with a protocol.
- **User preferences and cross-project working style** → memory files, not docs.
