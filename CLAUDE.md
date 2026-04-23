# prediction-arb

Cross-platform prediction market arbitrage scanner. Data collection and verification only — no live trading yet.

## Project Goal

Identify arbitrage opportunities between Kalshi and Polymarket on MLB/NBA/NHL game-winner markets. Buy YES on Team A (one platform) + YES on Team B (other platform) for the same game. One side always pays $1; gross spread = 1 - P1 - P2. Profitable when spread > fees.

## Architecture

```
main.py                  — event-driven WS run loop, REST discovery every 60min
config.py                — fee constants, tax rates, thresholds, team name maps
arb_detector.py          — match markets, compute spreads
arb_tracker.py           — OPEN/CLOSE duration tracking, CSV logging
scrapers/kalshi.py       — Kalshi REST scraper (KXMLBGAME + KXNBAGAME + KXNHLGAME)
scrapers/kalshi_ws.py    — Kalshi WebSocket client (RSA-PSS auth)
scrapers/polymarket.py   — Polymarket Gamma + CLOB scraper
scrapers/polymarket_ws.py — Polymarket CLOB WebSocket client (no auth)
```

**Discovery flow (hourly REST):**
1. Kalshi: single paginated `GET /events?with_nested_markets=true` per series (3 calls: MLB + NBA + NHL, no 429s)
2. Polymarket: `GET /events?slug=...` per matched game via Gamma API, use `outcomePrices` as initial prices
3. Subscribe new tickers/tokens to existing WS connections
4. Live prices maintained by WS from that point — no further REST polling

## Markets

- **Kalshi**: `KXMLBGAME` (MLB), `KXNBAGAME` (NBA), `KXNHLGAME` (NHL). Series → events → 2 markets per game (one per team). Price field: `yes_ask_dollars`. Each market also has `no_ask_dollars` — buying NO on Team B is economically identical to buying YES on Team A (same $1 payout), but the two order books can have different prices. `arb_detector` checks both and uses whichever is cheaper; `kalshi_side` column records which was used.
- **Polymarket**: Gamma API for discovery + prices. CLOB WS for live prices. Slug format: `{sport}-{away}-{home}-YYYY-MM-DD`.

## Matching

Slug derived from Kalshi event ticker: `KXMLBGAME-26APR221410BALKC` → `mlb-bal-kc-2026-04-22`. NBA and NHL tickers have no time field. After slug match, 12h datetime guard prevents same-teams consecutive-day false matches (game datetimes must be within 12h).

**Critical:** Polymarket `outcomePrices` from Gamma = displayed probability, NOT the actual ask. Always CLOB-refresh candidates. For the BAL/KC arb: Gamma showed Royals 55% but CLOB ask was different — only CLOB is tradeable.

## Fee Model

```python
kalshi_fee  = 0.07 * P * (1 - P)          # taker per contract (confirmed)
poly_fee    = 0.03 * P * (1 - P)          # taker per share (confirmed post-v2)
tax_rate    = 0.2855                        # federal 24% + Utah 4.55%
after_tax   = net_pretax * 0.7145
```

**Fee notes:**
- Kalshi: 7¢/dollar-of-risk. We are always takers.
- Polymarket: `0.03 * P * (1-P)`, confirmed against docs post-CLOB v2 (2026-04-28). Peaks at **0.75%** at P=0.5.
- Combined ceiling at P=0.5: Kalshi 1.75¢ + Poly 0.75¢ = **2.5¢ per $1 payout** (~2.5% gross spread consumed by fees at worst case).
- Both formulas are per-share/per-contract.

MIN_GROSS_SPREAD = 3% (dual logging: 3% → `arb_durations_3.csv`, 4% → `arb_durations_4.csv`).

## Team Name Mapping

All maps in `config.py`. Must use sport-specific tables — MLB/NBA/NHL abbreviations overlap.

- **MLB**: Kalshi `yes_sub_title` = city name (e.g. `"Los Angeles D"` = Dodgers, `"Los Angeles A"` = Angels). Polymarket uses full team names.
- **NBA**: Kalshi `yes_sub_title` = city name. Polymarket uses short nicknames.
- **NHL**: Kalshi `yes_sub_title` = `"{ABBR} {Nickname}"` format (e.g. `"EDM Oilers"`, `"UTA Mammoth"`). Polymarket uses short nicknames; Utah Hockey Club → `"Utah"` (not "Mammoth").
- **NHL Polymarket slug quirks (confirmed)**: `lak` (not `la`), `mon` (not `mtl`), `las` (not `vgk`), `utah` (not `uta`).

## Known Data Quirks

- Kalshi `game_datetime`: uses `occurrence_datetime` when available, falls back to `expected_expiration_time` (~3h after game start) when null. Regular season games are also affected. Use Polymarket `game_datetime` (from `gameStartTime`) for time-relative calculations like `minutes_to_first_pitch` — it reliably reflects actual first pitch.
- Late-night games (>8 PM ET): Kalshi ticker date = local venue date, Polymarket slug date = same local date. They match correctly.
- Future series games (low OI, vol_24h = OI): brand-new market, all volume in last 24h. Prices unreliable — wide CLOB spread.
- $0 volume Polymarket markets: CLOB may return valid ask (limit orders exist) but spread is wide. Treat with caution.

## Arb Philosophy / Observations

**Pre-game arbs (days out):**
- Low OI/volume on both sides — prices stale, wide bid-ask
- Spread may not be real or executable
- Avoid until markets mature closer to game time

**Pre-game arbs (same-day, 0–3 hours before first pitch):**
- Observed: 400k–1.2M Kalshi OI, $64k–$391k Polymarket liquidity — comparable to in-game
- Durations of 2–13 seconds observed at 4–14% spreads — far more executable than in-game
- Platforms can disagree significantly on win probability (14% spread observed on ATL@WSH)
- **May be the better execution target** — liquid AND long enough windows to fill both legs
- Need more data to confirm this holds broadly

**In-game arbs (observed during BAL/KC, TOR/LAA, OAK/SEA):**
- High OI + vol_24h (400k–1M+ contracts). Real, liquid markets.
- Windows typically 17ms–856ms — requires sub-100ms execution infrastructure
- Large spreads close fastest (7%+ often <100ms) — high profit but low fill probability

**Implication:** Target arbs by executability, not game phase. The three factors that determine whether an arb is worth attempting:
1. **Duration** — needs to be long enough for both legs to fill given your network latency. Pre-game same-day arbs often last seconds; in-game arbs often last milliseconds.
2. **Liquidity** — sufficient book depth on both platforms to fill your order size without slippage. Use Kalshi OI and Polymarket liquidity as proxies.
3. **Spread size** — wide enough to clear fees (>2.5% gross) but not so wide that it closes before your orders land. Upper cutoff TBD from real fill data.

Days-out pre-game arbs remain unreliable. Same-day pre-game and in-game are both valid targets — prioritize based on which regime offers better duration/liquidity tradeoff at execution time.

**Spread size vs. duration (preliminary — needs more data):**

Early data (n=19 closed arbs at 4%+ threshold) shows an inverse correlation between opening gross spread and duration:

| Gross spread | Mean duration |
|---|---|
| 4% | 0.856s |
| 5% | 0.403s |
| 7% | 0.051s |
| 12% | 0.017s |
| 17% | 0.139s |

Larger mispricings close faster — likely because platforms and competing bots reprice more aggressively when the gap is large. This means the highest-payout arbs are also the least executable.

**Implication for execution layer:** There is likely an optimal spread band — wide enough to be profitable after fees, narrow enough to still be open when orders land. Executing on every detected arb regardless of size is probably suboptimal. The upper cutoff (reject arbs above X% gross) should be tuned once real order fill-time data is available.

Expected value framework:
```
EV = P(both legs fill) × net_pretax - P(one leg fails) × single-leg fee cost
```
P(fill) is a function of duration. At very high spreads, P(fill) → 0 and EV turns negative despite large net_pretax.

**Failed leg strategy — hold vs. unwind (unresolved, needs real fill data):**

When one leg fills (FOK) and the other cancels, two options:

*Default: unwind the filled leg immediately.*
- Sell back into the book, lose bid-ask spread (~1-2%)
- Frees capital immediately
- Net effect over many attempts: ~neutral (half the time price moved in your favor, half against), minus the fee on the filled leg

*Alternative: hold the filled leg to game resolution.*
- EV depends on which leg filled:
  - **Opener leg filled**: you bought at the opener's new fair value → hold ≈ unwind (both lose just the fee)
  - **Follower leg filled**: you bought the stale/cheap leg below its new fair value → hold has significantly positive EV (e.g., bought at 0.55 when fair value is now 0.70 → expected gain 0.15 - fee)
- Downside: capital locked for hours, high variance per bet, directional exposure accumulates across simultaneous games
- We log `opener` in CSV — we know which leg was which at detection time

*Why this matters:* the follower leg in a fast in-game arb is structurally below fair value (that's what creates the arb). Holding it is not a neutral gamble — it's a below-fair-value position. Over enough attempts, this could outperform the unwind strategy in EV despite higher variance per trade.

*Not implemented yet. Needs real execution data to evaluate P(fill) per leg and actual fair-value discounts before committing to either strategy.*

**Confound to control for:** The large-spread, fast-close observations all occurred during a single high-volatility game moment (PIT@TEX late-inning scoring run). Rapid repricing in that context may cause both large spreads AND fast closures simultaneously — meaning game phase, not spread size alone, could be the true driver. Need more data across varied game states before treating spread as a standalone filter signal.

## WebSocket Streaming

Both WS clients are live and replace REST price polling entirely. REST is used only for hourly market discovery.

- **Kalshi**: `wss://api.elections.kalshi.com/trade-api/ws/v2`. RSA-PSS auth required on connect. Subscribe per market ticker to `ticker` channel. Server sends WS Ping frames; library auto-responds.
- **Polymarket**: `wss://ws-subscriptions-clob.polymarket.com/ws/market`. No auth. Subscribe by YES token ID. Client sends `"PING"` every 10s; server responds `"PONG"`.
- Both clients reconnect with exponential backoff. Dynamic subscribe adds new markets to existing connections on hourly rediscovery.
- Arb check runs on every price tick — sub-second latency.

## Execution Layer (not built yet)

- Kalshi: RSA-PSS auth (API key + private key in `.env`)
- Polymarket US: ED25519 auth (API key in `.env`)
- `.env` vars: `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY`, `POLYMARKET_API_KEY_ID`, `POLYMARKET_PRIVATE_KEY`

**Language:** Python is the right choice until network latency is no longer the bottleneck. The hot path (tick → arb check → order submit) is microseconds of CPU work — network RTT dominates by 10–100x. Only revisit if VPS placement + connection pre-warming are fully optimized and Python processing time becomes a measurable fraction of the arb window. At that point, a partial Rust rewrite of the hot path is more realistic than a full port.

**Latency is the single most critical factor for execution viability.** Early data shows median arb windows of ~85ms — this is not a soft target. Every millisecond saved directly increases fill rate. All execution layer decisions should be evaluated primarily through the lens of latency.

**Server locations (preliminary — not independently verified, subject to change):**
- **Kalshi**: Believed to be Chicago based on latency testing by VPS providers (0.82–1.14ms RTT from Chicago datacenters). Aligns with their CFTC-regulated exchange status — Chicago is standard for US derivatives infra. Not confirmed directly.
- **Polymarket CLOB**: Believed to be AWS eu-west-2 (London) based on multiple community sources. Our own benchmark showed ~27ms from home, but this is likely Cloudflare's CDN terminating locally and proxying to London — actual order-to-confirmation RTT from the US is probably ~130ms. Not confirmed directly.
- **No single optimal VPS location**: Chicago gives ~1ms to Kalshi but ~130ms to Polymarket. London gives the reverse. US East Coast (~30–50ms to Kalshi, ~80–100ms to Polymarket) is the best compromise if opener is unknown. Once opener data is sufficient to show a consistent pattern, VPS location should be optimized toward the follower platform (the one you're racing to fill on).
- **Re-verify before any VPS decision**: Polymarket scheduled a CLOB v2 infrastructure overhaul for April 28, 2026 — server locations may have changed. Benchmark order endpoint RTTs from candidate VPS locations before committing.

**Pre-execution benchmarks required:**
1. **Order submission round-trip latency** — benchmark the actual order endpoints on both platforms, not read endpoints. Measure full chain: request signing (RSA-PSS on Kalshi, ED25519 on Polymarket) + TCP round-trip + server processing + response parsed. Read endpoints hit caches; order endpoints hit matching engine — completely different latency profile.
2. **VPS placement** — highest-leverage latency reduction available. A VPS co-located with platform servers can cut RTT from 20–80ms (home connection) to 1–5ms. Both platforms need low latency since no consistent opener pattern exists — one platform does not reliably reprice first. Evaluate after benchmarking order endpoints to know which region to target.
3. **Sandbox testing** — Kalshi has a demo environment. Confirm whether Polymarket CLOB has one before benchmarking live orders.

**Polymarket CLOB v2 order changes (launched 2026-04-28) — relevant when building execution layer:**
- WS URLs/payloads unchanged — `polymarket_ws.py` unaffected
- Collateral: USDC.e → pUSD. Wrap via Collateral Onramp `wrap()` before first trade.
- Order structure: removed `nonce`, `feeRateBps`, `taker`; added `timestamp` (ms), `metadata`, `builder`. EIP-712 domain `"1"` → `"2"`, new Exchange contract `0xE111180000d2663C0091e4f400237545B87B996B`.
- Builder auth: HMAC headers → `builderCode` (bytes32) field on each order.
- **Session heartbeat**: send every 5s or all open orders cancelled (separate from WS PING at 10s).
- **FOK orders**: use Fill-Or-Kill for arb — fills both legs or cancels, no partial exposure.
- Test environment: `https://clob-v2.polymarket.com` (test markets: gamma-api refs 73106, 79831).

**Connection pre-warming (implement before first live order):**
- **HTTP Keep-Alive** — `aiohttp.ClientSession` already maintains persistent connections (no TCP handshake per request). Ensure the session is never discarded between orders. Add a cheap keepalive request (e.g. `GET /balance`) every 30–60s during active games to prevent server-side connection expiry — a cold connection on the first order re-pays the full handshake cost.
- **HTTP/2** — single TCP connection with multiple concurrent requests in flight; eliminates head-of-line blocking when submitting both legs simultaneously. `aiohttp` supports HTTP/2 via `aiohttp[speedups]`. Check if both order endpoints support it (Kalshi behind CloudFront likely yes; Polymarket CLOB uncertain).
- **TLS session resumption** — TLS 1.3 0-RTT / TLS 1.2 session tickets skip the full TLS negotiation on reconnect. Handled automatically when reusing a persistent connection; falls back to full handshake if connection was dropped. Another reason to keep connections warm.

## Output Files

- `arb_durations_3.csv` — all arbs ≥ 3% gross (CSV only, no console output)
- `arb_durations_4.csv` — arbs ≥ 4% gross (console + CSV)
- Columns: `event, game, sport, gross_spread, net_pretax, first_seen, closed_at, duration_seconds, peak_gross, opener, minutes_to_first_pitch, kalshi_team, kalshi_side, poly_team, kalshi_ask, kalshi_bid, poly_ask, poly_bid, kalshi_oi, kalshi_vol_24h, poly_liquidity, game_datetime`
- Both files gitignored.
