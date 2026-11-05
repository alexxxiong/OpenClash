#!/bin/sh
# A leftover UNIX socket path does not prove that its daemon accepts requests.
set -eu
phase=${1:-}
socket=${2:-}
limit=${3:-30}
ubus=${MU_OC_UBUS:-/bin/ubus}
proc=${MU_OC_PROC_ROOT:-/proc}
case "$phase" in bus|rpcd) ;; *) exit 64 ;; esac
case "$limit" in ''|*[!0-9]*) exit 64 ;; esac
[ -n "$socket" ] && [ "$limit" -gt 0 ] || exit 64
started=$(cut -d. -f1 "$proc/uptime")
deadline=$((started + limit))

available() {
    if [ "$phase" = bus ]; then
        "$ubus" -t 1 -s "$socket" list >/dev/null 2>&1
    else
        session=$("$ubus" -t 1 -s "$socket" list session 2>/dev/null) || return 1
        [ "$session" = session ] || return 1
        board=$("$ubus" -t 1 -s "$socket" call system board '{}' 2>/dev/null) || return 1
        kernel=$(printf '%s\n' "$board" | jsonfilter -e '@.kernel') || return 1
        [ -n "$kernel" ]
    fi
}

while :; do
    if available; then echo "MU5250: private $phase is ready."; exit 0; fi
    now=$(cut -d. -f1 "$proc/uptime")
    [ "$now" -lt "$deadline" ] || { echo "MU5250: private $phase readiness timed out." >&2; exit 1; }
    sleep 1
done
