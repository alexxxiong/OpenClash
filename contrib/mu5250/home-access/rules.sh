#!/bin/sh
set -eu
base=${MU_HOME_BASE:-/data/tailscale-home}
export PATH=${MU_HOME_TEST_BIN:+$MU_HOME_TEST_BIN:}/usr/sbin:/usr/bin:/sbin:/bin
mkdir -p "$base/run"
exec 8>"$base/run/rules.lock"
flock -x 8
ipt() { iptables -w 5 "$@"; }
cleanup() {
    [ -f "$base/run/rules.owned" ] || return 0
    while ipt -t filter -C FORWARD -j MU_HOME_FWD 2>/dev/null; do ipt -t filter -D FORWARD -j MU_HOME_FWD; done
    while ipt -t filter -C INPUT -j MU_HOME_IN 2>/dev/null; do ipt -t filter -D INPUT -j MU_HOME_IN; done
    while ipt -t mangle -C FORWARD -j MU_HOME_MSS 2>/dev/null; do ipt -t mangle -D FORWARD -j MU_HOME_MSS; done
    for entry in 'filter MU_HOME_FWD' 'filter MU_HOME_IN' 'mangle MU_HOME_MSS'; do
        set -- $entry
        if ipt -t "$1" -S "$2" >/dev/null 2>&1; then
            ipt -t "$1" -F "$2"
            ipt -t "$1" -X "$2"
        fi
    done
    rm -f "$base/run/rules.owned"
}
case "${1:-}" in
    stop) cleanup; exit 0 ;;
    status) test -f "$base/run/rules.owned"; exit $? ;;
    start) ;;
    *) exit 64 ;;
esac
ip -4 route get 192.168.50.20 2>/dev/null | grep -q 'dev ts-home' || exit 69
if [ -f "$base/run/rules.owned" ]; then
    ipt -t filter -C FORWARD -j MU_HOME_FWD &&
    ipt -t filter -C INPUT -j MU_HOME_IN &&
    ipt -t mangle -C FORWARD -j MU_HOME_MSS && exit 0
    cleanup
fi
# Reject pre-existing chains before taking ownership or mutating any rule.
for entry in 'filter MU_HOME_FWD' 'filter MU_HOME_IN' 'mangle MU_HOME_MSS'; do
    set -- $entry
    if ipt -t "$1" -S "$2" >/dev/null 2>&1; then exit 73; fi
done
umask 077
: > "$base/run/rules.owned"
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
ipt -t filter -N MU_HOME_FWD
ipt -t filter -N MU_HOME_IN
ipt -t mangle -N MU_HOME_MSS
ipt -t filter -A MU_HOME_FWD -i br-lan -o ts-home -s 192.168.3.0/24 -d 192.168.50.0/24 -j ACCEPT
ipt -t filter -A MU_HOME_FWD -i ts-home -o br-lan -s 192.168.50.0/24 -d 192.168.3.0/24 -j ACCEPT
ipt -t filter -A MU_HOME_IN -i ts-home -s 192.168.50.0/24 -d 192.168.3.1/32 -j ACCEPT
ipt -t filter -A MU_HOME_IN -p udp --dport 41641 -j ACCEPT
# Preserve the source subnet in both directions. Tailscale also has NoSNAT=true.
ipt -t mangle -A MU_HOME_MSS -i br-lan -o ts-home -s 192.168.3.0/24 -d 192.168.50.0/24 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu
ipt -t mangle -A MU_HOME_MSS -i ts-home -o br-lan -s 192.168.50.0/24 -d 192.168.3.0/24 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu
ipt -t filter -I FORWARD 1 -j MU_HOME_FWD
ipt -t filter -I INPUT 1 -j MU_HOME_IN
ipt -t mangle -I FORWARD 1 -j MU_HOME_MSS
trap - EXIT INT TERM
