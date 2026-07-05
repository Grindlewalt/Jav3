#!/usr/bin/env bash
# Boot the Jarvis sandbox VM (invoked by jarvis-vm.service — don't run by hand
# unless debugging). The disk is a qcow2 overlay on the read-only golden base;
# if no overlay exists one is created, so "nuke" is simply: stop the unit,
# delete overlay.qcow2, start the unit.
#
# The VM is persistent: it boots once and stays up. Networking is QEMU
# user-mode with SSH forwarded to 127.0.0.1:2222 on the host — the VM can
# reach out, nothing can reach in except the loopback SSH forward.
set -euo pipefail

VM_DIR="${VM_DIR:-$HOME/jarvis/data/vm}"
AAVMF_CODE="/usr/share/AAVMF/AAVMF_CODE.fd"
SSH_PORT="${JARVIS_VM_SSH_PORT:-2222}"
MEM_MB="${JARVIS_VM_MEM_MB:-1024}"
CPUS="${JARVIS_VM_CPUS:-2}"

cd "$VM_DIR"
[[ -f base.qcow2 ]] || { echo "no base.qcow2 — run build_base.sh first" >&2; exit 1; }
[[ -f overlay.qcow2 ]] || qemu-img create -f qcow2 -b base.qcow2 -F qcow2 overlay.qcow2
[[ -f efi_vars_run.fd ]] || cp efi_vars.fd efi_vars_run.fd

exec qemu-system-aarch64 \
  -machine virt,gic-version=host -accel kvm -cpu host \
  -smp "$CPUS" -m "$MEM_MB" \
  -drive if=pflash,format=raw,readonly=on,file="$AAVMF_CODE" \
  -drive if=pflash,format=raw,file=efi_vars_run.fd \
  -drive file=overlay.qcow2,if=virtio,format=qcow2 \
  -netdev "user,id=n0,hostfwd=tcp:127.0.0.1:${SSH_PORT}-:22" \
  -device virtio-net-pci,netdev=n0 \
  -device virtio-rng-pci \
  -display none -serial file:console.log
