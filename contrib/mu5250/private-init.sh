#!/bin/sh
set -eu
umask 077
base=/data/openclash
export PATH=/usr/bin:/usr/sbin:/bin:/sbin
export LD_LIBRARY_PATH=/usr/lib:/lib
web= bus= rpc=
cleanup() {
    /etc/init.d/openclash stop >> "$base/run/autostart.log" 2>&1 || true
    for p in "$web" "$rpc" "$bus"; do [ -z "$p" ] || kill "$p" 2>/dev/null || true; done
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
for i in $(seq 1 60); do
    ip -4 addr show br-lan 2>/dev/null | grep -q 'inet 192.168.3.1/' && break
    sleep 1
done
mkdir -p "$base/run/rpcd"
mount --bind "$base/run/rpcd" /tmp/run/rpcd
touch "$base/run/host-ubus.sock"
mount --bind /var/run/ubus/ubus.sock "$base/run/host-ubus.sock"
/sbin/ubusd -s "$base/run/ubus.sock" >"$base/run/ubusd.log" 2>&1 &
bus=$!
printf '%s\n' "$bus" > "$base/run/ubusd.pid"
/bin/sh "$base/bin/wait-private-ubus.sh" bus "$base/run/ubus.sock" 30
/sbin/rpcd -s "$base/run/ubus.sock" >"$base/run/rpcd.log" 2>&1 &
rpc=$!
printf '%s\n' "$rpc" > "$base/run/rpcd.pid"
/bin/sh "$base/bin/wait-private-ubus.sh" rpcd "$base/run/ubus.sock" 30
mount --bind "$base/run/ubus.sock" /var/run/ubus/ubus.sock
/data/bin/dashboard-uhttpd -f -h /www -p 192.168.3.1:8081 -x /cgi-bin -u /ubus -U "$base/run/ubus.sock" -t 300 -T 60 -D >"$base/run/uhttpd.log" 2>&1 &
web=$!
printf '%s\n' "$web" > "$base/run/uhttpd.pid"
if [ "$(uci -q get openclash.config.enable)" = 1 ]; then
    /bin/sh "$base/bin/start-enabled-core.sh" >> "$base/run/autostart.log" 2>&1 || true
fi
wait "$web"
