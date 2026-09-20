# Operations runbook

---

## The box

AWS EC2 **t3.micro**, us-east-1 (Virginia). Instance `i-0923ce83c9a4b7047`, AWS account
`828841719603`. Co-located with Polymarket origin servers.

| Leg | Latency |
|---|---|
| Polymarket | ~1.2 ms (was 5-6 ms from home) |
| Kalshi | ~0.8 ms (was 18-19 ms from home) |

```bash
ssh -i ~/.ssh/arb-key.pem ubuntu@98.82.172.44
```

### Three things that will bite you

⚠️ **SSH is IP-locked.** Security group `sg-0714ac951294552f4` allows port 22 from specific
/32s only. **A home-IP change looks exactly like a dead box** — SSH times out while the
instance is perfectly healthy. This cost most of a session on 2026-09-19.

```bash
aws ec2 authorize-security-group-ingress --region us-east-1 \
  --group-id sg-0714ac951294552f4 \
  --ip-permissions "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=$(curl -s https://checkip.amazonaws.com)/32}]"
```

⚠️ **No Elastic IP.** `98.82.172.44` is a dynamic public IP held only while the instance
runs. **Stopping the instance loses it**, breaking every hardcoded reference in these docs
and the SSH command above. Don't stop/start without allocating an EIP first, or expect a
new address.

⚠️ **Root volume is only 8GB** (~68% used as of 2026-09-19, after trimming swap to 1GB).
`convergence_log.csv` and 30 days of rotated `scanner.log` are the growth drivers. Check
`df -h /` before adding anything large. The swapfile is deliberately **1GB, not 2GB** — a
2GB file pushed the volume to 83%.

---

## Service management

Runs under **systemd since 2026-09-19**. `screen` is gone — it could not restart the
process after an OOM kill, which is why one OOM became a four-month outage
([incidents.md](incidents.md#2026-05-15--2026-09-19-the-four-month-outage)).

```bash
sudo systemctl status  arb-scanner   # or: ./deploy/ops.sh status
sudo systemctl restart arb-scanner
sudo systemctl stop    arb-scanner
```

- Logs → `~/prediction-arb/scanner.log`. Logrotate: daily, keep 30, gzipped.
  **`delaycompress` means `scanner.log.1` is NOT gzipped** — grepping only `*.gz` silently
  skips the most recent full day. Use both `zgrep` and `grep`.
  The logrotate config needs `su ubuntu ubuntu` or it **silently skips** the group-writable
  directory.
- `arb-scanner-restart.timer` recycles the service daily at **10:00 UTC** to cap the memory
  leak. Logrotate runs ~00:49 UTC.
- Memory caps: `MemoryHigh=600M`, `MemoryMax=700M`, `OOMPolicy=restart`, `Restart=always`.
  Raised from 550/650 to fit CFB.
- 1GB swapfile at `/swapfile`, in `/etc/fstab`.

Unit files live in `deploy/` in this repo:

```bash
sudo cp deploy/arb-scanner* /etc/systemd/system/
sudo cp deploy/logrotate-arb /etc/logrotate.d/arb-scanner
sudo chown root:root /etc/logrotate.d/arb-scanner
sudo systemctl daemon-reload
```

⚠️ **Never start the scanner by hand.** The service is `enabled` and auto-restarts, so a
manual `python3 main.py` is a **second executor trading the same Polymarket account
concurrently**. Check `systemctl is-active arb-scanner` first. Running a dry-run scanner
alongside the live one on 2026-09-19 pushed the live process 264MB into swap — it kept
trading, with its heap on disk. Restart the service to recover.

---

## Operating the box — use `deploy/ops.sh`

All routine operations go through one script, so the safety checks are executed rather
than remembered. Run it from the repo root.

```bash
./deploy/ops.sh status       # what is running, and is it actually working
./deploy/ops.sh deploy       # push -> pull on box -> syntax check -> restart -> verify
./deploy/ops.sh restart      # safe restart (refuses if a position is open)
./deploy/ops.sh stop | start
./deploy/ops.sh logs [n]
./deploy/ops.sh reconcile    # runs reconcile.py on the box
./deploy/ops.sh pull         # CSVs + log into a dated vps_pull_* dir
./deploy/ops.sh heartbeats   # full RSS/lag history across rotated logs
```

`pull` writes to a dated directory rather than over the repo-root CSVs — those are stale
May subsets, and overwriting them would destroy the only local copy of
`convergence_log.csv`. `heartbeats` reads `*.gz` + `.log.1` + `.log`, because
`delaycompress` leaves `scanner.log.1` ungzipped and grepping only `*.gz` silently skips
the most recent full day.

Add `--force` to override the open-position guard on `restart`/`deploy`/`stop`.

**What it enforces, and why each check exists:**

| Check | Why |
|---|---|
| Local tree clean + pushed before deploy | What runs on the box should be a named commit, not a working copy |
| `BUYS == SELLS` in `executions.csv` | A restart abandons an open position. This is the guard the 2026-05-14 incident argues for |
| Every module parses on the box before restart | A syntax error means the service restart-loops at `RestartSec=15`, and the CloudWatch alarm may never fire — see [Alerting](#alerting) |
| Waits for a real post-restart heartbeat | `active` is not health. It asserts a `live` heartbeat actually appeared |
| Warns if tracked `.py` differ from the commit | Detects the scp-drift failure below automatically |
| Reports HEALTH ALERT and stuck-position counts | Both are silent otherwise |

**Deploy is `git pull`, not `scp`** (since 2026-09-20). Both work, but scp silently
desynchronizes the box's git state: it ran for months at commit `872f350` with a dirty tree
while the code was five commits newer, so `git log` on the box was a lie and the only way to
know what was running was hashing every file. With `git pull`, `git log -1` is a true
answer. Use scp only for an emergency uncommitted hotfix, and commit it immediately after.

⚠️ **`git pull` alone is NOT a deploy.** Python loads code into memory at start, so pulling
without restarting leaves the box *looking* updated (new commit in `git log`) while still
executing the old code, with no error anywhere. `ops.sh deploy` always restarts.

⚠️ **Nothing deploys automatically.** There is no webhook, cron, or git hook on the box —
verified 2026-09-20. Pushing to GitHub does nothing until someone runs `ops.sh deploy`.
`arb-scanner-restart.timer` restarts whatever is already on disk; it does not fetch.

Expect the **first** post-restart heartbeat to show a large `loop lag max` and 12-13 GC
collections. That is the known startup/discovery signature, not a regression
([findings-rejected.md](findings-rejected.md#gc-tuning--three-attempts-none-fixed-the-pauses)).

Backups from the 2026-09-20 reconciliation are on the box at
`~/vps-pre-reconcile-20260920/` (pre-reset code) and `~/vps-stray-quarantine-20260920/`
(dead May-era copies: `scrapers/main.py`, `scrapers/executor.py`, a root-level
`polymarket_us_private_ws.py`, two dry-run CSVs). Delete once you are satisfied nothing
regressed.

---

## Health check, in order

```bash
systemctl is-active arb-scanner
systemctl show arb-scanner -p ActiveState -p NRestarts -p MemoryCurrent
grep rss ~/prediction-arb/scanner.log | tail -3      # heartbeat RSS + tasks
grep "Stores:" ~/prediction-arb/scanner.log | tail -3
```

**The line that matters most is `Stores: N Kalshi markets, M Poly markets`.**
`M == 0` with `N > 0` means **discovery is silently broken** — not that markets are quiet.
That exact signature ran for four months while heartbeats looked healthy.

Heartbeat fields (every 300s):
`warmup/live · K: n/N confirmed · P: n/N confirmed · 3%: n active · 4%: n active ·
last-tick age · rss NNNMB tasks N · loop lag max NNms · gc N cols max NNms tot NNms · csvq N`
— peaks reset each window.

---

## Alerting

Motivated by three silent failures in one day. Process liveness would have caught **none**
of them, so the checks assert on **meaning**:

| Check | Catches |
|---|---|
| Kalshi confirmed > 0 while Poly confirmed == 0 | Discovery/WS broken (the May–Sep outage) |
| Empty Kalshi store after 600s uptime | Discovery dead entirely |
| No price tick for 180s | Feeds stalled |
| Executor enabled but private WS not ready | Cannot trade (2026-09-19 evening) |
| ≥25 arbs at 4%+ with zero execution attempts | A filter is blocking everything |
| CSV writer backlog > 100 | Writer thread wedged |
| Any `executor._stuck_positions` | Failed taker exits left positions open |

All seven checks are independent — the first six were briefly `elif`-chained in pairs,
which meant "arbs but no attempts" could never fire while the private WS was down. Fixed
2026-09-19; keep them independent, since one silent failure does not exclude another.

### Email is the primary channel

The box runs unattended for months, so email matters more than logs.

- SNS topic `arn:aws:sns:us-east-1:828841719603:arb-scanner-alerts`, subscribed to the
  operator's email.
- The instance carries IAM role **`arb-scanner-role`** via instance profile
  `arb-scanner-profile`, scoped to `sns:Publish` **on that topic only**. boto3 picks the
  credentials up from IMDSv2 — **there are no AWS keys in `.env`**.
  If alerting stops, first check the role is still associated:
  ```bash
  aws ec2 describe-iam-instance-profile-associations --region us-east-1 \
    --filters Name=instance-id,Values=i-0923ce83c9a4b7047
  ```
- `main._send_email_alert()` de-duplicates: an identical problem set is not re-sent for
  `ALERT_REPEAT_SUPPRESS_S` (1 hour), so a long outage doesn't flood the inbox.
- Emails carry confirmed-market counts, arb count, execution attempts and RSS — enough to
  triage without SSHing in.

⚠️ **A subscription must be CONFIRMED to deliver.** A pending subscription silently
swallows every alert — publishes succeed, nothing arrives.

```bash
aws sns list-subscriptions-by-topic --region us-east-1 \
  --topic-arn arn:aws:sns:us-east-1:828841719603:arb-scanner-alerts
```

A real ARN means live; `PendingConfirmation` means alerts are going nowhere.

### Optional channels (no-op when unset)

- `ALERT_WEBHOOK_URL` — POSTed a JSON summary (Slack/Discord).
- `ALERT_HEARTBEAT_URL` — pinged on every HEALTHY heartbeat. Dead man's switch: with
  healthchecks.io the **absence** of a ping alerts, which is the only way to catch the box
  disappearing entirely. **Still unset** — see
  [future-work.md](future-work.md#7-close-the-remaining-monitoring-gap).

### AWS-side backstops (no box involvement, same topic)

- `arb-scanner-process-dead` — NetworkIn < 10MB/5min for 15min (live ~250MB/5min, dead ~3KB).
- `arb-scanner-cpu-credits-low` — CPUCreditBalance < 50, the throttling early warning.

**The AWS bill is not a liveness signal.** The instance stayed `running` with all
reachability checks `ok` and ~$1/mo billing for the entire four-month outage.

---

## Pulling data

```bash
scp -i ~/.ssh/arb-key.pem 'ubuntu@98.82.172.44:~/prediction-arb/*.csv' ~/Documents/prediction-arb/
scp -i ~/.ssh/arb-key.pem ubuntu@98.82.172.44:~/prediction-arb/scanner.log ~/Documents/prediction-arb/scanner_vps.log
```

Local repo-root CSVs are **stale subsets** of `vps_pull_20260919/`. Analyse the pull, not
the root files — see [performance-log.md](performance-log.md).

**After every trading session:** `python3 reconcile.py executions.csv`. `executions.csv` is
intent, not truth.

---

## Environment

`.env` contains exactly four variables:
`POLYMARKET_API_KEY_ID`, `POLYMARKET_PRIVATE_KEY`, `KALSHI_API_KEY_ID`,
`KALSHI_PRIVATE_KEY`.

Optional env-only settings with defaults: `ALERT_WEBHOOK_URL`, `ALERT_HEARTBEAT_URL`,
`ALERT_SNS_TOPIC_ARN`, `ALERT_SNS_REGION`.

⚠️ **The GitHub repo is PUBLIC.** `.env` and `.env.*` are gitignored — the `.env.*` pattern
was added 2026-09-19 after key backups were found unignored.

---

## Kalshi API key rotation

`rotate_kalshi_key.py` automates it: validates the PEM, **proves the key against the live
API before changing anything**, backs up `.env`, rewrites it, deploys to the VPS, restarts.
Run with `--verify-only` to test a key harmlessly; `--skip-deploy` for local-only.

**Generate the key locally, never let Kalshi generate it.** Kalshi's "Create API key"
dialog accepts an optional RSA **public** key; supplying one means Kalshi never generates
or displays a private key. The current key lives at `~/.kalshi-keys/kalshi-2026-09-19.pem`
(mode 600, outside the repo) and has never left the machine.

**Scopes: `read` ONLY.** "Full access" includes `write::transfer` — withdrawals. Never
grant it to this bot. Rationale and the Kalshi-side detail:
[platforms.md](platforms.md#kalshi).

Store the PEM **quoted** in `.env`. The previous key was stored unquoted across multiple
lines, which both leaked it into a transcript and caused
`python-dotenv could not parse statement starting at line 32` on every startup.

Verification after the 2026-09-19 rotation: `K: 204/204 confirmed` on the WS, zero auth
failures, new key id on both local and VPS `.env`.

---

## Accounts

| Platform | Account | Balance (2026-09-19) | Used for |
|---|---|---|---|
| Polymarket US | `fat.lobster` (iOS app) | ~$70 | **All execution** |
| Kalshi | — | $10 | Read-only price feed |
