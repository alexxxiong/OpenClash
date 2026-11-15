#!/bin/sh
set -eu
base=/data/tailscale-home
while :; do
    p=$(cat "$base/run/tailscaled.pid" 2>/dev/null || true)
    case "$p" in ''|*[!0-9]*) break ;; esac
    [ "$(readlink /proc/"$p"/exe 2>/dev/null)" = "$base/bin/tailscaled" ] || break
    if ip -4 route get 192.168.50.20 2>/dev/null | grep -q 'dev ts-home'; then
        /bin/sh "$base/rules.sh" start || true
    elif [ -f "$base/run/rules.owned" ]; then
        /bin/sh "$base/rules.sh" stop || true
    fi
    if [ -f "$base/run/tailscaled.log" ] && [ "$(wc -c < "$base/run/tailscaled.log")" -gt 4194304 ]; then
        : > "$base/run/tailscaled.log"
    fi
    sleep 5
done
/bin/sh "$base/rules.sh" stop || true
