#!/bin/bash
set -eu
clock=0
mock_ubus() {
    printf '%s %s\n' "$clock" "$*" >> "$TEST_ROOT/calls"
    [ "$clock" -ge "${TEST_BUS_AT:-0}" ] || return 1
    case " $* " in
        *' list session '*)
            [ "$clock" -ge "${TEST_SESSION_AT:-0}" ] || return 1
            [ "${TEST_EMPTY_SESSION:-0}" != 1 ] || return 0
            echo session ;;
        *' call system board '*)
            if [ "$clock" -ge "${TEST_BOARD_AT:-0}" ]; then echo '{"kernel":"5.15.170-perf"}'; else echo '{}'; fi ;;
        *' list '*) return 0 ;;
        *) return 1 ;;
    esac
}
jsonfilter() {
    data=$(cat)
    case "$data" in *'"kernel"'*) echo 5.15.170-perf ;; *) return 0 ;; esac
}
date() { echo 'Readiness must not depend on system clock' >&2; return 99; }
sleep() { clock=$((clock+$1)); printf '%s.00 0.00\n' "$clock" > "$MU_OC_PROC_ROOT/uptime"; }
printf '0.00 0.00\n' > "$MU_OC_PROC_ROOT/uptime"
MU_OC_UBUS=mock_ubus
. "$TEST_SCRIPT" "$TEST_PHASE" "$TEST_ROOT/ubus.sock" "${TEST_LIMIT:-8}"
