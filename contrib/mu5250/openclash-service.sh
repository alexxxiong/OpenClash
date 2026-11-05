#!/bin/sh
# Invoked only from the application's mount namespace, never via firmware procd.
base=/data/openclash
export PATH=${MU_OC_TEST_BIN:+$MU_OC_TEST_BIN:}/usr/bin:/usr/sbin:/bin:/sbin
export LD_LIBRARY_PATH=/usr/lib:/lib
umask 077
mkdir -p "$base/run"

core_running() {
    [ -s "$base/run/core.pid" ] || return 1
    pid=$(cat "$base/run/core.pid")
    case "$pid" in ''|*[!0-9]*) return 1 ;; esac
    [ "$(readlink /proc/"$pid"/exe 2>/dev/null)" = "$base/etc/core/clash_meta" ]
}

case "${1:-}" in
    status|running) core_running; exit $? ;;
    enabled) [ "$(uci -q get openclash.config.enable)" = 1 ]; exit $? ;;
    watchdog)
        watched=${2:-}
        case "$watched" in ''|*[!0-9]*) exit 64 ;; esac
        while [ "$(cat "$base/run/core.pid" 2>/dev/null)" = "$watched" ] && core_running; do
            "$base/bin/core-clock-guard.sh" step "$watched" >/dev/null 2>&1 || true
            if [ -f /tmp/openclash.log ] && [ "$(wc -c < /tmp/openclash.log)" -gt 2097152 ]; then
                : > /tmp/openclash.log
                printf '%s\n' 'MU5250: runtime log size limit reached; log truncated.' >> /tmp/openclash.log
            fi
            sleep 2
        done
        # A restart owns the new PID; an old watcher must not detach its rules.
        exec 8>"$base/run/service.lock"
        flock -x 8 || exit 1
        if [ "$(cat "$base/run/core.pid" 2>/dev/null)" = "$watched" ]; then
            "$base/bin/network-rules.sh" stop || exit 1
            rm -f "$base/run/core.pid"
            printf '%s\n' 'MU5250: core exited; application routing detached.' >> /tmp/openclash.log
            retry=1
        fi
        exec 8>&-
        if [ "${retry:-0}" = 1 ] && [ "$(uci -q get openclash.config.enable)" = 1 ]; then
            now=$(cut -d. -f1 /proc/uptime)
            stamp=0; count=0
            if [ -r "$base/run/restarts" ]; then read -r stamp count < "$base/run/restarts"; fi
            if [ "$((now - stamp))" -gt 120 ] || [ "$now" -lt "$stamp" ]; then count=0; fi
            count=$((count + 1))
            printf '%s %s\n' "$now" "$count" > "$base/run/restarts"
            if [ "$count" -le 3 ]; then
                sleep 5
                MU_OC_AUTORESTART=1 exec /bin/sh "$base/bin/openclash-service.sh" start
            fi
            printf '%s\n' 'MU5250: repeated core failures; routing remains detached. Restart from LuCI after checking the log.' >> /tmp/openclash.log
        fi
        exit 0 ;;
esac

exec 8>"$base/run/service.lock"
flock -x 8 || exit 1
. /lib/functions.sh
. /usr/share/openclash/mu5250-upstream-init.sh

managed_stop() {
    "$base/bin/network-rules.sh" stop || return 1
    if core_running; then
        owned_pid=$pid
        # Remove ownership first so a retiring watcher cannot affect a new start.
        rm -f "$base/run/core.pid"
        kill -TERM "$owned_pid" 2>/dev/null || true
        for i in 1 2 3 4 5; do
            [ "$(readlink /proc/"$owned_pid"/exe 2>/dev/null)" = "$base/etc/core/clash_meta" ] || break
            sleep 1
        done
        if [ "$(readlink /proc/"$owned_pid"/exe 2>/dev/null)" = "$base/etc/core/clash_meta" ]; then
            kill -KILL "$owned_pid" 2>/dev/null || true
        fi
    fi
    rm -f "$base/run/core.pid"
    LOG_TIP 'MU5250: OpenClash stopped; original firmware networking remains active.'
}

# Retain upstream subscription, YAML, overwrite, and UI behavior. All firmware
# lifecycle and DNS/firewall operations are provided by this application instead.
procd_running() { core_running; }
change_dnsmasq() { :; }
revert_dnsmasq() { :; }
add_cron() { :; }
del_cron() { :; }
service_started() { :; }
check_core_status() { :; }
stop_service() { managed_stop; }
stop() { managed_stop; }
start_fail() { LOG_ERROR 'MU5250: startup failed; routing detached.'; managed_stop; exit 1; }
check_run_quick() { QUICK_START_CHECK=true; QUICK_START=false; }
set_firewall() { "$base/bin/network-rules.sh" start; }
revert_firewall() { "$base/bin/network-rules.sh" stop; }
do_run_file() {
    meta_core_path=/etc/openclash/core/clash_meta
    [ -x "$meta_core_path" ] || start_fail
    ln -sfn "$meta_core_path" /etc/openclash/clash || start_fail
    core_type=Meta
    [ -z "$OIX_TOKEN" ] || core_type=Oix
    uci -q set openclash.config.core_type="$core_type"
    uci -q commit openclash
    mkdir -p /etc/openclash/history /etc/openclash/proxy_provider /etc/openclash/rule_provider
}
start_run_core() {
    [ -s "$TMP_CONFIG_FILE" ] || start_fail
    ruby "$base/bin/finalize-config.rb" "$TMP_CONFIG_FILE" >> "$LOG_FILE" 2>&1 || start_fail
    export SAFE_PATHS=/usr/share/openclash:/etc/ssl
    export CLASH_AGE_SECRET_KEY="$SECRET_KEY" OIX_TOKEN="$OIX_TOKEN" OIX_CLIENT=openclash
    "$CLASH" -t -d "$CLASH_CONFIG" -f "$TMP_CONFIG_FILE" >> "$LOG_FILE" 2>&1 || start_fail
    mv -f "$TMP_CONFIG_FILE" "$CONFIG_FILE" || start_fail
    chmod 600 "$CONFIG_FILE"
    "$base/bin/core-clock-guard.sh" prepare || start_fail
    (exec 8>&-; exec "$CLASH" -d "$CLASH_CONFIG" -f "$CONFIG_FILE") >> "$LOG_FILE" 2>&1 </dev/null &
    core_pid=$!
    printf '%s\n' "$core_pid" > "$base/run/core.pid"
    "$base/bin/core-ready.sh" wait "$core_pid" 120 >> "$LOG_FILE" 2>&1 || start_fail
    "$base/bin/core-clock-guard.sh" wait "$core_pid" 30 >> "$LOG_FILE" 2>&1 || start_fail
    set_firewall || start_fail
    (exec 8>&-; exec /bin/sh "$base/bin/openclash-service.sh" watchdog "$core_pid") >/dev/null 2>&1 </dev/null &
    LOG_TIP 'MU5250: core is ready; managed routing scope applied.'
}

managed_start() {
    core_running && return 0
    [ "${MU_OC_AUTORESTART:-0}" = 1 ] || rm -f "$base/run/restarts"
    config_path=$(uci -q get openclash.config.config_path)
    case "$config_path" in /etc/openclash/config/*) ;; *) LOG_ERROR 'Select a configuration in Config Manage first.'; return 1 ;; esac
    name=${config_path##*/}
    case "$name" in ''|*[!a-zA-Z0-9_.\ -]*) LOG_ERROR 'Unsupported configuration filename'; return 1 ;; esac
    [ -s "$config_path" ] || { LOG_ERROR 'Selected configuration does not exist'; return 1; }
    mode=$(uci -q get openclash.config.en_mode)
    case "$mode" in fake-ip|redir-host) ;; *) LOG_ERROR 'MU5250 currently supports enhanced fake-IP or redir-host mode.'; return 1 ;; esac
    start_service
}

case "${1:-}" in
    start|boot) managed_start ;;
    stop|shutdown) managed_stop ;;
    restart|reload) managed_stop && managed_start ;;
    enable) uci -q set openclash.config.enable=1; uci -q commit openclash ;;
    disable) uci -q set openclash.config.enable=0; uci -q commit openclash ;;
    *) exit 64 ;;
esac
