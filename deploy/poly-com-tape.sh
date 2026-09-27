#!/usr/bin/env bash
# Nightly full-tick price tape from polymarket.COM, for the tracker comparison
# in docs/open-questions.md ("is .com a better proxy than Kalshi?").
#
# Why a wrapper and not just the timer calling python directly:
#
#   - The first 2026-09-23 run recorded book snapshots only (price_change parsing
#     was broken), so the dataset could not resolve the sub-second lead it exists
#     to measure. That is fixed, but the failure was silent — hence the row-count
#     report at the end of every run, written where ops.sh tape can show it.
#   - A second long-lived process changes the OOM headroom math on a 908MB box
#     that already swaps. This refuses to start when the scanner is already near
#     the memguard threshold: research must never be the reason trading dies.
#   - Full tick is ~25x the row rate of the snapshot run. Raw nights are ~100MB+
#     and the box has ~2GB free, so finished tapes are gzipped and pruned here.
#
# Read-only, no credentials, no import of scanner code.

set -uo pipefail

DIR=/home/ubuntu/prediction-arb
OUTDIR="$DIR/research"
LOG="$OUTDIR/tape.log"
MINUTES="${MINUTES:-300}"
MAX_GAMES="${MAX_GAMES:-10}"
HOURS_BACK="${HOURS_BACK:-6}"
# Below the 900MB memguard line, so a night of collection cannot be what tips a
# scanner that is already climbing. Skipping a night costs nothing; an OOM during
# a slate costs a position and a measurement window.
SKIP_ABOVE_MB="${SKIP_ABOVE_MB:-700}"
MIN_FREE_MB="${MIN_FREE_MB:-600}"

mkdir -p "$OUTDIR"
say() { echo "$(date -u +%FT%TZ) tape: $*" >>"$LOG"; }

# --- guard: scanner memory pressure (anon = VmRSS + VmSwap, same number memguard
# --- uses; cgroup counters are blind to how this process actually dies)
pid=$(systemctl show arb-scanner -p MainPID --value 2>/dev/null)
if [ -n "$pid" ] && [ "$pid" != "0" ] && [ -r "/proc/$pid/status" ]; then
  rss=$(awk '/^VmRSS:/{print $2}' "/proc/$pid/status")
  swp=$(awk '/^VmSwap:/{print $2}' "/proc/$pid/status"); swp=${swp:-0}
  anon=$(( (rss + swp) / 1024 ))
  if [ "$anon" -ge "$SKIP_ABOVE_MB" ]; then
    say "SKIP — scanner anon ${anon}MB >= ${SKIP_ABOVE_MB}MB, not adding load before memguard acts"
    exit 0
  fi
  say "scanner anon ${anon}MB — ok to collect"
fi

# --- guard: disk
free_mb=$(df -Pm / | awk 'NR==2{print $4}')
if [ "$free_mb" -lt "$MIN_FREE_MB" ]; then
  say "SKIP — only ${free_mb}MB free on /, need ${MIN_FREE_MB}MB"
  exit 0
fi

OUT="$OUTDIR/poly_com_tape_$(date -u +%Y%m%d).csv"
say "start — ${MINUTES}min, max ${MAX_GAMES} games, out $(basename "$OUT")"

python3 "$DIR/poly_com_tape.py" \
  --minutes "$MINUTES" --max-games "$MAX_GAMES" --hours-back "$HOURS_BACK" \
  --out "$OUT" >>"$LOG" 2>&1
rc=$?

# Assert on MEANING, not exit status. A clean exit with 40 rows is the failure
# mode this project keeps hitting: everything "worked" and the dataset is empty.
rows=0; [ -f "$OUT" ] && rows=$(( $(wc -l <"$OUT") - 1 ))
if [ "$rows" -lt 5000 ]; then
  say "WARNING — only ${rows} rows (rc=$rc). Expect ~10k+/hour at full tick; a near-empty tape means resolution or parsing broke, not a quiet slate."
else
  say "done — ${rows} rows (rc=$rc)"
fi

# --- gzip finished tapes (never today's, it may still be open) and prune
find "$OUTDIR" -name 'poly_com_tape_*.csv' ! -name "$(basename "$OUT")" -mtime +0 \
  -exec gzip -f {} \; 2>/dev/null
ls -1t "$OUTDIR"/poly_com_tape_*.csv.gz 2>/dev/null | tail -n +8 | xargs -r rm -f
say "tapes on disk: $(ls -1 "$OUTDIR"/poly_com_tape_* 2>/dev/null | wc -l), $(du -sh "$OUTDIR" | cut -f1)"
