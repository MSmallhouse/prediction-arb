# Approaches tried and rejected

Read this before proposing something that "sounds obvious." Everything here was either
measured and found not to help, or reasoned through and found actively harmful. Each
entry records **what would have to change** for it to be worth revisiting.

---

## GC tuning — three attempts, none fixed the pauses

`_gc_callback` records real collection pauses; the heartbeat reports
`gc N cols max NNms tot NNms` next to `loop lag max`.

**Baseline (2026-09-19):** startup/discovery windows show **12-13 collections, 755-898ms
max pause**, ~2000ms total. Median arb life is **36ms** (re-measured 2026-09-22; this line
previously said 85ms), so a single collection can sleep through dozens of opportunity
windows, not several. **Steady state shows ZERO collections** yet still
~42ms loop lag — so steady-state lag is *not* GC.

| Attempt | Result |
|---|---|
| `gc.freeze()` after warmup (63,433 objects frozen) | No change — 898ms max pause on the next discovery |
| `gc.disable()` during discovery | **Worse**: 9 collections / 1219ms max. Deferring only concentrated the pause; same total GC time. **Reverted.** |
| Per-page extraction (stop accumulating raw event dicts across pages) | No GC change — 858ms max. **Kept anyway**: strictly less memory-hungry, RSS notably lower |

**Why freeze failed:** the pauses come from the *transient garbage* discovery allocates
(CFB events carry ~444 markets each), not from rescanning long-lived objects. Freezing
what survives does nothing about what churns.

⚠️ **Measurement caveat on the freeze result.** `gc.freeze()` runs 120s after startup, but
every measurement above was taken in the *startup* window, before the freeze had run. Its
effect on the **hourly discovery cycle is UNMEASURED**, not disproven. To test properly:
let the service run undisturbed through an hourly discovery (no restarts) and compare that
window's `gc` figures against the startup window's. The freeze call is currently left in
place — harmless and small; remove it if heap retention shows up in the leak investigation.

**Would revisit if:** the freeze measurement above is done properly and shows a win; or
allocation is cut at the source (discard prop markets *during* JSON parse rather than
after); or discovery moves to a subprocess so its garbage never touches the trading heap.
Note the constraint on that last one — the child shares the service cgroup, so
`MemoryMax=700M` would need raising, and the box only has 908MB.

Related: [open-questions.md § Event-loop lag](open-questions.md#event-loop-lag-32-72ms-at-the-tail).

---

## Per-sport sharding across multiple VPSs

Evaluated and rejected 2026-09-19. Sharding market data by instrument **is** a standard
HFT pattern — once you are throughput-bound. We are not: single-threaded feed handlers
routinely sustain 100k+ messages/sec and we process a few hundred. The bottleneck was our
own **O(N) rescan**, which cost one refactor instead of N hosts (see
[findings-validated.md § Incremental detection](findings-validated.md#incremental-detection)).

**Why it would actively hurt us, not merely be unnecessary:**

- **One Polymarket account across all shards.** Balance, rate limits and the private order
  WS are all account-scoped. Every shard would receive order events for every other
  shard's orders — exactly what `await_terminal()` assumes cannot happen.
- `_in_flight` stops being global, so shards cannot coordinate risk against a shared ~$68
  balance.
- More hosts = more silent-failure surface, on a system that already died unnoticed for
  four months ([incidents.md](incidents.md#2026-05-15--2026-09-19-the-four-month-outage)).

**Cheaper things to do first, in order:** (1) incremental detection ✅ done, (2) CSV writes
off the event loop ✅ done, (3) loop-lag visibility ✅ done, (4) move off the burstable
t3.micro to fixed-performance if CPU approaches the 10% baseline (we run ~7%), (5)
`uvloop`, (6) skip games whose books cannot produce 4% (FCS books pinned at 0.01/0.00).

**If sharding ever happens:** multiple **processes on one box** before multiple hosts —
same isolation, no cross-host state, keeps colocation.

Source material worth re-reading if this is reopened:
- T3 burstable baseline/throttling — <https://aws.amazon.com/ec2/instance-types/t3/>, <https://dev.to/ssshreyans26/aws-burstable-instances-explained-cpu-credits-throttling-and-why-your-t3-instance-isnt-what-you-39o4>
- Measuring asyncio event-loop lag — <https://mergify.com/blog/detecting-blocking-tasks-in-asyncio-by-measuring-event-loop-latency>
- HFT feed-handler sharding norms — <https://medium.com/@gwrx2005/design-and-implementation-of-a-low-latency-high-frequency-trading-system-for-cryptocurrency-markets-a1034fe33d97>
- Polymarket order lifecycle — <https://docs.polymarket.com/concepts/order-lifecycle>

---

## `synchronousExecution: true` — removed, do not reintroduce

Originally used to dodge ghost fills (the server purges filled-then-canceled orders before
`retrieve()` returns "not found"). It cost **100-460ms of server-side block on every
failed order**, which held `_in_flight` and prevented retrying subsequent opens of the
same arb.

Of 44 missed 4%+ arbs, **17 "should have fired" and didn't**, purely because `_in_flight`
was still locked by a stalled prior attempt.

**Replaced by** the private order WebSocket (see
[findings-validated.md](findings-validated.md#private-order-websocket)). Fill rate went
5.5% → 27.8%.

---

## FOK for taker exits — replaced by IOC + verification

FOK is all-or-nothing, so any bid movement between our read and the order's arrival killed
the order entirely — and we logged a fictional sell. Cost ~$2.04 of positions silently
held to expiry. Full post-mortem: [incidents.md](incidents.md#2026-05-14-fok-taker-exits-logged-fictional-fills).

**Note the asymmetry:** FOK is still correct for the *entry* buy at `quantity=1` (nothing
to partially fill). It is wrong for exits, where getting out partially beats not at all.

---

## The ~1s "sports taker delay" — investigated, NOT confirmed

An early fill logged `buy_latency_ms = 1178`, which looked like the ~1s sports taker delay
Polymarket documents for polymarket.com. **Subsequent fills refuted it** — same evening,
same account, same markets:

| Time | Game | Action | buy_latency_ms |
|---|---|---|---|
| 21:38 | PHI @ NYM | BUY_FAILED | 1178 |
| 22:03 | BOS @ TB | BUY (filled) | 123 |
| 22:04 | MIL @ BAL | BUY (filled) | 67 |

67ms matches the May baseline (60ms median) exactly. The 1178ms outlier was the **first
order placed after a fresh private-WS connection** and carried a 41s-stale Polymarket
price (`ws_age=P41503ms`) — a cold-path artefact, not a platform delay.

**Conclusion:** there is no evidence of a ~1s taker delay on polymarket.us. The taker path
remains viable and the strategic pivot to maker-only is **not forced**. Keep watching
`buy_latency_ms`; reopen if it clusters near 1000ms.

---

## `screen` as a process supervisor

It cannot restart a dead process. This is the entire reason the 2026-05-15 OOM became a
four-month outage. Replaced by systemd with `Restart=always` + `OOMPolicy=restart`.
See [operations.md](operations.md).

---

## Readiness gated on the order-channel snapshot alone

Polymarket's `SUBSCRIPTION_TYPE_ORDER` channel sends **nothing** — no snapshot, no `eof`,
no heartbeat — when the account has zero open orders. Gating readiness on it hangs forever
and disables the executor silently. Confirmed via spike against both raw WS and the SDK's
`PrivateWebSocket`. Details in [platforms.md](platforms.md#the-order-channel-is-silent-when-there-are-no-open-orders)
and the outage in [incidents.md](incidents.md#2026-09-19-evening-the-executor-was-silently-disabled).
