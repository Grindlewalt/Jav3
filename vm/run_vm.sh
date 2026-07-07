#!/usr/bin/env bash
# Boot the Jarvis sandbox VM (invoked by jarvis-vm.service — don't run by hand
# unless debugging). The disk is a qcow2 overlay on the read-only golden base;
# if no overlay exists one is created, so "nuke" is simply: stop the unit,
# delete overlay.qcow2, start the unit.
#
# The VM is persistent: it boots once and stays up. Networking is a host tap
# (jvtap0, created by the root unit jarvis-vm-net.service) behind a
# deny-by-default nftables egress firewall; DNS/DHCP come from the logged
# dnsmasq on 10.66.0.1. The guest is 10.66.0.10, SSH straight to :22.
# The guest's auditd exec log streams out over a virtio console into
# audit-stream.log (append mode — survives nukes).
set -euo pipefail

VM_DIR="${VM_DIR:-$HOME/jarvis/data/vm}"
AAVMF_CODE="/usr/share/AAVMF/AAVMF_CODE.fd"
MEM_MB="${JARVIS_VM_MEM_MB:-1024}"
CPUS="${JARVIS_VM_CPUS:-2}"

cd "$VM_DIR"
[[ -f base.qcow2 ]] || { echo "no base.qcow2 — run build_base.sh first" >&2; exit 1; }
ip link show jvtap0 >/dev/null 2>&1 || {
  echo "no jvtap0 — start the root net unit first: sudo systemctl start jarvis-vm-net" >&2
  exit 1
}
[[ -f overlay.qcow2 ]] || qemu-img create -f qcow2 -b base.qcow2 -F qcow2 overlay.qcow2
[[ -f efi_vars_run.fd ]] || cp efi_vars.fd efi_vars_run.fd

exec qemu-system-aarch64 \
  -machine virt,gic-version=host -accel kvm -cpu host \
  -smp "$CPUS" -m "$MEM_MB" \
  -drive if=pflash,format=raw,readonly=on,file="$AAVMF_CODE" \
  -drive if=pflash,format=raw,file=efi_vars_run.fd \
  -drive file=overlay.qcow2,if=virtio,format=qcow2 \
  -netdev tap,id=n0,ifname=jvtap0,script=no,downscript=no \
  -device virtio-net-pci,netdev=n0 \
  -device virtio-rng-pci \
  -device virtio-serial-pci \
  -chardev file,id=audit0,path=audit-stream.log,append=on \
  -device virtconsole,chardev=audit0,name=jarvis.audit \
  -display none -serial file:console.log
