#!/bin/bash
set -euo pipefail
test "$(readlink /proc/self/ns/net)" != "${MU_HOME_HOST_NS:?must run in an isolated network namespace}"
source_script=$1
lab=$(mktemp -d /opt/mu5250-home/tests/lab.XXXXXXXX)
trap 'case "$lab" in /opt/mu5250-home/tests/lab.*) rm -rf -- "$lab" ;; esac' EXIT
export MU_HOME_BASE=$lab/base
mkdir -p "$MU_HOME_BASE/run"
ip link add br-lan type dummy
ip addr add 192.168.3.1/24 dev br-lan
ip link set br-lan up
ip link add ts-home type dummy
ip addr add 100.101.102.103/32 dev ts-home
ip link set ts-home up
ip route add 192.168.50.0/24 dev ts-home
iptables -P FORWARD DROP
iptables -N MU_OC_FWD
iptables -A MU_OC_FWD -j RETURN
iptables -A FORWARD -j MU_OC_FWD
iptables -t nat -S >/dev/null
iptables -t nat -N HOME_TEST_FOREIGN
iptables -t nat -A POSTROUTING -j HOME_TEST_FOREIGN
iptables -t nat -A HOME_TEST_FOREIGN -j RETURN
iptables -t mangle -N HOME_TEST_MANGLE
iptables -t mangle -A FORWARD -j HOME_TEST_MANGLE
iptables -t mangle -A HOME_TEST_MANGLE -j RETURN
snapshot() { iptables-save | sed '/^#/d'; }
snapshot > "$lab/before"
clean() { snapshot > "$lab/current"; diff -u "$lab/before" "$lab/current"; test ! -e "$MU_HOME_BASE/run/rules.owned"; }
/bin/sh "$source_script" start
iptables -C MU_HOME_FWD -i br-lan -o ts-home -s 192.168.3.0/24 -d 192.168.50.0/24 -j ACCEPT
iptables -C MU_HOME_FWD -i ts-home -o br-lan -s 192.168.50.0/24 -d 192.168.3.0/24 -j ACCEPT
iptables -C MU_HOME_IN -i ts-home -s 192.168.50.0/24 -d 192.168.3.1/32 -j ACCEPT
iptables -t mangle -C MU_HOME_MSS -i br-lan -o ts-home -s 192.168.3.0/24 -d 192.168.50.0/24 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu
iptables -t mangle -C MU_HOME_MSS -i ts-home -o br-lan -s 192.168.50.0/24 -d 192.168.3.0/24 -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu
! iptables-save -t nat | grep -q MU_HOME
test "$(iptables -S MU_HOME_FWD | wc -l)" -eq 3
test "$(iptables -S MU_HOME_IN | wc -l)" -eq 3
test "$(iptables -t mangle -S MU_HOME_MSS | wc -l)" -eq 3
echo 'PASS both subnets and router input accepted, MSS clamped, no SNAT'
snapshot > "$lab/installed"
/bin/sh "$source_script" start
snapshot > "$lab/repeated"
cmp "$lab/installed" "$lab/repeated"
/bin/sh "$source_script" stop
/bin/sh "$source_script" stop
clean
echo 'PASS repeated start/stop preserves original OpenClash and firewall'
ip route del 192.168.50.0/24 dev ts-home
if /bin/sh "$source_script" start; then exit 1; fi
clean
ip route add 192.168.50.0/24 dev ts-home
echo 'PASS absent home route refuses without installing rules'
iptables -t mangle -N MU_HOME_MSS
iptables -t mangle -A MU_HOME_MSS -j RETURN
snapshot > "$lab/collision"
if /bin/sh "$source_script" start; then exit 1; fi
snapshot > "$lab/current"
cmp "$lab/collision" "$lab/current"
test ! -e "$MU_HOME_BASE/run/rules.owned"
iptables -t mangle -F MU_HOME_MSS
iptables -t mangle -X MU_HOME_MSS
clean
echo 'PASS foreign chain collision is preserved'
mkdir -p "$lab/bin"
cat > "$lab/bin/iptables" <<'EOF'
#!/bin/sh
case "$*" in '-w 5 -t mangle -I FORWARD 1 -j MU_HOME_MSS') exit 1 ;; esac
exec /usr/sbin/iptables "$@"
EOF
chmod 700 "$lab/bin/iptables"
if MU_HOME_TEST_BIN="$lab/bin" /bin/sh "$source_script" start; then exit 1; fi
clean
echo 'PASS partial install failure rolls back its own changes'
/bin/sh "$source_script" start
iptables -D FORWARD -j MU_HOME_FWD
/bin/sh "$source_script" start
iptables -C FORWARD -j MU_HOME_FWD
/bin/sh "$source_script" stop
clean
echo 'PASS missing owned hook is repaired without duplicates'
