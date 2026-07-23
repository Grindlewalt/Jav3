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

# Network device: netless by default (vsock-only). When JARVIS_VM_EGRESS=1 the
# guest gets a tap NIC bridged to the host, where nftables + the egress proxy
# monitor and broker every byte (A1). The tap (jvtap0) must pre-exist — net_up.sh
# creates it owned by this user before we boot, so rootless QEMU can open it.
if [[ "${JARVIS_VM_EGRESS:-0}" == "1" ]]; then
  ip link show jvtap0 >/dev/null 2>&1 || { echo "jvtap0 missing — net_up.sh must run first" >&2; exit 1; }
  NETDEV=(-netdev tap,id=n0,ifname=jvtap0,script=no,downscript=no
          -device virtio-net-pci,netdev=n0,mac=52:54:00:12:34:60)
else
  NETDEV=(-nic none)
fi

exec qemu-system-aarch64 \
  -machine virt,gic-version=host -accel kvm -cpu host \
  -smp "$CPUS" -m "$MEM_MB" \
  "${NETDEV[@]}" \
  -drive if=pflash,format=raw,readonly=on,file="$AAVMF_CODE" \
  -drive if=pflash,format=raw,file=efi_vars_run.fd \
  -drive file=overlay.qcow2,if=virtio,format=qcow2 \
  -device vhost-vsock-pci,guest-cid="$CID" \
  -device virtio-rng-pci \
  -display none -serial file:console.log
