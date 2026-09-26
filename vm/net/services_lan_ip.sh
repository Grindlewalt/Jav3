#!/usr/bin/env bash
# services_lan_ip.sh: give Jav3's services their OWN LAN address (DESIGN-BOXES.md,
# operator decision 0.4). WP3. Run by hand, as root, on the Jav3 host. Nothing
# in Jav3 runs this script.
#
# Why: a service exposed with expose "lan" gets a host-side relay (backend/vm/
# portfwd.py). It must never listen on the address Jav3's own UI is served on:
# browsers send cookies regardless of port (SECURITY-RESIDUAL-RISK.md #13), so a
# hostile service on Jav3's address would receive the operator's session cookie.
# A second address is a different cookie origin.
#
# Usage:
#   sudo vm/net/services_lan_ip.sh add    <ip>/<prefix> [iface] [--macvlan] [--persist] [--ui-port N]
#   sudo vm/net/services_lan_ip.sh del    <ip>/<prefix> [iface] [--macvlan]
#   sudo vm/net/services_lan_ip.sh status
#
# Modes:
#   alias   (default) a secondary address on the LAN interface, label <iface>:jsvc.
#           Same MAC as the host; simplest; the router sees one device, two IPs.
#   macvlan (--macvlan) a macvlan interface `jsvc0` with its own MAC. Use when the
#           router/DHCP reservations should see a separate device. Note: with
#           macvlan the host itself cannot talk to jsvc0's address (fine: only
#           LAN clients need it).
#
# Jav3 recognises the address as dedicated ONLY by that label/interface name
# (portfwd.check_lan_ip), and refuses one that is also an address Jav3 answers
# on. Pick an unused address OUTSIDE your DHCP pool (or reserve it), then set
#   JARVIS_SERVICES_LAN_IP=<ip>      (config: services_lan_ip)
# and restart Jav3.
#
# `add` also installs an nft table `jav3_svc_lan` that DROPS Jav3's UI port on
# the new address, so the operator cannot end up logged in to Jav3 through it
# (a login there would put the cookie on the services' origin).
#
# --persist writes /etc/systemd/system/jav3-services-lan-ip.service, a oneshot
# that re-runs `add` at boot.
#
# ROLLBACK (any time; services with LAN exposure stop being reachable, nothing
# else changes):
#   1. unset JARVIS_SERVICES_LAN_IP (or set it to "") and restart Jav3: LAN relays
#      are refused from then on, loopback relays keep working;
#   2. sudo vm/net/services_lan_ip.sh del <ip>/<prefix> [iface] [--macvlan]
#      (removes the address or jsvc0, the nft table and the boot unit);
#   3. check: `ip -o -4 addr show` no longer lists the address, and
#      `nft list tables` has no jav3_svc_lan.
# If the host lost its LAN after `add` (wrong prefix/iface), from the console:
#   ip addr flush label '*:jsvc'; ip link del jsvc0; nft delete table inet jav3_svc_lan
set -euo pipefail

usage() { sed -n '12,16p' "$0" >&2; exit 2; }

cmd="${1:-}"; shift || true
[[ "$cmd" == "status" ]] && {
  ip -o -4 addr show | grep -E 'jsvc' || echo "no services LAN address"
  nft list table inet jav3_svc_lan 2>/dev/null || echo "no jav3_svc_lan table"
  exit 0; }
[[ "$cmd" == "add" || "$cmd" == "del" ]] || usage

cidr="${1:-}"; shift || true
[[ "$cidr" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/[0-9]{1,2}$ ]] || { echo "need <ip>/<prefix>" >&2; usage; }
ip_only="${cidr%/*}"

iface=""; macvlan=0; persist=0; ui_port=8000; keep_unit=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --macvlan) macvlan=1 ;;
    --persist) persist=1 ;;
    --keep-unit) keep_unit=1 ;;     # used by the boot unit's ExecStop
    --ui-port) ui_port="${2:?}"; shift ;;
    -*) usage ;;
    *) iface="$1" ;;
  esac
  shift
done
[[ -n "$iface" ]] || iface="$(ip -o -4 route show default | awk '{print $5; exit}')"
[[ -n "$iface" ]] || { echo "cannot find the LAN interface; pass it" >&2; exit 1; }
[[ "$iface" =~ ^[a-zA-Z0-9_.-]{1,15}$ ]] || { echo "bad interface name" >&2; exit 1; }
[[ "$ui_port" =~ ^[0-9]{1,5}$ ]] || { echo "bad --ui-port" >&2; exit 1; }

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)" >&2; exit 1; }

private() {  # RFC 1918 only
  local a b; IFS=. read -r a b _ _ <<<"$1"
  [[ $a -eq 10 ]] || [[ $a -eq 172 && $b -ge 16 && $b -le 31 ]] || [[ $a -eq 192 && $b -eq 168 ]]
}

if [[ "$cmd" == "add" ]]; then
  private "$ip_only" || { echo "$ip_only is not a private (RFC 1918) address" >&2; exit 1; }
  [[ "$ip_only" == 10.201.* ]] && { echo "10.201.0.0/16 is the guests' network" >&2; exit 1; }
  primary="$(ip -o -4 addr show dev "$iface" | awk '{print $4}' | cut -d/ -f1 | head -1)"
  [[ "$ip_only" != "$primary" ]] || { echo "$ip_only is $iface's own (Jav3's) address" >&2; exit 1; }
  if ip -o -4 addr show | awk '{print $4}' | cut -d/ -f1 | grep -qx "$ip_only"; then
    echo "$ip_only is already on this host" >&2; exit 1
  fi
  if command -v arping >/dev/null; then     # iputils: -D exits 0 when nobody answers
    arping -D -q -c 2 -I "$iface" "$ip_only" || {
      echo "$ip_only answers ARP on the LAN: something already uses it" >&2; exit 1; }
  fi

  if [[ $macvlan -eq 1 ]]; then
    ip link add jsvc0 link "$iface" type macvlan mode bridge
    ip addr add "$cidr" dev jsvc0
    ip link set jsvc0 up
  else
    ip addr add "$cidr" dev "$iface" label "$iface:jsvc"
  fi

  nft -f - <<EOF
table inet jav3_svc_lan
delete table inet jav3_svc_lan
table inet jav3_svc_lan {
  chain input {
    type filter hook input priority -5; policy accept;
    ip daddr $ip_only tcp dport $ui_port drop
  }
}
EOF

  if [[ $persist -eq 1 ]]; then
    self="$(readlink -f "$0")"
    extra=""; [[ $macvlan -eq 1 ]] && extra="--macvlan"
    cat >/etc/systemd/system/jav3-services-lan-ip.service <<EOF
[Unit]
Description=Jav3 services' dedicated LAN address ($cidr)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$self add $cidr $iface $extra --ui-port $ui_port
ExecStop=$self del $cidr $iface $extra --keep-unit

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable jav3-services-lan-ip.service
  fi
  echo "added $cidr ($( [[ $macvlan -eq 1 ]] && echo "jsvc0 on $iface" || echo "$iface:jsvc")); set JARVIS_SERVICES_LAN_IP=$ip_only"
else
  if [[ $macvlan -eq 1 ]]; then
    ip link del jsvc0 2>/dev/null || true
  else
    ip addr del "$cidr" dev "$iface" 2>/dev/null || true
  fi
  nft delete table inet jav3_svc_lan 2>/dev/null || true
  if [[ $keep_unit -eq 0 && -f /etc/systemd/system/jav3-services-lan-ip.service ]]; then
    systemctl disable jav3-services-lan-ip.service 2>/dev/null || true
    rm -f /etc/systemd/system/jav3-services-lan-ip.service
    systemctl daemon-reload
  fi
  [[ $keep_unit -eq 1 ]] || echo "removed $cidr; unset JARVIS_SERVICES_LAN_IP and restart Jav3"
fi
