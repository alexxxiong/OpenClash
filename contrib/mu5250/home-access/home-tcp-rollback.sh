#!/bin/sh
set -eu
systemctl disable --now mu5250-home-tcp.service
/usr/bin/python3 /opt/mu5250-home-tcp/optimize.py --cleanup
printf 'Scoped TCP optimization removed; gateway proxies and global defaults preserved\n'
