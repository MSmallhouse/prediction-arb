## GC pauses: three fixes tried, none confirmed working (2026-09-19)

`_gc_callback` records real collection pauses; the heartbeat reports `gc N cols max NNms tot NNms` beside `loop lag max`.

Baseline: **startup/discovery windows show 12-13 collections, 755-898ms max pause**, ~2000ms total. Median arb life is 85ms, so one collection can swallow several opportunity windows. Steady state shows **ZERO collections** yet ~42ms loop lag, so steady-state lag is NOT GC — most likely burst WS frame processing (real work, not a stall).

Attempts, all measured against the startup window:

| attempt | result |
|---|---|
| `gc.freeze()` after warmup (63,433 objects) | no change — 898ms max |
| `gc.disable()` during discovery | **worse**: 9 cols / 1219ms max. Deferring just concentrated the pause; same total GC time |
| per-page extraction (stop accumulating raw event dicts across pages) | no change — 858ms max |

**⚠️ Measurement flaw — the freeze result is not valid.** `gc.freeze()` runs 120s after startup, but every measurement above was taken in the *startup* window, before the freeze had run. So freeze's effect on the hourly discovery cycle is **UNMEASURED**, not disproven. To test it properly: let the service run undisturbed through an hourly discovery (no restarts) and compare that window's `gc` figures against the startup window's.

`gc.disable()` was reverted (it made the tail worse). Per-page extraction was KEPT — it did not help GC, but it is strictly less memory-hungry and RSS was notably lower afterwards.

Remaining candidate if freeze turns out not to help: run discovery in a subprocess so its garbage never touches the trading heap. Note the constraint — the child shares the service cgroup, so `MemoryMax=700M` would need raising, and the box only has 908MB.

# prediction-arb

Cross-platform prediction market arbitrage scanner + Strategy B executor. Kalshi vs Polymarket US on MLB/NBA/NHL game-winner markets.

## Architecture

```
main.py                      — event-driven WS run loop, REST discovery every 60min
config.py                    — fee constants, tax rates, thresholds, team name maps
arb_detector.py              — match markets, compute spreads (YES/NO optimization); `evaluate_event()` = single-game re-price for the tick path
arb_tracker.py               — OPEN/CLOSE duration tracking, CSV logging
convergence_tracker.py       — 60s post-arb price tracking for Strategy B analysis
executor.py                  — Strategy B live execution (buy on Poly, maker sell, timeout exit)
scrapers/kalshi.py           — Kalshi REST scraper (KXMLBGAME + KXNBAGAME + KXNHLGAME)
scrapers/kalshi_ws.py        — Kalshi orderbook_delta WS (RSA-PSS auth, local book state)
scrapers/kalshi_orders.py    — Kalshi FOK order placement (built, auth verified, $10 balance)
scrapers/polymarket_us.py    — Polymarket US REST discovery (polymarket-us SDK, seriesId + date-window query)
scrapers/polymarket_us_ws.py — Polymarket US market-data WS (ED25519 auth, raw websockets)
scrapers/polymarket_us_private_ws.py — Polymarket US private order-event WS (terminal-state await for executor)
scrapers/polymarket.py       — retained for PolymarketMarket dataclass + slug derivation only
csv_writer.py                — daemon-thread CSV writer; keeps file I/O off the event loop
reconcile.py                 — post-session audit: portfolio truth vs executions.csv
deploy/                      — systemd unit + restart timer + logrotate config for the VPS
```

## Platforms

**Kalshi** — CFTC-regulated. REST + orderbook_delta WS. RSA-PSS auth. Maker fee = 25% of taker.
**Polymarket US** (`polymarket.us`) — CFTC-regulated (QCX LLC). ED25519 auth. Maker fee = 0. Account = "fat.lobster". Balance ~$70.

## Discovery Flow

1. Kalshi: paginated `GET /events?with_nested_markets=true` per series
2. Derive slugs from Kalshi tickers: `KXMLBGAME-26APR241915PHIATL` → `mlb-phi-atl-2026-04-24`
3. Polymarket US: `client.events.list({"seriesId": [id], "startDateMin": ..., "startDateMax": ...})`, find the full-game winner market, match by slug (strip `aec-` prefix). Team names normalized via `_normalize_poly_team()`.
4. Each game → TWO `PolymarketMarket` objects (long + short) with synthetic token IDs
5. WS: Kalshi `orderbook_delta` by market_ticker, Poly US `market_data` by market_slug

Series IDs: MLB 2026 = 15, NBA 2025 = 4, NHL 2025 = 6, CFB 2026 = 225. Resolved dynamically at every discovery by `refresh_series_ids()` (reads `/v1/series`, picks the newest `<sport>-<year>` slug) — each season is a NEW series id, so hardcoded values go stale at rollover. As of 2026-09-19 Polymarket has not yet created `nba-2026` / `nhl-2026`.

## College Football (CFB) — detect-only since 2026-09-19

Added to gather data; **arbs are logged but never traded** (`executor.config.excluded_sports = {"CFB"}`). The velocity and price-drop thresholds were tuned on baseball/hockey, and football scores in 7-point chunks — unvalidated here.

**CFB is the one sport whose slug cannot be derived from the Kalshi ticker.** Kalshi writes `KXNCAAFGAME-26SEP19UNCCLEM`; Polymarket writes `cfb-ncar-clmsn-2026-09-19` — different abbreviation schemes. Matching instead works on **normalised school name + kickoff time**:

1. Both platforms expose the school name — Kalshi `yes_sub_title` ("North Carolina"), Polymarket `team.safeName`. `config.normalize_cfb_team()` reduces both to one canonical form, so ~250 teams need no hand-written map. Only 12 of 250 names genuinely diverge; those are in `CFB_NAME_ALIASES` (`UMass`→`Massachusetts`, `NC St.`→`North Carolina State`, …).
2. `main._join_cfb()` pairs Kalshi events to Polymarket events on (team pair + kickoff within `CFB_JOIN_MAX_HOURS`), then calls `scrapers.polymarket.register_cfb_slug_map()`.
3. `kalshi_ticker_to_poly_slug()` consults that registry for `KXNCAAFGAME` tickers, so `find_arbs()` and everything downstream are unchanged.

Unmatched Polymarket games are dropped rather than stored — otherwise they burn a WS subscription with no Kalshi counterpart to arb against.

**Capacity — the binding constraint.** A Saturday slate is 291 open Kalshi CFB events. Two guards:
- `CFB_LOOKAHEAD_HOURS = 3.0` limits discovery to games near kickoff (the executor only trades inside 180 min anyway). Measured 2026-09-19: 6h = 73 games = 512MB RSS; 3h = 39 games = 444MB RSS vs 275MB without CFB.
- `fetch_all_prices()` pulls an ENTIRE Kalshi series regardless of the window, so CFB markets outside the lookahead are filtered out in the discovery loop before reaching the stores. Without that filter all 582 CFB markets get subscribed.

systemd caps were raised to `MemoryHigh=600M` / `MemoryMax=700M` to fit this. Verified live: `K 212/212 confirmed, P 192/192 confirmed`, 39/39 CFB events joined, 0 unmatched.

Polymarket CFB fee coefficient is 0.0695 — identical to MLB, so the fee model is unchanged.

## Arb Detection

4 Kalshi order books per game (Team A YES/NO, Team B YES/NO). YES on Team A = NO on Team B (same payout). Pick cheaper. Both prices from real-time `orderbook_delta`.

```
p1 = min(k_market.yes_ask, opp_k_market.no_ask)
p2 = poly_market.yes_ask
gross = 1 - p1 - p2
```

## 2026-09-19 Outage Post-Mortem (bot dead 2026-05-15 → 2026-09-19)

Four independent breakages, all now fixed. Any one alone stops trading.

1. **OOM kill.** `Killed process 49704 (python3) anon-rss:528264kB` at 2026-05-15 14:08:43 UTC. t3.micro = 908MB, no swap, and `screen` does not restart a dead process. (An earlier OOM on 2026-05-01 killed it the same way.) Leak rate ≈ 38MB/day on top of a ~265MB steady state; `prune WS subscription state` slowed it but did not stop it. Fix: 1GB swapfile + systemd unit with `Restart=always`, `MemoryHigh=550M`, `MemoryMax=650M`, `OOMPolicy=restart`, plus a daily 10:00 UTC recycle timer. Heartbeat now logs `rss NNNMB tasks N` so the leak can actually be measured.
2. **`series_id` was never a real filter.** The gateway silently IGNORES the snake_case `series_id` query param and returns events from every series — an MLB query came back full of NFL events. The honoured param is `seriesId` as a LIST of ints. Discovery used to compensate by paging blindly from a hardcoded offset and slug-matching; once the season moved past those offsets it matched zero markets forever. Now it queries `seriesId` + `startDateMin`/`startDateMax` for the exact Kalshi date window — one page, no offset cache.
3. **Market-type tag renamed.** `sportsMarketType == "moneyline"` no longer exists; it is now sport-specific (`baseball_team_full_game_winner`, `hockey_team_full_game_winner`, …). Events also now carry ~444 markets each (props, per-inning, spreads), and first-five/per-inning winner markets share `sportsMarketTypeV2 == SPORTS_MARKET_TYPE_MONEYLINE` — so matching on V2 alone picks the wrong market. `_pick_full_game_moneyline()` matches the `_full_game_winner` suffix, with the legacy `"moneyline"` value as fallback.
4. **Kalshi NHL labels went city-only.** `yes_sub_title` changed from `"SEA Kraken"` to `"Seattle"`, so every NHL market failed to parse. All 32 city labels added to `NHL_KALSHI_TO_CANONICAL`. Separately, `NHL_KALSHI_ABBR_SET` was missing `CHI` and `NJ` (Kalshi writes the Devils as `NJ`, not `NJD`), which dropped NYI@NJ / NYR@NJ / MIN@CHI games and spammed a warning on every WS tick.

Lesson: every one of these fails SILENTLY — the scanner logged healthy heartbeats while matching zero Poly markets. Watch `Stores: N Kalshi markets, M Poly markets` in the log; M == 0 with N > 0 means discovery is broken, not that markets are quiet.

## Hot-Path Architecture (reworked 2026-09-19)

Three changes, made after measuring where the latency budget actually goes. Fill latency is ~60ms median and is almost entirely network + exchange; in-process detection was ~1ms. So none of this was about raw arb math — it was about the event loop staying free to service the *next* tick.

**1. Incremental detection.** `_check_arbs()` used to rebuild both index dicts and rescan every game on every tick. Since tick rate also scales with slate size, total CPU grew ~quadratically. Now `arb_detector.evaluate_event()` re-prices only the game that ticked, into `main._arb_cache` (event_ticker → opportunities), and the tracker still receives the flattened full set — that last part is essential, because `ArbTracker.update()` treats any arb missing from the list as CLOSED. Pass `changed_events=None` for a full rescan (startup, post-discovery).

Measured per-tick detection cost (dev machine; t3.micro is slower, ratios hold):

| games | old full scan | new per-event | |
|---|---|---|---|
| 67 | 0.099 ms | 0.003 ms | 29x |
| 96 | 0.142 ms | 0.004 ms | 35x |
| 291 | 0.433 ms | 0.009 ms | 47x |

The point is the *flatness*: cost barely grows with slate size now, so adding sports is close to free.

**2. CSV writes off the event loop.** `log_arb_duration()` and the convergence flush did synchronous `open()`/`write()` from inside the tick path — one blocking call stalls every other market. `csv_writer.py` owns a daemon thread and a queue; rows are still BUILT on the loop (so they capture prices at the instant they were true) and only the I/O is handed off. `convergence_tracker.flush_expired()` also moved off the tick path onto `_housekeeping_loop()` (5s cadence; trackers expire on a 60s horizon).

⚠️ **SIGTERM drains the queue.** Rows now live briefly in a daemon-thread queue, and `systemctl restart` sends SIGTERM, which does NOT raise `KeyboardInterrupt`. Without `_install_sigterm_handler()` every restart — including the daily 10:00 UTC recycle — would silently discard queued rows.

**3. Event-loop lag watchdog.** `_loop_lag_monitor()` samples every 250ms; the heartbeat reports `loop lag max NNms` (peak per 5-min window) and `csvq` (writer backlog), then resets.

**Measured result of all three** (same CFB load, before vs after): total CPU **8.1-8.3% → 6.7-7.1%**. Much smaller than the 30-47x detection speedup, because detection was never the bulk of CPU — WS parsing, JSON decode, TLS and logging dominate. The real win is that CPU no longer scales quadratically with sport count.

## Why NOT shard per-sport across VPSs (decided 2026-09-19)

Considered and rejected for now. Sharding market data by instrument IS a standard HFT pattern once you are throughput-bound, but single-threaded handlers routinely sustain 100k+ messages/sec and we are doing a few hundred — the bottleneck was our own O(N) rescan, which cost one refactor instead of N hosts.

Specific hazards for THIS system, which are what make it a bad trade rather than merely unnecessary:
- **One Polymarket account across shards.** Balance, rate limits and the private order WS are all account-scoped. Every shard would receive order events for every other shard's orders — exactly what `await_terminal()` assumes cannot happen.
- `_in_flight` stops being global, so shards cannot coordinate risk against a shared ~$68 balance.
- More hosts = more silent-failure surface on a system that already went dead for four months unnoticed (see the alerting gap).

Cheaper things to do first, in order: (1) incremental detection ✅ done, (2) CSV off-loop ✅ done, (3) loop-lag visibility ✅ done, (4) move off burstable t3.micro to a fixed-performance instance if CPU approaches the 10% baseline, (5) `uvloop`, (6) skip games whose books cannot produce 4% (FCS books pinned at 0.01/0.00). If sharding ever happens, do **multiple processes on one box** before multiple hosts — same isolation, no cross-host state, keeps colocation.

Re-discover the reasoning:
- AWS T3 burstable baseline/throttling: https://aws.amazon.com/ec2/instance-types/t3/ and https://dev.to/ssshreyans26/aws-burstable-instances-explained-cpu-credits-throttling-and-why-your-t3-instance-isnt-what-you-39o4
- Measuring asyncio event-loop lag / blocking: https://mergify.com/blog/detecting-blocking-tasks-in-asyncio-by-measuring-event-loop-latency
- HFT feed-handler sharding norms: https://medium.com/@gwrx2005/design-and-implementation-of-a-low-latency-high-frequency-trading-system-for-cryptocurrency-markets-a1034fe33d97
- Polymarket taker delay / order lifecycle: https://docs.polymarket.com/concepts/order-lifecycle

## 2026-09-19 (evening): executor was silently disabled — FIXED

**Symptom:** 103 MLB 4%+ arbs detected in one evening, **zero execution attempts**. `executions.csv` had only its header for the day.

**Cause:** `PolymarketUSPrivateWSClient` armed the executor only after receiving an `accountBalanceSubscriptionSnapshot`. Polymarket **stopped sending that snapshot**. Verified from both our client and the SDK's own `PrivateWebSocket`: subscribing to `SUBSCRIPTION_TYPE_ORDER`, `_POSITION` and `_ACCOUNT_BALANCE` yields **zero frames** when there are no open orders/positions. The socket connects and stays open; it is simply silent. The executor's `if _private_ws is None or not _private_ws.is_ready: return` guard then blocks every arb, without logging anything.

**Fix:** `_confirm_ready()` now arms the executor after the subscribes are sent AND a REST `account.balances()` call succeeds. The old snapshot path is retained, so if Polymarket restores the snapshot we automatically resume using the stronger proof.

⚠️ **This is a weaker guarantee than before.** It proves the socket connected, subscribes were sent, and credentials work — it does NOT prove the order channel will deliver. The mitigation is the pre-existing fallback: when `await_terminal()` times out (1s), the executor checks `portfolio.positions()` to learn the real outcome. Watch for `position fallback` warnings in the log; a rise means the order channel is dead and fills are being resolved by the slow path.

**Lesson (third silent failure found in one day):** every gate in this system fails closed and quiet. A readiness flag that never sets looks exactly like a market with no opportunities.

## Sports taker delay — investigated and NOT confirmed (2026-09-19)

An early attempt logged `buy_latency_ms = 1178`, which looked like the ~1s sports taker delay Polymarket documents for polymarket.com. **Subsequent fills refuted it.** Same evening, same account, same markets:

| time | game | action | buy_latency_ms |
|---|---|---|---|
| 21:38 | PHI @ NYM | BUY_FAILED | 1178 |
| 22:03 | BOS @ TB | BUY (filled) | 123 |
| 22:04 | MIL @ BAL | BUY (filled) | 67 |

67ms matches the May baseline (60ms median) exactly. **There is no evidence of a ~1s taker delay on polymarket.us.** The 1178ms outlier was the first order placed after a fresh private-WS connection and carried a 41s-stale Polymarket price (`ws_age=P41503ms`); treat it as a cold-path artefact, not a platform delay.

Conclusion: the taker path is still viable on polymarket.us, and the strategic pivot to maker-only is NOT forced. Keep watching `buy_latency_ms`; revisit if it clusters near 1000ms.

## First live trades since May (2026-09-19 evening)

| game | buy | sell | exit | hold | P&L |
|---|---|---|---|---|---|
| BOS @ TB | 0.2500 | 0.3000 | SELL_CONVERGED | 464ms | **+$0.0406** |
| MIL @ BAL | 0.3950 | 0.3000 | SELL_PRICE_DROP | 5133ms | **-$0.1174** |

Net -$0.077 on two fills. The converged exit hit its +5c target in 464ms — the strategy working as designed. The loss gapped ~9.5c through the 5c stop DESPITE passing the Kalshi velocity filter, which is a data point for the open velocity-threshold question: the filter did not prevent this gap.

## Health checks & alerting (added 2026-09-19)

Three silent failures in one day (SG lockout, discovery matching zero markets, executor never arming) motivated this. Process liveness would have caught NONE of them, so the checks assert on meaning:

- no Polymarket markets confirmed while Kalshi has some → discovery/WS broken (the May–Sep outage)
- executor enabled but private WS not ready → cannot trade (the 2026-09-19 evening outage)
- N arbs at 4%+ with zero execution attempts → a filter is blocking everything (same outage, different signature)
- no price tick for 180s → feeds stalled
- CSV writer backlog, or positions left open by failed taker exits

Wired into the heartbeat; each problem logs `HEALTH ALERT: ...` at ERROR level **and emails the operator**.

**Email delivery (the primary channel, since this box runs unattended for months):**
- SNS topic `arn:aws:sns:us-east-1:828841719603:arb-scanner-alerts`, subscribed to the operator's email.
- The EC2 instance carries IAM role **`arb-scanner-role`** via instance profile `arb-scanner-profile`, scoped to `sns:Publish` on that topic ONLY. boto3 picks the credentials up from IMDSv2 — **there are no AWS keys in `.env`**. If alerting ever stops, check the role is still associated: `aws ec2 describe-iam-instance-profile-associations --region us-east-1 --filters Name=instance-id,Values=i-0923ce83c9a4b7047`.
- `main._send_email_alert()` de-duplicates: an identical problem set is not re-sent for `ALERT_REPEAT_SUPPRESS_S` (1h), so a long outage doesn't flood the inbox.
- Emails include confirmed-market counts, arb count, execution attempts and RSS — enough to triage without SSHing in.

Also available, both optional and no-op when unset:
- `ALERT_WEBHOOK_URL` — POSTed a JSON summary (Slack/Discord).
- `ALERT_HEARTBEAT_URL` — pinged on every HEALTHY heartbeat. Dead man's switch: with healthchecks.io the ABSENCE of a ping alerts, which is the only way to catch the box disappearing entirely. **Still unset** — SNS cannot detect a process that never runs, only the CloudWatch NetworkIn alarm can, and that takes 15 minutes.

AWS-side backstop alarms (no box involvement, same topic):
- `arb-scanner-process-dead` — NetworkIn < 10MB/5min for 15min (live ~250MB/5min, dead ~3KB).
- `arb-scanner-cpu-credits-low` — CPUCreditBalance < 50, the throttling early warning.

⚠️ **A subscription must be CONFIRMED to deliver.** A pending subscription silently swallows every alert — publishes succeed, nothing arrives. Verify with:
`aws sns list-subscriptions-by-topic --region us-east-1 --topic-arn arn:aws:sns:us-east-1:828841719603:arb-scanner-alerts` — a real ARN means live, `PendingConfirmation` means alerts are going nowhere.

## GC pauses: measured, and gc.freeze() did NOT fix them

`_gc_callback` records real collection pauses; the heartbeat reports `gc N cols max NNms tot NNms` beside `loop lag max`.

Findings (2026-09-19):
- **Startup/discovery windows: 12-13 collections, max pause 755-898ms.** Long enough to sleep through entire arb windows (median arb life 85ms).
- **Steady state: ZERO collections, yet loop lag still ~42ms.** So GC does NOT explain steady-state lag — that is most likely burst WS frame processing (real work, not a stall), and would need less per-message work or true parallelism, not GC tuning.
- **`gc.freeze()` after warmup did not help.** It froze 63,433 objects, and the next discovery still showed `gc 13 cols max 898.6ms`. Reason: the pauses come from the *transient garbage* discovery allocates (CFB events carry ~444 markets each), not from re-scanning long-lived objects. Freezing what survives does nothing about what churns.

Next things to try, in order: `gc.disable()` around the discovery/parse window with a manual `gc.collect()` afterwards at a chosen moment; cut allocation by discarding prop markets during JSON parse rather than after; or move discovery into a subprocess so its garbage never touches the trading loop's heap. The freeze call is currently left in place (harmless, small) — remove it if the heap-retention cost shows up in the leak investigation.

## Fee Model

```python
kalshi_taker  = 0.07 * P * (1 - P)    # peaks 1.75c at P=0.5
kalshi_maker  = 0.0175 * P * (1 - P)   # 25% of taker, peaks 0.44c
poly_taker    = 0.05 * P * (1 - P)     # peaks 1.25c at P=0.5
poly_maker    = 0                       # free (confirmed)
```

## Execution — Strategy B (live)

Single-leg convergence: buy cheap on Poly when Kalshi opens an arb. Place maker sell at target. Monitor via WS. Exit on convergence, timeout, or price drop.

### Tunable Variables

**`sell_target_offset`** = 5c — maker sell at buy_price + 5c. Higher = more profit per fill but lower fill rate. Optimal from n=70 convergence data. Re-evaluate with more data.

**`timeout_seconds`** = 15s — cancel maker sell and taker exit at bid. Convergence window passes by ~15s. Longer = price drifts against us.

**`price_drop_threshold`** = 5c — taker exit if ask drops 5c below buy. Based on limited n=17 drawdown data. Most winners never draw down at all. Re-evaluate.

**`min_gross_spread`** = 4% — minimum arb gross to trigger execution.

**`only_kalshi_opener`** = True — only execute when Kalshi opened (77-83% of arbs, higher convergence rate).

**`min_buy_price`** = 15c — skip teams below 15c (heading to 0c, convergence = downward). No upper cap — buying 90c+ teams heading to 100c is profitable (asymmetric: more room up than down, 5/5 profitable in historical data).

### Executor Filters

Kalshi opener only, 4%+ gross, in-game (≤180min to pitch), poly depth > 0, buy price ≥ 15c, Kalshi velocity < 5c/2s.

### Critical Implementation Details

- **BUY_SHORT price inversion**: polymarket.us `price` field for SHORT = long-side price. Executor sends `1 - yes_ask` for SHORT intents. Same inversion for SELL_SHORT.
- **Private order WS replaces `synchronousExecution`** (since 2026-05-12): `client.orders.create()` fires async; `scrapers/polymarket_us_private_ws.PolymarketUSPrivateWSClient` subscribes to `SUBSCRIPTION_TYPE_ORDER` on `wss://api.polymarket.us/v1/ws/private` and exposes `await_terminal(order_id, timeout=1.0)`. Frees the executor from 100-460ms server stalls on failed orders, which were blocking `_in_flight` and preventing retry on subsequent opens of the same arb. Buffers events that arrive before the executor registers its waiter (race fix). Falls back to `portfolio.positions()` if no terminal event within 1s. Executor disables itself until private WS reports ready.
- **Readiness signal: balance snapshot, not order snapshot.** Polymarket's `SUBSCRIPTION_TYPE_ORDER` channel sends NO message when there are zero open orders — no snapshot, no `eof`, no heartbeat. So we also subscribe to `SUBSCRIPTION_TYPE_ACCOUNT_BALANCE` (sent AFTER orders subscribe); receiving its snapshot proves both subscriptions are live. Took a spike against the SDK + raw WS to discover this — both confirmed silent on the order channel alone.
- **Stale-order policy on snapshot.** If the order channel does send a snapshot (because we have open orders from a prior crash), we log the count but ignore the contents. Auto-cancel is not implemented; with $70 balance and `quantity=1`, blast radius is small.
- **OrderState enum spelling: `ORDER_STATE_CANCELED` (one L).** The SDK enum uses American spelling. Earlier code checked `ORDER_STATE_CANCELLED` which silently never matched (didn't show up in old data because we were reading `executions[].type` not `order.state`).
- **Pre-buy gross recheck**: Right before `create()`, re-reads live Kalshi + Poly prices from WS stores and recalculates gross. Skips if arb has evaporated since detection. Zero latency (dict lookups). Prevents buying on stale signals.
- **max_trades=0**: unlimited trades, executor runs indefinitely.
- **Event-driven monitoring**: WS ticks call `executor.on_price_update()` → sets `asyncio.Event` → monitor wakes instantly. No polling.
- **Opener caveat**: `opener` field = which platform ticked most recently, NOT which diverged from fair value. Works 88% of the time but fails on game-ending moments where both platforms race to 0c/100c. Price range filter (15c) mitigates this.
- **Kalshi velocity filter** (since 2026-05-13): `KalshiWSClient` records `yes_ask` history in a 5s deque per ticker on every price-changing tick. `yes_ask_velocity(ticker, window_s=2.0)` returns max absolute change in window. Executor skips if velocity ≥ 5c — game-scoring events create arbs that then gap through the 5c price_drop stop in a single WS tick (all 4 price_drop losses on 2026-05-13 gapped: CHC@ATL -21c, DET@NYM -11c, KC@CWS -8c). Logs `k_vel=Xc` at fire time for post-hoc analysis. Config: `kalshi_velocity_threshold=0.05`, `kalshi_velocity_window_s=2.0`.
- **Taker exit retry loop** (since 2026-05-14): timeout/price_drop exits previously used FOK + assumed `sell_price=current_bid` with **no fill verification**. When the bid moved between read and order arrival, FOK killed the order entirely and we logged a fictional sell — leaving the position open until game expiry. Reconciliation against `portfolio.activities()` found 6 unsold positions over 2 days (~$2.04 cost at risk; SF@LAD held 2 LONG -$0.40, SEA@HOU held 1 LONG -$0.46, others mixed). Fix: switch to **IOC**, capture `order_id` from response, **`await _private_ws.await_terminal()`** to read actual `cumQuantity`. If 0, retry up to `MAX_TAKER_EXIT_ATTEMPTS=3` times, **chasing the bid by 1c** each attempt. If all attempts fail: log `EXIT FAILED — POSITION STILL OPEN`, append to `_stuck_positions`, write `SELL_EXIT_FAILED` row with real failure state in `error` column. New invariant: `SELL_TIMEOUT` / `SELL_PRICE_DROP` rows in `executions.csv` mean an actually-verified fill, not an assumed one.
- **`reconcile.py`** (since 2026-05-14): standalone reconciliation script. Pulls `portfolio.positions()` (currently open) + `portfolio.activities()` (resolutions + trades), joins against `executions.csv`. Reports: open positions needing manual close, positions held to game expiry, logged BUYs without matching trade, per-market logged-vs-Polymarket position diffs. Run after every session: `python3 reconcile.py executions.csv`. Caveat: activities API may not include our maker-side fills, so SELL_CONVERGED maker hits can show as "logged sell with no trade" false-positives — the truer signal is the resolved-positions section.

**⚠️ IMPORTANT — Scaling bug (not yet fixed):**
IOC allows partial fills. Maker sell MUST use actual `cumQuantity` from fill, NOT `config.quantity`. Fix before increasing quantity above 1. Same applies to the new taker-exit IOC retries: each retry assumes full fill at `target_bid`, which holds at quantity=1 but breaks at higher sizes.

## VPS Deployment

Running on AWS EC2 t3.micro in us-east-1 (Virginia), instance `i-0923ce83c9a4b7047`, AWS account 828841719603. Co-located with Polymarket origin servers.
- Poly latency: ~1.2ms (was 5-6ms from home)
- Kalshi latency: ~0.8ms (was 18-19ms from home)
- SSH: `ssh -i ~/.ssh/arb-key.pem ubuntu@98.82.172.44`
- **Runs under systemd since 2026-09-19** (screen is gone — it could not restart the process after an OOM kill):
  - `sudo systemctl {status,restart,stop} arb-scanner` · logs → `~/prediction-arb/scanner.log` (logrotate: daily, keep 30, gzipped — use `zgrep` on rotated copies; the config needs `su ubuntu ubuntu` or logrotate silently skips the group-writable directory)
  - `arb-scanner-restart.timer` recycles it daily at 10:00 UTC to cap the memory leak
  - unit files live in `deploy/` in this repo; install with `sudo cp deploy/arb-scanner* /etc/systemd/system/ && sudo cp deploy/logrotate-arb /etc/logrotate.d/arb-scanner && sudo chown root:root /etc/logrotate.d/arb-scanner && sudo systemctl daemon-reload`
  - 1GB swapfile at `/swapfile` (in `/etc/fstab`). Deliberately 1GB, not 2GB: the root volume is only 8GB and a 2GB file pushed it to 83%.
  - **Don't run a second full scanner (e.g. a dry run) on this box while the service is live** — doing so on 2026-09-19 pushed the live process 264MB into swap (`rss` fell from 275MB to 16MB). It kept trading, but with its heap on disk. Restart the service to pull it back.
- **SSH is IP-locked**: security group `sg-0714ac951294552f4` allows port 22 from specific /32s only. A home-IP change looks exactly like a dead box — SSH times out while the instance is healthy. Fix: `aws ec2 authorize-security-group-ingress --region us-east-1 --group-id sg-0714ac951294552f4 --ip-permissions "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=$(curl -s https://checkip.amazonaws.com)/32}]"`
- **No Elastic IP.** `98.82.172.44` is a dynamic public IP held only while the instance runs — STOPPING the instance loses it, and every hardcoded reference here plus the SSH command breaks. Don't stop/start the instance without either allocating an EIP first or expecting a new address.
- **Root volume is 8GB** (~68% used as of 2026-09-19, after trimming swap to 1GB). `convergence_log.csv` and 30 days of rotated `scanner.log` are the growth drivers — check `df -h /` before adding anything large.
- Pull data: `scp -i ~/.ssh/arb-key.pem ubuntu@98.82.172.44:~/prediction-arb/*.csv ~/Documents/prediction-arb/`

## Key Data (n=801 arbs at 4%+, n=70 convergence tracked)

- Kalshi opener: 77% MLB, 77% NBA, 83% NHL
- Median arb duration: 85ms MLB, 33ms NBA, 97ms NHL
- Strategy B profitable: 88% MLB, 87% NBA, 88% NHL
- EV per arb: 10.0c MLB, 6.7c NBA, 12.2c NHL
- Convergence (Poly moves up): 66% MLB, 50% NBA, 79% NHL
- Time to first profitable exit: median 385ms (75% within 1s)
- Best exit timing: median 10.3s
- From home: 37% of arbs last >145ms (buy could fill). 62% too fast.
- From VPS: 66% viable (~50ms setup).

### Post-private-WS baseline (2026-05-13, n=54 attempts, n=15 fills)
- **Fill rate: 27.8%** (up from 5.5% pre-private-WS). Private WS removal of `synchronousExecution` blocking confirmed as the cause.
- Fill latency: median 60ms (down from 70-85ms), max 226ms (down from 460ms).
- Win rate: 6/15 converged (all wins), 4/15 price_drop (all losses), 5/15 timeout (1 win). Net -$0.44.
- **Root cause of losses**: all 4 price_drop exits gapped through the 5c threshold in a single WS tick. These are game-scoring events creating arbs, not mispricing — velocity filter added to block them.
- Converged exits: 6/6 wins, net +$0.23. Signal quality intact when market behaves normally.
- **⚠️ All taker-exit P&L numbers above are unreliable** — pre-2026-05-14 the executor logged FOK exits as filled without verification. True P&L is worse: reconciliation found 6 unsold positions held to expiry across 5/12-5/13 (SF@LAD -$0.40, SEA@HOU -$0.46, plus 4 mixed-outcome SHORT holds). Adjusted 5/13 net likely ~-$1.00 instead of -$0.44. Re-baseline once IOC retry loop has accumulated ~20 fills.

## Open Questions To Revisit

- **Event-loop lag is 32-72ms at the tail** (measured 2026-09-19, right after the hot-path rework). Steady-state heartbeats reported `loop lag max 72.0ms` then `32.2ms` per 5-min window; the first window showed 2245ms but that includes startup/discovery. This matters: against a ~60ms fill latency, a 30-70ms stall is not noise. It is NOT detection any more — that is now 0.009ms/tick. Prime suspects, in order: **CPython GC pauses on a ~450MB heap** (test with `gc.set_threshold()` tuning or `gc.freeze()` after discovery), synchronous `log.info`/`print` writes to scanner.log on the tick path, the hourly discovery cycle, and the 5s convergence flush building many rows at once. Next step: log which coroutine was running when lag spikes, or sample with `asyncio` debug mode. Do not add sports capacity assuming headroom until this is understood.

- **CFB arbs ARE appearing** (first data 2026-09-19 ~21:00 UTC): e.g. `CENCON @ MONST gross=5.0%`, `YALE @ HOLY gross=4.0%`. The earlier "zero CFB arbs in 7 minutes" was simply too short a sample. ⚠️ **Rows logged between 20:32 and 21:07 UTC on 2026-09-19 are mislabeled `MLB`** — `arb_tracker._sport()` fell through to MLB for any unrecognised series, so early CFB arbs are recorded as MLB and pollute the MLB baseline for that window. Fixed (CFB added; unknown series now label `UNKNOWN`), but exclude that window when analysing either sport.

- **CFB: is it tradeable?** (opened 2026-09-19, detect-only). Two things to learn before enabling execution: (a) **arb frequency** — none were detected in the first ~7 minutes live, which may mean CFB books track each other more tightly than MLB/NHL, or simply that few in-window games were in play; count `,CFB,` rows in `arb_durations_3.csv` / `_4.csv` over a full Saturday. (b) **velocity threshold** — `kalshi_velocity_threshold=0.05` was fitted to baseball/hockey tick sizes; a touchdown moves a football line far more than 5c in 2s, so the filter may block every real CFB signal, or fail to block gap risk. Compare `k_vel=` values on CFB arbs against outcomes before trusting it. Also note many CFB books (especially FCS) sit at 0.01/0.00 once a game is decided — `min_buy_price=15c` filters most, but check for junk arbs against near-zero books. Enable execution by removing `"CFB"` from `executor.config.excluded_sports`.

- **Kalshi API key rotated 2026-09-19** ✅. The old key's RSA PEM had been stored unquoted across multiple lines in `.env` and was printed into an assistant transcript. Replacement notes:
  - **Generated locally, not by Kalshi.** Kalshi's "Create API key" dialog accepts an optional RSA public key; supplying one means Kalshi never generates or displays a private key. Ours lives at `~/.kalshi-keys/kalshi-2026-09-19.pem` (mode 600, outside the repo) and has never left the machine. Prefer this flow on every future rotation.
  - **Scopes: `read` ONLY — "Full access" deliberately unchecked.** The bot only reads from Kalshi: the `/trade-api/ws/v2` orderbook WS and `/trade-api/v2/portfolio/balance`. `place_fok_order()` in `scrapers/kalshi_orders.py` is dead code (not imported; its only call site is commented out) because Strategy B executes exclusively on Polymarket. "Full access" includes `write::transfer`, i.e. withdrawals — never grant it to this bot. If Kalshi-side execution is ever added, create a SEPARATE key with granular `write::trade` and still no `write::transfer`.
  - Scope choice has **no latency effect** — it is an authorization check, and Kalshi's rate limits key off account tier, not scopes.
  - `.env` is now exactly 4 variables and parses cleanly. The dead `KALSHI_PRIVATE_KEY_NEWLINES` block (unreferenced by any code) was what caused `python-dotenv could not parse statement starting at line 32` on every startup.
  - `rotate_kalshi_key.py` automates future rotations: validates the PEM, **proves the key against the live API before changing anything**, backs up `.env`, rewrites it, deploys to the VPS, restarts. Run with `--verify-only` to test a key harmlessly.
  - ⚠️ `.gitignore` now covers `.env.*` — key backups were NOT ignored before, and this repo is public.
  - Verified after rotation: `K: 204/204 confirmed` on the WS, zero auth failures, new key id on both local and VPS `.env`.

- **Memory leak: source unidentified, only mitigated** (opened 2026-09-19; first re-read due 2026-09-22, ideally 2026-09-26). The 2026-05-15 OOM proved growth to 528MB over ~14d uptime (≈38MB/day). systemd caps + the daily recycle make it non-fatal, but nothing identified WHAT leaks.

  **Baseline to compare against**: 275MB RSS / 8 tasks at 134 Kalshi + 114 Poly markets (2026-09-19 19:48 UTC, freshly started).

  **Already ruled out** — don't re-investigate without new evidence: `arb_tracker._active` (popped on close), `convergence_tracker` (`_flush_one` pops tracker + both slug indexes at 60s), `kalshi_ws._books` / `_price_history` / `_sid_tickers` / `_sid_seq` / `_ticker_sid` (all pruned by `prune_tickers()` on discovery), asyncio task accumulation (`tasks` stayed flat at 8-9 across a 7min dry run and live operation).

  **Plausible that it is already fixed**: the pre-2026-09-19 discovery code accumulated up to 3 pages (~600) of full event payloads per hourly cycle, and events now carry ~444 markets each. The rewrite holds one page (~50 events). If the leak was there, growth is now flat. Confirm before chasing anything.

  **How to read the data**:
  ```bash
  # all heartbeat samples, oldest first. delaycompress means scanner.log.1 is
  # NOT gzipped, so grepping only *.gz silently skips the most recent full day.
  zgrep -h "rss" ~/prediction-arb/scanner.log.*.gz 2>/dev/null
  grep -h "rss" ~/prediction-arb/scanner.log.1 2>/dev/null
  grep -h "rss" ~/prediction-arb/scanner.log
  # discovery boundaries to align against (hourly)
  grep "Stores:" ~/prediction-arb/scanner.log
  ```
  Interpretation — the shape is the diagnostic, the absolute number is not:
  - **Step up at each hourly `Stores:` line** → discovery/REST path (retained event payloads; biggest suspect).
  - **Linear between discoveries** → WS tick path (book state, per-tick allocations).
  - **Sawtooth that recovers** → GC churn, not a leak.
  - **Flat across a full 24h window** → leak died with the discovery rewrite; close this item.

  **Caveats when reading**: (a) `arb-scanner-restart.timer` recycles at 10:00 UTC daily, so every window is ≤24h and RSS resets there — do not read a restart drop as a fix; (b) logrotate runs ~00:49 UTC, so a rotated file spans a recycle boundary mid-file; (c) market count drives baseline RSS, so compare windows with similar `Stores:` counts, not raw peaks (MLB slate size swings, and NHL/NBA regular seasons start in Oct).

  **Wait at least 72h / 3 recycle windows** before drawing conclusions; 7 days for a rate you would act on. For an uninterrupted longer curve, `sudo systemctl stop arb-scanner-restart.timer` — at 38MB/day the 650M cap gives ~10 days of headroom and systemd restarts on breach anyway. Remember to re-enable it.

  **Next step only if growth is real**: hourly `gc` object-count histogram by type (cheap, sampled) to name the culprit; `tracemalloc` top-10 by traceback only if the histogram is ambiguous — it costs ~10-25% CPU on the hot path, which is the latency budget the whole edge depends on, so enable it off-hours.

- **NBA slug derivation unverified for 2026-27** (check when Polymarket creates `nba-2026`, ~Oct 20 2026): `kalshi_ticker_to_poly_slug("KXNBAGAME-26OCT20PHINYK")` → `nba-phi-ny-2026-10-20`. The Knicks map to `ny` (not `nyk`) in `NBA_CANONICAL_TO_POLY_ABBR` — that was correct for the 2025-26 series but has not been checked against a live 2026-27 Poly slug. Until the series exists, NBA matches 0 markets and the mapping cannot be tested. Verify by comparing derived slugs against `client.events.list({"seriesId": [<nba-2026 id>], ...})` event slugs; the same check applies to the other NBA two-letter abbrs.

- **Post-private-WS baseline** ✅ ANSWERED (2026-05-13, n=15 fills): Fill rate 27.8% vs 5.5% — confirmed large improvement. Latency median 60ms (was 70-85ms for fills). Private WS removal of blocking stalls was the key unlock.
- **Velocity filter effectiveness** (collect after ~20 post-filter fires): Does the 5c/2s threshold correctly block game-event arbs while passing real mispricing? Check `k_vel=Xc` values in scanner log for fired trades vs skipped. Tune threshold if too aggressive (missing good trades) or too loose (still getting gapped).
- **Win rate recovery** (collect ~20 more fills post-velocity-filter): Today's 47% win rate (vs 88% historical) was dominated by 4 gap losses (-$0.55). With velocity filter blocking those, expect win rate to recover toward the historical 88%. Confirm with data.
- **WS staleness vs fill rate** (instrumented 2026-05-12): executions.csv logs `poly_ws_age_ms` and `kalshi_ws_age_ms` at fire time. Today's fills showed a wide range (15ms – 71s). High poly_ws_age on BUF@MON (71s) won anyway — stale = quiet market, not wrong price. Need more data to isolate whether stale data correlates with non-fills.
- **Cheap-buy fill rate**: Pre-private-WS: 0 of 19 fills at buy_price <0.50. Post-private-WS today: 5 fills below 0.50 (MIA@MIN 0.37, TB@TOR 0.15, BUF@MON 0.34, KC@CWS 0.35, DET@NYM 0.17). Fill rate gap between cheap/expensive largely closed — confirm with more data.

### Pre-private-WS baseline (Apr 30 – May 1, n=55 attempts)
- Fill rate: 5.5% (3 fills). Net P&L: -$0.045 (1 winner +$0.045, 1 small win +$0.004, 1 loss -$0.094).
- Latency split: fills ~70-85ms; non-fills 60-460ms (server holds via `synchronousExecution` until expire).
- **Counterfactual win rate: 77%** — 30 of 39 BUY_FAILED arbs (with convergence data) would have reached `buy_price + 5c` within 15s if filled. Strategy validated; fill rate is the bottleneck, not signal quality.
- 56% of attempted arbs closed in <85ms — unreachable as taker no matter how fast we get. Hard ceiling for taker-side strategy on these markets.
- Detection-to-fire latency in our code path: 1ms median. Network+server is the entire latency budget.

## Future Optimizations (not yet implemented)

- **Pre-placed maker bids** (highest leverage): Strategy is taker-bound; 56% of 4%+ arbs close in <85ms (faster than our fastest VPS→Poly round-trip), so we can't catch them as taker no matter how fast we are. Pre-place resting maker bids on Poly at expected entry prices for active games — when arb opens, we're already in the book. Tradeoffs: capital tied up; adverse-selection risk; need cancel/replace logic. Shift from taker to maker.
- **+1¢ bid experiment**: Pay 1c more than ask for liquidity priority. Easy A/B after velocity filter baseline is collected. Likely marginal — books move wholesale during the race, not just by 1c, but worth confirming.
- **Same-team arb comparison**: Currently arb detector pairs opposite teams cross-platform (K:TeamA + P:TeamB). For Strategy B (single-leg), comparing same team across platforms (K:TeamA vs P:TeamA) may be more accurate — prevents buying a team heading to 0c when cross-team math shows a "gap." Requires rethinking arb detection pipeline.
- **P(fill) × size curve**: Optimal FOK size is NOT max depth. Need to model fill rate degradation as size increases. Start collecting data at quantity=1, then scale up. **Blocked by scaling bug** above (maker sell must use actual `cumQuantity`, not `config.quantity`).

## Output Files

- `arb_durations_3.csv` / `arb_durations_4.csv` — arb detection log (OPEN/CLOSE events)
- `convergence_log.csv` — 60s post-arb price tracking (Poly + Kalshi ticks)
- `executions.csv` — live trade log (BUY, SELL, errors, profit/loss, latency). Action `SELL_EXIT_FAILED` (since 2026-05-14) = all taker-exit retries failed; position still open at write time, must be closed manually or via `reconcile.py`.
- `reconcile.py` — standalone audit. Joins `portfolio.positions()` + `portfolio.activities()` against `executions.csv`. Always run after a session to catch any unsold positions before they resolve.
- All three CSVs join on `arb_id` = `first_seen` ISO timestamp
- Columns in arb_durations: `event, game, sport, gross_spread, net_pretax, first_seen, closed_at, duration_seconds, peak_gross, opener, minutes_to_first_pitch, kalshi_team, kalshi_side, poly_team, kalshi_ask, kalshi_bid, poly_ask, poly_bid, kalshi_oi, kalshi_vol_24h, game_datetime, kalshi_depth, poly_depth`
- Columns in executions: `arb_id, timestamp, game, sport, action, market_slug, intent, buy_price, sell_price, quantity, gross_spread, profit, buy_fee, sell_fee, hold_time_ms, order_id, poly_bid_at_exit, exit_reason, error, buy_latency_ms, sell_latency_ms, poly_depth, poly_ws_age_ms, kalshi_ws_age_ms`
