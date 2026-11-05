#!/bin/sh
# Bounded boot retries run after LuCI is listening; never block firmware rc.local.
base=/data/openclash
service=${MU_OC_SERVICE:-/etc/init.d/openclash}
for delay in 0 5 15; do
    [ "$(uci -q get openclash.config.enable)" = 1 ] || exit 0
    [ "$delay" = 0 ] || sleep "$delay"
    [ "$(uci -q get openclash.config.enable)" = 1 ] || exit 0
    if "$service" start; then
        echo 'MU5250: enabled core boot startup completed.'
        exit 0
    fi
    echo 'MU5250: boot startup attempt failed; retry remains bounded.' >&2
done
echo 'MU5250: boot retries exhausted; LuCI remains available for inspection.' >&2
exit 1
