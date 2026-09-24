#!/usr/bin/env bash
# Jarvis v3 — one-shot installer.
#
#   bash scripts/install.sh --check              # report only, change nothing
#   sudo bash scripts/install.sh --root-phase    # the part that needs root
#   bash scripts/install.sh                      # everything else
#
# Re-runnable from any state: every step checks before it acts, so running this
# after a git pull is also the upgrade path.
#
# WHY IT IS SPLIT IN TWO
# The old setup_pi.sh assumed passwordless sudo and `apt`. On the main server we
# have neither — no sudo at all, and pacman — so a single privileged script
# could not even start, and the failure came out as a wall of apt errors that
# said nothing about the real problem. So: the handful of things that genuinely
# need root are collected into ONE phase the operator can paste as ONE command,
# and everything else runs unprivileged. `--check` tells you which of the two
# you still owe, without touching the machine.
#
# WHAT NEEDS ROOT, AND WHY (the list is deliberately short)
#   packages          qemu, node, python venv module, rsync, rclone (backups)
#   kvm modules       the agent loop runs inside a KVM guest; no KVM, no Jarvis
#   vhost_vsock       the guest's only channel to the host supervisor
#   kvm group         so rootless qemu can open /dev/kvm
#   enable-linger     so the systemd --user service survives logout/reboot
#
# ONE THING NO SCRIPT CAN DO: if the CPU's virtualization extension is switched
# off in firmware, KVM cannot load and this installer will tell you so and stop.
# That is a reboot into BIOS, by a human, at the machine.
set -euo pipefail

# When piped over ssh (`--target`) there is no file and no checkout, and the
# naive dirname/.. lands on some unrelated directory. An empty REPO_DIR means
# "no checkout here" and the checkout-dependent checks skip themselves.
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
else
  REPO_DIR=""
fi

# ---------------------------------------------------------------- options ----
DO_CHECK=0 DO_ROOT=0 DO_USER=1 BUILD_FRONTEND=1 BUILD_IMAGE=1
FROM_HOST="" FORCE=0 ASSUME_YES=0 TARGET_HOST="" STATE_DIR_OPT=""
# $SUDO_USER is only meaningful when we are actually running under sudo. Taking
# it unconditionally means a stale value inherited from the environment wins
# over who we really are — which reported the wrong username inside a sandbox.
if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ]; then
  TARGET_USER="$SUDO_USER"
else
  TARGET_USER="$(id -un)"
fi

usage() {
  sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  cat <<'EOF'

Options
  --check              preflight only: report what is missing, change nothing
  --target <host>      run the preflight on a REMOTE host over ssh and report
                       what it is missing, changing nothing there. Needs no
                       checkout on the far side. e.g. --target claude@main
  --root-phase         run ONLY the privileged steps (run this under sudo)
  --user <name>        target user for the root phase (default: $SUDO_USER)
  --from <host:path>   migrate durable state from an existing install first,
                       e.g. --from grindlewalt@atomostest:jarvis. <path> may be
                       the old checkout (state inside it) or a state dir.
  --state-dir <dir>    where durable state lives (default ~/.local/share/jarvis,
                       or JARVIS_STATE_DIR); a non-default one is written to
                       ~/.config/jarvis/env so the service uses it too
  --no-build           skip the frontend build
  --no-image           skip building the guest golden image (slow, ~10 min)
  --force              overwrite existing local state during --from
  --yes                do not prompt
  -h, --help           this text
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --check)      DO_CHECK=1; DO_USER=0 ;;
    --target)     TARGET_HOST="$2"; DO_CHECK=1; DO_USER=0; shift ;;
    --root-phase) DO_ROOT=1; DO_USER=0 ;;
    --user)       TARGET_USER="$2"; shift ;;
    --from)       FROM_HOST="$2"; shift ;;
    --state-dir)  STATE_DIR_OPT="$2"; shift ;;
    --no-build)   BUILD_FRONTEND=0 ;;
    --no-image)   BUILD_IMAGE=0 ;;
    --force)      FORCE=1 ;;
    --yes|-y)     ASSUME_YES=1 ;;
    -h|--help)    usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 64 ;;
  esac
  shift
done

# ------------------------------------------------------------------ output ---
BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; OFF=$'\033[0m'
[ -t 1 ] || { BOLD=""; RED=""; GREEN=""; YELLOW=""; OFF=""; }

step() { printf '\n%s== %s%s\n' "$BOLD" "$*" "$OFF"; }
ok()   { printf '  %sok%s    %s\n' "$GREEN" "$OFF" "$*"; }
warn() { printf '  %swarn%s  %s\n' "$YELLOW" "$OFF" "$*"; }
bad()  { printf '  %sMISS%s  %s\n' "$RED" "$OFF" "$*"; }
# The fix for a failed check belongs next to that check, not in a summary at the
# bottom. A named failure with its own command is a 10-second job; the same
# failure discovered three screens from its remedy is a debugging session.
fix()  { printf '        fix: %s\n' "$*"; }
die()  { printf '\n%serror:%s %s\n' "$RED" "$OFF" "$*" >&2; exit 1; }

# ------------------------------------------------------------------ detect ---
ARCH="$(uname -m)"
# Package names differ per arch as well as per distro. On Arch specifically,
# `qemu-base` is a headless metapackage that already depends on qemu-system-x86,
# qemu-img and edk2-ovmf — so it is the right minimal choice on x86_64, and the
# wrong one on aarch64, where it would pull the x86 emulator and no ARM firmware.
# (`qemu-desktop` and `qemu-full` also work but drag ~40 GUI/audio packages onto
# what is a headless server.)
case "$ARCH" in
  aarch64|arm64)
    ARCH=aarch64
    QEMU_ARCH_PKG_APT=qemu-system-arm; FW_PKG_APT=qemu-efi-aarch64
    QEMU_PKG_PAC=qemu-system-aarch64;  FW_PKG_PAC=edk2-aarch64 ;;
  x86_64|amd64)
    ARCH=x86_64
    QEMU_ARCH_PKG_APT=qemu-system-x86; FW_PKG_APT=ovmf
    QEMU_PKG_PAC=qemu-base;            FW_PKG_PAC=edk2-ovmf ;;
  *) die "unsupported architecture $ARCH (need aarch64 or x86_64 — the guest runs under KVM, not emulation)" ;;
esac

if   command -v apt-get >/dev/null 2>&1; then PKG=apt
elif command -v pacman  >/dev/null 2>&1; then PKG=pacman
elif command -v dnf     >/dev/null 2>&1; then PKG=dnf
else PKG=unknown
fi

DISTRO="$( . /etc/os-release 2>/dev/null && echo "${PRETTY_NAME:-unknown}" )"

case "$PKG" in
  apt)    PACKAGES=(python3-venv python3-pip nodejs npm git curl rsync rclone
                    "$QEMU_ARCH_PKG_APT" qemu-utils "$FW_PKG_APT" cloud-image-utils) ;;
  pacman) PACKAGES=(python nodejs npm git curl rsync rclone "$QEMU_PKG_PAC" qemu-img
                    "$FW_PKG_PAC" libisoburn) ;;
  dnf)    PACKAGES=(python3 python3-pip nodejs npm git curl rsync rclone
                    qemu-kvm qemu-img edk2-ovmf xorriso) ;;
  *)      PACKAGES=() ;;
esac

have() { command -v "$1" >/dev/null 2>&1; }

# Durable state (memory/ projects/ skills/ agents/ data/) lives in ONE dir
# outside the checkout — backend/config.py's state_dir. Same precedence as the
# app: --state-dir, then JARVIS_STATE_DIR from the environment or the env file,
# then the default.
STATE_DIR="$STATE_DIR_OPT"
[ -n "$STATE_DIR" ] || STATE_DIR="${JARVIS_STATE_DIR:-}"
if [ -z "$STATE_DIR" ] && [ -f "$HOME/.config/jarvis/env" ]; then
  STATE_DIR="$(sed -n 's/^JARVIS_STATE_DIR=//p' "$HOME/.config/jarvis/env" | tail -1 | tr -d "\"'")"
fi
STATE_DIR="${STATE_DIR:-$HOME/.local/share/jarvis}"
STATE_DIR="${STATE_DIR/#\~/$HOME}"

# Where the guest images resolve, exactly as the app resolves them (an existing
# box may still run from the old in-checkout layout until migrate-state).
vm_dir() {
  if [ -n "$REPO_DIR" ] && [ -x "$REPO_DIR/.venv/bin/python" ]; then
    (cd "$REPO_DIR" && .venv/bin/python -m backend.cli paths vm_dir 2>/dev/null) && return
  fi
  echo "$STATE_DIR/data/vm"
}

# ---------------------------------------------------------------- preflight --
# Each check appends to MISSING_ROOT (needs the root phase) or MISSING_USER.
MISSING_ROOT=() MISSING_USER=() BLOCKED=()

check_cpu_virt() {
  # A working /dev/kvm settles the question on every architecture — if the
  # kernel published the device, virtualization is on. Check that FIRST, before
  # any flag guessing.
  if [ -e /dev/kvm ]; then
    ok "virtualization enabled (/dev/kvm exists)"
    return 0
  fi

  # Device absent does NOT imply module absent. Inside a container, a sandbox,
  # or any minimal /dev, the module can be loaded on the host while the node is
  # simply not present in this namespace — and telling someone to modprobe a
  # module they already have loaded sends them to fix the wrong machine.
  # Same family of mistake as inferring firmware state from a cpuinfo flag.
  if kvm_module_loaded; then
    bad "/dev/kvm absent, but the kvm module IS loaded"
    fix "this is a namespaced or minimal /dev, not a module problem — bind the"
    fix "  node in (e.g. --dev-bind /dev/kvm /dev/kvm) rather than modprobing"
    MISSING_ROOT+=("kvm-device")
    return 0
  fi

  # No /dev/kvm. Why depends entirely on the architecture, and getting this
  # wrong is worse than not checking: `vmx`/`svm` are x86-only CPUID flags and
  # aarch64 does not advertise virtualization in /proc/cpuinfo at all, so the
  # naive grep reported "DISABLED IN FIRMWARE" on a working Raspberry Pi and
  # told the operator to go find a BIOS that does not exist.
  if [ "$ARCH" = x86_64 ]; then
    if grep -qwE 'vmx|svm' /proc/cpuinfo; then
      # Flag present but no device: the module simply is not loaded.
      warn "virtualization supported but /dev/kvm absent — the kvm module is not loaded"
    else
      # On AMD the sub-feature bits (svm_lock, npt, nrip_save…) still enumerate
      # when the base SVM bit is masked, which is exactly what a BIOS-disabled
      # machine looks like. This is the one failure no amount of sudo fixes.
      bad "CPU virtualization is DISABLED IN FIRMWARE (no vmx/svm in /proc/cpuinfo)"
      BLOCKED+=("Reboot into BIOS/UEFI and enable virtualization.")
      BLOCKED+=("  Intel: VT-x.  AMD: 'SVM Mode', usually under Advanced > CPU Configuration.")
      BLOCKED+=("  Without it the kvm module cannot load and the agent loop has nowhere")
      BLOCKED+=("  to run — the host-side fallback loop was removed in M4e (2026-08-02).")
    fi
  else
    # aarch64: no cpuinfo flag exists to test. KVM needs the CPU to have booted
    # at EL2 and the kernel to have KVM support; both show up as the device
    # appearing, so there is nothing else to probe. Say what is true and do NOT
    # send anyone to a BIOS — a Pi has no such screen.
    bad "no /dev/kvm on $ARCH"
    fix "check the kernel has KVM support and the CPU booted at EL2: dmesg | grep -i kvm"
  fi
}

kvm_module_loaded() {
  grep -qE '^(kvm|kvm_amd|kvm_intel) ' /proc/modules 2>/dev/null
}

check_kvm() {
  if [ ! -e /dev/kvm ]; then
    # check_cpu_virt already diagnosed why and printed the right remedy for it;
    # repeating a second, differently-worded guess here only added noise.
    return 0
  fi
  if [ -r /dev/kvm ] && [ -w /dev/kvm ]; then
    ok "/dev/kvm readable and writable"
  else
    bad "/dev/kvm exists but $TARGET_USER cannot open it"
    fix "sudo usermod -aG kvm $TARGET_USER   (then log out and back in)"
    fix "  or grant it by udev rule if you would rather not use the group"
    MISSING_ROOT+=("kvm-group")
  fi
}

# What actually matters is whether the device OPENS, not how that was arranged.
# A udev rule granting 0666 makes group membership irrelevant, so demanding the
# group anyway reports a correctly-configured host as broken — the same mistake
# as testing a proxy for the thing instead of the thing.
check_kvm_group() {
  if [ -r /dev/kvm ] && [ -w /dev/kvm ]; then
    ok "kvm device is openable (group membership not required)"
  elif [ -e /dev/kvm ]; then
    : # unopenable device already reported by check_kvm, with the same fix
  elif id -nG "$TARGET_USER" 2>/dev/null | tr ' ' '\n' | grep -qx kvm; then
    ok "$TARGET_USER is in the kvm group"
  else
    # No device yet, so openability cannot be tested. Flag it as a likely need
    # rather than a definite failure.
    warn "$TARGET_USER is not in the kvm group — likely needed once /dev/kvm exists"
    fix "sudo usermod -aG kvm $TARGET_USER"
  fi
}

check_vsock() {
  if [ -e /dev/vhost-vsock ]; then
    ok "/dev/vhost-vsock present"
  else
    bad "/dev/vhost-vsock missing (vhost_vsock module not loaded)"
    fix "sudo modprobe vhost_vsock && echo vhost_vsock | sudo tee /etc/modules-load.d/vhost_vsock.conf"
    MISSING_ROOT+=("vhost-vsock")
  fi
}

check_packages() {
  local missing=()
  have "qemu-system-$ARCH" || missing+=("qemu-system-$ARCH")
  have qemu-img            || missing+=("qemu-img")
  have node                || missing+=("node")
  have npm                 || missing+=("npm")
  have git                 || missing+=("git")
  have rsync               || missing+=("rsync")
  python3 -c 'import venv' >/dev/null 2>&1 || missing+=("python3 venv module")
  # any one of these can write the cloud-init seed ISO
  if ! have cloud-localds && ! have xorriso && ! have genisoimage && ! have mkisofs; then
    missing+=("cloud-localds/xorriso (cloud-init seed)")
  fi
  # rclone only powers backups — worth a line, never a blocker
  have rclone || warn "rclone not installed — backups unavailable until it is (the root phase installs it)"
  if [ ${#missing[@]} -eq 0 ]; then
    ok "system packages present"
  else
    bad "missing packages: ${missing[*]}"
    case "$PKG" in
      apt)    fix "sudo apt-get install -y ${PACKAGES[*]}" ;;
      pacman) fix "sudo pacman -Sy --needed ${PACKAGES[*]}" ;;
      dnf)    fix "sudo dnf install -y ${PACKAGES[*]}" ;;
    esac
    MISSING_ROOT+=("packages")
  fi

  # UEFI firmware, via the same resolver the VM scripts use, so preflight and
  # runtime can never disagree about where the firmware is. In remote mode there
  # is no checkout to source it from, so fall back to a plain path probe.
  local found=1
  if [ -n "$REPO_DIR" ] && [ -r "$REPO_DIR/vm/platform.sh" ]; then
    ( . "$REPO_DIR/vm/platform.sh" && jarvis_platform_detect ) >/dev/null 2>&1 || found=0
  else
    found=0
    local f
    for f in /usr/share/AAVMF/AAVMF_CODE.fd /usr/share/edk2/aarch64/QEMU_EFI.fd \
             /usr/share/OVMF/OVMF_CODE_4M.fd /usr/share/OVMF/OVMF_CODE.fd \
             /usr/share/edk2/x64/OVMF_CODE.4m.fd /usr/share/edk2/x64/OVMF_CODE.fd \
             /usr/share/edk2/ovmf/OVMF_CODE.fd; do
      [ -r "$f" ] && { found=1; break; }
    done
  fi
  if [ "$found" = 1 ]; then
    ok "UEFI firmware found for $ARCH"
  else
    bad "no UEFI firmware for $ARCH"
    case "$PKG" in
      apt)    fix "sudo apt-get install -y $FW_PKG_APT" ;;
      pacman) fix "sudo pacman -Sy --needed $FW_PKG_PAC" ;;
      dnf)    fix "sudo dnf install -y edk2-ovmf" ;;
    esac
    MISSING_ROOT+=("packages")
  fi
}

check_node_version() {
  have node || return 0
  local major; major="$(node -v 2>/dev/null | sed 's/^v\([0-9]*\).*/\1/')"
  if [ -n "$major" ] && [ "$major" -lt 18 ] 2>/dev/null; then
    warn "node $(node -v) is older than 18 — the Vite build may fail"
  fi
}

check_linger() {
  local l; l="$(loginctl show-user "$TARGET_USER" -p Linger --value 2>/dev/null || echo no)"
  if [ "$l" = yes ]; then
    ok "systemd linger enabled for $TARGET_USER"
  else
    bad "linger disabled for $TARGET_USER (the service would die at logout)"
    fix "sudo loginctl enable-linger $TARGET_USER"
    MISSING_ROOT+=("linger")
  fi
}

# Everything that depends on there being a checkout. Skipped in remote mode,
# where we are asking "can this machine host Jarvis", not "is it installed".
check_user_side() {
  if [ -z "$REPO_DIR" ] || [ ! -d "$REPO_DIR/backend" ]; then
    warn "no Jarvis checkout here — skipping install-state checks"
    return 0
  fi
  if [ -x "$REPO_DIR/.venv/bin/python" ]; then ok "python venv built"
  else bad "no .venv"; fix "bash $REPO_DIR/scripts/install.sh"; MISSING_USER+=("venv"); fi

  if [ -f "$REPO_DIR/frontend/dist/index.html" ]; then ok "frontend built"
  else bad "frontend not built"; fix "cd $REPO_DIR/frontend && npm install && npm run build"; MISSING_USER+=("frontend"); fi

  if [ -f "$HOME/.config/jarvis/env" ]; then ok "config file present"
  else bad "no ~/.config/jarvis/env"; fix "mkdir -p ~/.config/jarvis && touch ~/.config/jarvis/env && chmod 600 ~/.config/jarvis/env"; MISSING_USER+=("config"); fi

  if [ -f "$HOME/.config/systemd/user/jarvis.service" ]; then ok "systemd user unit installed"
  else bad "jarvis.service not installed"; fix "bash $REPO_DIR/scripts/install.sh"; MISSING_USER+=("unit"); fi

  local vmd; vmd="$(vm_dir)"
  if ls "$vmd"/base-v*.qcow2 >/dev/null 2>&1; then ok "guest golden image present ($vmd)"
  else bad "no guest golden image"; fix "VM_DIR=$vmd bash $REPO_DIR/vm/build_base.sh"; MISSING_USER+=("image"); fi

  if [ -f "$REPO_DIR/data/jarvis.db" ] && [ ! -f "$STATE_DIR/data/jarvis.db" ]; then
    warn "state is still inside the checkout ($REPO_DIR) — it keeps working there"
    fix "systemctl --user stop jarvis && $REPO_DIR/.venv/bin/python -m backend.cli migrate-state && systemctl --user start jarvis"
  fi
}

preflight() {
  step "preflight — $DISTRO, $ARCH, package manager: $PKG, user: $TARGET_USER"
  check_cpu_virt
  check_kvm
  check_kvm_group
  check_vsock
  check_packages
  check_node_version
  check_linger
  check_user_side
}

# --------------------------------------------------------------- root phase --
root_phase() {
  [ "$(id -u)" -eq 0 ] || die "--root-phase must run as root (use sudo)"
  id "$TARGET_USER" >/dev/null 2>&1 || die "no such user: $TARGET_USER (pass --user)"

  step "packages ($PKG)"
  case "$PKG" in
    apt)    apt-get update -qq && apt-get install -y -qq "${PACKAGES[@]}" ;;
    pacman) pacman -Sy --needed --noconfirm "${PACKAGES[@]}" ;;
    dnf)    dnf install -y -q "${PACKAGES[@]}" ;;
    *)      warn "unknown package manager — install by hand: ${PACKAGES[*]}" ;;
  esac
  ok "packages installed"

  step "kvm + vsock kernel modules"
  local kvm_mod=""
  grep -qw vmx /proc/cpuinfo && kvm_mod=kvm_intel
  grep -qw svm /proc/cpuinfo && kvm_mod=kvm_amd
  if [ -z "$kvm_mod" ]; then
    warn "no vmx/svm flag — virtualization is off in firmware; skipping modprobe"
    warn "enable VT-x / SVM Mode in BIOS and re-run this phase"
  else
    modprobe "$kvm_mod" || warn "modprobe $kvm_mod failed"
    printf '%s\n' "$kvm_mod" > /etc/modules-load.d/kvm.conf
    ok "$kvm_mod loaded and persisted"
  fi
  modprobe vhost_vsock || warn "modprobe vhost_vsock failed"
  echo vhost_vsock > /etc/modules-load.d/vhost_vsock.conf
  ok "vhost_vsock loaded and persisted"

  step "group membership"
  # /dev/kvm and /dev/vhost-vsock are group kvm; the service user must be in it
  # or rootless qemu cannot open them.
  getent group kvm >/dev/null || groupadd -r kvm
  usermod -aG kvm "$TARGET_USER"
  ok "$TARGET_USER added to group kvm (needs a fresh login to take effect)"

  step "linger"
  loginctl enable-linger "$TARGET_USER"
  ok "linger enabled for $TARGET_USER (user service survives logout and reboot)"

  printf '\n%sroot phase done.%s Now run, as %s:\n\n    bash %s/scripts/install.sh\n\n' \
    "$BOLD" "$OFF" "$TARGET_USER" "$REPO_DIR"
}

# -------------------------------------------------------- state migration ----
# Runs BEFORE anything else touches local state. Jarvis's durable data is all
# outside git — memory/, projects/, skills/, agents/ and the SQLite DB — so on a
# migration it exists in exactly one place and a silent skip here loses it.
# Hence: unreachable source is a hard failure, never a warning.
#
# The source may be either layout: an old install keeps its state inside the
# checkout (<path>/data/jarvis.db), a new one in its state dir. Either way it
# lands in THIS box's $STATE_DIR. tools/ is not copied: tools are code and come
# with the checkout.
pull_state() {
  local spec="$1" host path root
  host="${spec%%:*}"
  path="${spec#*:}"
  [ "$host" != "$spec" ] || die "--from wants host:path (e.g. user@host:jarvis)"
  [ -n "$path" ] || path="jarvis"

  step "migrate durable state from $host:$path into $STATE_DIR"

  ssh -o ConnectTimeout=10 -o BatchMode=yes "$host" true 2>/dev/null \
    || die "cannot reach $host over ssh.
       Refusing to continue: this phase is the only copy of Jarvis's durable
       state (memory/, projects/, skills/, agents/, data/jarvis.db) and
       skipping it silently would start a fresh install over the top of a
       migration. Fix the source host, then re-run with the same --from."

  ssh "$host" "test -d '$path'" \
    || die "$host:$path does not exist or is not a directory"

  # Which layout? A DB directly under <path> means <path> is the state root
  # (an old checkout, or a state dir named outright). Otherwise ask the source
  # install where its state dir resolves.
  if ssh "$host" "test -f '$path/data/jarvis.db'"; then
    root="$path"
  else
    root="$(ssh "$host" "cd '$path' && ./.venv/bin/python -m backend.cli paths state_dir" 2>/dev/null || true)"
    [ -n "$root" ] || root='.local/share/jarvis'
    ssh "$host" "test -f '$root/data/jarvis.db'" \
      || die "no data/jarvis.db under $host:$path or its state dir ($root)"
  fi
  ok "source state root: $host:$root"

  # Refuse to clobber. On a genuine migration the target is empty; if it is not,
  # the operator gets to decide rather than discovering it afterwards.
  if [ "$FORCE" -ne 1 ]; then
    local d
    for d in memory projects skills agents; do
      if [ -n "$(ls -A "$STATE_DIR/$d" 2>/dev/null | grep -v '^\.gitkeep$' || true)" ]; then
        die "$STATE_DIR/$d is not empty — refusing to overwrite existing state.
       Re-run with --force if you really mean to replace it."
      fi
    done
    if [ -f "$STATE_DIR/data/jarvis.db" ]; then
      die "$STATE_DIR/data/jarvis.db already exists — refusing to overwrite.
       Re-run with --force if you really mean to replace it."
    fi
  fi

  # Snapshot the source DB through SQLite's backup API rather than copying the
  # file. A live database in WAL mode cannot be safely cp'd — you get a torn
  # read or a missing -wal and the copy opens corrupt. .backup is consistent
  # even against a running Jarvis. Plain python3: a state dir has no venv.
  step "  snapshotting the source database"
  # The snapshot holds every conversation and the users' bcrypt hashes: it is
  # created 0600 (umask 077) and removed on ANY exit from here on, success or
  # not — a failed migration must not leave a world-readable copy behind.
  local cleanup
  cleanup="rm -f '$root/data/jarvis.migrate.db'"
  # expanded now: the EXIT trap may fire after this function's locals are gone
  trap "ssh $(printf %q "$host") $(printf %q "$cleanup") >/dev/null 2>&1 || true" EXIT
  ssh "$host" "cd '$root' && umask 077 && python3 - <<'PY'
import sqlite3, os
src = 'data/jarvis.db'
dst = 'data/jarvis.migrate.db'
if os.path.exists(dst):
    os.remove(dst)
con = sqlite3.connect(src)
out = sqlite3.connect(dst)
os.chmod(dst, 0o600)
with out:
    con.backup(out)
out.close(); con.close()
print('snapshot ok', os.path.getsize(dst), 'bytes')
PY" || die "could not snapshot the source database"

  step "  copying files"
  mkdir -p "$STATE_DIR/data"
  # One rsync per directory: rsync only accepts a single remote host per
  # invocation, and the multi-source form silently does the wrong thing.
  #
  # data/vm is deliberately excluded: it is many GB of qcow2 AND it is built for
  # the source host's architecture, so copying it to a different box produces an
  # image that boots to nothing. install.sh rebuilds it natively instead.
  local d
  for d in memory projects skills agents; do
    if ssh "$host" "test -d '$root/$d'"; then
      rsync -a --info=stats1 \
        --exclude '__pycache__' --exclude '*.pyc' --exclude '.venv' \
        --exclude 'node_modules' --exclude 'dist' --exclude '.ephemeral-notes' \
        "$host:$root/$d/" "$STATE_DIR/$d/"
      ok "  $d"
    else
      warn "  $d not present on the source — skipped"
    fi
  done
  # Secret-bearing files land 0600 from the first byte (--chmod), not with
  # the source's mode until a chmod catches up.
  local private=(--chmod=D0700,F0600)
  rsync -a "${private[@]}" "$host:$root/data/jarvis.migrate.db" "$STATE_DIR/data/jarvis.db"
  # The JWT secret comes too, or every existing login token is invalidated.
  rsync -a "${private[@]}" "$host:$root/data/jwt_secret" "$STATE_DIR/data/jwt_secret" 2>/dev/null \
    || warn "no data/jwt_secret on the source (existing sessions will need a re-login)"
  mkdir -p "$HOME/.config/jarvis"
  chmod 700 "$HOME/.config/jarvis"
  rsync -a "${private[@]}" "$host:.config/jarvis/env" "$HOME/.config/jarvis/env" 2>/dev/null \
    || warn "no ~/.config/jarvis/env on the source — you will need to set the API key"
  rsync -a "${private[@]}" "$host:.config/jarvis/secrets.json" "$HOME/.config/jarvis/secrets.json" 2>/dev/null \
    || true
  # The source's env may pin ITS state dir; this box's is $STATE_DIR.
  if grep -q '^JARVIS_STATE_DIR=' "$HOME/.config/jarvis/env" 2>/dev/null; then
    sed -i "s#^JARVIS_STATE_DIR=.*#JARVIS_STATE_DIR=$STATE_DIR#" "$HOME/.config/jarvis/env"
  fi
  ssh "$host" "rm -f '$root/data/jarvis.migrate.db'" || true
  trap - EXIT

  step "  verifying the copy"
  python3 - "$STATE_DIR/data/jarvis.db" <<'PY'
import sqlite3, sys
db = sys.argv[1]
con = sqlite3.connect(db)
r = con.execute("PRAGMA integrity_check").fetchone()[0]
if r != "ok":
    raise SystemExit(f"integrity_check failed: {r}")
n = con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
print(f"  integrity ok, {n} conversations")
PY
  ok "state migrated"
}

# --------------------------------------------------------------- user phase --
user_phase() {
  [ "$(id -u)" -ne 0 ] || die "run the main phase as the service user, not root"
  cd "$REPO_DIR"

  step "python venv"
  [ -d .venv ] || python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
  ok "dependencies installed"

  if [ "$BUILD_FRONTEND" = 1 ]; then
    step "frontend build"
    if have npm; then
      # a lockfile means reproducible, integrity-checked installs: npm ci
      # installs exactly what it pins (and fails on drift) instead of
      # resolving fresh versions on every install
      if [ -f frontend/package-lock.json ]; then
        ( cd frontend && npm ci --no-fund --no-audit --silent && npm run build )
      else
        ( cd frontend && npm install --no-fund --no-audit --silent && npm run build )
      fi
      ok "frontend built to frontend/dist"
    else
      warn "npm not installed — skipping (the API will run, the GUI will 404)"
    fi
  fi

  step "config"
  mkdir -p "$HOME/.config/jarvis"
  touch "$HOME/.config/jarvis/env"
  chmod 600 "$HOME/.config/jarvis/env"
  # A non-default state dir must reach the service too, not just this script.
  if [ "$STATE_DIR" != "$HOME/.local/share/jarvis" ] \
     && ! grep -qxF "JARVIS_STATE_DIR=$STATE_DIR" "$HOME/.config/jarvis/env"; then
    sed -i '/^JARVIS_STATE_DIR=/d' "$HOME/.config/jarvis/env"
    echo "JARVIS_STATE_DIR=$STATE_DIR" >> "$HOME/.config/jarvis/env"
    ok "state dir $STATE_DIR recorded in ~/.config/jarvis/env"
  fi
  if grep -q 'JARVIS_DEEPSEEK_API_KEY' "$HOME/.config/jarvis/env" 2>/dev/null; then
    ok "API key present in ~/.config/jarvis/env"
  else
    warn "no JARVIS_DEEPSEEK_API_KEY in ~/.config/jarvis/env — add it before starting"
  fi

  step "systemd user units"
  mkdir -p "$HOME/.config/systemd/user"
  # The unit hardcodes %h/jarvis; if the checkout lives elsewhere, rewrite the
  # paths rather than silently installing a unit that points at nothing.
  sed "s#%h/jarvis#$REPO_DIR#g" scripts/jarvis.service \
    > "$HOME/.config/systemd/user/jarvis.service"
  sed "s#%h/jarvis#$REPO_DIR#g" scripts/jarvis-backup.service \
    > "$HOME/.config/systemd/user/jarvis-backup.service"
  cp scripts/jarvis-backup.timer   "$HOME/.config/systemd/user/" 2>/dev/null || true
  chmod +x scripts/backup.sh 2>/dev/null || true
  systemctl --user daemon-reload
  systemctl --user enable jarvis.service >/dev/null
  systemctl --user enable --now jarvis-backup.timer >/dev/null 2>&1 || true
  ok "jarvis.service installed and enabled"

  if [ "$BUILD_IMAGE" = 1 ]; then
    step "guest golden image"
    local vmd; vmd="$(vm_dir)"
    if ls "$vmd"/base-v*.qcow2 >/dev/null 2>&1; then
      ok "already built ($vmd)"
    elif [ ! -r /dev/kvm ]; then
      warn "no usable /dev/kvm — skipping the image build"
      warn "run the root phase (and enable virtualization in BIOS if needed), then:"
      warn "  VM_DIR=$vmd bash vm/build_base.sh"
    else
      echo "  building (downloads a Debian cloud image and boots it once; ~10 min)"
      VM_DIR="$vmd" bash vm/build_base.sh
      ok "golden image built"
    fi
  fi
}

# ------------------------------------------------------------------ verify ---
verify() {
  step "verify"
  systemctl --user restart jarvis || { warn "could not start jarvis.service"; return 1; }
  local i
  for i in $(seq 1 30); do
    if curl -sf localhost:8000/api/health >/dev/null 2>&1; then
      ok "health check passed — http://localhost:8000"
      return 0
    fi
    sleep 1
  done
  warn "no health response after 30s — check: journalctl --user -u jarvis -n 50"
  return 1
}

# -------------------------------------------------------------------- main ---

# Remote preflight: ship THIS script down the pipe and run its --check there.
# Nothing is installed and nothing is copied — it answers "could this machine
# host Jarvis, and what exactly is it missing" in one command, which is the
# question you want answered the moment you walk away from a BIOS screen.
if [ -n "$TARGET_HOST" ]; then
  [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ] \
    || die "--target needs to read this script from disk"
  printf '%s== remote preflight: %s%s\n' "$BOLD" "$TARGET_HOST" "$OFF"
  ssh -o ConnectTimeout=10 "$TARGET_HOST" 'bash -s -- --check' < "${BASH_SOURCE[0]}"
  rc=$?
  if [ $rc -eq 0 ]; then
    printf '\n%s%s is ready to host Jarvis.%s\n' "$GREEN" "$TARGET_HOST" "$OFF"
  else
    printf '\n%s%s is not ready — see the fix lines above.%s\n' "$YELLOW" "$TARGET_HOST" "$OFF"
  fi
  exit $rc
fi

if [ "$DO_ROOT" = 1 ]; then
  root_phase
  exit 0
fi

preflight

if [ ${#BLOCKED[@]} -gt 0 ]; then
  printf '\n%s%sBLOCKED — a human has to do this at the machine:%s\n' "$BOLD" "$RED" "$OFF"
  printf '  %s\n' "${BLOCKED[@]}"
fi

if [ ${#MISSING_ROOT[@]} -gt 0 ]; then
  # In remote-preflight mode there is no checkout on the far side yet, so name
  # the placeholder rather than printing `sudo bash /scripts/install.sh`.
  printf '\n%sNeeds one root window. Copy-paste this whole block:%s\n\n' "$BOLD" "$OFF"
  printf '    sudo bash %s/scripts/install.sh --root-phase --user %s\n\n' \
    "${REPO_DIR:-<path-to-jarvis-checkout>}" "$TARGET_USER"
  printf '  It installs: %s\n' "${PACKAGES[*]}"
  printf '  and: loads kvm + vhost_vsock (persisted), adds %s to the kvm group,\n' "$TARGET_USER"
  printf '  and enables systemd linger. Nothing else.\n'
  printf '\n  If you would rather run the individual commands yourself:\n\n'
  case "$PKG" in
    apt)    printf '    sudo apt-get update && sudo apt-get install -y %s\n' "${PACKAGES[*]}" ;;
    pacman) printf '    sudo pacman -Sy --needed %s\n' "${PACKAGES[*]}" ;;
    dnf)    printf '    sudo dnf install -y %s\n' "${PACKAGES[*]}" ;;
  esac
  if grep -qw vmx /proc/cpuinfo; then printf '    sudo modprobe kvm_intel && echo kvm_intel | sudo tee /etc/modules-load.d/kvm.conf\n'
  elif grep -qw svm /proc/cpuinfo; then printf '    sudo modprobe kvm_amd   && echo kvm_amd   | sudo tee /etc/modules-load.d/kvm.conf\n'
  else printf '    # (enable virtualization in BIOS first — see BLOCKED above)\n'; fi
  printf '    sudo modprobe vhost_vsock && echo vhost_vsock | sudo tee /etc/modules-load.d/vhost_vsock.conf\n'
  printf '    sudo usermod -aG kvm %s\n' "$TARGET_USER"
  printf '    sudo loginctl enable-linger %s\n' "$TARGET_USER"
fi

if [ "$DO_CHECK" = 1 ]; then
  if [ ${#MISSING_ROOT[@]} -eq 0 ] && [ ${#MISSING_USER[@]} -eq 0 ] && [ ${#BLOCKED[@]} -eq 0 ]; then
    printf '\n%sready.%s\n' "$GREEN" "$OFF"; exit 0
  fi
  exit 1
fi

# A missing root phase is not fatal for the user phase — the venv and the
# frontend build fine without KVM, and the image build skips itself with a
# clear message. Only stop if the operator has not seen the list yet.
if [ ${#MISSING_ROOT[@]} -gt 0 ] && [ "$ASSUME_YES" != 1 ]; then
  printf '\n'
  read -r -p "Root steps are outstanding. Continue with the unprivileged install anyway? [y/N] " reply
  case "$reply" in [yY]*) ;; *) echo "stopped."; exit 1 ;; esac
fi

[ -n "$FROM_HOST" ] && pull_state "$FROM_HOST"

user_phase

printf '\n%sinstalled.%s\n' "$BOLD" "$OFF"
printf '  state dir:            %s\n' "$(cd "$REPO_DIR" && .venv/bin/python -m backend.cli paths state_dir 2>/dev/null || echo "$STATE_DIR")"
printf '  create a login user:  %s/.venv/bin/python -m backend.cli create-user <name>   (prompts for the password)\n' "$REPO_DIR"
printf '  backups (rclone):     set a remote via /api/backup/config, or JARVIS_BACKUP_REMOTE in ~/.config/jarvis/env\n'
printf '  start:                systemctl --user restart jarvis\n'
printf '  logs:                 journalctl --user -u jarvis -f\n'
printf '  re-check:             bash %s/scripts/install.sh --check\n' "$REPO_DIR"

if [ ${#MISSING_ROOT[@]} -eq 0 ] && [ ${#BLOCKED[@]} -eq 0 ]; then
  verify || true
else
  printf '\n%snot starting:%s root steps above are still outstanding.\n' "$YELLOW" "$OFF"
fi
