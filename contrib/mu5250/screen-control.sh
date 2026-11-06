#!/bin/sh
# Local, root-only adapter. No HTTP listener, firmware service, or UI hooks.
set -u
umask 077
export PATH=${MU_OC_TEST_BIN:+$MU_OC_TEST_BIN:}/usr/bin:/usr/sbin:/bin:/sbin
export LD_LIBRARY_PATH=/usr/lib:/lib
base=${MU_OC_BASE:-/data/openclash}
proc=${MU_OC_PROC_ROOT:-/proc}
namespace=${MU_OC_NAMESPACE:-$base/bin/oc-namespace}
service=${MU_OC_SERVICE:-$base/bin/openclash-service.sh}
ready=${MU_OC_READY:-$base/bin/core-ready.sh}
budget=${MU_OC_STATUS_TIMEOUT:-8}
run=$base/run
lock=$run/screen-control.lock
record=$run/screen-control.state

case "$base:$proc:$namespace:$service:$ready" in *'/../'*|*'/./'*) exit 64 ;; esac
for path in "$base" "$proc" "$namespace" "$service" "$ready"; do
    case "$path" in /*) ;; *) exit 64 ;; esac
done
case "$budget" in ''|*[!0-9]*|0*) exit 64 ;; esac
[ "${#budget}" -le 2 ] && [ "$budget" -le 30 ] || exit 64
[ "$#" = 1 ] || exit 64
case "$1" in status|on|off) action=$1 ;; *) exit 64 ;; esac

valid_pid() {
    case "$1" in ''|0*|*[!0-9]*) return 1 ;; esac
    [ "${#1}" -le 10 ] && [ "$1" -gt 1 ] && [ "$1" -le 2147483647 ]
}

namespace_ok() {
    ns_pid=$(cat "$run/namespace.pid" 2>/dev/null) || return 1
    valid_pid "$ns_pid" || return 1
    [ -r "$proc/$ns_pid/cmdline" ] || return 1
    # Match the complete argv vector, not a substring or a later argument to
    # another program. This is exactly how start-openclash.sh launches it.
    tr '\000' '\n' < "$proc/$ns_pid/cmdline" |
        awk -v expected="$base/bin/private-init.sh" '
            NR==1 { shell=($0=="/bin/sh") }
            NR==2 { script=($0==expected) }
            END { exit !(NR==2 && shell && script) }'
}

monotonic() {
    read -r seconds rest < "$proc/uptime" || return 1
    seconds=${seconds%%.*}
    case "$seconds" in ''|*[!0-9]*) return 1 ;; esac
    [ "${#seconds}" -le 10 ] || return 1
    printf '%s\n' "$seconds"
}

# Read probes never create temporary files. A stuck external probe is killed;
# a shared monotonic deadline prevents multiplying that delay by probe count.
probe() (
    exec 9>&-
    now=$(monotonic) || exit 124
    [ "$now" -lt "$deadline" ] || exit 124
    "$@" 9>&- 2>/dev/null &
    child=$!
    (
        sleep 2
        kill -TERM "$child" 2>/dev/null || exit 0
        sleep 1
        kill -KILL "$child" 2>/dev/null || true
    ) </dev/null >/dev/null 2>&1 &
    timer=$!
    trap 'kill -TERM "$child" "$timer" 2>/dev/null; exit 124' HUP INT TERM
    wait "$child"; result=$?
    kill "$timer" 2>/dev/null || true
    wait "$timer" 2>/dev/null || true
    exit "$result"
)

private_probe() {
    namespace_ok || return 1
    probe "$namespace" enter "$ns_pid" "$@"
}

get_config() {
    private_probe uci -q get "openclash.config.$1"
}

has_line() { printf '%s\n' "$1" | grep -Fqx -- "$2"; }
has_policy() {
    printf '%s\n' "$1" | awk '
        $1=="17250:" && $2=="from" && $3=="all" && $4=="fwmark" &&
        ($5=="0x7250" || $5=="0x7250/0xffffffff") &&
        $6=="lookup" && $7==7250 && NF==7 { n++ }
        END { exit n!=1 }'
}
reserved_routes() {
    printf '%s\n' "$1" | awk '
        { for(i=1;i<NF;i++) if($i=="table" && $(i+1)==7250) {print; break} }'
}

# Inspect the real kernel, not network-rules.sh status (which reads a marker
# and acquires/creates a lock). All commands here are reads, even when off.
inspect_rules() {
    rules=false
    rules_present=false
    [ ! -e "$run/rules.owned" ] || rules_present=true
    metadata=true
    [ "$(cat "$run/rules.scope" 2>/dev/null)" = "$scope" ] || metadata=false
    [ "$(cat "$run/rules.engine" 2>/dev/null)" = "$engine" ] || metadata=false
    iface=$(cat "$run/rules.interface" 2>/dev/null) || iface=
    case "$iface" in ''|*[!a-zA-Z0-9_.:-]*) metadata=false ;; esac
    families=$(cat "$run/rules.families" 2>/dev/null) || families=
    expected_families=4
    [ "$scope" != all ] || expected_families=$(printf '4\n6')
    [ "$families" = "$expected_families" ] || metadata=false
    [ -f "$run/rules.owned" ] || metadata=false
    complete=true
    for family in 4 6; do
        tool=iptables
        [ "$family" = 4 ] || tool=ip6tables
        nat=$(probe "$tool" -w 1 -t nat -S) || return 1
        udp=$(probe "$tool" -w 1 -t mangle -S) || return 1
        forward=$(probe "$tool" -w 1 -t filter -S) || return 1
        policies=$(probe ip -"$family" rule show) || return 1
        routes=$(probe ip -"$family" route show table all) || return 1
        routes=$(reserved_routes "$routes")
        present=false
        if printf '%s\n' "$nat" "$udp" "$forward" |
            grep -Eq -- '(^-N MU_OC_(NAT|UDP|FWD)$| -j MU_OC_(NAT|UDP|FWD)( |$))'; then
            present=true
        fi
        if printf '%s\n' "$policies" | grep -Eq '(^17250:| lookup 7250( |$))'; then present=true; fi
        [ -z "$routes" ] || present=true
        [ "$present" = false ] || rules_present=true
        if [ "$scope" = off ] || { [ "$scope" != all ] && [ "$family" = 6 ]; }; then
            [ "$present" = false ] || complete=false
            continue
        fi
        has_line "$nat" "-A PREROUTING -i $iface -j MU_OC_NAT" || complete=false
        has_line "$udp" "-A PREROUTING -i $iface -j MU_OC_UDP" || complete=false
        has_line "$nat" "-A MU_OC_NAT -p udp -m udp --dport 53 -j REDIRECT --to-ports $dns" || complete=false
        has_line "$nat" "-A MU_OC_NAT -p tcp -m tcp --dport 53 -j REDIRECT --to-ports $dns" || complete=false
        has_line "$nat" "-A MU_OC_NAT -p tcp -j REDIRECT --to-ports $redir" || complete=false
        has_line "$udp" '-A MU_OC_UDP -p udp -m udp --dport 53 -j RETURN' || complete=false
        has_policy "$policies" || complete=false
        if [ "$engine" = tun ]; then
            has_line "$udp" '-A MU_OC_UDP -p udp -j MARK --set-xmark 0x7250/0xffffffff' || complete=false
            has_line "$udp" '-A MU_OC_UDP -p udp -j ACCEPT' || complete=false
            has_line "$forward" '-A FORWARD -j MU_OC_FWD' || complete=false
            has_line "$forward" "-A MU_OC_FWD -i $iface -o mu-oc-tun -p udp -m mark --mark 0x7250 -j ACCEPT" || complete=false
            has_line "$forward" "-A MU_OC_FWD -i mu-oc-tun -o $iface -p udp -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT" || complete=false
            printf '%s\n' "$routes" | grep -Eq '^default dev mu-oc-tun table 7250( |$)' || complete=false
        else
            target=192.168.3.1
            [ "$family" = 4 ] || target=::1
            has_line "$udp" "-A MU_OC_UDP -p udp -j TPROXY --on-port $tproxy --on-ip $target --tproxy-mark 0x7250/0xffffffff" || complete=false
            printf '%s\n' "$routes" | grep -Eq '^local (default|0.0.0.0/0|::/0) dev lo table 7250( |$)' || complete=false
        fi
        if [ "$scope" = test-ip ]; then
            test_ip=$(get_config mu5250_test_ip) || return 1
            case "$test_ip" in 192.168.3.[0-9]|192.168.3.[0-9][0-9]|192.168.3.[0-9][0-9][0-9]) ;; *) return 1 ;; esac
            octet=${test_ip##*.}
            [ "$octet" -gt 0 ] && [ "$octet" -lt 255 ] || return 1
            has_line "$nat" "-A MU_OC_NAT ! -s $test_ip/32 -j RETURN" || complete=false
            has_line "$udp" "-A MU_OC_UDP ! -s $test_ip/32 -j RETURN" || complete=false
        fi
    done
    if [ "$scope" = off ]; then
        [ "$rules_present" = false ] && rules=true
    elif [ "$metadata" = true ] && [ "$complete" = true ]; then
        rules=true
    fi
    return 0
}

observe() {
    enabled=null core=false core_ready=false clock=false rules=false
    rules_present=false scope=unknown reason=namespace-unavailable inspected=false
    namespace_ok || return 1
    started=$(monotonic) || { reason=inspection-failed; return 1; }
    deadline=$((started + budget))
    value=$(get_config enable) || { reason=config-unavailable; return 1; }
    case "$value" in 0) enabled=false ;; 1) enabled=true ;; *) reason=invalid-enable; return 1 ;; esac
    scope=$(get_config mu5250_proxy_scope) || { reason=config-unavailable; return 1; }
    engine=$(get_config mu5250_udp_engine) || { reason=config-unavailable; return 1; }
    case "$scope:$engine" in off:tun|off:tproxy|all:tun|all:tproxy|test-ip:tun|test-ip:tproxy) ;; *) reason=invalid-routing-config; return 1 ;; esac
    redir=$(get_config proxy_port) && dns=$(get_config dns_port) && tproxy=$(get_config tproxy_port) || { reason=config-unavailable; return 1; }
    for port in "$redir" "$dns" "$tproxy"; do
        case "$port" in ''|*[!0-9]*) reason=invalid-port; return 1 ;; esac
        [ "${#port}" -le 5 ] && [ "$port" -gt 0 ] && [ "$port" -le 65535 ] || { reason=invalid-port; return 1; }
    done
    core_pid=$(cat "$run/core.pid" 2>/dev/null) || core_pid=
    if valid_pid "$core_pid" && [ "$(readlink "$proc/$core_pid/exe" 2>/dev/null)" = "$base/etc/core/clash_meta" ]; then
        core=true
        private_probe /bin/sh "$ready" check "$core_pid" >/dev/null && core_ready=true
        [ "$(cat "$run/clock-ready.pid" 2>/dev/null)" != "$core_pid" ] || clock=true
    fi
    inspect_rules || { reason=inspection-failed; return 1; }
    # Reject a namespace/core which disappeared or changed during the probes.
    namespace_ok || { reason=namespace-unavailable; return 1; }
    if [ "$core" = true ]; then
        [ "$(cat "$run/core.pid" 2>/dev/null)" = "$core_pid" ] &&
            [ "$(readlink "$proc/$core_pid/exe" 2>/dev/null)" = "$base/etc/core/clash_meta" ] || { reason=core-changed; return 1; }
    fi
    inspected=true
    return 0
}

# Open an existing lock read-only: status never creates/truncates any file.
lock_busy() (
    [ -e "$1" ] || exit 1
    exec 9<"$1" || exit 0
    flock -n -s 9 >/dev/null 2>&1 && exit 1
    exit 0
)

load_record() {
    phase=unknown
    { read -r phase < "$record"; } 2>/dev/null || phase=unknown
    case "$phase" in starting|stopping|done|failed) ;; *) phase=unknown ;; esac
}

classify() {
    state=failed busy=false
    if lock_busy "$lock"; then
        busy=true
        load_record
        case "$phase" in
            starting|stopping) state=$phase; reason=control-in-progress ;;
            *) reason=control-in-progress ;;
        esac
    elif [ "$inspected" != true ]; then
        return 0
    elif [ "$enabled" = false ] && [ "$core" = false ] && [ "$rules_present" = false ]; then
        state=disabled; reason=disabled
    elif [ "$enabled" = true ] && [ "$core" = true ] && [ "$core_ready" = true ] && [ "$clock" = true ] && [ "$rules" = true ]; then
        state=running; reason=ready
    elif lock_busy "$run/service.lock"; then
        busy=true
        if [ "$enabled" = true ]; then state=starting; else state=stopping; fi
        reason=service-in-progress
    else
        load_record
        if [ "$phase" = failed ] || [ "$phase" = starting ] || [ "$phase" = stopping ]; then reason=last-control-failed
        elif [ "$enabled" = false ]; then reason=disabled-with-residue
        elif [ "$core" = false ]; then reason=core-not-running
        elif [ "$core_ready" = false ]; then reason=core-not-ready
        elif [ "$clock" = false ]; then reason=clock-not-ready
        else reason=rules-not-ready
        fi
    fi
}

emit() {
    # Every string is an internal constant/enum; no config, PID, or secrets.
    printf '{"state":"%s","enabled":%s,"core":%s,"ready":%s,"clock":%s,"rules":%s,"busy":%s,"reason":"%s"}\n' \
        "$state" "$enabled" "$core" "$core_ready" "$clock" "$rules" "$busy" "$reason"
}

if [ "$action" = status ]; then
    observe || true
    classify
    emit
    exit 0
fi

namespace_ok || { printf '{"accepted":false,"reason":"namespace-unavailable"}\n'; exit 69; }
mkdir -p "$run" || exit 73
exec 9>"$lock" || exit 73
flock -n -x 9 || { printf '{"accepted":false,"reason":"busy"}\n'; exit 75; }

save_phase() {
    printf '%s\n' "$1" > "$record.$$" && mv -f "$record.$$" "$record"
}

mutate() (
    # Only the worker owns the bridge lock. Never leak it to the core/watchdog.
    exec 9>&-
    namespace_ok || exit 1
    "$namespace" enter "$ns_pid" /bin/sh "$service" "$1" >/dev/null 2>&1
)

worker() {
    trap '' HUP
    trap 'save_phase failed; exit 1' INT TERM
    if [ "$action" = on ]; then
        mutate enable && mutate start || { save_phase failed; return 1; }
    else
        mutate disable && mutate stop || { save_phase failed; return 1; }
    fi
    observe || { save_phase failed; return 1; }
    if [ "$action" = on ]; then
        [ "$enabled" = true ] && [ "$core" = true ] && [ "$core_ready" = true ] &&
            [ "$clock" = true ] && [ "$rules" = true ] || { save_phase failed; return 1; }
    else
        [ "$enabled" = false ] && [ "$core" = false ] && [ "$rules_present" = false ] || { save_phase failed; return 1; }
    fi
    save_phase done
}

phase=starting
[ "$action" = on ] || phase=stopping
save_phase "$phase" || exit 73
( worker ) </dev/null >/dev/null 2>&1 &
exec 9>&-
printf '{"accepted":true,"action":"%s","state":"%s"}\n' "$action" "$phase"
