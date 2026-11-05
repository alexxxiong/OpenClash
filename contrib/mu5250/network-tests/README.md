Run from PowerShell in the project directory:

```powershell
python -B openclash-install-20261105\network-tests\run_tests.py
```

Requirements: Python 3 and Git Bash (`C:\Program Files\Git\bin\bash.exe`).
No packages, network access, elevated privileges, or device are needed.

The runner copies the source verbatim to `reviewed-network-rules.sh`, checks Bash
syntax, then sources that snapshot in Bash POSIX mode. It replaces `iptables`,
`ip6tables`, `ip`, `uci`, and `flock` with command wrappers that call the local
JSON-state simulator in `mock_net.py`. Only the `/sys/class/net/<interface>`
existence check is replaced by a shell function. Actual mkdir/rm/cat/printf work
solely on a separate per-case base under `work/`. The launcher PATH contains only
these mocks and Git Bash utilities. No SSH, ADB, sockets, or host firewall/route
commands are used.

The packet evaluator walks the emitted PREROUTING hooks and chains in
mangle-before-nat order, including interface/protocol/port/source/destination
matching, negation, jump/RETURN, REDIRECT, TPROXY, and nonterminal MARK --set-xmark.
It then selects fwmark policy routes by priority/prefix/metric and evaluates the
filter FORWARD hook, including input/output interface, mark masks, and conntrack
state. Firmware forwarding defaults to DROP, so a missing MU_OC_FWD hook causes
selected TUN UDP to fail the test. Mangle ACCEPT does not skip NAT/routing.
It asserts destinations and custom app ports rather than merely searching the
script for expected strings.
Policy-rule and route assertions check family, priority, table, mark and mask.
Firmware chains, policies, and routes are seeded and compared across cleanup.
The mock permits duplicate policy priorities and rejects deleting referenced
chains. Route show/add/del are modeled with family, table, prefix, route type,
device and supplied selectors; kernel-added protocol/scope attributes do not
prevent deletion. TUN interface existence is independently simulated. Unsupported
mock operations explicitly fail the test instead of masquerading as an injected
or expected shell failure. Failure injection covers partial installations and a real local TERM
signal to the test shell; network state always remains simulated.

`flock` is simulated: the test checks its argument, inherited descriptor, order,
and failure behavior. It does not prove real kernel lock contention. This is an
offline shell/control-flow and emitted-rule test, not a Linux kernel/conntrack,
BusyBox runtime, target interface, or live app-listener validation.

`tun_cases.py` extends the original regressions with TUN IPv4/IPv6 packet matrices,
strict UDP forward/return filters, scope and engine transitions, recorded-engine
cleanup, absence/collision checks, seven partial-install failures, TERM, cleanup
retry behavior, and preservation of foreign routes/chains. Main/local routing
tables and firmware FORWARD chains are seeded and compared. Mutation checks also
reject route flushes or writes outside the exact reserved table/priority. The
TUN matrix removes MARK or the FORWARD hook from an in-memory copy to demonstrate
that both are needed for a successful evaluated flow.

Outputs: `results.json`, the exact reviewed snapshot, generated `bin/` wrappers,
and `work/<case>/state.json` plus `shell.log`. Nonzero exit means a requirement
failed; findings are intentionally ordinary failing tests, not expected failures.

For focused reruns, supply case-name fragments as arguments. Results merge by
name only when the source hash matches the prior run; a full run replaces results.

```powershell
python -B openclash-install-20261105\network-tests\run_tests.py "IPv6 local-address" "hook removal"
```
