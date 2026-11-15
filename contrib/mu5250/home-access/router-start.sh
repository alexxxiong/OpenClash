#!/bin/sh
set -eu
base=/jffs/softcenter/mu5250-home-site
run=/tmp/mu5250-home-site
umask 077
mkdir -p "$run"
exec 9>"$run/start.lock"
flock -x 9
owned() {
    [ -s "$run/watch.pid" ] || return 1
    p=$(cat "$run/watch.pid")
    case "$p" in ''|*[!0-9]*) return 1 ;; esac
    [ -r /proc/"$p"/cmdline ] && tr '\000' ' ' < /proc/"$p"/cmdline | grep -q "$base/watch.sh"
}
case "${1:-start}" in
    start|start_boot|start_nat)
        owned && exit 0
        (exec 9>&-; exec /bin/sh "$base/watch.sh") </dev/null >/dev/null 2>&1 &
        printf '%s\n' "$!" > "$run/watch.pid"
        ;;
    stop|stop_boot|kill)
        if owned; then
            kill -TERM "$p"
            # Wait for the scoped watcher cleanup before returning.
            i=0
            while [ -r /proc/"$p"/cmdline ] && [ "$i" -lt 7 ]; do sleep 1; i=$((i+1)); done
        fi
        "$base/rules.sh" stop
        rm -f "$run/watch.pid"
        ;;
    *) exit 0 ;;
esac
