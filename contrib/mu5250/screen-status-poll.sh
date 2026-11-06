#!/bin/sh
set -eu
base=/data/openclash
umask 077
mkdir -p "$base/run"
exec 9>"$base/run/screen-status-poll.lock"
flock -n -x 9 || exit 0
printf '%s\n' "$$" > "$base/run/screen-status-poll.pid"
trap 'rm -f "$base/run/screen-status.json.$$"' EXIT
while :; do
    status=$(/bin/sh "$base/bin/screen-control.sh" status 9>&-) || status='{"state":"failed","enabled":null,"busy":false}'
    now=$(cut -d. -f1 /proc/uptime)
    printf '{"uptime":%s,"status":%s}\n' "$now" "$status" > "$base/run/screen-status.json.$$"
    mv -f "$base/run/screen-status.json.$$" "$base/run/screen-status.json"
    sleep 2 9>&-
done
