"""Offline bridge regressions; no device, credentials, or network access.

Runs the unchanged shell bridge AND core-ready.sh under Bash. All namespace,
UCI, curl, iptables and ip operations are fixtures. Linux uses real flock;
Git Bash without flock models contention explicitly (reported at the end).
Generated files live only in a TemporaryDirectory, outside the source tree.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "screen-control.sh"
BASH = os.environ.get("MU_OC_TEST_BASH") or (
    r"C:\Program Files\Git\bin\bash.exe" if os.name == "nt" else shutil.which("bash")
)
if not BASH:
    raise SystemExit("Bash is required (MU_OC_TEST_BASH may specify it).")


def unix(path):
    value = str(Path(path).resolve()).replace("\\", "/")
    return "/" + value[0].lower() + value[2:] if len(value) > 1 and value[1] == ":" else value


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(0o700)


FLOCK = subprocess.run(
    [BASH, "--noprofile", "--norc", "-c", "command -v flock"],
    capture_output=True, text=True, timeout=5,
).stdout.strip()

MOCK = r'''#!/bin/sh
set -eu
name=${0##*/}
case "$name" in
    oc-namespace|curl|iptables|ip6tables|ip)
        [ ! -e /dev/fd/9 ] || printf '%s\n' "$name" >> "$TEST_ROOT/leaked-lock" ;;
esac
printf '%s' "$name" >> "$TEST_ROOT/calls"
for arg in "$@"; do printf ' <%s>' "$arg" >> "$TEST_ROOT/calls"; done
printf '\n' >> "$TEST_ROOT/calls"
case "$name" in
    oc-namespace)
        [ "$1" = enter ] && [ "$2" = 101 ] || exit 96
        shift 2
        exec "$@" ;;
    uci)
        [ "$#" = 3 ] && [ "$1" = -q ] && [ "$2" = get ] || exit 96
        case "$3" in openclash.config.*) key=${3#openclash.config.} ;; *) exit 96 ;; esac
        if [ -e "$TEST_ROOT/hang-uci" ]; then exec sleep 30; fi
        if [ -e "$TEST_ROOT/advance-uci" ]; then printf '200.00 0.00\n' > "$MU_OC_PROC_ROOT/uptime"; fi
        cat "$TEST_ROOT/config/$key" ;;
    readlink)
        [ "$#" = 1 ] || exit 96
        cat "$1.target" ;;
    curl)
        # core-ready.sh's real stdin authentication path is exercised, without
        # copying the header to a log or allowing a real HTTP connection.
        [ "$*" = '-fsS --noproxy * -m 1 -H @- http://127.0.0.1:9091/version' ] || exit 96
        read -r header
        [ "$header" = 'Authorization: Bearer offline-only-secret' ] || exit 96
        [ ! -e "$TEST_ROOT/fail-api" ] ;;
    iptables|ip6tables)
        [ "$#" = 5 ] && [ "$1" = -w ] && [ "$2" = 1 ] && [ "$3" = -t ] && [ "$5" = -S ] || exit 96
        [ ! -e "$TEST_ROOT/fail-rules-read" ] || exit 2
        family=4; [ "$name" = iptables ] || family=6
        cat "$TEST_ROOT/kernel/$family.$4" ;;
    ip)
        family=${1#-}; shift
        case "$family:$*" in
            4:'rule show'|6:'rule show') cat "$TEST_ROOT/kernel/$family.policy" ;;
            4:'route show table all'|6:'route show table all') cat "$TEST_ROOT/kernel/$family.route" ;;
            *) exit 96 ;;
        esac ;;
    openclash-service.sh)
        [ "$#" = 1 ] || exit 96
        printf '%s\n' "$1" >> "$TEST_ROOT/service-calls"
        : > "$TEST_ROOT/entered-$1"
        : > "$TEST_ROOT/busy-control"
        while [ -e "$TEST_ROOT/gate-$1" ]; do sleep 0.05; done
        [ "${TEST_FAIL_ACTION:-}" != "$1" ] || exit 7
        case "$1" in
            enable) printf '1\n' > "$TEST_ROOT/config/enable" ;;
            disable) printf '0\n' > "$TEST_ROOT/config/enable" ;;
            start)
                [ "${TEST_START_NOOP:-0}" != 1 ] || exit 0
                /bin/sh "$TEST_ROOT/activate.sh" ;;
            stop) /bin/sh "$TEST_ROOT/deactivate.sh" ;;
            *) exit 96 ;;
        esac ;;
    flock)
        # Only needed on Git Bash, where util-linux flock is absent. This does
        # not claim to test Windows kernel lock inheritance; Linux runs real
        # flock with the same bridge and independent concurrent callers.
        case "$*" in '-n -x 9'|'-n -s 9') ;; *) exit 96 ;; esac
        target=$(/usr/bin/readlink /dev/fd/9)
        case "$target" in
            */screen-control.lock)
                phase=$(cat "$MU_OC_BASE/run/screen-control.state" 2>/dev/null || true)
                if [ -e "$TEST_ROOT/busy-control" ]; then
                    case "$phase" in starting|stopping) exit 1 ;; esac
                fi ;;
            */service.lock) [ ! -e "$TEST_ROOT/busy-service" ] || exit 1 ;;
            *) exit 96 ;;
        esac ;;
    *) exit 96 ;;
esac
'''


class Lab:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mu-oc-screen-")
        self.root = Path(self.temp.name)
        self.base = self.root / "app"
        self.run = self.base / "run"
        self.proc = self.root / "proc"
        self.bin = self.root / "bin"
        self.sys = self.root / "sys"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("MU_OC_")}
        self.env.update({
            "TEST_ROOT": unix(self.root), "MU_OC_BASE": unix(self.base),
            "MU_OC_PROC_ROOT": unix(self.proc), "MU_OC_SYS_ROOT": unix(self.sys),
            "MU_OC_TEST_BIN": unix(self.bin), "MU_OC_READY": unix(ROOT / "core-ready.sh"),
            "TEST_SCRIPT": unix(SOURCE), "MU_OC_STATUS_TIMEOUT": "15",
            "MSYS2_ARG_CONV_EXCL": "*",
        })
        self.run.mkdir(parents=True)
        self.bin.mkdir()
        write(self.proc / "uptime", "100.00 0.00\n")
        write(self.run / "namespace.pid", "101\n")
        self.cmdline(b"/bin/sh\0" + (unix(self.base) + "/bin/private-init.sh").encode() + b"\0")
        for name in ["uci", "readlink", "curl", "iptables", "ip6tables", "ip"]:
            write(self.bin / name, MOCK)
        write(self.base / "bin/oc-namespace", MOCK)
        write(self.base / "bin/openclash-service.sh", MOCK)
        if FLOCK:
            # Keep the same fixed command path, delegating to the real utility.
            write(self.bin / "flock", '#!/bin/sh\nexec "' + FLOCK + '" "$@"\n')
        else:
            write(self.bin / "flock", MOCK)
        self.config = {
            "enable": "1", "mu5250_proxy_scope": "all", "mu5250_udp_engine": "tun",
            "proxy_port": "7892", "dns_port": "7874", "tproxy_port": "7895",
            "mixed_port": "7893", "cn_port": "9091", "dashboard_password": "offline-only-secret",
            "mu5250_test_ip": "192.168.3.120",
        }
        for key, value in self.config.items():
            write(self.root / "config" / key, value + "\n")
        self.protected = {
            "etc/config/current.yaml": "proxies: [美国 Fusion 08]\nrules: [unchanged]\n",
            "etc/proxy_provider/oixCloud": "synthetic-encrypted-provider\n",
            "etc/history/selection": "Proxy=美国 Fusion 08\n",
            "firmware/sentinel": "stock configuration must not change\n",
        }
        for name, text in self.protected.items():
            write(self.base / name, text)
        self.active()
        self.prepare_service_templates()

    def cmdline(self, value):
        path = self.proc / "101/cmdline"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)

    def set_config(self, key, value):
        self.config[key] = value
        write(self.root / "config" / key, value + "\n")

    def active(self, scope="all", engine="tun"):
        self.set_config("mu5250_proxy_scope", scope)
        self.set_config("mu5250_udp_engine", engine)
        write(self.run / "core.pid", "123\n")
        write(self.proc / "123/exe.target", unix(self.base) + "/etc/core/clash_meta\n")
        write(self.run / "clock-ready.pid", "123\n")
        for kind in ["tcp", "tcp6", "udp", "udp6"]:
            write(self.proc / "net" / kind, "header\n")
        write(self.proc / "net/tcp", "header\n" + "".join(
            f"0: 00000000:{port:04X} 00000000:0000 0A\n" for port in [7892, 7893, 7874]))
        write(self.proc / "net/udp", "header\n0: 00000000:1EC2 00000000:0000 07\n0: 00000000:1ED7 00000000:0000 07\n")
        write(self.sys / "class/net/mu-oc-tun/flags", "0x1\n")
        for family in [4, 6]:
            self.kernel(family, scope != "off" and (scope == "all" or family == 4), engine, scope)
        if scope != "off":
            for name, text in {"owned": "", "scope": scope + "\n", "engine": engine + "\n",
                               "interface": "br-lan\n", "families": "4\n6\n" if scope == "all" else "4\n"}.items():
                write(self.run / ("rules." + name), text)
        else:
            for path in self.run.glob("rules.*"):
                path.unlink()

    def kernel(self, family, active, engine="tun", scope="all"):
        nat = "-P PREROUTING ACCEPT\n"
        udp = "-P PREROUTING ACCEPT\n"
        forward = "-P FORWARD DROP\n-A FORWARD -j FIRMWARE_FORWARD\n"
        policy = "1000: from all fwmark 0x9 lookup 999\n"
        route = "default dev wan table 254\n"
        if active:
            nat += "-N MU_OC_NAT\n-A PREROUTING -i br-lan -j MU_OC_NAT\n"
            udp += "-N MU_OC_UDP\n-A PREROUTING -i br-lan -j MU_OC_UDP\n"
            if scope == "test-ip":
                nat += "-A MU_OC_NAT ! -s 192.168.3.120/32 -j RETURN\n"
                udp += "-A MU_OC_UDP ! -s 192.168.3.120/32 -j RETURN\n"
            nat += "-A MU_OC_NAT -p udp -m udp --dport 53 -j REDIRECT --to-ports 7874\n-A MU_OC_NAT -p tcp -m tcp --dport 53 -j REDIRECT --to-ports 7874\n-A MU_OC_NAT -p tcp -j REDIRECT --to-ports 7892\n"
            udp += "-A MU_OC_UDP -p udp -m udp --dport 53 -j RETURN\n"
            policy += "17250: from all fwmark 0x7250 lookup 7250\n"
            if engine == "tun":
                udp += "-A MU_OC_UDP -p udp -j MARK --set-xmark 0x7250/0xffffffff\n-A MU_OC_UDP -p udp -j ACCEPT\n"
                forward += "-N MU_OC_FWD\n-A FORWARD -j MU_OC_FWD\n-A MU_OC_FWD -i br-lan -o mu-oc-tun -p udp -m mark --mark 0x7250 -j ACCEPT\n-A MU_OC_FWD -i mu-oc-tun -o br-lan -p udp -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT\n"
                route += "default dev mu-oc-tun table 7250 scope link\n"
            else:
                target = "192.168.3.1" if family == 4 else "::1"
                udp += f"-A MU_OC_UDP -p udp -j TPROXY --on-port 7895 --on-ip {target} --tproxy-mark 0x7250/0xffffffff\n"
                route += "local default dev lo table 7250\n"
        for table, text in {"nat": nat, "mangle": udp, "filter": forward, "policy": policy, "route": route}.items():
            write(self.root / f"kernel/{family}.{table}", text)

    def inactive(self):
        self.set_config("enable", "0")
        for name in ["core.pid", "clock-ready.pid"]:
            (self.run / name).unlink(missing_ok=True)
        for path in self.run.glob("rules.*"):
            path.unlink()
        for family in [4, 6]:
            self.kernel(family, False)

    def prepare_service_templates(self):
        files = [p for prefix in [self.run, self.proc, self.sys, self.root / "kernel"]
                 for p in prefix.rglob("*") if p.is_file()]
        activation = ["#!/bin/sh", "set -eu"]
        for index, path in enumerate(files):
            template = self.root / "templates" / str(index)
            write(template, path.read_text())
            activation.append(f"mkdir -p '{unix(path.parent)}'")
            activation.append(f"cp '{unix(template)}' '{unix(path)}'")
        write(self.root / "activate.sh", "\n".join(activation) + "\n")
        deactivation = ["#!/bin/sh", "set -eu",
                        'rm -f "$MU_OC_BASE/run/core.pid" "$MU_OC_BASE/run/clock-ready.pid" "$MU_OC_BASE"/run/rules.*']
        for family in [4, 6]:
            for table, text in {"nat": "-P PREROUTING ACCEPT\n", "mangle": "-P PREROUTING ACCEPT\n",
                                "filter": "-P FORWARD DROP\n-A FORWARD -j FIRMWARE_FORWARD\n",
                                "policy": "1000: from all fwmark 0x9 lookup 999\n", "route": "default dev wan table 254\n"}.items():
                target = self.root / f"kernel/{family}.{table}"
                template = self.root / f"templates/off-{family}-{table}"
                write(template, text)
                deactivation.append(f"cp '{unix(template)}' '{unix(target)}'")
        write(self.root / "deactivate.sh", "\n".join(deactivation) + "\n")

    def invoke(self, *args, **extra):
        env = self.env.copy()
        env.update(extra)
        return subprocess.run(
            [BASH, "--noprofile", "--norc", "-c", 'exec /bin/sh "$TEST_SCRIPT" "$@"', "test", *args],
            env=env, capture_output=True, text=True, timeout=22,
        )

    def status(self, expected=None):
        result = self.invoke("status")
        assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
        value = json.loads(result.stdout)
        assert len(result.stdout) < 250, result.stdout
        assert "offline-only-secret" not in result.stdout + result.stderr
        if expected:
            assert value["state"] == expected, value
        return value

    def wait_for(self, path):
        until = time.monotonic() + 12
        while not path.exists():
            if time.monotonic() >= until:
                raise AssertionError(f"Timed out waiting for {path.name}")
            time.sleep(0.04)

    def settled(self):
        until = time.monotonic() + 18
        while time.monotonic() < until:
            phase = (self.run / "screen-control.state").read_text().strip()
            if phase in {"done", "failed"}:
                return phase
            time.sleep(0.05)
        raise AssertionError("Background worker did not settle")

    def snapshot(self):
        return {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for top in [self.base, self.proc, self.sys, self.root / "kernel", self.root / "config"]
                for p in top.rglob("*") if p.is_file()}

    def close(self):
        for path in self.root.glob("gate-*"):
            path.unlink(missing_ok=True)
        if (self.run / "screen-control.state").exists():
            self.settled()
        self.temp.cleanup()


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.lab = Lab()

    def tearDown(self):
        self.lab.close()

    def test_running_requires_real_ready_core_clock_and_kernel_rules(self):
        value = self.lab.status("running")
        for key in ["enabled", "core", "ready", "clock", "rules"]:
            self.assertIs(value[key], True)
        self.assertFalse(value["busy"])

    def test_status_is_read_only_including_absent_lock_and_record(self):
        before = self.lab.snapshot()
        self.lab.status("running")
        self.assertEqual(before, self.lab.snapshot())
        self.assertFalse((self.lab.run / "screen-control.lock").exists())
        calls = (self.lab.root / "calls").read_text()
        self.assertNotIn("openclash-service.sh", calls)
        self.assertNotIn("<set>", calls)
        self.assertNotIn("<commit>", calls)

    def test_status_does_not_truncate_existing_control_files(self):
        write(self.lab.run / "screen-control.lock", "keep-lock-contents\n")
        write(self.lab.run / "screen-control.state", "done\n")
        before = self.lab.snapshot()
        self.lab.status("running")
        self.assertEqual(before, self.lab.snapshot())

    def test_missing_namespace_status_does_not_create_app(self):
        absent = self.lab.root / "absent"
        result = self.lab.invoke("status", MU_OC_BASE=unix(absent))
        value = json.loads(result.stdout)
        self.assertEqual(value["state"], "failed")
        self.assertIsNone(value["enabled"])
        self.assertFalse(absent.exists())

    def test_invalid_namespace_pids_never_enter_or_read_proc_cmdline(self):
        for pid in ["", "0", "1", "-1", "00101", "101/../../1", "101\n102", "999999999999999999", "101; echo bad"]:
            with self.subTest(pid=pid):
                write(self.lab.run / "namespace.pid", pid)
                result = self.lab.invoke("on")
                self.assertEqual(result.returncode, 69)
                self.assertEqual(json.loads(result.stdout)["reason"], "namespace-unavailable")
        self.assertFalse((self.lab.root / "calls").exists())

    def test_namespace_owner_must_be_exact_shell_and_script(self):
        script = (unix(self.lab.base) + "/bin/private-init.sh").encode()
        for value in [b"/bin/sh\0" + script + b".old\0", b"/bin/echo\0" + script + b"\0",
                      b"/bin/sh\0-c\0" + script + b"\0", b"/bin/sh\0" + script + b"\0extra\0"]:
            with self.subTest(value=value):
                self.lab.cmdline(value)
                self.assertEqual(self.lab.invoke("off").returncode, 69)
        self.assertFalse((self.lab.root / "calls").exists())

    def test_only_three_public_commands_and_no_extra_arguments(self):
        for args in [(), ("restart",), ("enable",), ("__worker",), ("on; reboot",), ("status", "on")]:
            with self.subTest(args=args):
                self.assertEqual(self.lab.invoke(*args).returncode, 64)
        self.assertFalse((self.lab.root / "calls").exists())

    def test_enabled_is_not_running(self):
        (self.lab.run / "core.pid").unlink()
        self.assertTrue(self.lab.status("failed")["enabled"])

    def test_foreign_or_reused_core_pid_is_not_accepted(self):
        write(self.lab.proc / "123/exe.target", "/bin/sleep\n")
        value = self.lab.status("failed")
        self.assertFalse(value["core"])
        self.assertNotIn("curl", (self.lab.root / "calls").read_text())

    def test_stale_clock_pid_is_not_ready(self):
        write(self.lab.run / "clock-ready.pid", "122\n")
        self.assertEqual(self.lab.status("failed")["reason"], "clock-not-ready")

    def test_controller_failure_and_missing_dns_or_tun_are_not_ready(self):
        for name in ["api", "dns", "tun"]:
            with self.subTest(name=name):
                self.lab.active()
                if name == "api":
                    (self.lab.root / "fail-api").touch()
                elif name == "dns":
                    write(self.lab.proc / "net/udp", "header\n")
                else:
                    write(self.lab.sys / "class/net/mu-oc-tun/flags", "0x0\n")
                self.assertFalse(self.lab.status("failed")["ready"])
                (self.lab.root / "fail-api").unlink(missing_ok=True)

    def test_marker_only_does_not_prove_installed_rules(self):
        self.lab.kernel(4, False)
        self.assertFalse(self.lab.status("failed")["rules"])

    def test_missing_hooks_dns_mark_forward_policy_or_route_fail(self):
        cases = [("nat", "-A PREROUTING"), ("nat", "-p tcp -m tcp --dport 53"),
                 ("mangle", "--set-xmark"), ("filter", "-A MU_OC_FWD -i br-lan"),
                 ("policy", "17250:"), ("route", "table 7250")]
        for table, fragment in cases:
            with self.subTest(table=table, fragment=fragment):
                self.lab.active()
                path = self.lab.root / f"kernel/4.{table}"
                write(path, "\n".join(line for line in path.read_text().splitlines() if fragment not in line) + "\n")
                self.assertFalse(self.lab.status("failed")["rules"])

    def test_missing_ipv6_rules_or_wrong_ownership_metadata_fail(self):
        self.lab.kernel(6, False)
        self.assertFalse(self.lab.status("failed")["rules"])
        self.lab.active()
        write(self.lab.run / "rules.engine", "tproxy\n")
        self.assertFalse(self.lab.status("failed")["rules"])

    def test_kernel_query_error_is_failed_not_disabled(self):
        self.lab.inactive()
        (self.lab.root / "fail-rules-read").touch()
        self.assertEqual(self.lab.status("failed")["reason"], "inspection-failed")

    def test_disabled_requires_no_core_and_no_kernel_residue(self):
        self.lab.inactive()
        self.lab.status("disabled")
        # No marker: detect a leftover application chain/hook directly.
        write(self.lab.root / "kernel/4.nat", "-N MU_OC_NAT\n")
        self.assertEqual(self.lab.status("failed")["reason"], "disabled-with-residue")

    def test_scope_off_and_test_ip_preserve_their_meaning(self):
        for scope in ["off", "test-ip"]:
            with self.subTest(scope=scope):
                self.lab.active(scope=scope)
                self.lab.status("running")

    def test_tproxy_snapshot_is_supported_without_tun_rules(self):
        self.lab.active(engine="tproxy")
        self.lab.status("running")

    def test_invalid_enable_and_port_fail_without_secret_output(self):
        self.lab.set_config("enable", "garbage")
        self.assertIsNone(self.lab.status("failed")["enabled"])
        self.lab.set_config("enable", "1")
        self.lab.set_config("dns_port", "65536")
        self.assertEqual(self.lab.status("failed")["reason"], "invalid-port")

    def test_stuck_read_probe_is_bounded(self):
        (self.lab.root / "hang-uci").touch()
        started = time.monotonic()
        self.lab.status("failed")
        self.assertLess(time.monotonic() - started, 6)

    def test_shared_monotonic_deadline_stops_additional_probes(self):
        (self.lab.root / "advance-uci").touch()
        value = json.loads(self.lab.invoke("status", MU_OC_STATUS_TIMEOUT="1").stdout)
        self.assertEqual(value["state"], "failed")
        calls = (self.lab.root / "calls").read_text()
        self.assertEqual(calls.count("oc-namespace"), 1)
        self.assertNotIn("iptables", calls)

    def test_unheld_stale_transition_is_failed(self):
        self.lab.inactive()
        self.lab.set_config("enable", "1")
        write(self.lab.run / "screen-control.lock", "")
        write(self.lab.run / "screen-control.state", "starting\n")
        self.assertEqual(self.lab.status("failed")["reason"], "last-control-failed")
        write(self.lab.run / "screen-control.state", "failed\n")

    def test_boot_service_lock_distinguishes_starting_and_stopping(self):
        lock = self.lab.run / "service.lock"
        write(lock, "")
        holder = None
        gate = self.lab.root / "service-gate"
        if FLOCK:
            gate.touch()
            script = '''export PATH="$MU_OC_TEST_BIN:/usr/bin:/bin"
exec 9>"$MU_OC_BASE/run/service.lock"
flock -n -x 9 || exit 1
touch "$TEST_ROOT/service-held"
while [ -e "$TEST_ROOT/service-gate" ]; do sleep 0.05; done
'''
            holder = subprocess.Popen([BASH, "-c", script], env=self.lab.env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.lab.wait_for(self.lab.root / "service-held")
        else:
            (self.lab.root / "busy-service").touch()
        try:
            (self.lab.run / "core.pid").unlink()
            self.lab.status("starting")
            self.lab.set_config("enable", "0")
            self.lab.status("stopping")
        finally:
            gate.unlink(missing_ok=True)
            if holder:
                holder.wait(timeout=4)

    def test_on_dispatches_before_start_finishes_and_serializes_clicks(self):
        self.lab.inactive()
        gate = self.lab.root / "gate-start"
        gate.touch()
        response = self.lab.invoke("on")
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertTrue(json.loads(response.stdout)["accepted"])
        self.lab.wait_for(self.lab.root / "entered-start")
        self.lab.status("starting")
        contended = self.lab.invoke("off")
        self.assertEqual(contended.returncode, 75)
        self.assertEqual(json.loads(contended.stdout)["reason"], "busy")
        gate.unlink()
        self.assertEqual(self.lab.settled(), "done")
        self.lab.status("running")
        self.assertEqual((self.lab.root / "service-calls").read_text().splitlines(), ["enable", "start"])
        self.assertFalse((self.lab.root / "leaked-lock").exists())

    def test_off_is_async_disables_then_stops_and_preserves_user_data(self):
        before = {name: (self.lab.base / name).read_bytes() for name in self.lab.protected}
        gate = self.lab.root / "gate-stop"
        gate.touch()
        self.assertEqual(self.lab.invoke("off").returncode, 0)
        self.lab.wait_for(self.lab.root / "entered-stop")
        value = self.lab.status("stopping")
        self.assertFalse(value["enabled"])
        gate.unlink()
        self.assertEqual(self.lab.settled(), "done")
        self.lab.status("disabled")
        self.assertEqual((self.lab.root / "service-calls").read_text().splitlines(), ["disable", "stop"])
        self.assertFalse((self.lab.root / "leaked-lock").exists())
        for name, data in before.items():
            self.assertEqual((self.lab.base / name).read_bytes(), data)

    def test_failed_enable_does_not_start_and_can_be_retried(self):
        self.lab.inactive()
        self.assertEqual(self.lab.invoke("on", TEST_FAIL_ACTION="enable").returncode, 0)
        self.assertEqual(self.lab.settled(), "failed")
        self.assertEqual((self.lab.root / "service-calls").read_text().splitlines(), ["enable"])
        self.lab.status("disabled")
        self.assertEqual(self.lab.invoke("on").returncode, 0)
        self.assertEqual(self.lab.settled(), "done")
        self.lab.status("running")

    def test_start_failure_is_not_a_permanent_starting_state(self):
        self.lab.inactive()
        self.assertEqual(self.lab.invoke("on", TEST_FAIL_ACTION="start").returncode, 0)
        self.assertEqual(self.lab.settled(), "failed")
        self.assertEqual(self.lab.status("failed")["reason"], "last-control-failed")

    def test_successful_command_without_actual_ready_state_is_failed(self):
        self.lab.inactive()
        self.assertEqual(self.lab.invoke("on", TEST_START_NOOP="1").returncode, 0)
        self.assertEqual(self.lab.settled(), "failed")
        value = self.lab.status("failed")
        self.assertTrue(value["enabled"])
        self.assertFalse(value["core"])

    def test_stop_failure_reports_actual_remaining_core_and_rules(self):
        self.assertEqual(self.lab.invoke("off", TEST_FAIL_ACTION="stop").returncode, 0)
        self.assertEqual(self.lab.settled(), "failed")
        value = self.lab.status("failed")
        self.assertFalse(value["enabled"])
        self.assertTrue(value["core"])
        self.assertTrue(value["rules"])


if __name__ == "__main__":
    syntax = subprocess.run([BASH, "-n", unix(SOURCE)], capture_output=True, text=True, timeout=5)
    if syntax.returncode:
        raise SystemExit(syntax.stderr)
    print("Lock coverage:", "native flock" if FLOCK else "Git Bash contention model; native flock requires Linux", flush=True)
    unittest.main(verbosity=2)
