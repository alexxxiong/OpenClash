#!/bin/sh
# Host mount namespace only; started by the existing application entry point.
set -eu
umask 077
base=/data/openclash
export PATH=/usr/bin:/usr/sbin:/bin:/sbin
mkdir -p "$base/run"
exec 9>"$base/run/screen-network-watch.lock"
flock -n -x 9 || exit 0
printf '%s\n' "$$" > "$base/run/screen-network-watch.pid"
read -r ui_hash tool_hash library_hash library < "$base/lib/screen-network.manifest"
for hash in "$ui_hash" "$tool_hash" "$library_hash"; do
    case "$hash" in ''|*[!0-9a-f]*) exit 64 ;; esac
    [ "${#hash}" = 64 ] || exit 64
done
suffix=${library#/data/openclash/lib/screen-network-}
case "$suffix" in *.so) suffix=${suffix%.so} ;; *) exit 64 ;; esac
case "$suffix" in ''|*[!0-9a-f]*) exit 64 ;; esac
[ "${#suffix}" = 12 ] || exit 64
[ "$library" = "$base/lib/screen-network-$suffix.so" ] || exit 64
tool=$base/bin/oc-screen-load
[ "$(sha256sum "$tool" | cut -d' ' -f1)" = "$tool_hash" ] || exit 65
[ "$(sha256sum "$library" | cut -d' ' -f1)" = "$library_hash" ] || exit 65
ready_tool=$base/bin/oc-screen-ready
read -r ready_hash < "$base/lib/screen-ready.sha256"
case "$ready_hash" in ''|*[!0-9a-f]*) exit 64 ;; esac
[ "${#ready_hash}" = 64 ] || exit 64
[ "$(sha256sum "$ready_tool" | cut -d' ' -f1)" = "$ready_hash" ] || exit 65
# This unit has CONFIG_PID_NS disabled. Never infer this from a missing proc file.
zcat /proc/config.gz | grep -x '# CONFIG_PID_NS is not set' >/dev/null || exit 69
owned_poller() {
    p=$(cat "$base/run/screen-status-poll.pid" 2>/dev/null) || return 1
    case "$p" in ''|0*|*[!0-9]*) return 1 ;; esac
    [ -r "/proc/$p/cmdline" ] || return 1
    tr '\000' '\n' < "/proc/$p/cmdline" |
        awk 'NR==1 {a=($0=="/bin/sh")} NR==2 {b=($0=="/data/openclash/bin/screen-status-poll.sh")} END {exit !(NR==2&&a&&b)}'
}
attempted=
checked=
waiting=
note() {
    t=unknown
    if [ -r /proc/uptime ]; then read -r t rest </proc/uptime || t=unknown; fi
    printf '[%s] %s\n' "$t" "$*"
}
while :; do
    if ! owned_poller; then
        (exec 9>&-; exec /bin/sh "$base/bin/screen-status-poll.sh") >"$base/run/screen-status-poll.log" 2>&1 </dev/null &
    fi
    pid=$(pidof zte_topsw_devui 2>/dev/null) || pid=
    case "$pid" in ''|0*|*[!0-9]*) sleep 1 9>&-; continue ;; esac
    if [ "$attempted" != "$pid" ]; then
        if [ "$checked" != "$pid" ]; then
            checked=$pid
            if [ "$(sha256sum /usr/bin/zte_topsw_devui | cut -d' ' -f1)" != "$ui_hash" ]; then
                attempted=$pid
                note 'Unsupported stock UI checksum; screen extension skipped'
                sleep 5 9>&-; continue
            fi
        fi
        # A restored watcher adopts an already loaded copy without reattaching.
        if grep -Fq "$library" "/proc/$pid/maps"; then
            attempted=$pid
            if [ "$(cat "$base/run/screen-network.active" 2>/dev/null)" = "$pid $library" ]; then
                note "Adopted existing screen extension for UI PID $pid"
            else
                note "Library already mapped without confirmed activation for UI PID $pid; skipped"
            fi
        else
            # Only passive proc reads while waiting: initialized LVGL, a display
            # buffer, valid timers and an advancing GUI-loop heartbeat.
            if [ "$waiting" != "$pid" ]; then
                waiting=$pid
                note "Waiting for UI PID $pid readiness"
            fi
            if "$ready_tool" --pid "$pid" >"$base/run/screen-ready.$pid.json" 9>&-; then
                [ "$(pidof zte_topsw_devui 2>/dev/null)" = "$pid" ] || continue
                note "UI PID $pid is ready; loading screen extension"
            else
                code=$?
                if [ "$code" != 75 ]; then
                    attempted=$pid
                    note "UI readiness refused for PID $pid (exit $code); extension skipped"
                fi
                sleep 1 9>&-; continue
            fi
            # Remember before attachment; loader failures are never retried on
            # this PID. Readiness waits do not consume the single load attempt.
            attempted=$pid
            result=$base/run/screen-network-load.$pid.log
            if "$tool" --confirm-no-pidns --expect-exe /usr/bin/zte_topsw_devui \
                --expect-sha256 "$ui_hash" --pid "$pid" --so "$library" \
                --expect-so-sha256 "$library_hash" --timeout-ms 3000 --safe-wait-ms 5000 \
                --entry mu_screen_install --arg0 0 --ack-loader-risk >"$result" 2>&1 9>&- &&
                grep -q 'mu_screen_install returned 0x0;' "$result"; then
                printf '%s %s\n' "$pid" "$library" > "$base/run/screen-network.active"
                note "Installed screen extension for UI PID $pid"
            else
                note "Screen extension attempt failed for UI PID $pid; no automatic retry"
            fi
        fi
    fi
    sleep 5 9>&-
done
