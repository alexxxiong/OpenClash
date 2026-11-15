#!/bin/sh
set -eu
base=/jffs/softcenter/mu5250-home-site
trap '"$base/rules.sh" stop' EXIT
trap 'exit 0' TERM INT
while :; do
    if [ "$(dbus get tailscale_enable)" = 1 ] && ip -4 route get 192.168.3.1 2>/dev/null | grep -q 'dev tailscale0'; then
        "$base/rules.sh" start || true
    else
        "$base/rules.sh" stop || true
    fi
    sleep 5
done
