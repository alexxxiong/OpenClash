#!/bin/sh
set -eu
umask 077
base=/data/openclash
mkdir -p "$base/run"
exec 9>"$base/run/namespace-start.lock"
flock -x 9
start_screen() {
    if [ -x "$base/bin/screen-start.sh" ]; then
        (exec 9>&-; exec nohup /bin/sh "$base/bin/screen-start.sh") >>"$base/run/screen-start.log" 2>&1 </dev/null &
    elif [ -x "$base/bin/screen-network-watch.sh" ] && [ -s "$base/lib/screen-network.manifest" ]; then
        (exec 9>&-; exec nohup /bin/sh "$base/bin/screen-network-watch.sh") >"$base/run/screen-network-watch.log" 2>&1 </dev/null &
    fi
}
if [ -s "$base/run/namespace.pid" ]; then
    pid=$(cat "$base/run/namespace.pid")
    case "$pid" in ''|*[!0-9]*) exit 73 ;; esac
    if [ -r /proc/"$pid"/cmdline ] && tr '\000' ' ' < /proc/"$pid"/cmdline | grep -q '/data/openclash/bin/private-init.sh'; then
        echo 'Independent LuCI namespace is already running'
        start_screen
        exit 0
    fi
fi
if [ -s "$base/run/core.pid" ]; then
    pid=$(cat "$base/run/core.pid")
    case "$pid" in ''|*[!0-9]*) exit 73 ;; esac
    if [ "$(readlink /proc/"$pid"/exe 2>/dev/null)" = "$base/etc/core/clash_meta" ]; then
        echo 'An existing application core needs namespace recovery' >&2
        exit 73
    fi
fi
(exec 9>&-; exec nohup "$base/bin/oc-namespace" new "$base" /bin/sh "$base/bin/private-init.sh") > "$base/run/namespace.log" 2>&1 </dev/null &
echo 'Independent LuCI namespace startup dispatched'
start_screen
