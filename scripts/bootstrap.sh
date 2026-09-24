#!/bin/sh
# Server bootstrap: fetch the code, then hand over to scripts/install.sh.
#
#   curl -fsSL <raw-url-of-this-file> | sh
#   curl -fsSL <raw-url-of-this-file> | sh -s -- --no-image --yes
#
# Any arguments are passed through to install.sh unchanged. Environment:
#   JARVIS_REPO   git URL to clone      (default: the GitHub origin below)
#   JARVIS_REF    branch or tag         (default: the repo's default branch)
#   JARVIS_DIR    where to put it       (default: ~/jarvis)
#
# Run it as the user the server will run as, not root: the checkout, the venv
# and the systemd --user unit all belong to that user. The few steps that need
# root stay in install.sh's --root-phase, and install.sh prints that one sudo
# command when it is still owed. If git itself is missing this script installs
# it (as root, or through sudo, which asks for a password on the terminal).
#
# Everything is inside main(), and the call is wrapped in `{ ...; }`: a
# download cut short anywhere — including part-way through that last line,
# where a bare `main` would still have run with its arguments dropped — is a
# syntax error that runs nothing (tests/sr5/bootstrap_truncation.sh).
set -eu

main() {
  repo="${JARVIS_REPO:-https://github.com/Grindlewalt/Jav3.git}"
  ref="${JARVIS_REF:-}"
  dir="${JARVIS_DIR:-$HOME/jarvis}"

  say() { printf '== %s\n' "$*"; }
  die() { printf 'error: %s\n' "$*" >&2; exit 1; }

  if [ "$(id -u)" -eq 0 ] && [ -z "${JARVIS_ALLOW_ROOT:-}" ]; then
    die "run this as the user the server should run as, not root.
       (install.sh prints the one sudo command it needs.)
       Set JARVIS_ALLOW_ROOT=1 to install for root anyway."
  fi

  command -v bash >/dev/null 2>&1 || die "install.sh needs bash; install it first"

  if ! command -v git >/dev/null 2>&1; then
    say "installing git"
    as_root=""
    if [ "$(id -u)" -ne 0 ]; then
      command -v sudo >/dev/null 2>&1 \
        || die "git is missing and there is no sudo — install git as root, then re-run"
      as_root="sudo"
    fi
    if   command -v apt-get >/dev/null 2>&1; then $as_root apt-get update -q && $as_root apt-get install -y git
    elif command -v pacman  >/dev/null 2>&1; then $as_root pacman -Sy --needed --noconfirm git
    elif command -v dnf     >/dev/null 2>&1; then $as_root dnf install -y git
    else die "no apt-get, pacman or dnf here — install git yourself, then re-run"
    fi
  fi

  if [ -d "$dir/.git" ]; then
    say "updating $dir"
    git -C "$dir" pull --ff-only -q || die "could not fast-forward $dir — it has local changes; sort them out, then re-run"
  elif [ -e "$dir" ] && [ -n "$(ls -A "$dir" 2>/dev/null)" ]; then
    die "$dir exists and is not a checkout — move it aside or set JARVIS_DIR"
  else
    say "cloning $repo into $dir"
    if [ -n "$ref" ]; then
      git clone -q --branch "$ref" "$repo" "$dir"
    else
      git clone -q "$repo" "$dir"
    fi
  fi

  say "handing over to $dir/scripts/install.sh"
  # stdin here is this script arriving through the pipe, so install.sh's
  # questions would read the pipe instead of the person. Give it the terminal;
  # with no terminal at all, answers come from --yes or not at all.
  if [ -r /dev/tty ] && (: </dev/tty) 2>/dev/null; then
    exec bash "$dir/scripts/install.sh" "$@" </dev/tty
  fi
  exec bash "$dir/scripts/install.sh" "$@" </dev/null
}

{ main "$@"; exit; }
