#!/usr/bin/env bash
# Build the versioned golden guest image for Jarvis's sandbox VM.
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
VM_DIR="${VM_DIR:-$HOME/jarvis/data/vm}"
IMAGE_URL="https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-arm64.qcow2"
CHECKSUMS_URL="https://cloud.debian.org/images/cloud/trixie/latest/SHA512SUMS"
AAVMF_CODE="/usr/share/AAVMF/AAVMF_CODE.fd"
AAVMF_VARS="/usr/share/AAVMF/AAVMF_VARS.fd"
DISK_SIZE="8G"
BASE="base-${VERSION}.qcow2"

mkdir -p "$VM_DIR"
cd "$VM_DIR"
[[ -f "$BASE" ]] && { echo "$BASE already exists — delete it first to rebuild" >&2; exit 1; }

echo "== [1/5] fetch + verify Debian genericcloud arm64 =="
if [[ ! -f pristine.qcow2 ]]; then
  curl -fL --retry 3 -o pristine.qcow2.part "$IMAGE_URL"
  curl -fL --retry 3 -o SHA512SUMS "$CHECKSUMS_URL"
  want=$(grep 'genericcloud-arm64.qcow2$' SHA512SUMS | awk '{print $1}' | head -1)
  got=$(sha512sum pristine.qcow2.part | awk '{print $1}')
  [[ -n "$want" && "$want" == "$got" ]] || { echo "checksum mismatch (want=$want got=$got)" >&2; exit 1; }
  mv pristine.qcow2.part pristine.qcow2
fi

echo "== [2/5] cloud-init seed (bakes the guest bootstrap + boot unit, no SSH/network) =="
bootstrap_b64=$(base64 -w0 "$SCRIPT_DIR/guest/bootstrap.py")
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
# proxy, and those exist to find other paths.
packages:
  - python3-pip
  - python3-venv
  - python3-dev
  - git
  - curl
  - ca-certificates
  - nodejs
  - npm
  - build-essential
  - pkg-config
  - jq
  - ripgrep
  - sqlite3
  - unzip
  - zip
  - xz-utils
write_files:
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
      Description=Jarvis guest runtime bootstrap (fetch package over vsock, run loop)
      After=multi-user.target
      [Service]
      Type=simple
      ExecStart=/usr/bin/python3 /opt/jarvis/bootstrap.py
      Restart=no
      StandardOutput=journal+console
      StandardError=journal+console
      [Install]
      WantedBy=multi-user.target
runcmd:
  - systemctl disable systemd-networkd-wait-online.service || true
  - systemctl mask systemd-networkd-wait-online.service || true
  # The genericcloud base ships network tooling we don't want in an
  # assumed-compromised guest — including a full sshd that trixie's
  # systemd-ssh-generator will happily bind to AF_VSOCK, our control channel.
  # dpkg --force-depends, NOT apt: cloud-init hard-depends on ssh-import-id ->
  # openssh-client, so an apt purge removes cloud-init out from under this
  # very provisioning run (it died pre-poweroff and the build hung). dpkg
  # leaves cloud-init installed with an unmet dep record nothing ever reads.
  - dpkg --purge --force-depends openssh-server openssh-sftp-server openssh-client ssh-import-id socat tcpdump netcat-openbsd || true
  - systemctl enable jarvis-guest.service
  - touch /etc/jarvis-provisioned
power_state:
  mode: poweroff
  message: provisioning complete
EOF
cloud-localds seed.iso user-data meta-data

echo "== [3/5] provision boot (KVM, SLIRP net for cloud-init only) =="
cp pristine.qcow2 base-work.qcow2
qemu-img resize base-work.qcow2 "$DISK_SIZE"
cp "$AAVMF_VARS" efi_vars_build.fd
timeout 1800 qemu-system-aarch64 \
  -machine virt,gic-version=host -accel kvm -cpu host \
  -smp 2 -m 1024 \
  -drive if=pflash,format=raw,readonly=on,file="$AAVMF_CODE" \
  -drive if=pflash,format=raw,file=efi_vars_build.fd \
  -drive file=base-work.qcow2,if=virtio,format=qcow2 \
  -drive file=seed.iso,if=virtio,format=raw,readonly=on \
  -netdev user,id=n0 -device virtio-net-pci,netdev=n0 \
  -display none -serial file:provision-console.log

echo "== [4/5] verify provisioning =="
grep -q 'provisioning complete\|jarvis-provisioned\|reached target.*Power-Off\|Power down' provision-console.log \
  || { echo "provisioning may have failed — see $VM_DIR/provision-console.log" >&2; exit 1; }

echo "== [5/5] freeze read-only golden image =="
mv base-work.qcow2 "$BASE"
chmod 444 "$BASE"
cp "$AAVMF_VARS" efi_vars.fd
rm -f seed.iso user-data meta-data efi_vars_build.fd
echo "built $VM_DIR/$BASE (version $VERSION)"
