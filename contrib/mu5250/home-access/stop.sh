#!/bin/sh
set -eu
base=/data/tailscale-home
umask 077
touch "$base/disabled"
if [ -s "$base/run/watch.pid" ]; then
    p=$(cat "$base/run/watch.pid")
    case "$p" in ''|*[!0-9]*) exit 73 ;; esac
    if [ -r /proc/"$p"/cmdline ] && tr '\000' ' ' < /proc/"$p"/cmdline | grep -q "$base/watch.sh"; then
        kill -TERM "$p"
    fi
fi
/bin/sh "$base/rules.sh" stop
if [ -s "$base/run/tailscaled.pid" ]; then
    p=$(cat "$base/run/tailscaled.pid")
    case "$p" in ''|*[!0-9]*) exit 73 ;; esac
    if [ "$(readlink /proc/"$p"/exe 2>/dev/null)" = "$base/bin/tailscaled" ]; then
        "$base/bin/tailscale" --socket="$base/run/tailscaled.sock" down || true
        kill -TERM "$p"
    fi
fi
