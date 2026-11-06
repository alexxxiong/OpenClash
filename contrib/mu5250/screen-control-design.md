# Local OpenClash control/status bridge

The bridge, native Network-page widget and status poller were integrated on the
MU5250 CN B27 unit on 2026-11-07. See `screen-network.md` for live acceptance,
startup behavior and rollback. This document describes the bridge contract.

## Entry point

Intended future location: `/data/openclash/bin/screen-control.sh`, root-owned
and executable only by the trusted local integration. This is a local command,
not a network listener or an authentication boundary.

```sh
/bin/sh /data/openclash/bin/screen-control.sh status
/bin/sh /data/openclash/bin/screen-control.sh on
/bin/sh /data/openclash/bin/screen-control.sh off
```

Exactly one argument is accepted. Arbitrary commands, restart, additional
arguments, and shell fragments are rejected with exit 64. Future UI code must
use fixed argv, not concatenate input into a shell command. The screen callback
dispatches off the LVGL thread; the separate poller writes an atomic cache and
the native timer updates the widget on the UI thread.

Both reads and writes first validate a canonical numeric `namespace.pid` (>1),
then match the **complete** `/proc/PID/cmdline` argv vector:
`/bin/sh`, `/data/openclash/bin/private-init.sh`, and no extra arguments.
Every namespace operation revalidates this identity before calling the existing
`oc-namespace enter PID ...`. It never starts/recreates a missing namespace.
The existing namespace helper does not atomically pin identity and enter;
PID replacement between validation and `setns()` remains a narrow race. An
atomic namespace-helper enhancement is outside the three owned new files.

## Response contract

`status` returns one small JSON object (under 250 bytes in the fixtures), and
exit 0 even if the observed state is failed. Failure is data the UI must show.

```json
{"state":"running","enabled":true,"core":true,"ready":true,"clock":true,"rules":true,"busy":false,"reason":"ready"}
```

| State | Meaning |
| --- | --- |
| `disabled` | enable=0, no owned live core and no recorded/kernel routing residue |
| `starting` | a live bridge control lock records starting, or the existing service is busy while enabled |
| `stopping` | a live bridge control lock records stopping, or the existing service is busy while disabled |
| `running` | enable=1, exact core executable, readiness check, matching clock PID, and rules verified |
| `failed` | namespace/config/inspection failure, missing readiness, or incomplete cleanup without an active operation |

`enabled` is `null` when it could not be read/validated; it is never guessed.
`core` means PID ownership, not connectivity to a selected proxy node. `ready`
uses the existing **read-only** `core-ready.sh check PID`, which verifies the
controller, proxy/DNS listeners and UDP interface. `clock` requires
`clock-ready.pid == core.pid`. No clock guard step/flush is triggered by reads.
`rules` means the configured routing scope is satisfied, including deliberate
`mu5250_proxy_scope=off` (healthy explicit proxy, no LAN interception). It does
not claim a real Internet or node handshake check.

Kernel inspection checks both families: application-chain hooks, DNS/TCP
redirect targets, UDP mark/TPROXY, TUN forwarding, policy priority 17250/mark
0x7250/table 7250, and table routes. Metadata must agree with configured
scope/engine. Test-IP scope also requires its source guards. Missing hooks
cannot be masked by stale `rules.*` marker files. Unexpected residual chains,
reserved routes or policies prevent declaring disabled. Query errors fail
closed. This is a health check of the adapter's essential rules, not a complete
firewall-equivalence audit. It expects the adapter's iptables `-S` formatting;
on-device formatting compatibility was verified on the current B27 unit.

Status does not mkdir/touch/truncate/write files, change UCI, acquire a service
write lock, run `network-rules.sh status`, or call a mutating service command.
It opens **existing** locks read-only and probes with shared locks, allowing
concurrent readers to coexist. Each external probe
has a 2-second TERM / 1-second KILL watchdog; a shared monotonic uptime deadline
defaults to 8 seconds. With normal `/proc` and the fixed existing helpers,
deadline overrun is at most the last probe's 3-second bound. Controller curl
already has a 1-second bound. OS scheduling is not a hard real-time guarantee.
Only read-only localhost controller requests are made by readiness checking.
No secrets, PIDs, node definitions, curl bodies, or raw helper errors are
included in JSON. Operational errors are fixed reason codes.

## Controls

Accepted request, exit 0:

```json
{"accepted":true,"action":"on","state":"starting"}
```

This is an acknowledgement, **not** proof of completion. The UI should disable
the switch during a transition and keep polling until running/disabled/failed.
`on`: private service `enable`, then `start`. `off`: `disable`, then `stop`.
Only these four fixed actions are used; no restart, arbitrary UCI assignment,
subscription modification, node selection, or rule/config editing is added.
Normal existing service startup may regenerate its runtime config as usual.
The bridge neither reads nor changes core keys or the original system clock.

The background worker inherits the control flock, while the caller immediately
closes its fd and returns. All inherited UI pipes are redirected. Service and
probe children close the bridge fd so the core/watchdog cannot accidentally
retain it. Controls are nonblocking and serialized by `screen-control.lock`.
A competing click returns `{"accepted":false,"reason":"busy"}` with exit 75;
requests are rejected rather than queued or silently reordered. Namespace
unavailable returns the analogous reason and exit 69. Storage/locking setup
failure returns 73. Dependency failures are not retried indefinitely.

`screen-control.state` is an atomic, single enum (`starting`, `stopping`,
`done`, `failed`), mode 600 under the application's run directory. A stale
transition without a held lock is a failure, never permanent starting.
Actual healthy running/disabled state takes priority over an old failure
record, allowing recovery from LuCI to be recognized. The existing service's
own lock still protects individual operations; external LuCI/boot actions can
interleave between enable/start or disable/stop. The worker verifies actual
state after completion and reports failure instead of assuming success.

The worker relies on the existing service's startup/stop limits. It does not
kill service descendants or change the watchdog when an unusual helper hangs;
status remains bounded and reports a live transition. Recovery from a stuck
service or a parent restart is an integration concern, not a reason to restart
firmware services here. No run directory is made when namespace validation fails.

## Offline tests and trusted overrides

```sh
python contrib/mu5250/test-screen-control.py
```

Run from any directory; no dependencies beyond Python stdlib and Bash. Windows
defaults to Git Bash, Linux to `bash` on PATH; `MU_OC_TEST_BASH` overrides it.
Fixtures execute the actual bridge and actual readiness helper. Namespace,
UCI, controller curl and kernel commands are all mocked, with an allowlist
that rejects unexpected/mutating operations. Nothing connects to a device.
All generated executables are chmod 700, use LF, and are cleaned from a
temporary directory outside the repository. Tests do not emit result files.

Linux uses real `flock` for concurrent callers. If Git Bash lacks it, tests
explicitly model lock contention; that result is **not** evidence of native
Linux flock inheritance. Parent Linux CI/device acceptance must check it.

Trusted root/test configuration can override `MU_OC_BASE`, `MU_OC_PROC_ROOT`,
`MU_OC_NAMESPACE`, `MU_OC_SERVICE`, `MU_OC_READY`, `MU_OC_STATUS_TIMEOUT` (1–30),
and `MU_OC_TEST_BIN` (for command fixtures). Existing readiness additionally
supports `MU_OC_SYS_ROOT`. Defaults are the current application paths and a
fixed system PATH. These environment variables must not be supplied by an
untrusted screen/web client; launch with a controlled environment. They are
test hooks, not public request fields or additional commands.

Native Linux control locks, paths, rule formatting, actual screen on/off cycles,
core/listener/clock/rule readiness and preservation of selected groups and policy
were verified on B27. The user confirmed the final on/off cycle without visual
rollback. Whole-device reboot acceptance remains unverified.
