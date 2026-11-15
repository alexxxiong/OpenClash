#!/bin/sh
set -eu
run=${MU_SITE_RUN:-/tmp/mu5250-home-site}
export PATH=${MU_SITE_TEST_BIN:+$MU_SITE_TEST_BIN:}/usr/sbin:/usr/bin:/sbin:/bin:/koolshare/bin
umask 077
mkdir -p "$run"
exec 8>"$run/rules.lock"
flock -x 8
# This ASUS firmware uses iptables 1.4.12.2, which has no -w option.
# The feature's flock still serializes its own updates.
ipt() { iptables "$@"; }
cleanup() {
    [ -f "$run/rules.owned" ] || return 0
    while ipt -t nat -C POSTROUTING -j MU_SITE_NAT 2>/dev/null; do ipt -t nat -D POSTROUTING -j MU_SITE_NAT; done
    while ipt -t mangle -C FORWARD -j MU_SITE_MSS 2>/dev/null; do ipt -t mangle -D FORWARD -j MU_SITE_MSS; done
    for entry in 'nat MU_SITE_NAT' 'mangle MU_SITE_MSS'; do
        set -- $entry
        if ipt -t "$1" -S "$2" >/dev/null 2>&1; then
            ipt -t "$1" -F "$2"
            ipt -t "$1" -X "$2"
        fi
    done
    rm -f "$run/rules.owned"
}
case "${1:-}" in
    stop) cleanup; exit 0 ;;
    start) ;;
    *) exit 64 ;;
esac
ip -4 route get 192.168.3.1 2>/dev/null | grep -q 'dev tailscale0' || exit 69
if [ -f "$run/rules.owned" ]; then
    ipt -t nat -C POSTROUTING -j MU_SITE_NAT &&
    ipt -t mangle -C FORWARD -j MU_SITE_MSS && exit 0
    cleanup
fi
for entry in 'nat MU_SITE_NAT' 'mangle MU_SITE_MSS'; do
    set -- $entry
    if ipt -t "$1" -S "$2" >/dev/null 2>&1; then exit 73; fi
done
: > "$run/rules.owned"
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
ipt -t nat -N MU_SITE_NAT
ipt -t mangle -N MU_SITE_MSS
# The stock plugin adds its own MASQUERADE even with NoSNAT=true.
# Terminate NAT only for these two LANs, preserving all other plugin rules.
ipt -t nat -A MU_SITE_NAT -o tailscale0 -s 192.168.50.0/24 -d 192.168.3.0/24 -j ACCEPT
ipt -t mangle -A MU_SITE_MSS -i br0 -o tailscale0 -s 192.168.50.0/24 -d 192.168.3.0/24 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu
ipt -t mangle -A MU_SITE_MSS -i tailscale0 -o br0 -s 192.168.3.0/24 -d 192.168.50.0/24 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu
ipt -t nat -I POSTROUTING 1 -j MU_SITE_NAT
ipt -t mangle -I FORWARD 1 -j MU_SITE_MSS
trap - EXIT INT TERM
