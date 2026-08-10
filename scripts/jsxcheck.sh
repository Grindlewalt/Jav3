#!/usr/bin/env bash
# Syntax-check JSX files without a local Node.
#
# This laptop has no Node, so `npm run build` on the Pi is normally the first
# thing that ever parses a frontend edit — which makes a typo a deploy-and-wait
# round trip. esbuild is already on the Pi as a vite dependency and will parse
# JSX on stdin, so this pipes each file over and reports only the failures.
#
# It is a SYNTAX gate, not a review: esbuild will happily accept a hook-order
# violation, a bad prop or an undefined variable. It catches the class of
# mistake that blanks the app for a stupid reason.
#
# Usage:  scripts/jsxcheck.sh frontend/src/App.jsx frontend/src/pages/*.jsx
set -uo pipefail
HOST="${JSXCHECK_HOST:-grindlewalt@atomostest}"
ESB="~/jarvis/frontend/node_modules/.bin/esbuild"
rc=0
for f in "$@"; do
  [ -f "$f" ] || { echo "MISSING  $f"; rc=1; continue; }
  # CSS is skipped on purpose: esbuild's standalone CSS parser reports
  # syntax errors on this stylesheet that vite's own pipeline builds without
  # complaint (styles.css:1926 today), so it is a false-positive generator.
  case "$f" in
    *.css) echo "skip     $f (esbuild's css parser disagrees with vite's)"; continue ;;
    *.jsx) loader=jsx ;;
    *) loader=js ;;
  esac
  # stdin puts esbuild in transform mode, which has no --outfile: discard the
  # transformed output on the far side and keep only what it says on stderr.
  out=$(ssh -o BatchMode=yes "$HOST" \
          "$ESB --loader=$loader --log-level=warning > /dev/null" \
          < "$f" 2>&1)
  if [ -n "$out" ]; then
    echo "FAIL     $f"
    echo "$out" | sed 's/^/         /'
    rc=1
  else
    echo "ok       $f"
  fi
done
exit $rc
