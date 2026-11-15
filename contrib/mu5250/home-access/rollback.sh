#!/bin/sh
set -eu
base=/data/tailscale-home
backup=/data/tailscale-home-backup-20261115
if [ -f "$base/dns-preimage.json" ]; then
    ns=$(cat /data/openclash/run/namespace.pid)
    case "$ns" in ''|*[!0-9]*) exit 73 ;; esac
    case "$(tr '\000' ' ' < /proc/"$ns"/cmdline)" in *'/data/openclash/bin/private-init.sh'*) ;; *) exit 73 ;; esac
    /data/openclash/bin/oc-namespace enter "$ns" /bin/sh -c "ruby '$base/apply-dns.rb' disable"
fi
"$base/stop.sh"
if cmp -s /data/openclash/bin/finalize-config.rb "$base/finalize-config.candidate.rb"; then
    cp -p "$backup/finalize-config.rb" /data/openclash/bin/finalize-config.rb
fi
# Remove only this feature's marked startup block; retain all other current hooks.
awk '/^# BEGIN MU5250 HOME ACCESS$/{skip=1;next} /^# END MU5250 HOME ACCESS$/{skip=0;next} !skip{print}' /etc/rc.local > "$base/run/rc.local.rollback"
sh -n "$base/run/rc.local.rollback"
cat "$base/run/rc.local.rollback" > /etc/rc.local
sh -n /etc/rc.local
mode=$(stat -c '%a' "$backup/rc.local")
case "$mode" in ''|*[!0-7]*) exit 73 ;; esac
chmod "$mode" /etc/rc.local
sha256sum -c "$backup/firmware.sha256"
echo 'Home access rolled back; OpenClash remains running'
