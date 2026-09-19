# Architecture

**Verified against source 2026-09-19.** Line references are to that state of the tree.

---

## What it is

An event-driven asyncio process. WebSocket ticks from Kalshi and Polymarket US feed a local
book state; each tick re-prices **only the game that changed** and hands any resulting
opportunity to a tracker and to the Strategy B executor. REST discovery refreshes the
market universe every 60 minutes.

Everything runs in **one process, one event loop, one thread** except the CSV writer
daemon. That is deliberate — see
[findings-rejected.md § sharding](findings-rejected.md#per-sport-sharding-across-multiple-vpss).

---

## Module map

| File | Role |
|---|---|
| `main.py` (1079 ln) | Run loop. WS callbacks → incremental detection → trackers → executor. Owns discovery, heartbeat, health checks + SNS alerting, GC instrumentation, loop-lag watchdog, SIGTERM drain |
| `config.py` (570) | Constants only: Kalshi URL + series tickers, fee coefficients, tax rates, `MIN_GROSS_SPREAD`, MLB/NBA/NHL team maps, `CFB_NAME_ALIASES` + `normalize_cfb_team()` |
| `arb_detector.py` (188) | `evaluate_event()` (single game, tick path), `find_arbs()` (full scan), `ArbOpportunity`, fee helpers |
| `arb_tracker.py` (194) | OPEN/CLOSE diffing via `update()`, `_sport()`, `log_arb_duration()` → builds row, hands to `csv_writer` |
| `convergence_tracker.py` (277) | 60s post-arb price capture (`TRACKING_DURATION = 60.0`), indexed by poly token / slug / kalshi ticker; `flush_expired()` called from housekeeping |
| `csv_writer.py` (83) | Daemon thread + unbounded queue. `queue_rows`, `pending`, `flush(timeout)` |
| `executor.py` (659) | Strategy B: gates → FOK buy → GTC maker sell → event-driven monitor → exit. **Writes `executions.csv` synchronously**, not via `csv_writer` |
| `reconcile.py` (266) | Standalone CLI audit: `portfolio.positions()` + paginated `activities()` vs `executions.csv` |
| `rotate_kalshi_key.py` (159) | Key rotation CLI: validate PEM → prove against live API → backup + rewrite `.env` → scp → restart |
| `scrapers/kalshi.py` (257) | REST discovery per series, `discover_{mlb,nba,nhl,cfb}_events`, `fetch_all_prices`, `KalshiMarket` |
| `scrapers/kalshi_ws.py` (467) | `orderbook_delta` WS, RSA-PSS auth, local `_MarketBook`, seq-gap snapshot re-request, `subscribe`/`unsubscribe`, `yes_ask_velocity()` |
| `scrapers/polymarket.py` (391) | `PolymarketMarket` dataclass, `kalshi_ticker_to_poly_slug()`, CFB slug registry, `_normalize_poly_team`. Also holds legacy polymarket.com gamma/CLOB code |
| `scrapers/polymarket_us.py` (342) | SDK REST discovery: `refresh_series_ids()`, `discover_moneyline_markets`, `discover_cfb_markets`, `discover_all_sports` |
| `scrapers/polymarket_us_ws.py` (248) | Public `market_data` WS, ED25519 auth, `_last_prices` dedupe |
| `scrapers/polymarket_us_private_ws.py` (273) | Private order WS: ORDER then ACCOUNT_BALANCE subscribes, `_confirm_ready()`, `await_terminal()`, early-terminal buffer |
| `deploy/` | systemd unit + restart timer + logrotate config |

**Dead code** (imported by nothing — candidates for deletion):
`scrapers/polymarket_ws.py`, `scrapers/kalshi_orders.py` (its only `place_fok_order` call
site is commented out), the gamma/CLOB half of `scrapers/polymarket.py`,
`test_ws_unsubscribe.py`. Plus dead imports of `find_arbs` and `MIN_GROSS_SPREAD` in
`main.py`. Full list: [open-questions.md](open-questions.md#doccode-drift-found-2026-09-19).

---

## Tasks and cadence

| Task | Cadence |
|---|---|
| `heartbeat` | 300 s (`HEARTBEAT_INTERVAL`) |
| `housekeeping` | 5 s (`HOUSEKEEPING_INTERVAL`) — convergence flush, watchdogs |
| `loop-lag` | samples every 250 ms (`LOOP_LAG_SAMPLE_S`) |
| `_discovery_loop` | 60 min (`DISCOVERY_INTERVAL`). Runs on the main coroutine, not a task |
| `polymarket-us-ws`, `polymarket-us-private-ws`, `kalshi-ws` | spawned inside the first discovery |
| `exec-<game>` | one per trade attempt |

`WARMUP_SECONDS = 30` after the first discovery before the heartbeat reports "live".

---

## Discovery flow

1. Construct the Polymarket US SDK client; force `executor.config.enabled = True` and
   `max_trades = 0`.
2. `asyncio.gather` over `discover_{mlb,nba,nhl}_events` + `discover_cfb_events(within_hours=3.0)`.
3. Derive Polymarket slugs from every Kalshi ticker via `kalshi_ticker_to_poly_slug()`.
4. `fetch_all_prices()` (per-series, paginated), then `discover_all_sports` in a thread —
   `refresh_series_ids()` plus per-sport `seriesId` + date-window queries.
5. **Filter out CFB markets outside the lookahead window.** Required: `fetch_all_prices()`
   returns an entire series regardless of the window
   ([sports.md § Capacity](sports.md#capacity--the-binding-constraint)).
6. If CFB tickers exist: `discover_cfb_markets` over `[now−6h, now+lookahead+6h]` →
   `_join_cfb()` (team pair + kickoff ≤ `CFB_JOIN_MAX_HOURS`) → `register_cfb_slug_map()`.
7. `_us_markets_to_poly_markets` (two synthetic tokens per game) → `_populate_stores`
   (preserves live WS prices, prunes stale) → `_rebuild_indexes`.
8. Unsubscribe stale tickers/slugs. First pass launches the three WS tasks and starts the
   30s warmup; later passes subscribe dynamically and run a full `_check_arbs()`.
9. Sleep 3600s. Any exception in steps 2 or 4-6 → log, sleep 300s, retry.

Pagination: `PAGE_SIZE = 200`, `MAX_PAGES = 10`, `CFB_PAGE_SIZE = 50`, `DATE_PAD_DAYS = 1`.

The API traps that make each of these steps non-obvious are in
[platforms.md](platforms.md); the outage they caused is in [incidents.md](incidents.md).

---

## Arb detection

Each game has **four Kalshi order books** (Team A YES/NO, Team B YES/NO). YES on Team A is
economically the same as NO on Team B — same payout — so we take whichever is cheaper. Both
prices come from the live `orderbook_delta` book, never REST.

```
p1    = min(k_market.yes_ask, opp_k_market.no_ask)
p2    = poly_market.yes_ask
gross = 1 - p1 - p2
```

**Incremental by default.** `evaluate_event()` re-prices only the game that ticked, into
`main._arb_cache` (event_ticker → opportunities). Pass `changed_events=None` for a full
rescan (startup, post-discovery).

⚠️ **Invariant:** the tracker must receive the **flattened full set** of arbs, not just the
changed ones. `ArbTracker.update()` treats any arb missing from the list as CLOSED.
Measurements: [findings-validated.md](findings-validated.md#incremental-detection).

Thresholds: `THRESHOLD_3 = 0.03` → `arb_durations_3.csv`, `THRESHOLD_4 = 0.04` →
`arb_durations_4.csv`. The two files are **independent logs, not nested**
([performance-log.md](performance-log.md#how-to-read-the-data-files-without-getting-it-wrong)).

---

## Fee model

⚠️ **Only two of these constants exist in code.** `KALSHI_FEE_COEFF = 0.07` and
`POLY_SPORTS_FEE_COEFF = 0.05`. The maker formulas below are a **documented model that was
never implemented**, and `executor.py` hardcodes `0.05` inline rather than importing the
constant. There are **no per-sport fee coefficients** — an older claim that CFB uses 0.0695
is unsupported by the code.

```python
kalshi_taker = 0.07   * P * (1 - P)   # peaks 1.75c at P=0.5   [in code]
poly_taker   = 0.05   * P * (1 - P)   # peaks 1.25c at P=0.5   [in code]
kalshi_maker = 0.0175 * P * (1 - P)   # 25% of taker, peaks 0.44c  [doc only]
poly_maker   = 0                      # free, confirmed            [doc only]
```

Stale comments: `arb_detector.poly_fee()`'s docstring and the comment above
`POLY_SPORTS_FEE_COEFF` both still say 0.03.

Tax constants: `FEDERAL_TAX_RATE = 0.24`, `STATE_TAX_RATE = 0.0455`,
`COMBINED_TAX_RATE = 0.2855`, `AFTER_TAX_MULTIPLIER = 0.7145`.

**Fees are not a rounding error at this size.** Over the 33 closed trades of 2026-05-12→15,
buy fees totalled $1.5733 against a net P&L of −$0.9744.

---

## Output files

| File | Contents |
|---|---|
| `arb_durations_3.csv` / `_4.csv` | Arb detection log, OPEN + CLOSE rows |
| `convergence_log.csv` | 60s post-arb price tracking (Poly + Kalshi ticks) |
| `executions.csv` | Live trade log — BUY, SELL, errors, P&L, latency |
| `scanner.log` | Heartbeats, health alerts, fire lines |

All three CSVs join on `arb_id` = the `first_seen` ISO timestamp.

**arb_durations columns:** `event, game, sport, gross_spread, net_pretax, first_seen,
closed_at, duration_seconds, peak_gross, opener, minutes_to_first_pitch, kalshi_team,
kalshi_side, poly_team, kalshi_ask, kalshi_bid, poly_ask, poly_bid, kalshi_oi,
kalshi_vol_24h, game_datetime, kalshi_depth, poly_depth`

**executions columns:** `arb_id, timestamp, game, sport, action, market_slug, intent,
buy_price, sell_price, quantity, gross_spread, profit, buy_fee, sell_fee, hold_time_ms,
order_id, poly_bid_at_exit, exit_reason, error, buy_latency_ms, sell_latency_ms, poly_depth,
poly_ws_age_ms, kalshi_ws_age_ms`

`SELL_EXIT_FAILED` (since 2026-05-14) means every taker-exit retry failed and **the
position was still open at write time** — close it manually or via `reconcile.py`.

---

## Concurrency invariants

- **CSV rows are BUILT on the loop, WRITTEN off it.** Building on the loop is what captures
  prices at the instant they were true; only the I/O is handed to the daemon thread.
- **SIGTERM must drain the writer queue.** `systemctl restart` sends SIGTERM, which does
  **not** raise `KeyboardInterrupt`. Without `_install_sigterm_handler()` every restart,
  including the daily 10:00 UTC recycle, silently discards queued rows.
- **`_in_flight` is global and must stay that way** — it is the only thing coordinating risk
  against a single ~$70 Polymarket balance.
- **The Kalshi velocity check runs before `_in_flight.add`**, so a filtered ticker stays
  retryable on its next open.
