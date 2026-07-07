#!/usr/bin/env bash
# Root-side network setup for the Jarvis sandbox VM (invoked by
# jarvis-vm-net.service). Creates the tap the (rootless) QEMU user unit opens,
# addresses it, and loads the deny-by-default nftables ruleset.
#
#   vm-net.sh up      create jvtap0 (owned by $JARVIS_USER), 10.66.0.1/24,
#                     enable forwarding, load nft rules, punch DOCKER-USER
#   vm-net.sh down    drop the nft table and the tap
set -euo pipefail

JARVIS_USER="${JARVIS_USER:-grindlewalt}"
TAP=jvtap0
HOST_IP=10.66.0.1/24
NFT_RULES="$(cd "$(dirname "$0")" && pwd)/jarvis-vm.nft"

up() {
  ip tuntap add dev "$TAP" mode tap user "$JARVIS_USER" 2>/dev/null || true
  ip addr replace "$HOST_IP" dev "$TAP"
  ip link set "$TAP" up
  sysctl -qw net.ipv4.ip_forward=1
  nft -f "$NFT_RULES"
  # Docker (if present) sets FORWARD policy DROP in the legacy iptables table;
  # let tap traffic through *that* table — our own nft table stays the single
  # deny-by-default authority for the guest.
  if iptables -nL DOCKER-USER >/dev/null 2>&1; then
    iptables -C DOCKER-USER -i "$TAP" -j ACCEPT 2>/dev/null || iptables -I DOCKER-USER 1 -i "$TAP" -j ACCEPT
    iptables -C DOCKER-USER -o "$TAP" -j ACCEPT 2>/dev/null || iptables -I DOCKER-USER 1 -o "$TAP" -j ACCEPT
  fi
  install -d -m 755 /var/log/jarvis-vm
}

down() {
  nft delete table inet jarvis_vm 2>/dev/null || true
  if iptables -nL DOCKER-USER >/dev/null 2>&1; then
    iptables -D DOCKER-USER -i "$TAP" -j ACCEPT 2>/dev/null || true
    iptables -D DOCKER-USER -o "$TAP" -j ACCEPT 2>/dev/null || true
  fi
  ip link del "$TAP" 2>/dev/null || true
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  *) echo "usage: $0 up|down" >&2; exit 2 ;;
esac
