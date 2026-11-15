#!/bin/sh
set -eu
# Only this task's dedicated server unit; existing web/TURN/RustDesk stay running.
systemctl disable --now mu5250-peer-relay.service
printf 'Dedicated peer relay stopped; state retained for controlled recovery\n'
