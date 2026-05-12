# prediction-arb

Cross-platform prediction market arbitrage scanner + Strategy B executor. Kalshi vs Polymarket US on MLB/NBA/NHL game-winner markets.

## Architecture

```
main.py                      — event-driven WS run loop, REST discovery every 60min
config.py                    — fee constants, tax rates, thresholds, team name maps
arb_detector.py              — match markets, compute spreads (YES/NO optimization)
arb_tracker.py               — OPEN/CLOSE duration tracking, CSV logging
convergence_tracker.py       — 60s post-arb price tracking for Strategy B analysis
executor.py                  — Strategy B live execution (buy on Poly, maker sell, timeout exit)
scrapers/kalshi.py           — Kalshi REST scraper (KXMLBGAME + KXNBAGAME + KXNHLGAME)
scrapers/kalshi_ws.py        — Kalshi orderbook_delta WS (RSA-PSS auth, local book state)
scrapers/kalshi_orders.py    — Kalshi FOK order placement (built, auth verified, $10 balance)
scrapers/polymarket_us.py    — Polymarket US REST discovery (polymarket-us SDK, offset caching)
scrapers/polymarket_us_ws.py — Polymarket US market-data WS (ED25519 auth, raw websockets)
scrapers/polymarket_us_private_ws.py — Polymarket US private order-event WS (terminal-state await for executor)
scrapers/polymarket.py       — retained for PolymarketMarket dataclass + slug derivation only
```

## Platforms

**Kalshi** — CFTC-regulated. REST + orderbook_delta WS. RSA-PSS auth. Maker fee = 25% of taker.
**Polymarket US** (`polymarket.us`) — CFTC-regulated (QCX LLC). ED25519 auth. Maker fee = 0. Account = "fat.lobster". Balance ~$70.

## Discovery Flow

1. Kalshi: paginated `GET /events?with_nested_markets=true` per series
2. Derive slugs from Kalshi tickers: `KXMLBGAME-26APR241915PHIATL` → `mlb-phi-atl-2026-04-24`
3. Polymarket US: `client.events.list({series_id})`, find `sportsMarketType == "moneyline"`, match by slug (strip `aec-` prefix). Team names normalized via `_normalize_poly_team()`.
4. Each game → TWO `PolymarketMarket` objects (long + short) with synthetic token IDs
5. WS: Kalshi `orderbook_delta` by market_ticker, Poly US `market_data` by market_slug

Series IDs: MLB 2026 = 15, NBA 2025 = 4, NHL 2025 = 6.

## Arb Detection

4 Kalshi order books per game (Team A YES/NO, Team B YES/NO). YES on Team A = NO on Team B (same payout). Pick cheaper. Both prices from real-time `orderbook_delta`.

```
p1 = min(k_market.yes_ask, opp_k_market.no_ask)
p2 = poly_market.yes_ask
gross = 1 - p1 - p2
```

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

Kalshi opener only, 4%+ gross, in-game (≤180min to pitch), poly depth > 0, buy price ≥ 15c.

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

**⚠️ IMPORTANT — Scaling bug (not yet fixed):**
IOC allows partial fills. Maker sell MUST use actual `cumQuantity` from fill, NOT `config.quantity`. Fix before increasing quantity above 1.

## VPS Deployment

Running on AWS EC2 t3.micro in us-east-1 (Virginia). Co-located with Polymarket origin servers.
- Poly latency: ~1.2ms (was 5-6ms from home)
- Kalshi latency: ~0.8ms (was 18-19ms from home)
- SSH: `ssh -i ~/.ssh/arb-key.pem ubuntu@98.82.172.44`
- Run in screen: `screen -S arb` → `python3 main.py 2>&1 | tee scanner.log`
- Detach: `Ctrl+A` then `d`. Reattach: `screen -r arb`
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

## Open Questions To Revisit

- **Post-private-WS baseline** (collect after ~10-20 fills): Did dropping `synchronousExecution` improve attempt rate by freeing `_in_flight` from server stalls? Compare `buy_latency_ms` distribution to the Apr 30 – May 1 baseline (fills 70-85ms, non-fills 60-460ms median 104ms).
- **WS staleness vs fill rate** (instrumented 2026-05-12): executions.csv logs `poly_ws_age_ms` and `kalshi_ws_age_ms` at fire time. After ~20 fire events, check: are non-fills correlated with stale (>50ms) Poly WS data, or are we firing on fresh data but losing the speed race? If stale → investigate WS lag / dropped ticks. If fresh → confirms fill rate is a pure race-to-the-book problem (see baseline below).
- **Cheap-buy fill rate** (Apr 30 – May 1 baseline): 0 of 19 attempts at buy_price <0.50 filled, vs 2 of 35 above. Counterfactual convergence rate was similar for both buckets — cheap side has tighter/faster market making (algos defending against adverse selection). Post-private-WS, recheck whether this asymmetry persists.

### Pre-private-WS baseline (Apr 30 – May 1, n=55 attempts)
- Fill rate: 5.5% (3 fills). Net P&L: -$0.045 (1 winner +$0.045, 1 small win +$0.004, 1 loss -$0.094).
- Latency split: fills ~70-85ms; non-fills 60-460ms (server holds via `synchronousExecution` until expire).
- **Counterfactual win rate: 77%** — 30 of 39 BUY_FAILED arbs (with convergence data) would have reached `buy_price + 5c` within 15s if filled. Strategy validated; fill rate is the bottleneck, not signal quality.
- 56% of attempted arbs closed in <85ms — unreachable as taker no matter how fast we get. Hard ceiling for taker-side strategy on these markets.
- Detection-to-fire latency in our code path: 1ms median. Network+server is the entire latency budget.

## Future Optimizations (not yet implemented)

- **Pre-placed maker bids** (highest leverage): Strategy is taker-bound; 56% of 4%+ arbs close in <85ms (faster than our fastest VPS→Poly round-trip), so we can't catch them as taker no matter how fast we are. Pre-place resting maker bids on Poly at expected entry prices for active games — when arb opens, we're already in the book. Tradeoffs: capital tied up; adverse-selection risk; need cancel/replace logic. Shift from taker to maker.
- **+1¢ bid experiment**: Pay 1c more than ask for liquidity priority. Easy A/B once we have private-WS baseline numbers. Likely marginal — books move wholesale during the race, not just by 1c, but worth confirming.
- **Same-team arb comparison**: Currently arb detector pairs opposite teams cross-platform (K:TeamA + P:TeamB). For Strategy B (single-leg), comparing same team across platforms (K:TeamA vs P:TeamA) may be more accurate — prevents buying a team heading to 0c when cross-team math shows a "gap." Requires rethinking arb detection pipeline.
- **P(fill) × size curve**: Optimal FOK size is NOT max depth. Need to model fill rate degradation as size increases. Start collecting data at quantity=1, then scale up. **Blocked by scaling bug** above (maker sell must use actual `cumQuantity`, not `config.quantity`).
- **Kalshi velocity filter**: If Kalshi moved >10c in last 1-2s, skip — arb is a speed difference during game-ending moment, not real mispricing.

## Output Files

- `arb_durations_3.csv` / `arb_durations_4.csv` — arb detection log (OPEN/CLOSE events)
- `convergence_log.csv` — 60s post-arb price tracking (Poly + Kalshi ticks)
- `executions.csv` — live trade log (BUY, SELL, errors, profit/loss, latency)
- All three join on `arb_id` = `first_seen` ISO timestamp
- Columns in arb_durations: `event, game, sport, gross_spread, net_pretax, first_seen, closed_at, duration_seconds, peak_gross, opener, minutes_to_first_pitch, kalshi_team, kalshi_side, poly_team, kalshi_ask, kalshi_bid, poly_ask, poly_bid, kalshi_oi, kalshi_vol_24h, game_datetime, kalshi_depth, poly_depth`
- Columns in executions: `arb_id, timestamp, game, sport, action, market_slug, intent, buy_price, sell_price, quantity, gross_spread, profit, buy_fee, sell_fee, hold_time_ms, order_id, poly_bid_at_exit, exit_reason, error, buy_latency_ms, sell_latency_ms, poly_depth, poly_ws_age_ms, kalshi_ws_age_ms`
