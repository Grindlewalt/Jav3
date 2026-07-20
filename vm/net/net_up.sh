#!/usr/bin/env bash
# Bring the monitored-egress path up/down for the brain guest (A1). Run by
# backend/vm/lifecycle.py via `sudo -n` when settings.vm_egress is on; a no-op
# to the rest of the system when off (never called). Explicit-proxy model: the
# guest's only reachable host is the Pi on the DNS + proxy ports (see the .nft).
set -euo pipefail

TAP="${JARVIS_VM_TAP:-jvtap0}"
HOST_IP="${JARVIS_VM_HOST_IP:-10.201.0.1}"
OWNER="${JARVIS_VM_USER:-$(id -un)}"
HERE="$(cd "$(dirname "$0")" && pwd)"
NFT="$HERE/jarvis-egress.nft"
DNSCONF="$HERE/dnsmasq-egress.conf"
PIDF=/run/jarvis-dnsmasq.pid
TCPDUMP_PIDF=/run/jarvis-tcpdump.pid
PCAP_DIR=/var/log/jarvis-vm

up() {
  ip tuntap add dev "$TAP" mode tap user "$OWNER" 2>/dev/null || true
  ip addr replace "$HOST_IP/24" dev "$TAP"
  ip link set "$TAP" up
  sysctl -qw net.ipv4.ip_forward=1
  nft -f "$NFT"
  install -d -m 755 "$PCAP_DIR"
  # Docker sets FORWARD DROP in legacy iptables on the Pi; let tap in.
  if iptables -nL DOCKER-USER >/dev/null 2>&1; then
    iptables -C DOCKER-USER -i "$TAP" -j ACCEPT 2>/dev/null || iptables -I DOCKER-USER 1 -i "$TAP" -j ACCEPT
  fi
  # logged DNS + single-lease DHCP
  [[ -f "$PIDF" ]] && pkill -F "$PIDF" 2>/dev/null || true
  dnsmasq --conf-file="$DNSCONF" --pid-file="$PIDF"
  # rolling pcap on the tap — ground truth for the beacon catcher
  if [[ "${JARVIS_VM_PCAP:-1}" == "1" ]]; then
    [[ -f "$TCPDUMP_PIDF" ]] && pkill -F "$TCPDUMP_PIDF" 2>/dev/null || true
    tcpdump -i "$TAP" -U -n -G 3600 -W 24 -w "$PCAP_DIR/jvtap-%Y%m%d%H%M.pcap" \
      >/dev/null 2>&1 &
    echo $! > "$TCPDUMP_PIDF"
  fi
}

down() {
  [[ -f "$TCPDUMP_PIDF" ]] && pkill -F "$TCPDUMP_PIDF" 2>/dev/null || true
  [[ -f "$PIDF" ]] && pkill -F "$PIDF" 2>/dev/null || true
  nft delete table inet jarvis_vm 2>/dev/null || true
  ip link del "$TAP" 2>/dev/null || true
}

case "${1:-up}" in
  up) up ;;
  down) down ;;
  *) echo "usage: $0 up|down" >&2; exit 1 ;;
esac
