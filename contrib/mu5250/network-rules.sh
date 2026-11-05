#!/bin/sh
# Application-owned LAN hooks only. No firmware firewall/DNS configuration writes.
set -eu
base=${MU_OC_BASE:-/data/openclash}
iface=${MU_OC_LAN:-br-lan}
table=7250
mark=0x7250
pref=17250
nat=MU_OC_NAT
udp=MU_OC_UDP
forward=MU_OC_FWD
mkdir -p "$base/run"
exec 9>"$base/run/network.lock"
flock -x 9

ipt() { iptables -w 5 "$@"; }
ip6t() { ip6tables -w 5 "$@"; }

owned_policy_exists() {
    listed=$(ip -"$1" rule show) || return 2
    printf '%s\n' "$listed" | awk -v p="$pref:" -v t="$table" '
        $1==p && $2=="from" && $3=="all" && $4=="fwmark" && ($5=="0x7250" || $5=="0x7250/0xffffffff") && $6=="lookup" && $7==t && NF==7 {found=1}
        END {exit found ? 0 : 1}'
}
owned_route_exists() {
    listed=$(ip -"$1" route show table all) || return 2
    printf '%s\n' "$listed" | awk -v e="$2" -v t="$table" '
        { reserved=0; for(i=1;i<NF;i++) if($i=="table" && $(i+1)==t) reserved=1 }
        reserved && ((e=="tun" && $1=="default" && $2=="dev" && $3=="mu-oc-tun") || (e!="tun" && $1=="local" && ($2=="default" || $2=="0.0.0.0/0" || $2=="::/0") && $3=="dev" && $4=="lo")) {found=1}
        END {exit found ? 0 : 1}'
}

stop_rules() {
    [ -f "$base/run/rules.owned" ] || return 0
    installed_iface=$(cat "$base/run/rules.interface") || return 1
    installed_families=$(cat "$base/run/rules.families") || return 1
    installed_engine=$(cat "$base/run/rules.engine") || return 1
    failed=0
    for version in $installed_families; do
        tool=ipt
        [ "$version" = 4 ] || tool=ip6t
        detached=1
        while "$tool" -t nat -C PREROUTING -i "$installed_iface" -j "$nat" >/dev/null 2>&1; do
            "$tool" -t nat -D PREROUTING -i "$installed_iface" -j "$nat" || { detached=0; break; }
        done
        while "$tool" -t mangle -C PREROUTING -i "$installed_iface" -j "$udp" >/dev/null 2>&1; do
            "$tool" -t mangle -D PREROUTING -i "$installed_iface" -j "$udp" || { detached=0; break; }
        done
        if [ "$(cat "$base/run/rules.engine" 2>/dev/null)" = tun ]; then
            while "$tool" -t filter -C FORWARD -j "$forward" >/dev/null 2>&1; do
                "$tool" -t filter -D FORWARD -j "$forward" || { detached=0; break; }
            done
        fi
        if [ "$detached" != 1 ]; then failed=1; continue; fi
        if "$tool" -t nat -L "$nat" -n >/dev/null 2>&1; then
            "$tool" -t nat -F "$nat" && "$tool" -t nat -X "$nat" || failed=1
        fi
        if "$tool" -t mangle -L "$udp" -n >/dev/null 2>&1; then
            "$tool" -t mangle -F "$udp" && "$tool" -t mangle -X "$udp" || failed=1
        fi
        if [ "$(cat "$base/run/rules.engine" 2>/dev/null)" = tun ] && "$tool" -t filter -L "$forward" -n >/dev/null 2>&1; then
            "$tool" -t filter -F "$forward" && "$tool" -t filter -X "$forward" || failed=1
        fi
        if ! ip -"$version" rule del fwmark "$mark/0xffffffff" table "$table" pref "$pref" >/dev/null 2>&1; then
            if owned_policy_exists "$version"; then failed=1; else [ "$?" = 1 ] || failed=1; fi
        fi
        if [ "$(cat "$base/run/rules.engine" 2>/dev/null)" = tun ]; then
            if ! ip -"$version" route del default dev mu-oc-tun table "$table" >/dev/null 2>&1; then
                if owned_route_exists "$version" "$installed_engine"; then failed=1; else [ "$?" = 1 ] || failed=1; fi
            fi
        else
            route=0.0.0.0/0
            [ "$version" = 4 ] || route=::/0
            if ! ip -"$version" route del local "$route" dev lo table "$table" >/dev/null 2>&1; then
                if owned_route_exists "$version" "$installed_engine"; then failed=1; else [ "$?" = 1 ] || failed=1; fi
            fi
        fi
    done
    [ "$failed" = 0 ] || return 1
    rm -f "$base/run/rules.owned" "$base/run/rules.scope" "$base/run/rules.interface" "$base/run/rules.families" "$base/run/rules.engine"
}

case "${1:-}" in
    stop) stop_rules; exit 0 ;;
    status) if [ -f "$base/run/rules.scope" ]; then cat "$base/run/rules.scope"; else echo off; fi; exit 0 ;;
    start|reload) ;;
    *) exit 64 ;;
esac

scope=$(uci -q get openclash.config.mu5250_proxy_scope || echo off)
case "$scope" in off|test-ip|all) ;; *) echo 'Invalid proxy scope' >&2; exit 64 ;; esac
if [ "$scope" = off ]; then stop_rules; echo 'Transparent routing is disabled'; exit 0; fi
test -d "/sys/class/net/$iface"
engine=$(uci -q get openclash.config.mu5250_udp_engine || echo tproxy)
case "$engine" in tproxy) ;; tun) test -d /sys/class/net/mu-oc-tun ;; *) exit 64 ;; esac
test_ip=$(uci -q get openclash.config.mu5250_test_ip || echo 192.168.3.120)
case "$test_ip" in 192.168.3.[0-9]|192.168.3.[0-9][0-9]|192.168.3.[0-9][0-9][0-9]) ;; *) exit 64 ;; esac
octet=${test_ip##*.}
[ "$octet" -gt 0 ] && [ "$octet" -lt 255 ] || exit 64
redir=$(uci -q get openclash.config.proxy_port || echo 7892)
tproxy=$(uci -q get openclash.config.tproxy_port || echo 7895)
dns=$(uci -q get openclash.config.dns_port || echo 7874)
for port in "$redir" "$tproxy" "$dns"; do
    case "$port" in ''|*[!0-9]*) exit 64 ;; esac
    [ "$port" -gt 0 ] && [ "$port" -le 65535 ] || exit 64
done

addresses4=$(ip -o -4 addr show) || exit 73
addresses6=
if [ "$scope" = all ]; then addresses6=$(ip -o -6 addr show) || exit 73; fi

for version in 4 6; do
    claimed=0
    old_engine=
    tool=ipt
    [ "$version" = 4 ] || tool=ip6t
    if [ -f "$base/run/rules.owned" ] && grep -qx "$version" "$base/run/rules.families"; then claimed=1; fi
    if [ "$claimed" = 1 ]; then old_engine=$(cat "$base/run/rules.engine") || exit 73; fi
    if [ "$old_engine" != tun ] && "$tool" -t filter -L "$forward" -n >/dev/null 2>&1; then
        echo 'Reserved forwarding chain exists without recorded ownership' >&2
        exit 73
    fi
    if [ "$claimed" = 0 ]; then
        tool=ipt
        [ "$version" = 4 ] || tool=ip6t
        if "$tool" -t nat -L "$nat" -n >/dev/null 2>&1 || "$tool" -t mangle -L "$udp" -n >/dev/null 2>&1 || "$tool" -t filter -L "$forward" -n >/dev/null 2>&1; then
            echo 'Reserved application chain already exists without an ownership record' >&2
            exit 73
        fi
    fi
    policies=$(ip -"$version" rule show) || exit 73
    if printf '%s\n' "$policies" | awk -v p="$pref:" -v own="$claimed" -v t="$table" '
        $1==p { if (!(own==1 && $2=="from" && $3=="all" && $4=="fwmark" && ($5=="0x7250" || $5=="0x7250/0xffffffff") && $6=="lookup" && $7==t && NF==7)) bad=1; n++ }
        END { exit (bad || n>own) ? 0 : 1 }'; then
        echo 'Reserved policy-routing priority is already occupied' >&2
        exit 73
    fi
    routes=$(ip -"$version" route show table all) || exit 73
    if printf '%s\n' "$routes" | awk -v own="$claimed" -v e="$old_engine" -v t="$table" '
        { reserved=0; for(i=1;i<NF;i++) if($i=="table" && $(i+1)==t) reserved=1 }
        reserved {n++; if (!(own==1 && ((e!="tun" && $1=="local" && ($2=="default" || $2=="0.0.0.0/0" || $2=="::/0") && $3=="dev" && $4=="lo") || (e=="tun" && $1=="default" && $2=="dev" && $3=="mu-oc-tun")))) bad=1}
        END {exit (bad || n>own) ? 0 : 1}'; then
        echo 'Reserved policy-routing table is already occupied' >&2
        exit 73
    fi
done
stop_rules
printf '%s\n' "$iface" > "$base/run/rules.interface"
printf '%s\n' "$engine" > "$base/run/rules.engine"
printf '4\n' > "$base/run/rules.families"
[ "$scope" != all ] || printf '6\n' >> "$base/run/rules.families"
: > "$base/run/rules.owned"
trap 'stop_rules' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

ipt -t nat -N "$nat"
ipt -t mangle -N "$udp"
if [ "$scope" = test-ip ]; then
    ipt -t nat -A "$nat" ! -s "$test_ip/32" -j RETURN
    ipt -t mangle -A "$udp" ! -s "$test_ip/32" -j RETURN
fi
# DNS is handled before local-destination bypass; SSH/OpenUI and all other local services bypass.
ipt -t nat -A "$nat" -p udp --dport 53 -j REDIRECT --to-ports "$dns"
ipt -t nat -A "$nat" -p tcp --dport 53 -j REDIRECT --to-ports "$dns"
ipt -t mangle -A "$udp" -p udp --dport 53 -j RETURN
for cidr in 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.168.0.0/16 224.0.0.0/4 240.0.0.0/4; do
    ipt -t nat -A "$nat" -d "$cidr" -j RETURN
    ipt -t mangle -A "$udp" -d "$cidr" -j RETURN
done
for address in $(printf '%s\n' "$addresses4" | awk '{print $4}'); do
    address=${address%/*}
    ipt -t nat -A "$nat" -d "$address/32" -j RETURN
    ipt -t mangle -A "$udp" -d "$address/32" -j RETURN
done
ipt -t nat -A "$nat" -p tcp -j REDIRECT --to-ports "$redir"
if [ "$engine" = tun ]; then
    ipt -t filter -N "$forward"
    ipt -t filter -A "$forward" -i "$iface" -o mu-oc-tun -p udp -m mark --mark "$mark/0xffffffff" -j ACCEPT
    ipt -t filter -A "$forward" -i mu-oc-tun -o "$iface" -p udp -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    ipt -t filter -I FORWARD 1 -j "$forward"
    ipt -t mangle -A "$udp" -p udp -j MARK --set-xmark "$mark/0xffffffff"
    ipt -t mangle -A "$udp" -p udp -j ACCEPT
    ip -4 route add default dev mu-oc-tun table "$table"
else
    ipt -t mangle -A "$udp" -p udp -j TPROXY --on-ip 192.168.3.1 --on-port "$tproxy" --tproxy-mark "$mark/0xffffffff"
    ip -4 route add local 0.0.0.0/0 dev lo table "$table"
fi
ip -4 rule add fwmark "$mark/0xffffffff" table "$table" pref "$pref"
ipt -t nat -I PREROUTING 1 -i "$iface" -j "$nat"
ipt -t mangle -I PREROUTING 1 -i "$iface" -j "$udp"

if [ "$scope" = all ]; then
    ip6t -t nat -N "$nat"
    ip6t -t mangle -N "$udp"
    ip6t -t nat -A "$nat" -p udp --dport 53 -j REDIRECT --to-ports "$dns"
    ip6t -t nat -A "$nat" -p tcp --dport 53 -j REDIRECT --to-ports "$dns"
    ip6t -t mangle -A "$udp" -p udp --dport 53 -j RETURN
    for cidr in ::/128 ::1/128 fc00::/7 fe80::/10 ff00::/8; do
        ip6t -t nat -A "$nat" -d "$cidr" -j RETURN
        ip6t -t mangle -A "$udp" -d "$cidr" -j RETURN
    done
    for address in $(printf '%s\n' "$addresses6" | awk '{print $4}'); do
        address=${address%/*}
        ip6t -t nat -A "$nat" -d "$address/128" -j RETURN
        ip6t -t mangle -A "$udp" -d "$address/128" -j RETURN
    done
    ip6t -t nat -A "$nat" -p tcp -j REDIRECT --to-ports "$redir"
    if [ "$engine" = tun ]; then
        ip6t -t filter -N "$forward"
        ip6t -t filter -A "$forward" -i "$iface" -o mu-oc-tun -p udp -m mark --mark "$mark/0xffffffff" -j ACCEPT
        ip6t -t filter -A "$forward" -i mu-oc-tun -o "$iface" -p udp -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
        ip6t -t filter -I FORWARD 1 -j "$forward"
        ip6t -t mangle -A "$udp" -p udp -j MARK --set-xmark "$mark/0xffffffff"
        ip6t -t mangle -A "$udp" -p udp -j ACCEPT
        ip -6 route add default dev mu-oc-tun table "$table"
    else
        ip6t -t mangle -A "$udp" -p udp -j TPROXY --on-ip ::1 --on-port "$tproxy" --tproxy-mark "$mark/0xffffffff"
        ip -6 route add local ::/0 dev lo table "$table"
    fi
    ip -6 rule add fwmark "$mark/0xffffffff" table "$table" pref "$pref"
    ip6t -t nat -I PREROUTING 1 -i "$iface" -j "$nat"
    ip6t -t mangle -I PREROUTING 1 -i "$iface" -j "$udp"
fi
printf '%s\n' "$scope" > "$base/run/rules.scope"
trap - EXIT HUP INT TERM
echo "Installed application-owned LAN proxy rules: $scope"
