#!/bin/sh
# Firmware network information remains on the original bus; UI sessions stay private.
case " $* " in
    *" network"*|*" zwrt_"*) exec /bin/ubus -s /data/openclash/run/host-ubus.sock "$@" ;;
    *) exec /bin/ubus "$@" ;;
esac
