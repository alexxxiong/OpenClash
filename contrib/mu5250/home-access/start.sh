#!/bin/sh
set -eu
base=/data/tailscale-home
[ ! -e "$base/disabled" ] || exit 0
umask 077
mkdir -p "$base/run" "$base/state"
exec 9>"$base/run/start.lock"
flock -x 9
owned() {
    [ -s "$base/run/tailscaled.pid" ] || return 1
    p=$(cat "$base/run/tailscaled.pid")
    case "$p" in ''|*[!0-9]*) return 1 ;; esac
    [ "$(readlink /proc/"$p"/exe 2>/dev/null)" = "$base/bin/tailscaled" ]
}
if ! owned; then
    (exec 9>&-; exec "$base/bin/tailscaled" --state="$base/state/tailscaled.state" \
        --socket="$base/run/tailscaled.sock" --tun=ts-home --port=41641) \
        >>"$base/run/tailscaled.log" 2>&1 </dev/null &
    printf '%s\n' "$!" > "$base/run/tailscaled.pid"
fi
# The watcher installs only its own rules after login and the specific home route.
if [ -s "$base/run/watch.pid" ]; then
    p=$(cat "$base/run/watch.pid")
    case "$p" in ''|*[!0-9]*) exit 73 ;; esac
    if [ -r /proc/"$p"/cmdline ] && tr '\000' ' ' < /proc/"$p"/cmdline | grep -q "$base/watch.sh"; then
        exit 0
    fi
fi
(exec 9>&-; exec /bin/sh "$base/watch.sh") >>"$base/run/watch.log" 2>&1 </dev/null &
printf '%s\n' "$!" > "$base/run/watch.pid"
