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
kalshi_fee  = 0.07 * P * (1 - P)          # taker per contract
poly_fee    = 0.03 * P^2 * (1 - P)        # sports taker per share
tax_rate    = 0.2855                        # federal 24% + Utah 4.55%
after_tax   = net_pretax * 0.7145
```

MIN_GROSS_SPREAD = 4% to flag. LOG_GROSS_THRESHOLD = 2% to write to CSV.

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

## Execution Layer (not built yet)

- Kalshi: RSA-PSS auth (API key + private key in `.env`)
- Polymarket US: ED25519 auth (API key in `.env`)
- `.env` vars: `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY`, `POLYMARKET_API_KEY_ID`, `POLYMARKET_PRIVATE_KEY`

## Output File

`arb_opportunities.csv` — one row per spread >= 2% per poll. Key columns: `sport`, `profitable`, `gross_spread`, `net_pretax`, `net_aftertax`, `kalshi_open_interest`, `kalshi_volume_24h`, `poly_liquidity`. File gitignored (can be large, contains trading data).
