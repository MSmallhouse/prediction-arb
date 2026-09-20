#!/usr/bin/env bash
# Out-of-process memory sampler for arb-scanner.
#
# Exists because VmRSS — the only number the heartbeat and memguard read — does
# not count pages the kernel has swapped out. On 2026-09-20 the process was
# OOM-killed at ~1.6GB of anon memory while RSS read 368MB and memguard logged
# "no action" fifty minutes earlier. See docs/incidents.md.
#
# Deliberately NOT part of the service: it samples from outside so it can be
# added and removed without restarting arb-scanner, which would truncate the
# open memory-leak measurement window (docs/open-questions.md).
#
# Runs from root cron every 5 minutes. Appends one CSV row per sample.
# anon_kb = vmrss_kb + vmswap_kb is the number that actually predicts an OOM.

set -uo pipefail

OUT=/var/log/arb-memsample.csv
UNIT=arb-scanner

if [ ! -f "$OUT" ]; then
  echo "ts,pid,nrestarts,active_enter,vmrss_kb,vmswap_kb,anon_kb,cg_current,cg_swap,cg_peak,kalshi,poly" >"$OUT"
fi

ts=$(date -u +%FT%TZ)
pid=$(systemctl show "$UNIT" -p MainPID --value 2>/dev/null || echo 0)
nrestarts=$(systemctl show "$UNIT" -p NRestarts --value 2>/dev/null || echo "")
active=$(systemctl show "$UNIT" -p ActiveEnterTimestamp --value 2>/dev/null | tr ' ' '_')

# Service down: record the gap rather than skipping the row, so a restart is
# visible in the curve instead of looking like a discontinuity.
if [ -z "$pid" ] || [ "$pid" = "0" ] || [ ! -r "/proc/$pid/status" ]; then
  echo "$ts,,$nrestarts,$active,,,,,,,," >>"$OUT"
  exit 0
fi

rss=$(awk '/^VmRSS:/{print $2}'  "/proc/$pid/status")
swp=$(awk '/^VmSwap:/{print $2}' "/proc/$pid/status")
rss=${rss:-0}; swp=${swp:-0}
anon=$(( rss + swp ))

cg_cur=$(systemctl show "$UNIT"  -p MemoryCurrent --value 2>/dev/null)
cg_swp=$(systemctl show "$UNIT"  -p MemorySwapCurrent --value 2>/dev/null)
cg_peak=$(systemctl show "$UNIT" -p MemoryPeak --value 2>/dev/null)

# Market count from the last discovery line — RSS is meaningless without it,
# since slate size drives the baseline (docs/open-questions.md).
stores=$(grep 'Stores:' /home/ubuntu/prediction-arb/scanner.log 2>/dev/null | tail -1)
kalshi=$(echo "$stores" | grep -oE 'Stores: [0-9]+' | grep -oE '[0-9]+' || true)
poly=$(echo "$stores"   | grep -oE '[0-9]+ Poly'   | grep -oE '[0-9]+' || true)

echo "$ts,$pid,$nrestarts,$active,$rss,$swp,$anon,$cg_cur,$cg_swp,$cg_peak,$kalshi,$poly" >>"$OUT"
