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

THRESHOLD_MB="${THRESHOLD_MB:-500}"     # below MemoryHigh=600M, so we restart before throttling
UNIT=arb-scanner
LOG=/home/ubuntu/prediction-arb/scanner.log

systemctl is-active --quiet "$UNIT" || { echo "$(date -u +%FT%TZ) memguard: $UNIT not active, nothing to do" >>"$LOG"; exit 0; }

# Use the main process's RSS, NOT systemd's MemoryCurrent. MemoryCurrent is the
# cgroup total and includes reclaimable page cache — measured 2026-09-20 it read
# 416MB while the process RSS was 207MB, so a 500MB threshold on it would have
# fired at ~250MB of real usage and restarted the scanner continuously. RSS is
# also the number the heartbeat logs and every baseline in the docs is quoted in,
# so this keeps the guard and the measurements on the same scale.
pid=$(systemctl show "$UNIT" -p MainPID --value)
[ -n "$pid" ] && [ "$pid" != "0" ] || exit 0
kb=$(ps -o rss= -p "$pid" 2>/dev/null | tr -d ' ')
[ -n "$kb" ] || exit 0
mb=$(( kb / 1024 ))

if [ "$mb" -ge "$THRESHOLD_MB" ]; then
  echo "$(date -u +%FT%TZ) memguard: RSS ${mb}MB >= ${THRESHOLD_MB}MB — graceful restart" >>"$LOG"
  systemctl restart "$UNIT"
else
  echo "$(date -u +%FT%TZ) memguard: RSS ${mb}MB < ${THRESHOLD_MB}MB — no action" >>"$LOG"
fi
