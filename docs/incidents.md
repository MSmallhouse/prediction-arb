# Incidents & post-mortems

Every failure this system has had so far has been **silent**. Process liveness would
have caught almost none of them. That pattern is the single most important thing on
this page — see [The silent-failure pattern](#the-silent-failure-pattern) at the bottom.

---

## 2026-05-15 → 2026-09-19: the four-month outage

The bot was dead for **127 days** and nobody noticed. Four independent breakages, each
sufficient on its own to stop trading. All four are fixed; all four failed quietly.

### 1. OOM kill (the thing that actually stopped it)

```
Killed process 49704 (python3) anon-rss:528264kB   2026-05-15 14:08:43 UTC
```

t3.micro = 908MB RAM, no swap at the time. The process was running under `screen`, which
**cannot restart a dead process**. An earlier OOM on 2026-05-01 had killed it the same
way at ~480MB after 10-11 days.

Growth rate ≈ **38MB/day** on top of a ~265MB steady state. The 2026-05-12 fix (pruning
WS subscription state on discovery) extended uptime from ~11 to ~14 days but did not stop
the leak. Root cause still unidentified — see
[open-questions.md § Memory leak](open-questions.md#memory-leak-source-unidentified-only-mitigated).

**Fixed by mitigation, not cure:** 1GB swapfile, systemd unit with `Restart=always`,
`MemoryHigh=600M` / `MemoryMax=700M`, `OOMPolicy=restart`, plus a memory-triggered recycle
timer. The heartbeat now logs `rss NNNMB tasks N` so the leak is measurable at all.

**Why it went unnoticed for four months:** the AWS bill kept arriving (~$1/mo), the
instance stayed `running` with all reachability checks `ok`, and it idled at 0.1% CPU.
The bill is not a liveness signal. See [operations.md § Alerting](operations.md#alerting).

### 2. `series_id` was never a real filter

The Polymarket US gateway **silently ignores** the snake_case `series_id` query param and
returns events from every series — an MLB query came back full of NFL events. The param
it honours is `seriesId`, as a **list of ints**.

Discovery had been compensating by paging blindly from a hardcoded offset and
slug-matching whatever came back. Once the season moved past those offsets, it matched
**zero markets, forever**, while logging healthy heartbeats.

**Fixed:** query `seriesId` + `startDateMin`/`startDateMax` for the exact Kalshi date
window. One page, no offset cache.

### 3. The market-type tag was renamed

`sportsMarketType == "moneyline"` stopped existing. It is now sport-specific:
`baseball_team_full_game_winner`, `hockey_team_full_game_winner`, and so on.

Worse, events now carry **~444 markets each** (props, per-inning, spreads), and
first-five / per-inning winner markets share `sportsMarketTypeV2 ==
SPORTS_MARKET_TYPE_MONEYLINE` — so matching on V2 alone silently picks the *wrong market*.

**Fixed:** `_pick_full_game_moneyline()` matches the `_full_game_winner` suffix, with the
legacy `"moneyline"` value retained as a fallback.

### 4. Kalshi NHL labels went city-only

`yes_sub_title` changed from `"SEA Kraken"` to `"Seattle"`, so every NHL market failed to
parse. All 32 city labels were added to `NHL_KALSHI_TO_CANONICAL`.

Separately `NHL_KALSHI_ABBR_SET` was missing `CHI` and `NJ` — Kalshi writes the Devils as
`NJ`, not `NJD` — which dropped NYI@NJ / NYR@NJ / MIN@CHI and spammed a warning on every
WS tick.

### The tell we should have watched

`Stores: N Kalshi markets, M Poly markets` in the log. **M == 0 with N > 0 means discovery
is broken**, not that markets are quiet. This is now an automated health check.

---

## 2026-09-19 (evening): the executor was silently disabled

**Symptom:** 103 MLB arbs at 4%+ detected in one evening, **zero execution attempts**.
`executions.csv` had nothing but its header.

**Cause:** `PolymarketUSPrivateWSClient` armed the executor only on receipt of an
`accountBalanceSubscriptionSnapshot`. Polymarket **stopped sending that snapshot**.
Verified against both our client and the SDK's own `PrivateWebSocket`: subscribing to
`SUBSCRIPTION_TYPE_ORDER`, `_POSITION` and `_ACCOUNT_BALANCE` yields **zero frames** when
there are no open orders or positions. The socket connects and stays open; it is simply
silent. The executor's `if _private_ws is None or not _private_ws.is_ready: return` guard
then blocked every arb, logging nothing.

**Fix:** `_confirm_ready()` now arms the executor once the subscribes are sent **and** a
REST `account.balances()` call succeeds. The old snapshot path is retained, so if
Polymarket restores the snapshot we automatically resume using the stronger proof.

⚠️ **This is a weaker guarantee than before.** It proves the socket connected, subscribes
were sent, and credentials work — it does **not** prove the order channel will deliver.
Mitigation is the pre-existing fallback: when `await_terminal()` times out (1s), the
executor reads `portfolio.positions()` to learn the real outcome. **Watch for
`position fallback` warnings** — a rise means the order channel is dead and fills are
being resolved by the slow path.

---

## 2026-05-14: FOK taker exits logged fictional fills

**Symptom:** the user reported game-completion losses on SF@LAD and SEA@HOU that did not
appear in `executions.csv` — every BUY had a matching SELL and the book looked balanced.

**Cause:** timeout and price-drop exits used **FOK** and then assumed
`sell_price = current_bid` with **no fill verification**. When the bid moved between our
read and the order's arrival, FOK killed the order outright and we logged a sell that
never happened — leaving the position open until game expiry.

`portfolio.activities()` revealed **6 unsold positions over 2 days** (~$2.04 at risk;
SF@LAD held 2 LONG -$0.40, SEA@HOU held 1 LONG -$0.46, others mixed). True two-day P&L
was roughly **2x the logged loss**.

**Fix:** switch to **IOC**, capture `order_id`, `await _private_ws.await_terminal()` to
read the actual `cumQuantity`. If 0, retry up to `MAX_TAKER_EXIT_ATTEMPTS=3`, **chasing
the bid by 1c** each attempt. If all attempts fail: log
`EXIT FAILED — POSITION STILL OPEN`, append to `_stuck_positions`, and write a
`SELL_EXIT_FAILED` row carrying the real failure state in the `error` column.

**New invariant:** a `SELL_TIMEOUT` / `SELL_PRICE_DROP` row in `executions.csv` now means
an actually-verified fill. Rows written before 2026-05-14 do not.

**Lasting rule:** `executions.csv` is *intent*, not ground truth. Run
`./deploy/ops.sh reconcile` after every session; when the CSV and
`portfolio.activities()` disagree, the API wins. See
[performance-log.md](performance-log.md) for which baselines this invalidates.

---

## 2026-09-19: SSH lockout that looked exactly like a dead box

SSH to the VPS timed out for most of a session. The instance was healthy the whole time.

**Cause:** SSH is IP-locked — security group `sg-0714ac951294552f4` permits port 22 from
specific /32s only, and the home IP had changed.

**Fix / recognition:** a home-IP change presents identically to a dead box. Before
concluding the instance is gone, re-authorize:

```bash
aws ec2 authorize-security-group-ingress --region us-east-1 \
  --group-id sg-0714ac951294552f4 \
  --ip-permissions "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=$(curl -s https://checkip.amazonaws.com)/32}]"
```

---

## 2026-09-19: Kalshi API key exposure (precautionary rotation)

The old key's RSA private-key PEM had been stored **unquoted across multiple lines** in
`.env` and was printed into an assistant transcript. Rotated the same day. Full procedure
and the hardened flow are in [operations.md § Key rotation](operations.md#kalshi-api-key-rotation).

Two collateral fixes: `.gitignore` now covers `.env.*` (key backups were **not** ignored,
and this repo is public), and a dead `KALSHI_PRIVATE_KEY_NEWLINES` block was removed — it
was the cause of `python-dotenv could not parse statement starting at line 32` on every
startup.

---

## 2026-09-19: ran a second scanner on the live box

Starting a dry-run scanner alongside the live service pushed the live process **264MB
into swap** (`rss` fell from 275MB to 16MB). It kept trading, but with its heap on disk.

**Rule:** never run a second full scanner on the VPS while the service is active. The
service is `enabled` and auto-restarts, so a manual `python3 main.py` is a *second
executor trading the same Polymarket account*. Run `./deploy/ops.sh status` first. If it happens, restart the service to pull the heap back into RAM.

---

## 2026-09-19: CFB arbs mislabeled as MLB

`arb_tracker._sport()` fell through to `MLB` for any unrecognised series, so CFB arbs
logged between **20:32 and 21:07 UTC on 2026-09-19** are recorded as MLB and pollute the
MLB baseline for that window. Fixed (CFB added; unknown series now label `UNKNOWN`).

**Exclude that window when analysing either sport.**

---

## The silent-failure pattern

Six distinct failures, three of them found on a single day. Every one of them:

- left the process alive and the heartbeat healthy,
- produced **no error log**, and
- looked identical to "the market is quiet."

A readiness flag that never sets looks exactly like a market with no opportunities. A
discovery that matches zero markets looks exactly like a slow night. A filter that blocks
everything looks exactly like a filter working correctly.

**Consequence for design:** every gate in this system fails *closed and quiet*. So health
checks must assert on **meaning**, not liveness — "did we confirm Poly markets?", "is the
executor armed?", "did N arbs at 4%+ produce zero attempts?" Those checks and their
alerting path are documented in [operations.md § Alerting](operations.md#alerting).
