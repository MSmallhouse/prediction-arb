# prediction-arb

Cross-platform prediction market arbitrage scanner. Kalshi vs Polymarket US on MLB/NBA/NHL game-winner markets.

## Architecture

```
main.py                    — event-driven WS run loop, REST discovery every 60min
config.py                  — fee constants, tax rates, thresholds, team name maps
arb_detector.py            — match markets, compute spreads (YES/NO optimization)
arb_tracker.py             — OPEN/CLOSE duration tracking, CSV logging
arb_verifier.py            — REST verification (built but removed from pipeline — too slow)
scrapers/kalshi.py         — Kalshi REST scraper (KXMLBGAME + KXNBAGAME + KXNHLGAME)
scrapers/kalshi_ws.py      — Kalshi orderbook_delta WS (RSA-PSS auth, local book state)
scrapers/kalshi_orders.py  — Kalshi FOK order placement (built, auth verified, $10 balance)
scrapers/polymarket_us.py  — Polymarket US REST discovery (polymarket-us SDK)
scrapers/polymarket_us_ws.py — Polymarket US WS (ED25519 auth, raw websockets)
scrapers/polymarket.py     — OLD polymarket.com scraper (retained for PolymarketMarket dataclass + slug derivation)
scrapers/polymarket_ws.py  — OLD polymarket.com WS (not used)
scrapers/poly_orders.py    — Polymarket order stub (needs polymarket-us SDK rewrite)
```

## Two Platforms

**Kalshi** — CFTC-regulated US exchange. REST + orderbook_delta WS. RSA-PSS auth.
**Polymarket US** (`polymarket.us`) — separate CFTC-regulated platform (QCX LLC). NOT polymarket.com. ED25519 auth. Mobile app account = "fat.lobster". API keys from `polymarket.us/developer`.

polymarket.com blocks US trading entirely. Prices between .com and .us are within 1c (likely same or arbed pools), but we can only trade on .us.

## Discovery Flow

1. Kalshi: paginated `GET /events?with_nested_markets=true` per series (MLB/NBA/NHL)
2. Derive slugs from Kalshi tickers: `KXMLBGAME-26APR241915PHIATL` → `mlb-phi-atl-2026-04-24`
3. Polymarket US: `client.events.list({series_id})` via SDK, find `sportsMarketType == "moneyline"`, match by slug (strip `aec-` prefix from polymarket.us slug)
4. Each polymarket.us game → TWO `PolymarketMarket` objects (long team + short team) with synthetic token IDs
5. Subscribe WS: Kalshi by market_ticker (orderbook_delta), Poly US by market_slug (market_data)

Series IDs: MLB 2026 = 15, NBA 2025 = 4, NHL 2025 = 6. Offset caching for fast pagination (~8s vs 33s).

## WebSocket Channels

**Kalshi `orderbook_delta`** (replaced `ticker` channel — ticker had ~1-2s lag and no NO prices):
- Snapshot + delta protocol. Local `_MarketBook` per market.
- `yes_ask = 1 - max(no_levels)`, `no_ask = 1 - max(yes_levels)` (Kalshi's complementary book model)
- Seq gap detection → auto re-snapshot
- Fires callback with: `yes_ask, yes_bid, no_ask, no_bid, yes_ask_size, no_ask_size`

**Polymarket US `market_data`** (full book snapshots, NOT deltas):
- `wss://api.polymarket.us/v1/ws/markets`, ED25519 auth headers
- Subscribe by market slug. One slug = one game = both teams' prices.
- `long_ask = offers[0].px`, `short_ask = 1 - bids[0].px`
- Only fires callback when best prices change (filters deep-book noise)

## Arb Detection

4 Kalshi order books per game (Team A YES/NO, Team B YES/NO). YES on Team A = NO on Team B (same payout). Pick cheaper. Both prices now from real-time `orderbook_delta` channel.

```
For each direction:
  p1 = min(k_market.yes_ask, opp_k_market.no_ask)  — cheapest Kalshi exposure
  p2 = poly_market.yes_ask                          — Polymarket cost
  gross = 1 - p1 - p2
```

`_arb_key = (event_slug, k_market.market_ticker, poly_token_id)` — direction-stable regardless of YES/NO choice.

## Fee Model

```python
kalshi_fee = 0.07 * P * (1 - P)    # taker per contract
poly_fee   = 0.05 * P * (1 - P)    # polymarket.us fee (NOT 0.03 — that was polymarket.com)
tax_rate   = 0.2855                 # federal 24% + Utah 4.55%
```

Combined ceiling at P=0.5: Kalshi 1.75¢ + Poly 1.25¢ = **3.0¢ per $1 payout**. MIN_GROSS_SPREAD = 3%.

**NOTE:** `POLY_SPORTS_FEE_COEFF` in config.py still says 0.03 — needs updating to 0.05 (Piece 4 pending).

## Team Name Mapping

All maps in `config.py`. Sport-specific tables required (abbreviations overlap).

- **MLB**: Kalshi `yes_sub_title` = city name. Poly uses full team names.
- **NBA**: Kalshi = city name. Poly = short nicknames.
- **NHL**: Kalshi = `"{ABBR} {Nickname}"`. Poly = short nicknames.

Abbreviation quirks (polymarket.us, confirmed 2026-04-24): `ath` (Athletics), `az` (Diamondbacks), `gs` (Warriors), `no` (Pelicans), `ny` (Knicks), `pho` (Suns), `sa` (Spurs), `cgy` (Flames), `la` (Kings), `nj` (Devils), `nas` (Predators), `uta` (Utah HC), `veg` (Golden Knights), `was` (Capitals).

## Key Data Findings (n=141 arbs at 4%+, n=265 at 3%+)

- Kalshi is the opener ~87% of the time (reprices first → creates the arb)
- Median arb duration: 158ms (4%+), 176ms (3%+)
- Pre-game arbs: 8-1925s duration but thin books (depth 4-359 contracts)
- In-game arbs: sub-second but deep books (depth up to 192k contracts)
- Inverse spread/duration correlation: 4% arbs avg 658ms, 10%+ arbs avg 830ms
- REST verification proved unreliable — REST cache lags WS by seconds. Removed from pipeline.

## Execution Strategy (not yet implemented)

- **Send Poly first** (follower platform, stale price, about to correct). If fills → send Kalshi (opener, stable price). If not → no exposure.
- **FOK orders on both platforms** — fills at exact price or cancels, zero partial risk.
- **VPS near Polymarket** if opener pattern holds — minimize follower fill latency.
- **Kalshi order client built** (`scrapers/kalshi_orders.py`). Auth verified, $10 balance.
- **Polymarket order client needs rewrite** for polymarket.us SDK (`POST /v1/orders` with `TIME_IN_FORCE_FILL_OR_KILL`).
- **Cooldown strategy** (don't fire until arb persists N ms) — proposed but needs fill data to calibrate.
- **Start with pre-game arbs** — 30-60s windows, latency irrelevant, $25/platform sufficient for 100 attempts.

## Output Files

- `arb_durations_3.csv` — all arbs ≥ 3% gross (CSV only)
- `arb_durations_4.csv` — arbs ≥ 4% gross (console + CSV)
- Columns: `event, game, sport, gross_spread, net_pretax, first_seen, closed_at, duration_seconds, peak_gross, opener, minutes_to_first_pitch, kalshi_team, kalshi_side, poly_team, kalshi_ask, kalshi_bid, poly_ask, poly_bid, kalshi_oi, kalshi_vol_24h, poly_liquidity, game_datetime, kalshi_depth`
