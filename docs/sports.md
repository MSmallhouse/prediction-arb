# Sports coverage

| Sport | Kalshi series | Poly series id | Status |
|---|---|---|---|
| MLB | `KXMLBGAME` | `15` (mlb-2026), refreshed dynamically | Live, traded |
| NBA | `KXNBAGAME` | `4` (nba-2025), refreshed dynamically | Live, traded — **slug map unverified for 2026-27** |
| NHL | `KXNHLGAME` | `6` (nhl-2025), refreshed dynamically | Live, traded |
| CFB | `KXNCAAFGAME` | `225` — **hardcoded, NOT refreshed** | Live, **detect-only** |

Series ids are seeded in `scrapers/polymarket_us.py` and re-resolved at every discovery by
`refresh_series_ids()`, which reads `/v1/series` and picks the newest `<sport>-<year>`
slug. **Each season gets a new series id**, so hardcoded values go stale at rollover.

⚠️ **CFB is the exception:** `config.POLY_US_CFB_SERIES_ID = "225"` is a hardcoded string
and `_SERIES_SLUG_RE` matches only `mlb|nba|nhl`. CFB will silently match zero markets at
the 2027 season rollover unless this is fixed.

As of 2026-09-19 Polymarket had not yet created `nba-2026` / `nhl-2026`.

---

## Slug derivation

For MLB / NBA / NHL, the Polymarket slug is **derived from the Kalshi ticker**:

```
KXMLBGAME-26APR241915PHIATL  →  mlb-phi-atl-2026-04-24
```

Team names are normalized through the maps in `config.py`
(`NHL_KALSHI_TO_CANONICAL`, `NBA_CANONICAL_TO_POLY_ABBR`, …) and Polymarket-side names via
`_normalize_poly_team()`. Matching strips the `aec-` prefix where present.

Each matched game produces **two** `PolymarketMarket` objects (long + short) with synthetic
token IDs.

---

## College football — the one sport whose slug cannot be derived

Kalshi writes `KXNCAAFGAME-26SEP19UNCCLEM`; Polymarket writes `cfb-ncar-clmsn-2026-09-19`.
Different abbreviation schemes entirely. Matching therefore works on **normalised school
name + kickoff time**:

1. Both platforms expose the school name — Kalshi `yes_sub_title` ("North Carolina"),
   Polymarket `team.safeName`. `config.normalize_cfb_team()` reduces both to one canonical
   form, so ~250 teams need no hand-written map. Only **11** names genuinely diverge; those
   live in `CFB_NAME_ALIASES` (`UMass`→`Massachusetts`, `NC St.`→`North Carolina State`, …).
2. `main._join_cfb()` pairs Kalshi events to Polymarket events on (team pair + kickoff
   within `CFB_JOIN_MAX_HOURS = 36.0`), then calls
   `scrapers.polymarket.register_cfb_slug_map()`.
3. `kalshi_ticker_to_poly_slug()` consults that registry for `KXNCAAFGAME` tickers, so
   `find_arbs()` and everything downstream are unchanged.

The 36-hour join tolerance is wide. It is safe only because a CFB team pair does not play
twice in two days — do not copy this join to a sport where they might.

Unmatched Polymarket games are **dropped** rather than stored; otherwise each burns a WS
subscription with no Kalshi counterpart to arb against.

### Detect-only

CFB arbs are logged but **never traded** — `executor.config.excluded_sports = {"CFB"}`.
The velocity and price-drop thresholds were fitted on baseball and hockey, and football
scores in 7-point chunks. Unvalidated here. See
[open-questions.md § Is CFB tradeable?](open-questions.md#is-cfb-tradeable).

Enable execution by removing `"CFB"` from `excluded_sports` — but only after the two
questions in that section are answered.

### Capacity — the binding constraint

A Saturday slate is **291 open Kalshi CFB events**. Two guards:

- `CFB_LOOKAHEAD_HOURS = 3.0` (`main.py`) limits discovery to games near kickoff — the
  executor only trades inside 180 minutes anyway.
  Measured 2026-09-19: **6h = 73 games = 512MB RSS; 3h = 39 games = 444MB RSS**, vs 275MB
  with no CFB at all.
- `fetch_all_prices()` pulls an **entire** Kalshi series regardless of the window, so CFB
  markets outside the lookahead are filtered in the discovery loop before reaching the
  stores. Without that filter all **582** CFB markets get subscribed.

systemd caps were raised to `MemoryHigh=600M` / `MemoryMax=700M` to fit this. Verified
live: `K 212/212 confirmed, P 192/192 confirmed`, 39/39 CFB events joined, 0 unmatched.

### CFB fees

⚠️ **Correction (2026-09-19 code audit):** an earlier note claimed "Polymarket CFB fee
coefficient is 0.0695." **No per-sport fee coefficient exists in the code.** CFB uses the
same `POLY_SPORTS_FEE_COEFF = 0.05` as every other sport. Either the note was wrong or the
per-sport model was never implemented — treat CFB fees as unverified until checked against
a real CFB fill.

---

## NBA slug derivation is unverified for 2026-27

`kalshi_ticker_to_poly_slug("KXNBAGAME-26OCT20PHINYK")` → `nba-phi-ny-2026-10-20`. The
Knicks map to `ny` (not `nyk`) in `NBA_CANONICAL_TO_POLY_ABBR`, which was correct for the
2025-26 series but has **not** been checked against a live 2026-27 Poly slug. Until
Polymarket creates `nba-2026` (~Oct 20 2026), NBA matches zero markets and the mapping
cannot be tested.

**Verification protocol:** compare derived slugs against the event slugs returned by
`client.events.list({"seriesId": [<nba-2026 id>], ...})`. The same check applies to every
other two-letter NBA abbreviation.

---

## Per-sport behaviour

⚠️ **The table below is the ORIGINAL n=801 baseline and two of its rows failed to
reproduce** on the larger n=1098 set re-analysed 2026-09-19. Treat it as historical.

| Metric (n=801, orig.) | MLB | NBA | NHL |
|---|---|---|---|
| Kalshi is opener | 77% | 77% | 83% |
| Median arb duration | 85 ms | 33 ms | 97 ms |
| Strategy B profitable | 88% | 87% | 88% |
| EV per arb | 10.0c | 6.7c | 12.2c |
| Convergence (Poly moves up) | 66% | 50% | 79% |

Re-measured on n=1098 closed 4%+ arbs (2026-04-30 → 05-15):

| Metric (n=1098, verified) | MLB | NBA | NHL |
|---|---|---|---|
| Kalshi is opener | **50.6%** | 55.6% | 84.4% |
| Median arb duration | 0.087 s | 0.035 s | 0.045 s |
| Share closing <85ms | 49.2% | 69.4% | 56.6% |
| n | 652 | 36 | 410 |

Two corrections that matter operationally:

- **MLB's Kalshi-opener share is ~50%, not 77%**, and swings from 31.6% to 100% day to
  day. Since `only_kalshi_opener=True` gates every trade, the filter is discarding about
  half of MLB arbs on an unstable signal. (The filter still has real predictive value —
  52.6% vs 15.0% convergence hit rate — but the headline rate was wrong.)
- **NHL's arb counts are inflated by pre-game markets.** `minutes_to_first_pitch` has a
  median of +3.3 days for NHL and only 10 distinct `game_datetime` values; just 31.7% of
  NHL 4%+ arbs are at or after puck drop. NHL produced 410 detections but only 11 execution
  attempts and 3 fills.

NBA arbs remain **the fastest and the least profitable** — a 35ms median life against ~60ms
fill latency means most are structurally uncatchable.

Full methodology, caveats and the rest of the data: [performance-log.md](performance-log.md).

---

## Adding a sport — checklist

1. Add the Kalshi series ticker to `config.py` and a `discover_<sport>_events` in
   `scrapers/kalshi.py`.
2. Add the sport to `_SERIES_SLUG_RE` in `polymarket_us.py` so its series id refreshes
   dynamically. **Do not hardcode the id** (see the CFB warning above).
3. Confirm the Polymarket market-type suffix — it is `<sport>_team_full_game_winner`, and
   matching on `sportsMarketTypeV2` alone picks the wrong market
   ([platforms.md](platforms.md#traps--discovery--rest)).
4. Build the team-name map, or a normalizer if the abbreviations do not correspond.
5. Add the sport to `arb_tracker._sport()` — unrecognised series now label `UNKNOWN`, but
   they used to fall through to `MLB` and silently pollute the MLB baseline
   ([incident](incidents.md#2026-09-19-cfb-arbs-mislabeled-as-mlb)).
6. Measure RSS impact before enabling — memory is the binding constraint on a 908MB box.
7. Start **detect-only** (`excluded_sports`) and validate the velocity threshold against
   that sport's scoring granularity before trading it.
