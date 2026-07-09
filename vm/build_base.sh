#!/usr/bin/env bash
# Build the golden base image for the Jarvis sandbox VM. Run ON the Pi, once
# (or whenever the base should be rebuilt from a pristine upstream image).
#
# Produces, under $VM_DIR:
#   base.qcow2          provisioned Debian arm64 golden image (read-only)
#   agent_ed25519[.pub] SSH keypair the host uses to reach the VM
#   known_hosts         pinned VM host key (baked in, survives nukes)
#   efi_vars.fd         UEFI vars template
#
# The runtime disk is an overlay on base.qcow2 (see run_vm.sh); nuking the VM
# just recreates the overlay, the base is never written to again.
set -euo pipefail

VM_DIR="${VM_DIR:-$HOME/jarvis/data/vm}"
IMAGE_URL="https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-arm64.qcow2"
CHECKSUMS_URL="https://cloud.debian.org/images/cloud/trixie/latest/SHA512SUMS"
AAVMF_CODE="/usr/share/AAVMF/AAVMF_CODE.fd"
AAVMF_VARS="/usr/share/AAVMF/AAVMF_VARS.fd"
DISK_SIZE="8G"

mkdir -p "$VM_DIR"
cd "$VM_DIR"

if [[ -f base.qcow2 ]]; then
  echo "base.qcow2 already exists in $VM_DIR — delete it first to rebuild." >&2
  exit 1
fi

echo "== downloading pristine image =="
if [[ ! -f pristine.qcow2 ]]; then
  curl -fL --retry 3 -o pristine.qcow2.part "$IMAGE_URL"
  curl -fL --retry 3 -o SHA512SUMS "$CHECKSUMS_URL"
  want=$(grep 'genericcloud-arm64.qcow2$' SHA512SUMS | awk '{print $1}' | head -1)
  got=$(sha512sum pristine.qcow2.part | awk '{print $1}')
  if [[ -z "$want" || "$want" != "$got" ]]; then
    echo "checksum mismatch (want=$want got=$got)" >&2
    exit 1
  fi
  mv pristine.qcow2.part pristine.qcow2
fi

echo "== generating keys =="
[[ -f agent_ed25519 ]] || ssh-keygen -q -t ed25519 -N '' -C jarvis-vm-agent -f agent_ed25519
[[ -f vm_host_ed25519 ]] || ssh-keygen -q -t ed25519 -N '' -C jarvis-vm-host -f vm_host_ed25519

echo "== building cloud-init seed =="
cat > user-data <<EOF
#cloud-config
hostname: jarvis-vm
users:
  - name: agent
    shell: /bin/bash
    lock_passwd: true
    # Full sudo inside the guest is fine: every guardrail lives host-side
    # (egress firewall, staging, state API, nuke). Root here only risks the VM.
    sudo: ALL=(ALL) NOPASSWD:ALL
    ssh_authorized_keys:
      - $(cat agent_ed25519.pub)
ssh_keys:
  ed25519_private: |
$(sed 's/^/    /' vm_host_ed25519)
  ed25519_public: $(cat vm_host_ed25519.pub)
ssh_deletekeys: true
package_update: true
packages:
  - python3
  - python3-venv
  - python3-pip
  - git
  - build-essential
  - curl
  - jq
  - unzip
  - auditd
  - nodejs
  - npm
write_files:
  - path: /etc/motd
    content: |
      Jarvis sandbox VM. Nothing here is durable — assume this disk can be
      recreated from the golden image at any moment.
  - path: /etc/audit/rules.d/jarvis-exec.rules
    content: |
      ## Log every exec (M4 monitored execution). Streamed to the host via
      ## audit-stream.service; the guest deleting its copy changes nothing.
      -a always,exit -F arch=b64 -S execve,execveat -k jexec
      ## Log sensitive-file READS (M4 sensitive-file coverage). A successful
      ## open/openat by a non-system user (the workspace runs as `agent`, uid
      ## >=1000); the host correlates the PATH records for these events and keeps
      ## only names matching the operator's sensitive globs (key `jread`), so the
      ## stream is broad but the retained evidence is scoped. If run-time volume
      ## is a problem, narrow this to `-w`-style dir watches on the fixed secret
      ## locations — the host parser keys off `jread` either way.
      -a always,exit -F arch=b64 -S openat,open -F auid>=1000 -F success=1 -k jread
  - path: /etc/systemd/system/audit-stream.service
    content: |
      [Unit]
      Description=Stream audit log to host (virtio console -> audit-stream.log)
      After=auditd.service
      Requires=auditd.service
      [Service]
      ExecStart=/bin/sh -c 'exec tail -n +1 -F /var/log/audit/audit.log > /dev/hvc0'
      Restart=always
      RestartSec=2
      [Install]
      WantedBy=multi-user.target
runcmd:
  - mkdir -p /workspace && chown agent:agent /workspace
  - systemctl enable audit-stream.service
  # jsdom for the beacon-catcher: renders agent-built HTML in the sandbox so
  # its network calls hit the tap. Installed at provision time (net available).
  - mkdir -p /opt/jarvis && cd /opt/jarvis && npm install --no-audit --no-fund jsdom
  - touch /etc/jarvis-provisioned
power_state:
  mode: poweroff
  message: provisioning complete
EOF
cat > meta-data <<EOF
instance-id: jarvis-vm-golden
local-hostname: jarvis-vm
EOF
cloud-localds seed.iso user-data meta-data

echo "== provisioning (boots once, installs toolchain, powers off) =="
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

grep -q 'jarvis-provisioned\|provisioning complete\|Power down' provision-console.log || {
  echo "provisioning may have failed — check $VM_DIR/provision-console.log" >&2
  exit 1
}

echo "== finalizing =="
mv base-work.qcow2 base.qcow2
chmod 444 base.qcow2
cp "$AAVMF_VARS" efi_vars.fd
# Pin the baked host key: tap address (primary) + legacy loopback forward.
{
  echo "10.66.0.10 $(cat vm_host_ed25519.pub)"
  echo "[127.0.0.1]:2222 $(cat vm_host_ed25519.pub)"
} > known_hosts
rm -f seed.iso user-data meta-data vm_host_ed25519 efi_vars_build.fd
chmod 600 agent_ed25519

echo "done. base image: $VM_DIR/base.qcow2 ($(du -h base.qcow2 | cut -f1))"
echo "next: systemctl --user start jarvis-vm  (run_vm.sh creates the overlay)"
