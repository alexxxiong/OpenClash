#!/bin/sh
# Shared, idempotent dispatcher for the early screen entry and the late app entry.
set -eu
umask 077
base=/data/openclash
export PATH=/usr/bin:/usr/sbin:/bin:/sbin
[ -x "$base/bin/screen-network-watch.sh" ] && [ -s "$base/lib/screen-network.manifest" ] || exit 0
mkdir -p "$base/run"
exec 9>"$base/run/screen-start.lock"
flock -n -x 9 || exit 0
owned_watcher() {
    p=$(cat "$base/run/screen-network-watch.pid" 2>/dev/null) || return 1
    case "$p" in ''|0*|*[!0-9]*) return 1 ;; esac
    [ -r "/proc/$p/cmdline" ] || return 1
    tr '\000' '\n' < "/proc/$p/cmdline" |
        awk 'NR==1 {a=($0=="/bin/sh")} NR==2 {b=($0=="/data/openclash/bin/screen-network-watch.sh")} END {exit !(NR==2&&a&&b)}'
}
if owned_watcher; then
    echo "Screen watcher already running (PID $p)"
    exit 0
fi
(exec 9>&-; exec nohup /bin/sh "$base/bin/screen-network-watch.sh") >"$base/run/screen-network-watch.log" 2>&1 </dev/null &
# Publish/observe the watcher's own PID while dispatch is serialized. Neither
# child keeps this lock, so the later LuCI entry cannot stall or truncate logs.
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    owned_watcher && { echo "Screen watcher dispatched (PID $p)"; exit 0; }
    sleep 0.05 9>&-
done
echo 'Screen watcher dispatched; inspect its log if PID publication is delayed'
exit 0
