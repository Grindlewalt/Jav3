#!/bin/sh
# Install the jav3 CLI from the server this script was downloaded from.
#
#   curl -fsSL <server>/cli/install.sh | sh
#
# The server fills in BASE when it serves this file. Installs into
# ~/.local/share/jav3 and puts a `jav3` launcher in ~/.local/bin (override with
# JAV3_BIN). httpx comes from the system python if it already has it, else from
# a private venv — never `pip install` into the system interpreter.
# Everything runs inside main(), and the call is wrapped in `{ ...; }` so a
# download cut short anywhere — even inside the last line — is a syntax error
# that executes nothing.
set -eu

# Same constraint as the server's requirements.txt; the upper bound keeps an
# incompatible major release from landing on a fresh install unannounced.
HTTPX_SPEC='httpx>=0.27,<1'

main() {
  BASE="@@BASE@@"
  bin_dir="${JAV3_BIN:-$HOME/.local/bin}"
  share="${XDG_DATA_HOME:-$HOME/.local/share}/jav3"

  command -v python3 >/dev/null 2>&1 || { echo "jav3 needs python3 (3.11+)" >&2; exit 1; }
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
    || { echo "jav3 needs Python 3.11 or newer ($(python3 -V 2>&1) found)" >&2; exit 1; }

  mkdir -p "$share" "$bin_dir"
  tmp="$share/jav3.download"
  curl -fsSL "$BASE/cli/jav3" -o "$tmp"
  python3 -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' "$tmp" \
    || { echo "downloaded CLI is not valid Python; not installing" >&2; rm -f "$tmp"; exit 1; }
  mv "$tmp" "$share/jav3"

  py="$(command -v python3)"
  if ! python3 -c 'import httpx' >/dev/null 2>&1; then
    if [ ! -x "$share/venv/bin/python" ]; then
      python3 -m venv "$share/venv" || {
        echo "could not create a venv — install python3-venv, or httpx via your" >&2
        echo "package manager (python3-httpx), then re-run this installer" >&2
        exit 1; }
    fi
    "$share/venv/bin/python" -m pip install --quiet --disable-pip-version-check "$HTTPX_SPEC"
    py="$share/venv/bin/python"
  fi

  cat > "$bin_dir/jav3" <<EOF
#!/bin/sh
exec "$py" "$share/jav3" "\$@"
EOF
  chmod 755 "$bin_dir/jav3"

  echo "installed: $bin_dir/jav3"
  case ":$PATH:" in *":$bin_dir:"*) ;; *) echo "note: $bin_dir is not on your PATH" ;; esac
  echo "next: jav3 login   (paste the line from Settings → Add computer)"
}

{ main "$@"; exit; }
