#!/usr/bin/env bash
# ops.sh — one entry point for operating the live arb-scanner on the VPS.
#
# Exists because the safety checks below are easy to skip when typed by hand,
# and every failure this system has had was silent. Run from the repo root.
#
#   ./deploy/ops.sh status      what is running, is it healthy
#   ./deploy/ops.sh deploy      push -> pull on box -> syntax check -> restart -> verify
#   ./deploy/ops.sh restart     safe restart (refuses if a position is open)
#   ./deploy/ops.sh stop|start
#   ./deploy/ops.sh logs [n]    tail scanner.log
#   ./deploy/ops.sh reconcile   run reconcile.py on the box
#
# Add --force to restart/deploy to override the open-position guard.

set -euo pipefail

HOST="ubuntu@98.82.172.44"
KEY="$HOME/.ssh/arb-key.pem"
DIR="~/prediction-arb"
SSH=(ssh -o ConnectTimeout=15 -o BatchMode=yes -i "$KEY" "$HOST")

FORCE=0
for a in "$@"; do [ "$a" = "--force" ] && FORCE=1; done

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
die()  { printf '\033[31mABORT: %s\033[0m\n' "$*" >&2; exit 1; }

# SSH is IP-locked to specific /32s. A timeout here is far more likely to be a
# changed home IP than a dead box — do not conclude the instance is gone.
preflight() {
  "${SSH[@]}" true 2>/dev/null || die "cannot reach $HOST.
  SSH is IP-locked; your home IP may have changed. Re-authorize with:
  aws ec2 authorize-security-group-ingress --region us-east-1 \\
    --group-id sg-0714ac951294552f4 \\
    --ip-permissions \"IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=\$(curl -s https://checkip.amazonaws.com)/32}]\""
}

# A restart abandons any position the executor is holding. BUYS == SELLS in
# executions.csv means nothing is open.
assert_no_open_position() {
  local out b s
  out=$("${SSH[@]}" "cd $DIR && echo \$(grep -c ',BUY,' executions.csv) \$(grep -cE ',SELL_' executions.csv)")
  b=${out% *}; s=${out#* }
  if [ "$b" != "$s" ]; then
    [ "$FORCE" = "1" ] && { echo "WARNING: $b BUYs vs $s SELLs — open position, continuing due to --force"; return; }
    die "$b BUYs vs $s SELLs — a position appears to be OPEN.
  Restarting now abandons it. Wait for the exit, or re-run with --force."
  fi
  echo "no open position ($b BUYs, $s SELLs)"
}

# Python holds the code in memory, so files on disk can be newer than what is
# running. Restart is never optional after changing code.
verify_running() {
  say "waiting for a post-restart heartbeat (up to ~6 min)"
  "${SSH[@]}" "
    for i in \$(seq 1 40); do
      hb=\$(grep heartbeat $DIR/scanner.log | tail -1)
      case \"\$hb\" in *' live '*) echo \"\$hb\"; break;; esac
      sleep 10
    done"
  say "post-restart health"
  cmd_status
  local errs
  errs=$("${SSH[@]}" "grep -cE 'Traceback|HEALTH ALERT' $DIR/scanner.log || true")
  [ "$errs" = "0" ] || echo "NOTE: $errs Traceback/HEALTH ALERT lines in the log — inspect with 'logs'"
}

cmd_status() {
  "${SSH[@]}" "
    echo \"service : \$(systemctl is-active arb-scanner)\"
    systemctl show arb-scanner -p NRestarts -p ActiveEnterTimestamp -p MemoryCurrent | sed 's/^/          /'
    cd $DIR
    echo \"commit  : \$(git log --oneline -1)\"
    dirty=\$(git status --porcelain -- '*.py' | wc -l | tr -d ' ')
    if [ \"\$dirty\" != \"0\" ]; then
      echo \"          WARNING: \$dirty tracked .py file(s) differ from the commit —\"
      echo \"          the box was modified outside git; 'git log' no longer says what is running\"
    fi
    echo
    echo 'last heartbeat:'; grep heartbeat scanner.log | tail -1 | sed 's/^/  /'
    echo 'last discovery:'; grep 'Stores:' scanner.log | tail -1 | sed 's/^/  /'
    echo
    echo \"alerts  : \$(grep -c 'HEALTH ALERT' scanner.log || true) HEALTH ALERT lines\"
    echo \"stuck   : \$(grep -c 'SELL_EXIT_FAILED' executions.csv || true) failed taker exits\"
  "
  cat <<'NOTE'

  Read the heartbeat, not just 'active'. Both counts must be non-zero:
  'P: 0/N confirmed' with Kalshi healthy means discovery is silently broken —
  that exact state ran for four months while heartbeats looked fine.
NOTE
}

cmd_deploy() {
  [ -z "$(git status --porcelain)" ] || die "local tree is dirty. Commit first — what runs on the box should be a named commit."
  git remote update >/dev/null 2>&1 || true
  [ -z "$(git log --oneline origin/main..HEAD)" ] || die "local commits are not pushed. Run: git push origin main"
  preflight
  say "pre-deploy state"; cmd_status
  say "checking for open positions"; assert_no_open_position
  say "pulling on the box"
  "${SSH[@]}" "cd $DIR && git pull --ff-only"
  say "syntax-checking every module before restart"
  "${SSH[@]}" "cd $DIR && for f in \$(git ls-files '*.py'); do python3 -c \"import ast,sys;ast.parse(open(sys.argv[1]).read())\" \$f || exit 1; done && echo 'all modules parse'"
  say "restarting"
  "${SSH[@]}" "sudo systemctl restart arb-scanner && echo restarted"
  verify_running
}

cmd_restart() {
  preflight
  say "checking for open positions"; assert_no_open_position
  "${SSH[@]}" "sudo systemctl restart arb-scanner && echo restarted"
  verify_running
}

case "${1:-status}" in
  status)    preflight; cmd_status ;;
  deploy)    cmd_deploy ;;
  restart)   cmd_restart ;;
  stop)      preflight; assert_no_open_position; "${SSH[@]}" "sudo systemctl stop arb-scanner && echo stopped.
NOTE: the daily 10:00 UTC timer will NOT restart a stopped service - start it yourself." ;;
  start)     preflight; "${SSH[@]}" "sudo systemctl start arb-scanner && echo started"; verify_running ;;
  logs)      preflight; "${SSH[@]}" "tail -${2:-40} $DIR/scanner.log" ;;
  reconcile) preflight; "${SSH[@]}" "cd $DIR && python3 reconcile.py executions.csv" ;;
  *)         sed -n '2,20p' "$0"; exit 1 ;;
esac
