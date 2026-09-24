#!/bin/sh
# SR5: feed every byte-prefix of scripts/bootstrap.sh to `sh` (as a cut-short
# `curl | sh` would) with git stubbed, and report which prefixes ran anything.
# Originally a bare trailing `main` meant a prefix ending in that word ran
# install.sh with its arguments dropped. Fixed: the call is `{ main "$@"; exit; }`,
# so only the complete script (with or without its final newline) runs.
# Exits 1 if any TRUNCATED prefix ran install.sh.
set -u
here="$(cd "$(dirname "$0")/../.." && pwd)"
src="$here/scripts/bootstrap.sh"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/bin"
cat > "$work/bin/git" <<'EOF'
#!/bin/sh
# stub: `git clone ... <dir>` makes a fake checkout whose install.sh logs argv
for last; do :; done
mkdir -p "$last/scripts"
printf '#!/bin/sh\necho "install.sh ran: [$*]" >> "%s/ran.log"\n' "$SR5_WORK" > "$last/scripts/install.sh"
EOF
chmod +x "$work/bin/git"
size=$(wc -c < "$src")
complete=$((size - 1))          # the whole script minus its trailing newline
ran=0
truncated=0
i=${SR5_FROM:-1}
while [ "$i" -le "$size" ]; do
  rm -rf "$work/home" "$work/ran.log"; mkdir -p "$work/home"
  head -c "$i" "$src" | env -i PATH="$work/bin:/usr/bin:/bin" HOME="$work/home" \
    SR5_WORK="$work" JARVIS_DIR="$work/home/jarvis" sh -s -- --no-image --yes \
    >"$work/out" 2>&1
  if [ -s "$work/ran.log" ]; then
    ran=$((ran + 1))
    [ "$i" -lt "$complete" ] && truncated=$((truncated + 1))
    printf 'prefix %d/%d ran: %s | tail=%s\n' "$i" "$size" "$(cat "$work/ran.log")" \
      "$(head -c "$i" "$src" | tail -c 12 | tr '\n' '~')"
  fi
  i=$((i + 1))
done
echo "prefixes that executed install.sh: $ran of $size (truncated ones: $truncated)"
[ "$truncated" -eq 0 ]
