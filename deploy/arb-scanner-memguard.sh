#!/usr/bin/env bash
# Restart arb-scanner ONLY when its memory actually warrants it.
#
# Replaces the old fixed-schedule recycle (daily, then every ~5 days). A periodic
# restart resets the RSS baseline whether or not memory is a problem, which meant
# the memory-leak curve was always chopped into fragments — check in just after a
# recycle and you had hours of data, not days.
#
# Restarting on need instead means:
#   - if the leak is gone (current evidence), the process runs for weeks and the
#     log holds one continuous RSS curve for as far back as logrotate keeps it;
#   - if the leak is real, it restarts only at the threshold, and gracefully.
#
# Graceful matters: this sends SIGTERM via systemctl, which drains the CSV writer
# queue. The MemoryMax=700M cgroup kill underneath is a SIGKILL and does NOT —
# queued rows are lost. So this guard exists to keep us off that backstop.

set -euo pipefail

# Threshold is on anon (RSS+swap), so it is no longer bounded by MemoryHigh=600M
# the way a pure-RSS number was. Observed OOM kills came at ~1.4GB anon; 900MB
# leaves roughly 12h of headroom at the measured ~40MB/h leak rate while sitting
# well clear of the 512MB CFB discovery peak that RSS alone used to collide with.
THRESHOLD_MB="${THRESHOLD_MB:-900}"
UNIT=arb-scanner
LOG=/home/ubuntu/prediction-arb/scanner.log

systemctl is-active --quiet "$UNIT" || { echo "$(date -u +%FT%TZ) memguard: $UNIT not active, nothing to do" >>"$LOG"; exit 0; }

# Read the main process's own counters, NOT systemd's MemoryCurrent. Every
# cgroup number here is blind to how this process actually dies. Measured at the
# 2026-09-22 06:30 kill: MemoryCurrent 431MB, MemoryPeak 579MB — both far under
# MemoryMax=700M — while MemorySwapCurrent was 961MB and process anon was
# 1375MB. The kill came from exhausting swap, which MemoryCurrent does not
# count and MemoryPeak does not track.
pid=$(systemctl show "$UNIT" -p MainPID --value)
[ -n "$pid" ] && [ "$pid" != "0" ] || exit 0
[ -r "/proc/$pid/status" ] || exit 0

# Anon = VmRSS + VmSwap, NOT RSS alone. RSS excludes swapped-out pages, and
# MemoryHigh=600M guarantees the kernel starts swapping this process before it
# can ever reach a 500MB RSS line. Measured 2026-09-22: RSS oscillated 450-520MB
# while true anon was 850MB and climbing, so the guard could not see the growth
# it exists to catch. Same number arb-memsample.sh records as anon_kb.
rss=$(awk '/^VmRSS:/{print $2}'  "/proc/$pid/status")
swp=$(awk '/^VmSwap:/{print $2}' "/proc/$pid/status")
[ -n "$rss" ] || exit 0
swp=${swp:-0}
mb=$(( (rss + swp) / 1024 ))

# Require TWO breaches before restarting. Discovery transiently spikes memory —
# measured 394MB mid-discovery against a 219MB steady state on 2026-09-20, and
# 512MB with CFB at a 6h lookahead. Discovery is hourly and this check is hourly,
# so a single-sample trigger could phase-lock with the spike and restart the
# scanner every hour forever. Two samples above the line means sustained growth.
#
# Headroom check: the observed kill point is ~1375MB anon and the measured leak
# is ~40MB/h, so firing at 900MB leaves ~11h even in the worst case where the
# first breach is missed and the second lands an hour later.
STATE=/var/lib/arb-scanner-memguard.count
[ -f "$STATE" ] || echo 0 > "$STATE"
count=$(cat "$STATE" 2>/dev/null || echo 0)

if [ "$mb" -ge "$THRESHOLD_MB" ]; then
  count=$(( count + 1 ))
  echo "$count" > "$STATE"
  if [ "$count" -ge 2 ]; then
    echo "$(date -u +%FT%TZ) memguard: anon ${mb}MB >= ${THRESHOLD_MB}MB for ${count} consecutive checks — graceful restart" >>"$LOG"
    echo 0 > "$STATE"
    systemctl restart "$UNIT"
  else
    echo "$(date -u +%FT%TZ) memguard: anon ${mb}MB >= ${THRESHOLD_MB}MB (${count}/2) — likely a discovery spike, waiting" >>"$LOG"
  fi
else
  # Decay by one instead of zeroing. A hard reset made the guard unable to fire
  # at all once swapping began: anon hovered either side of the line and every
  # dip wiped the count before it could reach 2. Logged 1/2 twice on 2026-09-22,
  # reset both times, while the process was at 850MB en route to an OOM kill.
  if [ "$count" != "0" ]; then
    count=$(( count - 1 ))
    echo "$count" > "$STATE"
    echo "$(date -u +%FT%TZ) memguard: anon ${mb}MB back under ${THRESHOLD_MB}MB — count decayed to ${count}" >>"$LOG"
  fi
  echo "$(date -u +%FT%TZ) memguard: anon ${mb}MB < ${THRESHOLD_MB}MB — no action" >>"$LOG"
fi
