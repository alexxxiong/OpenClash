#!/bin/sh
# Application-only: use the core's NTP clock, then discard pre-sync DNS answers.
set -eu
base=${MU_OC_BASE:-/data/openclash}
proc=${MU_OC_PROC_ROOT:-/proc}
log=${MU_OC_LOG:-/tmp/openclash.log}
expected=${MU_OC_CORE_EXE:-$base/etc/core/clash_meta}
umask 077
mkdir -p "$base/run"

event() {
    [ -r "$log" ] || return 0
    awk '/System clock is .* NTP time by / { last=$0 } END { if(last!="") print last }' "$log"
}

case "${1:-}" in
    prepare)
        event > "$base/run/clock-event"
        rm -f "$base/run/clock-ready.pid"
        exit 0 ;;
    step|wait) ;;
    *) exit 64 ;;
esac
mode=$1
pid=${2:-}
case "$pid" in ''|*[!0-9]*) exit 64 ;; esac

step() {
    [ "$(cat "$base/run/core.pid" 2>/dev/null)" = "$pid" ] || return 1
    [ "$(readlink "$proc/$pid/exe" 2>/dev/null)" = "$expected" ] || return 1
    latest=$(event)
    previous=$(cat "$base/run/clock-event" 2>/dev/null || true)
    if [ -n "$latest" ] && [ "$latest" != "$previous" ]; then
        port=${MU_OC_CONTROLLER:-$(uci -q get openclash.config.cn_port)}
        case "$port" in ''|*[!0-9]*) return 1 ;; esac
        [ "$port" -gt 0 ] && [ "$port" -le 65535 ] || return 1
        if [ -n "${MU_OC_SECRET_FILE:-}" ]; then
            secret=$(cat "$MU_OC_SECRET_FILE")
        else
            secret=$(uci -q get openclash.config.dashboard_password)
        fi
        [ -n "$secret" ] || return 1
        printf 'Authorization: Bearer %s\n' "$secret" |
            curl -fsS --noproxy '*' -m 3 -X POST -H @- "http://127.0.0.1:$port/cache/dns/flush" >/dev/null 2>&1 || return 1
        printf '%s\n' "$latest" > "$base/run/clock-event"
        printf '%s\n' "$pid" > "$base/run/clock-ready.pid"
        echo 'MU5250: core NTP synchronized; pre-sync DNS cache cleared.' >> "$log"
    fi
    [ "$(cat "$base/run/clock-ready.pid" 2>/dev/null)" = "$pid" ]
}

if [ "$mode" = step ]; then step; exit $?; fi
limit=${3:-30}
case "$limit" in ''|*[!0-9]*) exit 64 ;; esac
started=$(cut -d. -f1 "$proc/uptime")
deadline=$((started + limit))
while ! step; do
    [ -d "$proc/$pid" ] || exit 1
    now=$(cut -d. -f1 "$proc/uptime")
    [ "$now" -lt "$deadline" ] || { echo 'MU5250: core NTP readiness timed out.' >&2; exit 1; }
    sleep 1
done
