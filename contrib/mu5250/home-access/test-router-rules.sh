#!/bin/bash
set -euo pipefail
test "$(readlink /proc/self/ns/net)" != "${MU_HOME_HOST_NS:?must run in an isolated network namespace}"
source_script=$1
lab=$(mktemp -d /opt/mu5250-home/tests/lab.XXXXXXXX)
trap 'case "$lab" in /opt/mu5250-home/tests/lab.*) rm -rf -- "$lab" ;; esac' EXIT
export MU_SITE_RUN=$lab/run
ip link add br0 type dummy
ip addr add 192.168.50.1/24 dev br0
ip link set br0 up
ip link add tailscale0 type dummy
ip addr add 100.116.235.22/32 dev tailscale0
ip link set tailscale0 up
ip route add 192.168.3.0/24 dev tailscale0
iptables -t nat -A POSTROUTING ! -s 192.168.50.1/32 -o tailscale0 -m comment --comment tailscale_rule -j MASQUERADE
iptables -t mangle -N OIXCLASH_PROXY
iptables -t mangle -A PREROUTING -j OIXCLASH_PROXY
iptables -t mangle -A OIXCLASH_PROXY -d 192.168.0.0/16 -j RETURN
snapshot() { iptables-save | sed '/^#/d'; }
snapshot > "$lab/before"
clean() { snapshot > "$lab/current"; diff -u "$lab/before" "$lab/current"; test ! -e "$MU_SITE_RUN/rules.owned"; }
/bin/sh "$source_script" start
test "$(iptables -t nat -S POSTROUTING | sed -n 2p)" = '-A POSTROUTING -j MU_SITE_NAT'
iptables -t nat -C MU_SITE_NAT -o tailscale0 -s 192.168.50.0/24 -d 192.168.3.0/24 -j ACCEPT
iptables -t mangle -C MU_SITE_MSS -i br0 -o tailscale0 -s 192.168.50.0/24 -d 192.168.3.0/24 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu
iptables -t mangle -C MU_SITE_MSS -i tailscale0 -o br0 -s 192.168.3.0/24 -d 192.168.50.0/24 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu
echo 'PASS scoped no-SNAT precedes plugin MASQUERADE; bidirectional MSS valid'
snapshot > "$lab/installed"
/bin/sh "$source_script" start
snapshot > "$lab/repeated"
cmp "$lab/installed" "$lab/repeated"
/bin/sh "$source_script" stop
/bin/sh "$source_script" stop
clean
echo 'PASS repeated start/stop preserves plugin NAT and oixClash'
ip route del 192.168.3.0/24 dev tailscale0
if /bin/sh "$source_script" start; then exit 1; fi
clean
ip route add 192.168.3.0/24 dev tailscale0
echo 'PASS absent route refuses without mutation'
iptables -t nat -N MU_SITE_NAT
iptables -t nat -A MU_SITE_NAT -j RETURN
snapshot > "$lab/collision"
if /bin/sh "$source_script" start; then exit 1; fi
snapshot > "$lab/current"
cmp "$lab/collision" "$lab/current"
test ! -e "$MU_SITE_RUN/rules.owned"
iptables -t nat -F MU_SITE_NAT
iptables -t nat -X MU_SITE_NAT
clean
echo 'PASS foreign chain collision remains unchanged'
mkdir -p "$lab/bin"
cat > "$lab/bin/iptables" <<'EOF'
#!/bin/sh
case "$*" in '-t mangle -I FORWARD 1 -j MU_SITE_MSS') exit 1 ;; esac
exec /usr/sbin/iptables "$@"
EOF
chmod 700 "$lab/bin/iptables"
if MU_SITE_TEST_BIN="$lab/bin" /bin/sh "$source_script" start; then exit 1; fi
clean
echo 'PASS partial install rolls back owned changes'
/bin/sh "$source_script" start
iptables -t nat -D POSTROUTING -j MU_SITE_NAT
/bin/sh "$source_script" start
iptables -t nat -C POSTROUTING -j MU_SITE_NAT
/bin/sh "$source_script" stop
clean
echo 'PASS missing hook repaired without duplicates'
