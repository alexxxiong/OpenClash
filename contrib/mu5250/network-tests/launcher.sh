#!/bin/bash
# The only host-specific substitution: simulate the /sys interface existence
# test. Everything else in the unchanged snapshot executes normally.
export PATH="$MOCK_BIN:/usr/bin:/bin"
test() {
    if [ "$#" -eq 2 ] && [ "$1" = -d ] && [ "$2" = /sys/class/net/mu-oc-tun ]; then
        [ "$MOCK_TUN_EXISTS" = yes ]
    elif [ "$#" -eq 2 ] && [ "$1" = -d ] && [ "$2" = "/sys/class/net/${MU_OC_LAN:-br-lan}" ]; then
        [ "$MOCK_IFACE_EXISTS" = yes ]
    else
        builtin test "$@"
    fi
}
. "$1" "$2"
