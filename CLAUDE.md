# prediction-arb

Cross-platform prediction market arbitrage scanner. Data collection and verification only — no live trading yet.

## Project Goal

Identify arbitrage opportunities between Kalshi and Polymarket on MLB/NBA game-winner markets. Buy YES on Team A (one platform) + YES on Team B (other platform) for the same game. One side always pays $1; gross spread = 1 - P1 - P2. Profitable when spread > fees.

## Architecture

```
main.py              — async run loop, 10s poll, discovery every 60min
config.py            — fee constants, tax rates, thresholds, team name maps
arb_detector.py      — match markets, compute spreads, log to CSV
scrapers/kalshi.py   — Kalshi REST scraper (KXMLBGAME + KXNBAGAME)
scrapers/polymarket.py — Polymarket Gamma + CLOB scraper
```

**Poll flow:**
1. Kalshi: single paginated `GET /events?with_nested_markets=true` per series (2 calls total, no 429s)
2. Polymarket: `GET /events?slug=...` per matched game via Gamma API (43 calls, ~2s)
3. Pre-screen with Gamma prices → CLOB refresh only for candidates above MIN_GROSS_SPREAD
4. Log all spreads >= 2% to `arb_opportunities.csv` with `profitable` boolean

## Markets

- **Kalshi**: `KXMLBGAME` (MLB), `KXNBAGAME` (NBA). Series → events → 2 markets per game (one per team). Price field: `yes_ask_dollars`.
- **Polymarket**: Gamma API for discovery + prices. CLOB API for live ask on candidates. Slug format: `{sport}-{away}-{home}-YYYY-MM-DD`.

## Matching

Slug derived from Kalshi event ticker: `KXMLBGAME-26APR221410BALKC` → `mlb-bal-kc-2026-04-22`. NBA tickers have no time field. After slug match, 12h datetime guard prevents same-teams consecutive-day false matches (game datetimes must be within 12h).

**Critical:** Polymarket `outcomePrices` from Gamma = displayed probability, NOT the actual ask. Always CLOB-refresh candidates. For the BAL/KC arb: Gamma showed Royals 55% but CLOB ask was different — only CLOB is tradeable.

## Fee Model

```python
kalshi_fee  = 0.07 * P * (1 - P)          # taker per contract (confirmed correct)
poly_fee    = 0.03 * P^2 * (1 - P)        # sports taker per share (confirmed correct)
tax_rate    = 0.2855                        # federal 24% + Utah 4.55%
after_tax   = net_pretax * 0.7145
```

**Fee notes:**
- Kalshi: standard 7¢/dollar-of-risk. Makers pay ~25% of taker fee. We are always takers.
- Polymarket: `docs.polymarket.com` shows simpler `0.03 * P * (1-P)`, but `help.polymarket.com` sports fee formula expands to `0.03 * P^2 * (1-P)` (the p² version). Peak effective rate = 0.75% at P=0.50. Fee updated March 30, 2026 — our markets (April 2026) use this formula. Makers get rebates; we are always takers.
- Both formulas are per-share/per-contract. At P=0.5, Kalshi fee ≈ 1.75¢, Poly fee ≈ 0.375¢.

MIN_GROSS_SPREAD = 4% to flag. LOG_GROSS_THRESHOLD = 2% to write to arb_opportunities.csv.

## Team Name Mapping

Kalshi uses city names (`yes_sub_title`), Polymarket uses full team names (MLB) or short nicknames (NBA). All maps in `config.py`. NBA/MLB abbreviations overlap (BOS, DET, CLE, PHI, etc.) — must use sport-specific tables. Same-city disambiguation: `"Los Angeles D"` = Dodgers, `"Los Angeles A"` = Angels.

## Known Data Quirks

- Kalshi contingent playoff games: `occurrence_datetime` missing, fallback to `expected_expiration_time` (~3h after game start). This means Kalshi game_datetime ≈ expiration, not tip-off.
- Late-night games (>8 PM ET): Kalshi ticker date = local venue date, Polymarket slug date = same local date. They match correctly.
- Future series games (low OI, vol_24h = OI): brand-new market, all volume in last 24h. Prices unreliable — wide CLOB spread.
- $0 volume Polymarket markets: CLOB may return valid ask (limit orders exist) but spread is wide. Treat with caution.

## Arb Philosophy / Observations

**Pre-game arbs (days out):**
- Appear on future games with low OI/volume on both sides
- Prices stale or wide bid-ask → spread may not be real or executable
- More time to act but thin liquidity limits position size

**In-game arbs (observed during BAL/KC, TOR/LAA, OAK/SEA):**
- High OI + vol_24h (400k–1M+ contracts). Real, liquid markets.
- Spreads emerged and disappeared within one or two 10s poll cycles
- **Speed is critical** — these close fast as markets reprice in real time
- Best candidates for eventual execution layer

**Implication:** In-game arbs are the primary target. Pre-game arbs are interesting for data but require caution on price reliability.

## WebSocket Streaming (not built yet)

Current 10s REST polling introduces ~5–10s of latency vs the market. In-game arbs close within 1–2 poll cycles, so WebSocket is required to capitalize on them reliably.

**Kalshi WebSocket**
- URL: `wss://api.elections.kalshi.com/trade-api/ws/v2`
- **Auth required** even for public data: RSA-PSS sign `{timestamp}GET/trade-api/ws/v2`, include `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-SIGNATURE`, `KALSHI-ACCESS-TIMESTAMP` headers
- Subscribe to `ticker` channel per market ticker:
  ```json
  {"id": 1, "cmd": "subscribe", "params": {"channels": ["ticker"], "market_ticker": "KXMLBGAME-..."}}
  ```
- Tick message fields: `yes_ask_dollars`, `yes_bid_dollars`, `volume_fp`, `open_interest_fp`
- Heartbeat: server sends Ping every 10s; respond with Pong

**Polymarket CLOB WebSocket**
- URL: `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- **No auth required** for market channel
- Subscribe by YES token ID:
  ```json
  {"assets_ids": ["0x...token_id"], "type": "market"}
  ```
- Relevant events: `price_change`, `best_bid_ask` — these carry live ask prices (same as CLOB REST)
- Heartbeat: client sends `PING` every 10s, server responds `PONG`
- Dynamic subscribe: send `{"assets_ids": [...], "operation": "subscribe"}` to add markets without reconnecting

**Implementation plan:**
1. Establish Kalshi WS on startup (after RSA-PSS auth), subscribe to all discovered MLB/NBA tickers
2. Establish Polymarket WS on startup, subscribe to YES token IDs for all matched markets
3. Maintain in-memory price store updated by both WS streams
4. Run arb check on every price update (sub-second latency)
5. On new market discovery (hourly), subscribe new tickers/tokens to existing connections
6. Fall back to REST if either WS disconnects (reconnect with exponential backoff)

## Execution Layer (not built yet)

- Kalshi: RSA-PSS auth (API key + private key in `.env`)
- Polymarket US: ED25519 auth (API key in `.env`)
- `.env` vars: `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY`, `POLYMARKET_API_KEY_ID`, `POLYMARKET_PRIVATE_KEY`

## Output File

`arb_opportunities.csv` — one row per spread >= 2% per poll. Key columns: `sport`, `profitable`, `gross_spread`, `net_pretax`, `net_aftertax`, `kalshi_open_interest`, `kalshi_volume_24h`, `poly_liquidity`. File gitignored (can be large, contains trading data).
