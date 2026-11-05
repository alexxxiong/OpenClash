#!/bin/sh
# The controller starts before the proxy listeners and TUN on a cold start.
set -eu
base=${MU_OC_BASE:-/data/openclash}
proc=${MU_OC_PROC_ROOT:-/proc}
sys=${MU_OC_SYS_ROOT:-/sys}
mode=${1:-check}
pid=${2:-}
limit=${3:-120}
case "$pid:$limit" in *[!0-9:]*|:*|*:) exit 64 ;; esac
[ "$pid" -gt 0 ] && [ "$limit" -gt 0 ] || exit 64
case "$mode" in check|wait) ;; *) exit 64 ;; esac
engine=$(uci -q get openclash.config.mu5250_udp_engine || echo tproxy)
controller=$(uci -q get openclash.config.cn_port || echo 9091)
secret=$(uci -q get openclash.config.dashboard_password)
redir=$(uci -q get openclash.config.proxy_port || echo 7892)
mixed=$(uci -q get openclash.config.mixed_port || echo 7893)
dns=$(uci -q get openclash.config.dns_port || echo 7874)
tproxy=$(uci -q get openclash.config.tproxy_port || echo 7895)
for port in "$controller" "$redir" "$mixed" "$dns" "$tproxy"; do
    case "$port" in ''|*[!0-9]*) exit 64 ;; esac
    [ "$port" -gt 0 ] && [ "$port" -le 65535 ] || exit 64
done

socket_ready() {
    hex=$(printf '%04X' "$2")
    awk -v suffix=":$hex" -v state="$3" '
        FNR>1 && substr($2,length($2)-4)==suffix && $4==state {found=1}
        END {exit found ? 0 : 1}' "$proc/net/$1" "$proc/net/${1}6"
}

check_ready() {
    phase=waiting-for-core-exec
    [ "$(readlink "$proc/$pid/exe" 2>/dev/null)" = "$base/etc/core/clash_meta" ] || return 1
    phase=waiting-for-controller
    printf 'Authorization: Bearer %s\n' "$secret" | curl -fsS --noproxy '*' -m 1 -H @- "http://127.0.0.1:$controller/version" >/dev/null 2>&1 || return 1
    phase=waiting-for-proxy-listeners
    socket_ready tcp "$redir" 0A && socket_ready tcp "$mixed" 0A || return 1
    phase=waiting-for-dns-listeners
    socket_ready tcp "$dns" 0A && socket_ready udp "$dns" 07 || return 1
    if [ "$engine" = tun ]; then
        phase=waiting-for-tun
        [ -r "$sys/class/net/mu-oc-tun/flags" ] || return 1
        flags=$(cat "$sys/class/net/mu-oc-tun/flags")
        [ "$((flags & 1))" = 1 ] || return 1
    else
        phase=waiting-for-tproxy-listener
        socket_ready udp "$tproxy" 07 || return 1
    fi
    phase=ready
}

started=$(cut -d. -f1 "$proc/uptime")
deadline=$((started + limit))
last_phase=
while :; do
    if check_ready; then
        if [ "$mode" = wait ]; then
            printf 'ready\n' > "$base/run/startup-state"
            echo 'MU5250: controller, proxy listeners, DNS and UDP interface are ready.'
        fi
        exit 0
    fi
    [ "$mode" != check ] || exit 1
    if [ "$phase" != "$last_phase" ]; then
        printf '%s\n' "$phase" > "$base/run/startup-state"
        echo "MU5250: $phase"
        last_phase=$phase
    fi
    [ -d "$proc/$pid" ] || { echo 'MU5250: core exited during startup.' >&2; exit 1; }
    now=$(cut -d. -f1 "$proc/uptime")
    [ "$now" -lt "$deadline" ] || { echo "MU5250: startup readiness timed out at $phase." >&2; exit 1; }
    sleep 1
done
