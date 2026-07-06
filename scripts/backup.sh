#!/usr/bin/env bash
# Back up Jarvis's durable state to the Gitea backup repo. Idempotent: only
# commits + pushes when something actually changed. Run by the systemd timer,
# or by hand: bash scripts/backup.sh
#
# What's backed up: memory/, projects/, skills/, agents/, and a text dump of
# the SQLite DB (conversations, schedules, fetch ledger). Deliberately NOT
# backed up: the code (already in the GitHub repo), the VM images (huge,
# rebuildable), the JWT secret and API key (secrets), derived files.
set -euo pipefail

JARVIS="$HOME/jarvis"
BACKUP="$HOME/jarvis-backup"
TOKEN_FILE="$HOME/.config/jarvis/gitea-token"
REPO_HOST="${JARVIS_BACKUP_HOST:-atomosnas:3000}"
REPO_PATH="${JARVIS_BACKUP_PATH:-Grindlewalt/Jarvis-Backup.git}"
BRANCH="main"

[ -f "$TOKEN_FILE" ] || { echo "no gitea token at $TOKEN_FILE" >&2; exit 1; }
TOKEN="$(cat "$TOKEN_FILE")"
REMOTE="http://Grindlewalt:${TOKEN}@${REPO_HOST}/${REPO_PATH}"
redact() { sed "s/${TOKEN}/TOKEN/g"; }

if [ ! -d "$BACKUP/.git" ]; then
  mkdir -p "$BACKUP"
  git -C "$BACKUP" init -q
  git -C "$BACKUP" checkout -q -B "$BRANCH"
fi
git -C "$BACKUP" remote remove origin 2>/dev/null || true
git -C "$BACKUP" remote add origin "$REMOTE"
git -C "$BACKUP" config user.email "jarvis@atomostest"
git -C "$BACKUP" config user.name "Jarvis Backup"

# mirror durable state (drop junk + the ephemeral scratch dir)
RSYNC=(rsync -a --delete
  --exclude '__pycache__' --exclude '*.pyc' --exclude '.venv'
  --exclude 'node_modules' --exclude 'dist' --exclude '.ephemeral-notes')
for d in memory projects skills agents; do
  if [ -d "$JARVIS/$d" ]; then
    mkdir -p "$BACKUP/$d"
    "${RSYNC[@]}" "$JARVIS/$d/" "$BACKUP/$d/"
  fi
done

# consistent, diffable text dump of the DB (rolls back cleanly, unlike a blob)
mkdir -p "$BACKUP/data"
"$JARVIS/.venv/bin/python" - "$JARVIS/data/jarvis.db" "$BACKUP/data/jarvis.sql" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
con = sqlite3.connect(src)
try:
    with open(dst, "w") as f:
        for line in con.iterdump():
            f.write(line + "\n")
finally:
    con.close()
PY

cat > "$BACKUP/.gitignore" <<'EOF'
# never back these up (secrets / huge / derived)
data/vm/
data/jwt_secret
data/registry.json
EOF
cat > "$BACKUP/README.md" <<'EOF'
# Jarvis backup

Automatic snapshots of Jarvis's durable state from the test server. Each commit
is a restore point.

- `memory/` `projects/` `skills/` `agents/` — mirrored files
- `data/jarvis.sql` — text dump of the SQLite DB (conversations, schedules, ledger)

Code lives in the separate Jarvis repo; VM images and secrets are intentionally
excluded. Restore: check out a commit and rsync the dirs back / reload the DB.
EOF

git -C "$BACKUP" add -A
if git -C "$BACKUP" diff --cached --quiet; then
  echo "no changes to back up"
else
  git -C "$BACKUP" commit -q -m "backup $(date -u +%FT%TZ)"
  echo "committed a new snapshot"
fi

if git -C "$BACKUP" push -q origin "$BRANCH" 2>&1 | redact; then
  echo "backup pushed to $REPO_HOST/$REPO_PATH"
else
  echo "PUSH FAILED — does the repo exist on Gitea? (create an empty private" >&2
  echo "  '$REPO_PATH' or give the token write:user scope)" >&2
  exit 2
fi
