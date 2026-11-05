#!/bin/bash
set -eu
clock=0
mock_uci() {
    case "$3" in
      *.mu5250_udp_engine) echo "${TEST_ENGINE:-tun}" ;;
      *.cn_port) echo 9091 ;;
      *.dashboard_password) echo synthetic-test-secret ;;
      *.proxy_port) echo "${TEST_REDIR_PORT:-7892}" ;;
      *.mixed_port) echo 7893 ;;
      *.dns_port) echo 7874 ;;
      *.tproxy_port) echo 7895 ;;
      *.enable)
        if [ "${TEST_DISABLE_AFTER_FAIL:-0}" = 1 ] && [ -s "$TEST_ROOT/count" ]; then echo 0; else echo 1; fi ;;
      *) return 1 ;;
    esac
}
uci() { mock_uci "$@"; }
curl() { [ "$clock" -ge "${TEST_API_AT:-0}" ]; }
readlink() {
    if [ "$clock" -ge "${TEST_EXEC_AT:-0}" ]; then echo "$MU_OC_BASE/etc/core/clash_meta"; else echo /bin/busybox; fi
}
date() { echo 'Readiness must use monotonic uptime' >&2; return 99; }
tables() {
    printf '%s.00 0.00\n' "$clock" > "$MU_OC_PROC_ROOT/uptime"
    for kind in tcp tcp6 udp udp6; do printf 'header\n' > "$MU_OC_PROC_ROOT/net/$kind"; done
    if [ "$clock" -ge "${TEST_LISTEN_AT:-0}" ]; then
        for port in 7892 7893; do printf '0: 00000000:%04X 00000000:0000 %s\n' "$port" "${TEST_TCP_STATE:-0A}" >> "$MU_OC_PROC_ROOT/net/tcp"; done
        printf '0: 00000000:1EC2 00000000:0000 0A\n' >> "$MU_OC_PROC_ROOT/net/tcp6"
    fi
    if [ "$clock" -ge "${TEST_DNS_AT:-0}" ]; then
        printf '0: 00000000:1EC2 00000000:0000 07\n' >> "$MU_OC_PROC_ROOT/net/udp6"
    fi
    if [ "$clock" -ge "${TEST_TPROXY_AT:-0}" ]; then
        printf '0: 00000000:1ED7 00000000:0000 07\n' >> "$MU_OC_PROC_ROOT/net/udp"
    fi
    if [ "$clock" -ge "${TEST_TUN_AT:-0}" ]; then
        mkdir -p "$MU_OC_SYS_ROOT/class/net/mu-oc-tun"
        printf '%s\n' "${TEST_TUN_FLAGS:-0x1}" > "$MU_OC_SYS_ROOT/class/net/mu-oc-tun/flags"
    fi
    if [ "$clock" -ge "${TEST_EXIT_AT:-9999}" ]; then rmdir "$MU_OC_PROC_ROOT/123" 2>/dev/null || true; fi
}
sleep() {
    clock=$((clock + $1))
    tables
}
tables
. "$TEST_SCRIPT" "${TEST_MODE:-wait}" 123 "${TEST_LIMIT:-12}"
