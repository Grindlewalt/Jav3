#!/usr/bin/env bash
# Build the versioned golden guest image for Jav3's sandbox VM.
#
# Flow (proven on this Pi in the pre-prune sandbox layer, trimmed for vsock):
#   download Debian 13 genericcloud arm64 cloud image -> verify SHA512
#   -> cloud-init seed -> boot once (SLIRP net) to provision -> poweroff
#   -> freeze as read-only base-v<N>.qcow2.
#
# The guest gets NO SSH server and NO runtime network: its only path off-box is
# an AF_VSOCK channel to the host gateway. cloud-init bakes the Phase-2 self-test
# stub (guest_agent.py) + a boot unit that runs it. Rebuild bumps VERSION; the
# script refuses to clobber an existing base image.
set -euo pipefail

VERSION="${JARVIS_VM_IMAGE_VERSION:-v1}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Callers pass VM_DIR; by hand, ask the app where the images live (the state
# dir, or the old in-checkout layout on a not-yet-migrated box).
if [ -z "${VM_DIR:-}" ]; then
  VM_DIR="$(cd "$SCRIPT_DIR/.." && .venv/bin/python -m backend.cli paths vm_dir 2>/dev/null)" \
    || VM_DIR="${JARVIS_STATE_DIR:-$HOME/.local/share/jarvis}/data/vm"
fi

# Before a 350 MB download: the provisioning boot is -accel kvm, so without a
# usable /dev/kvm this can only fail, ten minutes later.
if [ ! -r /dev/kvm ] || [ ! -w /dev/kvm ]; then
  if [ -e /dev/kvm ]; then
    echo "error: /dev/kvm exists but $(id -un) cannot open it — sudo usermod -aG kvm $(id -un), then log in again" >&2
  else
    echo "error: no /dev/kvm — CPU virtualization is off in BIOS or the kvm module is not loaded." >&2
    echo "       bash $SCRIPT_DIR/../scripts/install.sh --check  says which, and the fix." >&2
  fi
  exit 1
fi

# Arch, firmware paths and the qemu binary are resolved per host — this used to
# be hardcoded aarch64/AAVMF, which is why the image could only ever be built on
# the Pi. See vm/platform.sh.
# shellcheck source=vm/platform.sh
. "$SCRIPT_DIR/platform.sh"
jarvis_platform_detect || exit 1

CHECKSUMS_URL="https://cloud.debian.org/images/cloud/trixie/latest/SHA512SUMS"
IMAGE_NAME="debian-13-genericcloud-${DEB_ARCH}.qcow2"
IMAGE_URL="https://cloud.debian.org/images/cloud/trixie/latest/${IMAGE_NAME}"
DISK_SIZE="8G"
BASE="base-${VERSION}.qcow2"

echo "== host: $VM_ARCH ($DEB_ARCH guest), $QEMU_BIN, firmware $FW_CODE =="

mkdir -p "$VM_DIR"
cd "$VM_DIR"
[[ -f "$BASE" ]] && { echo "$BASE already exists — delete it first to rebuild" >&2; exit 1; }

echo "== [1/5] fetch + verify Debian genericcloud $DEB_ARCH =="
if [[ ! -f pristine.qcow2 ]]; then
  curl -fL --retry 3 -o pristine.qcow2.part "$IMAGE_URL"
  curl -fL --retry 3 -o SHA512SUMS "$CHECKSUMS_URL"
  want=$(grep "${IMAGE_NAME}\$" SHA512SUMS | awk '{print $1}' | head -1)
  got=$(sha512sum pristine.qcow2.part | awk '{print $1}')
  [[ -n "$want" && "$want" == "$got" ]] || { echo "checksum mismatch (want=$want got=$got)" >&2; exit 1; }
  mv pristine.qcow2.part pristine.qcow2
fi

echo "== [2/5] cloud-init seed (bakes the guest bootstrap + boot unit, no SSH/network) =="
bootstrap_b64=$(base64 -w0 "$SCRIPT_DIR/guest/bootstrap.py")
# The base's apt list IS vm/images/main.recipe (one source of truth: the image
# variants in backend/vm/images.py layer on top of exactly this set). Tokens
# are checked against the Debian name[=version] grammar before they reach YAML.
RECIPE="$SCRIPT_DIR/images/main.recipe"
[[ -f "$RECIPE" ]] || { echo "missing $RECIPE" >&2; exit 1; }
pkg_yaml=""
for tok in $(sed -e 's/#.*//' "$RECIPE" | awk '$1 == "apt" { for (i = 2; i <= NF; i++) print $i }'); do
  [[ "$tok" =~ ^[a-z0-9][a-z0-9+.-]{1,62}(=[0-9A-Za-z.+~:-]{1,64})?$ ]] \
    || { echo "bad apt package in $RECIPE: $tok" >&2; exit 1; }
  pkg_yaml+="  - ${tok}"$'\n'
done
[[ -n "$pkg_yaml" ]] || { echo "no apt packages in $RECIPE" >&2; exit 1; }
# baseline.json (what a clean box of this image looks like: packages, enabled
# units, setuid files, processes, listeners) for WP4's unexpected-process rule.
# The guest prints each section base64'd on the serial console; step [5/5]
# assembles the JSON on the host. The script deletes itself after the run.
baseline_b64=$(base64 -w0 <<'SH'
#!/bin/sh
emit() { printf 'JAV3-BASELINE %s %s\n' "$1" "$(base64 -w0)" > /dev/console; }
dpkg-query -W -f '${Package}\t${Version}\n' | emit dpkg
systemctl list-unit-files --state=enabled --no-legend 2>/dev/null | awk '{print $1}' | emit units_enabled
# every .service the distribution ships (timer-started ones like apt-listchanges,
# fstrim and man-db are never "enabled"): processes in those units are the OS
find /usr/lib/systemd/system /lib/systemd/system -maxdepth 1 \( -type f -o -type l \) \
  -name '*.service' -printf '%f\n' 2>/dev/null | sort -u | emit units_vendor
find / -xdev -perm -4000 -type f 2>/dev/null | emit setuid
ps -eo comm= | sort -u | emit processes
{ ss -ltnupH 2>/dev/null || true; } | emit listening
printf 'JAV3-BASELINE-END\n' > /dev/console
SH
)
cat > meta-data <<EOF
instance-id: jarvis-guest-golden
local-hostname: jarvis-guest
EOF
cat > user-data <<EOF
#cloud-config
hostname: jarvis-guest
# no network at runtime -> don't let boot wait on a NIC. The provision boot
# DOES have SLIRP net, so dev tooling is baked here — runtime installs into
# the overlay are wiped by the idle scrub, so anything needed every run
# belongs in this list.
package_update: true
package_upgrade: false
# Curated dev toolchain: build-essential/python3-dev/pkg-config so pip and npm
# native modules compile; jq/ripgrep/sqlite3/zip tools because agents reach for
# them constantly. Deliberately absent: openssh-client, socat, netcat, nmap,
# tcpdump — the only sanctioned path off-box is vsock + the monitored egress
# proxy, and those exist to find other paths. The list lives in
# vm/images/main.recipe (read above).
packages:
${pkg_yaml}write_files:
  - path: /usr/local/sbin/jav3-baseline
    encoding: b64
    permissions: '0755'
    content: ${baseline_b64}
  - path: /opt/jarvis/bootstrap.py
    encoding: b64
    permissions: '0755'
    content: ${bootstrap_b64}
  - path: /etc/modules-load.d/vsock.conf
    content: |
      vmw_vsock_virtio_transport
  - path: /etc/jarvis-image-version
    content: |
      ${VERSION}
  - path: /etc/systemd/system/jarvis-guest.service
    content: |
      [Unit]
      Description=Jav3 guest runtime bootstrap (fetch package over vsock, run loop)
      After=multi-user.target
      [Service]
      Type=simple
      ExecStart=/usr/bin/python3 /opt/jarvis/bootstrap.py
      Restart=no
      StandardOutput=journal+console
      StandardError=journal+console
      [Install]
      WantedBy=multi-user.target
  # Finishes the removal below AFTER cloud-init has exited: started (never
  # enabled) from runcmd, ordered Before=cloud-final so systemd stops it --
  # running ExecStop -- only once cloud-final is down, at the provisioning
  # poweroff. Purging cloud-init while it runs killed the build before; leaving
  # it with unmet Depends made every later `apt-get install` exit 100 (e2e
  # BUG-9). It prints JAV3-DPKG-OK / JAV3-DPKG-BROKEN for step [4/5].
  - path: /etc/systemd/system/jav3-finish-purge.service
    content: |
      [Unit]
      Description=Jav3: purge cloud-init at the provisioning poweroff
      Before=cloud-final.service
      [Service]
      Type=oneshot
      RemainAfterExit=yes
      TimeoutStopSec=300
      ExecStart=/bin/true
      ExecStop=/bin/sh -c 'dpkg --purge --force-depends cloud-init >/dev/console 2>&1; if apt-get check >/dev/console 2>&1; then echo JAV3-DPKG-OK >/dev/console; else echo JAV3-DPKG-BROKEN >/dev/console; fi; rm -f /etc/systemd/system/jav3-finish-purge.service'
runcmd:
  - systemctl disable systemd-networkd-wait-online.service || true
  - systemctl mask systemd-networkd-wait-online.service || true
  # The genericcloud base ships network tooling we don't want in an
  # assumed-compromised guest — including a full sshd that trixie's
  # systemd-ssh-generator will happily bind to AF_VSOCK, our control channel.
  # dpkg --force-depends, NOT apt: cloud-init hard-depends on ssh-import-id ->
  # openssh-client, so an apt purge removes cloud-init out from under this
  # very provisioning run (it died pre-poweroff and the build hung). dpkg
  # leaves cloud-init installed with an unmet dep record -- which apt DOES
  # read (every later install exits 100), so jav3-finish-purge removes
  # cloud-init itself at the poweroff, once it has finished running.
  - dpkg --purge --force-depends openssh-server openssh-sftp-server openssh-client ssh-import-id socat tcpdump netcat-openbsd || true
  - systemctl daemon-reload
  - systemctl start --no-block jav3-finish-purge.service
  # a netless disposable guest has nothing to auto-upgrade from
  - systemctl disable unattended-upgrades.service apt-daily.timer apt-daily-upgrade.timer || true
  - systemctl enable jarvis-guest.service
  - /usr/local/sbin/jav3-baseline || true
  - rm -f /usr/local/sbin/jav3-baseline
  - touch /etc/jarvis-provisioned
power_state:
  mode: poweroff
  message: provisioning complete
EOF
jarvis_make_seed seed.iso

echo "== [3/5] provision boot (KVM, SLIRP net for cloud-init only) =="
cp pristine.qcow2 base-work.qcow2
qemu-img resize base-work.qcow2 "$DISK_SIZE"
cp "$FW_VARS" efi_vars_build.fd
# 1800s ASSUMES HARDWARE ACCELERATION. A provision boot takes ~9 min under
# MTTCG and a couple of minutes under KVM, so this is generous for either — but
# on an unaccelerated or heavily loaded host it can fire mid-provision, and the
# [4/5] check below then reports "provisioning may have failed", which reads as
# a broken image rather than a stopwatch. If you are diagnosing that message,
# check the tail of provision-console.log for a poweroff before assuming a bug.
timeout 1800 "$QEMU_BIN" \
  "${QEMU_MACHINE[@]}" \
  -smp 2 -m 1024 \
  -drive if=pflash,format=raw,readonly=on,file="$FW_CODE" \
  -drive if=pflash,format=raw,file=efi_vars_build.fd \
  -drive file=base-work.qcow2,if=virtio,format=qcow2 \
  -drive file=seed.iso,if=virtio,format=raw,readonly=on \
  -netdev user,id=n0 -device virtio-net-pci,netdev=n0 \
  -display none -serial file:provision-console.log

echo "== [4/5] verify provisioning =="
grep -q 'provisioning complete\|jarvis-provisioned\|reached target.*Power-Off\|Power down' provision-console.log \
  || { echo "provisioning may have failed — see $VM_DIR/provision-console.log" >&2; exit 1; }
# not fatal: the image builder repairs a leftover cloud-init record itself
# (backend/vm/builder_guest.repair_dpkg), but say so
grep -q 'JAV3-DPKG-OK' provision-console.log \
  || echo "WARNING: cloud-init purge not confirmed (no JAV3-DPKG-OK in provision-console.log); apt layers repair it at build time" >&2

echo "== [5/5] freeze read-only golden image =="
mv base-work.qcow2 "$BASE"
chmod 444 "$BASE"
cp "$FW_VARS" efi_vars.fd
# Record the arch this image was built for. data/ gets rsynced between hosts
# during a migration, and an arm64 image on an x86 host boots to nothing at all
# with the guest's console going to a log nobody reads. run_vm.sh checks this.
echo "$VM_ARCH" > "base-${VERSION}.arch"
# baseline.json from the console sections the guest printed (see jav3-baseline
# above). A missing baseline is a warning, not a failed build: WP4 then has no
# expected-process list for this base and says so.
python3 - provision-console.log "base-${VERSION}.baseline.json" "$BASE" <<'PY' \
  || echo "WARNING: no baseline.json for $BASE" >&2
# --- baseline-parse (tests/test_images.py runs this block) ---
import base64, datetime, json, sys
src, dst, image = sys.argv[1], sys.argv[2], sys.argv[3]
out = {"v": 1, "image": image, "captured_at":
       datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
ended = False
for raw in open(src, "rb").read().decode("utf-8", "replace").splitlines():
    line = raw.strip("\r\n\x00 ")
    i = line.find("JAV3-BASELINE")
    if i < 0:
        continue
    line = line[i:]
    if line.startswith("JAV3-BASELINE-END"):
        ended = True
        continue
    parts = line.split(" ", 2)
    if len(parts) != 3 or parts[1] not in ("dpkg", "units_enabled", "units_vendor", "setuid",
                                           "processes", "listening"):
        continue
    try:
        text = base64.b64decode(parts[2].strip(), validate=True).decode("utf-8", "replace")
    except ValueError:
        continue
    rows = [r for r in text.splitlines() if r.strip()]
    if parts[1] == "dpkg":
        out["dpkg"] = dict(r.split("\t", 1) for r in rows if "\t" in r)
    else:
        out[parts[1]] = rows
if not ended or "dpkg" not in out:
    sys.exit("baseline sections missing from the console log")
open(dst, "w").write(json.dumps(out, indent=1, sort_keys=True))
print(f"baseline: {len(out['dpkg'])} packages -> {dst}")
# --- end baseline-parse ---
PY
rm -f seed.iso user-data meta-data efi_vars_build.fd
echo "built $VM_DIR/$BASE (version $VERSION, $VM_ARCH)"
