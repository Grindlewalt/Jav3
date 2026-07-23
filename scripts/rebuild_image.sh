#!/usr/bin/env bash
# Monthly golden-image rebuild (D1). Computes the next version, builds it via
# vm/build_base.sh (which refuses to clobber an existing base), and leaves it in
# place — backend/vm/lifecycle._base_image() auto-activates the HIGHEST version
# on the next guest boot, so a fresh patched kernel takes over without an env
# edit. Runs standalone (works even if the app is down); driven by the timer.
set -euo pipefail
cd "$(dirname "$0")/.."

latest=$(ls data/vm/base-v*.qcow2 2>/dev/null \
         | sed -E 's|.*/base-v([0-9]+)\.qcow2|\1|' | sort -n | tail -1)
next=$(( ${latest:-0} + 1 ))
echo "[rebuild] building golden image v${next} (current highest: v${latest:-none})"

JARVIS_VM_IMAGE_VERSION="v${next}" bash vm/build_base.sh

if [[ -f "data/vm/base-v${next}.qcow2" ]]; then
  echo "[rebuild] built base-v${next}.qcow2 — auto-activates on next guest boot"
else
  echo "[rebuild] FAILED: base-v${next}.qcow2 was not produced" >&2
  exit 1
fi
