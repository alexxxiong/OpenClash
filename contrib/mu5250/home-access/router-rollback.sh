#!/bin/sh
set -eu
base=/jffs/softcenter/mu5250-home-backup-20261115
# Only undo the plugin we enabled, without changing oixClash or other VPNs.
test -f "$base/tailscale_enable"
site=/jffs/softcenter/mu5250-home-site
if [ -x "$site/start.sh" ]; then
    "$site/start.sh" stop
    for hook in /koolshare/init.d/S97mu5250home.sh /koolshare/init.d/N97mu5250home.sh; do
        if cmp -s "$hook" "$site/init.sh"; then rm -f "$hook"; fi
    done
fi
previous=$(cat "$base/tailscale_enable")
case "$previous" in ''|0) /koolshare/scripts/tailscale_config stop ;; *) exit 73 ;; esac
for key in tailscale_enable tailscale_ipv4_enable tailscale_ipv6_enable tailscale_advertise_routes tailscale_accept_routes tailscale_exit_node; do
    value=$(cat "$base/$key")
    case "$value" in
        '') dbus remove "$key" ;;
        0|1) dbus set "$key=$value" ;;
        *) exit 73 ;;
    esac
done
printf 'Original Tailscale switches restored; oixClash left running\n'
