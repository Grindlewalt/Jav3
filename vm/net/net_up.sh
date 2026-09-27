#!/usr/bin/env bash
# Bring the monitored-egress path up/down for the brain guest (A1). Run by
# backend/vm/lifecycle.py via `sudo -n` when settings.vm_egress is on; a no-op
# to the rest of the system when off (never called). Explicit-proxy model: the
# guest's only reachable host is the Pi on the DNS + proxy ports (see the .nft).
#
#   net_up.sh <up|down|up-boxes|down-boxes> [<tap> <host_ip> <pcap 0|1> <table>]
#
# Everything comes in ARGV (backend/vm/boxnet.net_up_argv): sudo's env_reset
# strips JARVIS_*, so env settings were silently ignored and a second instance
# tore down the default one's jvtap0 and tcpdump (e2e BUG-1). No extra args =
# the default install: jvtap0, 10.201.0.1, pcap on, table jarvis_vm.
#
# Runs as root, so it trusts nothing it is given: the tap must be jvtapN
# (N 0..254), the host IP exactly 10.201.N.1, the table jarvis_vm or
# jarvis_vm_<name>; a named table may not use jvtap0. Every object it creates
# or removes (tap, table, pid files, dns log, pcaps) is named from those, and
# teardown touches only this instance's own ones.
set -euo pipefail

ACTION="${1:-up}"
TAP="${2:-jvtap0}"
HOST_IP="${3:-10.201.0.1}"
PCAP="${4:-1}"
TABLE="${5:-jarvis_vm}"

die() { echo "net_up.sh: $*" >&2; exit 2; }

[[ "$TAP" =~ ^jvtap(0|[1-9][0-9]{0,2})$ ]] || die "bad tap name"
N="${TAP#jvtap}"
(( N <= 254 )) || die "tap out of range"
[[ "$HOST_IP" == "10.201.$N.1" ]] || die "host ip must be 10.201.$N.1"
GUEST_IP="10.201.$N.2"
[[ "$PCAP" == "0" || "$PCAP" == "1" ]] || die "pcap must be 0 or 1"
[[ "$TABLE" =~ ^jarvis_vm(_[a-z0-9]{1,12})?$ ]] || die "bad table"
if [[ "$TABLE" == "jarvis_vm" ]]; then SUF=""; else SUF="-${TABLE#jarvis_vm_}"; fi
[[ -n "$SUF" && "$TAP" == "jvtap0" ]] && die "a named instance may not use jvtap0"

# The tap MUST be owned by the user QEMU runs as (the app user), or the
# unprivileged guest can't attach ("could not configure /dev/net/tun: Operation
# not permitted"). This script runs under `sudo -n`, so $(id -un) is root — the
# wrong owner. sudo sets $SUDO_USER to the real invoking user, so prefer it.
OWNER="${SUDO_USER:-$(id -un)}"
[[ "$OWNER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "bad owner"
HERE="$(cd "$(dirname "$0")" && pwd)"
NFT="$HERE/jarvis-egress.nft"
DNSCONF="$HERE/dnsmasq-egress.conf"
PIDF="/run/jarvis-dnsmasq$SUF.pid"
TCPDUMP_PIDF="/run/jarvis-tcpdump$SUF.pid"
PCAP_DIR=/var/log/jarvis-vm

# The checked-in files, with this instance's names substituted (all validated
# above). Written by root under /run: never a file the app user can edit.
render_nft() {
  sed -E -e "s/jarvis_vm([^a-z0-9_]|\$)/$TABLE\\1/g" \
         -e "s/jvtap0([^0-9]|\$)/$TAP\\1/g" \
         -e "s/10\\.201\\.0\\.1([^0-9]|\$)/$HOST_IP\\1/g" \
         -e "s/10\\.201\\.0\\.2([^0-9]|\$)/$GUEST_IP\\1/g" "$NFT"
}

render_dns() {
  # a named instance answers only on its own shared tap, logs to its own file
  # and leaves DHCP (a host-wide :67) to the default install
  local named=()
  [[ -n "$SUF" ]] && named=(-e "s/^interface=jvtap\\*\$/interface=$TAP/"
                            -e '/^dhcp-/d' -e '/^log-dhcp/d')
  sed -E -e "s/jvtap0([^0-9]|\$)/$TAP\\1/g" \
         -e "s/10\\.201\\.0\\.1([^0-9]|\$)/$HOST_IP\\1/g" \
         -e "s/10\\.201\\.0\\.2([^0-9]|\$)/$GUEST_IP\\1/g" \
         -e "s|/dns\\.log\$|/dns$SUF.log|" ${named[@]+"${named[@]}"} "$DNSCONF"
}

up() {
  # recreate the tap fresh: a leftover tap from a prior run may be owned by the
  # wrong user, and `tuntap add` on an existing device is a silent no-op — so a
  # stale root-owned tap would persist and block the guest. Delete then add.
  ip tuntap del dev "$TAP" mode tap 2>/dev/null || true
  ip tuntap add dev "$TAP" mode tap user "$OWNER"
  ip addr replace "$HOST_IP/24" dev "$TAP"
  ip link set "$TAP" up
  sysctl -qw net.ipv4.ip_forward=1
  render_nft > "/run/jarvis-egress$SUF.nft"
  nft -f "/run/jarvis-egress$SUF.nft"
  install -d -m 755 "$PCAP_DIR"
  # Docker sets FORWARD DROP in legacy iptables on the Pi; let tap in.
  if iptables -nL DOCKER-USER >/dev/null 2>&1; then
    iptables -C DOCKER-USER -i "$TAP" -j ACCEPT 2>/dev/null || iptables -I DOCKER-USER 1 -i "$TAP" -j ACCEPT
  fi
  # logged DNS + single-lease DHCP
  [[ -f "$PIDF" ]] && pkill -F "$PIDF" 2>/dev/null || true
  render_dns > "/run/jarvis-dnsmasq$SUF.conf"
  dnsmasq --conf-file="/run/jarvis-dnsmasq$SUF.conf" --pid-file="$PIDF"
  # rolling pcap on the tap — ground truth for the beacon catcher
  if [[ "$PCAP" == "1" ]]; then
    [[ -f "$TCPDUMP_PIDF" ]] && pkill -F "$TCPDUMP_PIDF" 2>/dev/null || true
    tcpdump -i "$TAP" -U -n -G 3600 -W 24 -w "$PCAP_DIR/jvtap$SUF-%Y%m%d%H%M.pcap" \
      >/dev/null 2>&1 &
    echo $! > "$TCPDUMP_PIDF"
  fi
}

down() {
  [[ -f "$TCPDUMP_PIDF" ]] && pkill -F "$TCPDUMP_PIDF" 2>/dev/null || true
  [[ -f "$PIDF" ]] && pkill -F "$PIDF" 2>/dev/null || true
  nft delete table inet "$TABLE" 2>/dev/null || true
  ip link del "$TAP" 2>/dev/null || true
}

# The box taps THIS instance pinned (its own table's guest_taps set), never a
# name pattern: jvtap* also matches every other instance's boxes.
own_box_taps() {
  { nft list set inet "$TABLE" guest_taps 2>/dev/null || true; } \
    | grep -oE 'jvtap[1-9][0-9]{0,2}' | sort -u | grep -vx "$TAP" || true
}

# Multi-box mode (vm_boxes_enabled): the same, with the set-pinned ruleset and
# a resolver on every jvtap* (bind-dynamic picks up taps net_box.sh adds
# later). The files are the checked-in ones; nothing the app writes is loaded.
down_boxes() {
  for dev in $(own_box_taps); do
    ip link del "$dev" 2>/dev/null || true
  done
  down
}

case "$ACTION" in
  up) up ;;
  down) down ;;
  up-boxes) NFT="$HERE/jarvis-egress-boxes.nft"; DNSCONF="$HERE/dnsmasq-egress-boxes.conf"; up ;;
  down-boxes) down_boxes ;;
  # read-only renders for tests / an operator's review; no root needed
  render-nft) render_nft ;;
  render-nft-boxes) NFT="$HERE/jarvis-egress-boxes.nft"; render_nft ;;
  render-dns-boxes) DNSCONF="$HERE/dnsmasq-egress-boxes.conf"; render_dns ;;
  render-own-taps) own_box_taps ;;
  *) echo "usage: $0 up|down|up-boxes|down-boxes [tap host_ip pcap table]" >&2; exit 1 ;;
esac
