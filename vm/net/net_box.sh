#!/usr/bin/env bash
# Per-box network for multi-box mode (vm_boxes_enabled; backend/vm/boxnet.py).
# Run by the app via `sudo -n bash net_box.sh <action> <ifname> <host_ip> <guest_ip> [table]`
# ([table] = a named instance's jarvis_vm_<name>; sudo strips env, so it is argv).
#
#   add   <jvtapN> <10.201.N.1> <10.201.N.2>   create the tap (owned by the app
#                                              user) and pin it in the nft sets
#   del   <jvtapN> ...                         unpin and delete the tap
#   pin   <jvbrN>  <10.201.N.1> <10.201.N.2>   pin an existing interface only
#   unpin <jvbrN>  ...                         (docker bridges, WP8)
#
# Runs as root, so it trusts NOTHING it is given: the interface must be
# jvtapN / jvbrN with N in 4..254, and both addresses must be exactly the ones
# derived from that N. A caller cannot pin a LAN address, another box's
# address, or the shared box's jvtap0 (that one is static in the ruleset).
# The sets live in `table inet jarvis_vm`, loaded by `net_up.sh up-boxes`.
set -euo pipefail

ACTION="${1:-}"; IFN="${2:-}"; HOST_IP="${3:-}"; GUEST_IP="${4:-}"; TABLE="${5:-jarvis_vm}"
OWNER="${JARVIS_VM_USER:-${SUDO_USER:-$(id -un)}}"

die() { echo "net_box.sh: $*" >&2; exit 2; }

[[ "$TABLE" =~ ^jarvis_vm(_[a-z0-9]{1,12})?$ ]] || die "bad table"
[[ "$OWNER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "bad owner"
case "$ACTION" in
  add|del) [[ "$IFN" =~ ^jvtap([0-9]{1,3})$ ]] || die "bad tap name" ;;
  pin|unpin) [[ "$IFN" =~ ^jv(tap|br)([0-9]{1,3})$ ]] || die "bad interface name" ;;
  *) die "usage: $0 add|del|pin|unpin <ifname> <host_ip> <guest_ip>" ;;
esac
N="${IFN##*[a-z]}"
[[ "$N" =~ ^[1-9][0-9]{0,2}$ ]] || die "bad slot"
(( N >= 4 && N <= 254 )) || die "slot out of range"
[[ "$HOST_IP" == "10.201.$N.1" ]] || die "host ip must be 10.201.$N.1"
[[ "$GUEST_IP" == "10.201.$N.2" ]] || die "guest ip must be 10.201.$N.2"

pin() {
  nft add element inet "$TABLE" guest_taps "{ \"$IFN\" }"
  nft add element inet "$TABLE" tap_addr "{ \"$IFN\" . $HOST_IP }"
  nft add element inet "$TABLE" tap_src "{ \"$IFN\" . $GUEST_IP }"
}

unpin() {
  nft delete element inet "$TABLE" tap_src "{ \"$IFN\" . $GUEST_IP }" 2>/dev/null || true
  nft delete element inet "$TABLE" tap_addr "{ \"$IFN\" . $HOST_IP }" 2>/dev/null || true
  nft delete element inet "$TABLE" guest_taps "{ \"$IFN\" }" 2>/dev/null || true
}

case "$ACTION" in
  add)
    # fresh every time: a leftover tap may be owned by the wrong user
    ip tuntap del dev "$IFN" mode tap 2>/dev/null || true
    ip tuntap add dev "$IFN" mode tap user "$OWNER"
    ip addr replace "$HOST_IP/30" dev "$IFN"
    ip link set "$IFN" up
    unpin
    pin
    ;;
  del)
    unpin
    ip link del "$IFN" 2>/dev/null || true
    ;;
  pin) unpin; pin ;;
  unpin) unpin ;;
esac
