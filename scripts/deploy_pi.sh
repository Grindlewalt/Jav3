#!/usr/bin/env bash
# Deploy the working tree to the Pi, refusing to restart over live work.
#
# This exists because the guard keeps getting re-typed by hand and keeps
# getting it wrong. Twice now the check has printed a non-zero count and the
# restart has run anyway — once because the count was piped into `&&` (a
# successful `echo` is exit 0), once because the check and the deploy were on
# separate lines of the same ssh heredoc, so a non-zero exit gated nothing.
#
# `set -e` plus one chain is the whole point. Do not inline this again.
#
#   scripts/deploy_pi.sh            # guard, pull, build if needed, restart
#   scripts/deploy_pi.sh --no-build # skip the frontend build (backend-only change)
#   FORCE=1 scripts/deploy_pi.sh    # restart anyway (you had better be sure)
set -euo pipefail

PI="${PI:-grindlewalt@atomostest}"
BUILD=1
[ "${1:-}" = "--no-build" ] && BUILD=0

echo "==> guard: in-flight tool calls?"
ssh "$PI" 'cd ~/jarvis && .venv/bin/python - '"${FORCE:-0}"' <<PY
import sqlite3, sys
force = sys.argv[1] == "1"
n = sqlite3.connect("data/jarvis.db").execute(
    "SELECT COUNT(*) FROM tool_calls "
    "WHERE created_at > datetime(\"now\", \"-60 seconds\")").fetchone()[0]
print(f"    {n} tool call(s) in the last 60s")
if n and not force:
    print("    REFUSING to restart over live work (FORCE=1 to override)")
    sys.exit(1)
PY'

echo "==> pull"
ssh "$PI" 'cd ~/jarvis && git pull -q && git log --oneline -1'

if [ "$BUILD" = 1 ]; then
  echo "==> frontend build"
  ssh "$PI" 'cd ~/jarvis/frontend && npm run build 2>&1 | tail -2'
fi

echo "==> restart"
ssh "$PI" 'systemctl --user restart jarvis'
sleep 8
echo "==> health"
ssh "$PI" 'curl -sf localhost:8000/api/health && echo && systemctl --user is-active jarvis'
