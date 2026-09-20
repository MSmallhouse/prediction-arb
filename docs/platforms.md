# Platform notes: Kalshi & Polymarket US

Wire-level behaviour, auth, and the undocumented traps. Most of this was learned the
expensive way — cross-referenced to [incidents.md](incidents.md) where it cost us an outage.

---

## Kalshi

CFTC-regulated. REST for discovery, `orderbook_delta` WebSocket for prices.

- **Auth:** RSA-PSS request signing.
- **Fees:** maker = 25% of taker. See [architecture.md § Fee model](architecture.md#fee-model).
- **Balance:** $10 — not used. Strategy B executes exclusively on Polymarket, so Kalshi is
  a **read-only price feed** for us.
- **API key scopes: `read` ONLY.** "Full access" is deliberately unchecked because it
  includes `write::transfer`, i.e. withdrawals. Never grant that to this bot. If
  Kalshi-side execution is ever added, create a **separate** key with granular
  `write::trade` and still no `write::transfer`.
- Scope choice has **no latency effect** — it is an authorization check, and rate limits
  key off account tier, not scopes.
- `place_fok_order()` in `scrapers/kalshi_orders.py` is **dead code** (unimported; its only
  call site is commented out).

### Traps

- **`yes_sub_title` formatting is not stable across seasons.** NHL labels silently changed
  from `"SEA Kraken"` to `"Seattle"`, breaking every NHL parse. Any parser reading this
  field needs a canonicalizer and a loud failure path, not a silent skip.
  ([incident](incidents.md#4-kalshi-nhl-labels-went-city-only))
- **Abbreviation sets drift.** Kalshi writes the Devils as `NJ`, not `NJD`.
- **`fetch_all_prices()` pulls an ENTIRE series** regardless of any time window you asked
  for. CFB markets outside the lookahead must be filtered in the discovery loop before
  reaching the stores, or all 582 CFB markets get subscribed. See [sports.md](sports.md#capacity--the-binding-constraint).

---

### Settled markets and their filter traps

Probed 2026-09-20 while scoping the hold-to-maturity counterfactual
([open-questions.md](open-questions.md#does-the-arb-signal-predict-the-outcome-or-only-the-next-15-seconds-of-price)).

`GET /markets?series_ticker=KXMLBGAME&status=settled&limit=200` returns the game outcome
directly: **`result`** (`yes`/`no`) and **`expiration_value`** (the winning team's label).
No auth needed. For outcome backfill this is ideal — it speaks our exact team vocabulary,
so there is no name-mapping layer to get wrong.

Three traps:

- **`min_close_ts` / `max_close_ts` are silently ignored or silently over-strict.** A
  window that provably contains settled markets returns `{"cursor":"","markets":[]}` with
  no error. Same failure shape as the Polymarket `series_id` bug — a filter that returns
  an empty list looks exactly like "nothing happened". Paginate with the cursor instead
  and filter client-side.
- **`status=settled` returns rows whose own `status` field reads `finalized`**, and
  passing `status=finalized` is rejected: `"invalid status filter"`. Query on one
  spelling, read back the other.
- **Retention is roughly two months.** Paginating `KXMLBGAME` to cursor exhaustion on
  2026-09-20 yielded 1750 markets reaching back only to `close_time 2026-07-15`.
  `KXNHLGAME` returned zero (offseason). Anything older must come from ESPN.

---

## Polymarket US (`polymarket.us`)

CFTC-regulated (QCX LLC). Distinct from polymarket.com — **do not assume documented
polymarket.com behaviour applies.**

- **Auth:** ED25519.
- **Account:** `fat.lobster` (iOS mobile app). Balance ~$70 (2026-09-19).
- **Maker fee: 0** (confirmed).
- **SDK:** `polymarket-us`. Market-data WS uses raw `websockets`; private order events use
  `scrapers/polymarket_us_private_ws.py`.

### Traps — discovery / REST

- **`series_id` (snake_case) is silently ignored.** The gateway returns events from every
  series and never errors. The honoured param is **`seriesId`, as a list of ints**. This
  caused the four-month outage.
  ([incident](incidents.md#2-series_id-was-never-a-real-filter))
- **Each season is a NEW series id.** Hardcoded ids go stale at rollover.
  `refresh_series_ids()` re-resolves them from `/v1/series` at every discovery, picking the
  newest `<sport>-<year>` slug. Current values and their staleness are in
  [sports.md](sports.md).
- **`sportsMarketType == "moneyline"` no longer exists.** It is now sport-specific
  (`baseball_team_full_game_winner`, `hockey_team_full_game_winner`, …).
- **Matching on `sportsMarketTypeV2 == SPORTS_MARKET_TYPE_MONEYLINE` picks the WRONG
  market** — first-five and per-inning winner markets share that value. Match the
  `_full_game_winner` suffix instead (`_pick_full_game_moneyline()`, legacy `"moneyline"`
  retained as fallback).
- **Events carry ~444 markets each** (props, per-inning, spreads). This is the dominant
  source of discovery-time allocation and GC pressure. See
  [findings-rejected.md § GC tuning](findings-rejected.md#gc-tuning--three-attempts-none-fixed-the-pauses).

### Traps — order placement

- **SHORT price inversion.** The `price` field for a SHORT position is the **long-side
  price**. The executor must send `1 - yes_ask` for SHORT intents, and the same inversion
  applies to `SELL_SHORT`. Getting this wrong is silent and expensive.
- **`OrderState` enum uses American spelling: `ORDER_STATE_CANCELED` (one L).** Earlier
  code checked `ORDER_STATE_CANCELLED`, which never matched anything. It didn't surface in
  old data because we were reading `executions[].type`, not `order.state`.
- **IOC allows partial fills.** See the scaling bug in
  [open-questions.md § Known bugs](open-questions.md#known-unfixed-bug-quantity-scaling).
- **No confirmed sports taker delay** on polymarket.us, despite the documented ~1s delay on
  polymarket.com. See [findings-rejected.md](findings-rejected.md#the-1s-sports-taker-delay--investigated-not-confirmed).

### The order channel is silent when there are no open orders

Subscribing to `SUBSCRIPTION_TYPE_ORDER` on `wss://api.polymarket.us/v1/ws/private` yields
**no snapshot, no `eof`, no heartbeat — nothing** if the account has zero open orders. The
socket connects and stays open; it is simply silent. Confirmed by spike against both our
raw-WS client and the SDK's own `PrivateWebSocket` (60s of `recv()`, zero messages).

As of 2026-09-19 the same is true of `_POSITION` and `_ACCOUNT_BALANCE` with no
positions — Polymarket **stopped sending the balance snapshot** we had been using as the
readiness proof, which silently disabled the executor for an entire evening.
([incident](incidents.md#2026-09-19-evening-the-executor-was-silently-disabled))

**Current readiness rule:** arm the executor once the subscribes are sent **and** a REST
`account.balances()` call succeeds. The snapshot path is retained as the stronger proof if
Polymarket restores it. This proves credentials and connectivity, **not** that the order
channel will deliver — the backstop is the `portfolio.positions()` fallback after a 1s
`await_terminal()` timeout. Watch for `position fallback` warnings.

**Stale-order policy:** if the order channel *does* send a snapshot (because open orders
survived a crash), we log the count and **ignore the contents**. Auto-cancel is not
implemented; at ~$70 balance and `quantity=1` the blast radius is small.

---

## Ground truth vs our logs

`executions.csv` records what the executor **intended**. Polymarket's
`portfolio.positions()` and `portfolio.activities()` record what **happened**. When they
disagree, the API wins — and so does the user's lived experience in the Polymarket app.

Run `./deploy/ops.sh reconcile` after every session.

**Caveat:** the activities API may not include our maker-side fills, so `SELL_CONVERGED`
maker hits can show as "logged sell with no trade" false positives. The truer signal is
the resolved-positions section.
