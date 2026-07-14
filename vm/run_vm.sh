#!/usr/bin/env bash
# Boot the disposable guest: a qcow2 overlay on the read-only golden image, under
# KVM, with a vhost-vsock channel to the host and NO network device at all. The
# guest's only path off-box is vsock to the host gateway (CID 2). All guest writes
# land in the throwaway overlay; the golden base is never touched.
#
# This is the proven aarch64/KVM invocation from the old sandbox layer with the
# tap NIC removed and `-device vhost-vsock-pci` added. Normally launched by
# backend/vm/lifecycle.py (app-owned subprocess); runnable by hand for a boot test.
set -euo pipefail

VM_DIR="${VM_DIR:-$HOME/jarvis/data/vm}"
AAVMF_CODE="/usr/share/AAVMF/AAVMF_CODE.fd"
AAVMF_VARS="/usr/share/AAVMF/AAVMF_VARS.fd"
BASE="${JARVIS_VM_BASE:-base-v1.qcow2}"
MEM_MB="${JARVIS_VM_MEM_MB:-768}"
CPUS="${JARVIS_VM_CPUS:-2}"
CID="${JARVIS_VM_CID:-3}"          # guest CID (>=3); host is always CID 2

cd "$VM_DIR"
[[ -f "$BASE" ]] || { echo "no $BASE — run build_base.sh first" >&2; exit 1; }
[[ -f overlay.qcow2 ]] || qemu-img create -f qcow2 -b "$BASE" -F qcow2 overlay.qcow2 >/dev/null
cp "$AAVMF_VARS" efi_vars_run.fd     # fresh UEFI vars every boot (disposable)

exec qemu-system-aarch64 \
  -machine virt,gic-version=host -accel kvm -cpu host \
  -smp "$CPUS" -m "$MEM_MB" \
  -nic none \
  -drive if=pflash,format=raw,readonly=on,file="$AAVMF_CODE" \
  -drive if=pflash,format=raw,file=efi_vars_run.fd \
  -drive file=overlay.qcow2,if=virtio,format=qcow2 \
  -device vhost-vsock-pci,guest-cid="$CID" \
  -device virtio-rng-pci \
  -display none -serial file:console.log
