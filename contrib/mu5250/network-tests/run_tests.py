"""Run the unchanged shell script in Git Bash with offline network-command mocks.

All generated files remain in this directory. A nonzero exit means a requirement
failed; known bugs are deliberately not marked expected failures.
"""
from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT.parent / "network-rules.sh"
BASH = Path(r"C:\Program Files\Git\bin\bash.exe") if os.name == "nt" else Path(shutil.which("bash"))
WORK = ROOT / "work"
OWNED_CHAINS = {"MU_OC_NAT", "MU_OC_UDP", "MU_OC_FWD"}
RESULTS = []
ONLY = sys.argv[1:]  # Optional case-name fragments; merge selected reruns by name.


def bash_path(path):
    value = str(Path(path).resolve()).replace("\\", "/")
    return "/" + value[0].lower() + value[2:] if len(value) > 1 and value[1] == ":" else value


def seed():
    tables = {}
    for family in (4, 6):
        for table in ("nat", "mangle", "filter"):
            tables[f"{family}:{table}"] = {
                "PREROUTING": [["-j", "FIRMWARE_PRE"]],
                "FIRMWARE_PRE": [["-j", "RETURN"]],
                "INPUT": [["-p", "tcp", "--dport", "22", "-j", "ACCEPT"]],
                "OUTPUT": [], "POSTROUTING": [],
            }
            if table == "filter":
                tables[f"{family}:{table}"].update({
                    "FORWARD": [["-j", "FIRMWARE_FORWARD"]],
                    "FIRMWARE_FORWARD": [["-j", "DROP"]],
                })
    return {"tables": tables, "rules": [
                {"family": 4, "pref": 1000, "fwmark": "0x9/0xffffffff", "table": 999},
                {"family": 6, "pref": 1000, "fwmark": "0x9/0xffffffff", "table": 999}],
            "routes": [{"family": 4, "table": 999, "spec": ["default", "dev", "firmware", "table", "999"]},
                       {"family": 4, "table": 254, "spec": ["default", "via", "192.168.0.1", "dev", "wan", "table", "254"]},
                       {"family": 4, "table": 255, "spec": ["local", "192.168.3.1/32", "dev", "br-lan", "table", "255"]},
                       {"family": 6, "table": 254, "spec": ["default", "dev", "wan", "table", "254"]},
                       {"family": 6, "table": 255, "spec": ["local", "::1/128", "dev", "lo", "table", "255"]}],
            "interfaces": ["br-lan", "br-guest", "lo", "wan", "firmware", "mu-oc-tun"],
            "addresses": {"4": ["192.168.3.1/24", "203.0.113.9/24"],
                          "6": ["fe80::1/64", "fd00:3::1/64", "2001:db8:3::1/64"]},
            "uci": {"mu5250_proxy_scope": "test-ip", "mu5250_test_ip": "192.168.3.120",
                    "proxy_port": "17892", "tproxy_port": "17895", "dns_port": "17874"},
            "calls": [], "injections": []}


class Lab:
    def __init__(self, name, scope="test-ip", engine=None):
        self.folder = WORK / name
        if self.folder.exists():
            assert self.folder.resolve().parent == WORK.resolve()
            shutil.rmtree(self.folder)
        self.folder.mkdir(parents=True)
        self.state_file = self.folder / "state.json"
        self.base = self.folder / "base"
        self.state = seed()
        self.state["uci"]["mu5250_proxy_scope"] = scope
        if engine is not None:
            self.state["uci"]["mu5250_udp_engine"] = engine
        self.initial = copy.deepcopy(self.state)
        self.save()

    def save(self):
        self.state_file.write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    def run(self, action="start", iface=None, iface_exists=True, tun_exists=True):
        self.save()
        env = os.environ.copy()
        env.update({"MU_OC_BASE": bash_path(self.base), "MOCK_STATE": str(self.state_file),
                    "MOCK_DISPATCH": bash_path(ROOT / "mock_net.py"), "MOCK_PYTHON": bash_path(sys.executable),
                    "MOCK_BIN": bash_path(ROOT / "bin"), "MOCK_SIGNAL_FILE": str(self.folder / "signal"),
                    "MOCK_SIGNAL_SHELL": bash_path(self.folder / "signal"),
                    "MOCK_IFACE_EXISTS": "yes" if iface_exists else "no",
                    "MOCK_TUN_EXISTS": "yes" if tun_exists else "no", "PYTHONDONTWRITEBYTECODE": "1"})
        env.pop("MU_OC_LAN", None)
        if iface:
            env["MU_OC_LAN"] = iface
        completed = subprocess.run([str(BASH), "--noprofile", "--norc", "--posix", str(ROOT / "launcher.sh"),
                                    bash_path(ROOT / "reviewed-network-rules.sh"), action],
                                   cwd=ROOT, env=env, capture_output=True, text=True, timeout=90)
        self.state = json.loads(self.state_file.read_text(encoding="utf-8"))
        with (self.folder / "shell.log").open("a", encoding="utf-8") as log:
            log.write(f"{action}, iface={iface or 'br-lan'}, exit={completed.returncode}\n")
            log.write(completed.stdout + completed.stderr)
        unsupported = [c for c in self.state["calls"] if c.get("mock_error")]
        assert not unsupported, f"unsupported mock operation(s): {unsupported}"
        return completed

    def install(self):
        result = self.run()
        assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)

    def inject(self, command, args, action="fail", **extra):
        self.state["injections"].append({"command": command, "args": args, "action": action, **extra})

    def assert_clean(self):
        for key, chains in self.state["tables"].items():
            assert not OWNED_CHAINS.intersection(chains), f"owned chains remain in {key}"
            for rules in chains.values():
                assert not any("-j" in r and r[r.index("-j") + 1] in OWNED_CHAINS for r in rules), key
        assert self.state["tables"] == self.initial["tables"], "firmware firewall changed"
        assert self.state["routes"] == self.initial["routes"], "pre-existing routes changed"
        assert self.state["rules"] == self.initial["rules"], "pre-existing policy rules changed"
        for name in ("rules.owned", "rules.scope", "rules.interface", "rules.families", "rules.engine"):
            assert not (self.base / "run" / name).exists(), f"{name} remains"


def mark_matches(actual, selector):
    value, _, mask = selector.partition("/")
    mask = int(mask, 0) if mask else 0xffffffff
    return (actual & mask) == int(value, 0)


def walk_chain(state, family, table, chain, packet):
    """A MARK target continues traversal; ACCEPT terminates its current table."""
    for rule in state["tables"][f"{family}:{table}"][chain]:
        matches, negate, i = True, False, 0
        while i < len(rule):
            token = rule[i]
            if token == "!":
                negate, i = True, i + 1
                continue
            if token == "-j":
                break
            value = rule[i + 1]
            if token == "-m":
                assert value in ("mark", "conntrack"), rule
                i += 2
                continue
            if token == "-p":
                match = packet["protocol"] == value
            elif token == "--dport":
                match = packet["port"] == int(value)
            elif token in ("-i", "-o"):
                match = packet["iface" if token == "-i" else "out_iface"] == value
            elif token in ("-s", "-d"):
                match = ipaddress.ip_address(packet["src" if token == "-s" else "dst"]) in ipaddress.ip_network(value, strict=False)
            elif token == "--mark":
                match = mark_matches(packet["mark"], value)
            elif token == "--ctstate":
                match = packet["ctstate"] in value.split(",")
            else:
                raise AssertionError(f"unsupported packet predicate: {rule}")
            matches = matches and (not match if negate else match)
            negate, i = False, i + 2
        if not matches:
            continue
        target = rule[rule.index("-j") + 1]
        if target == "RETURN":
            return None
        if target == "MARK":
            value, mask = rule[rule.index("--set-xmark") + 1].split("/")
            packet["mark"] = ((packet["mark"] & ~int(mask, 0)) ^ int(value, 0)) & 0xffffffff
            continue
        if target == "REDIRECT":
            return (target, int(rule[rule.index("--to-ports") + 1]))
        if target == "TPROXY":
            value, mask = rule[rule.index("--tproxy-mark") + 1].split("/")
            packet["mark"] = ((packet["mark"] & ~int(mask, 0)) ^ int(value, 0)) & 0xffffffff
            return (target, int(rule[rule.index("--on-port") + 1]))
        if target in ("ACCEPT", "DROP"):
            return (target, None)
        verdict = walk_chain(state, family, table, target, packet)
        if verdict:
            return verdict
    return None


def policy_route(state, family, dst, mark):
    from mock_net import route_parts
    for rule in sorted((r for r in state["rules"] if r["family"] == family), key=lambda r: r["pref"]):
        if not mark_matches(mark, rule["fwmark"]):
            continue
        routes = [r for r in state["routes"] if r["family"] == family and r["table"] == rule["table"]
                  and ipaddress.ip_address(dst) in ipaddress.ip_network(route_parts(r["spec"], family)["prefix"])]
        if routes:
            return min(routes, key=lambda r: (-ipaddress.ip_network(route_parts(r["spec"], family)["prefix"]).prefixlen,
                                              int(route_parts(r["spec"], family).get("metric", 0))))
    return None


def forward_verdict(state, family, protocol="udp", iface="br-lan", out_iface="mu-oc-tun", mark=0, ctstate="NEW"):
    packet = {"protocol": protocol, "port": 443, "iface": iface, "out_iface": out_iface,
              "mark": mark, "ctstate": ctstate,
              "src": "192.168.3.120" if family == 4 else "2001:db8:3::120",
              "dst": "1.1.1.1" if family == 4 else "2606:4700:4700::1111"}
    return walk_chain(state, family, "filter", "FORWARD", packet)


def trace_packet(state, family, protocol, dst, src=None, port=443, iface="br-lan", initial_mark=0):
    from mock_net import route_parts
    packet = {"protocol": protocol, "port": port, "iface": iface, "out_iface": None, "mark": initial_mark,
              "ctstate": "NEW", "src": src or ("192.168.3.120" if family == 4 else "2001:db8:3::120"), "dst": dst}
    mangle = walk_chain(state, family, "mangle", "PREROUTING", packet)
    # Mangle ACCEPT ends that table, not NAT or the routing/forwarding stages.
    nat = None if mangle and mangle[0] == "DROP" else walk_chain(state, family, "nat", "PREROUTING", packet)
    route = policy_route(state, family, dst, packet["mark"])
    result = nat or (mangle if mangle and mangle[0] in ("TPROXY", "DROP") else None)
    forwarding = None
    if not result and route and route_parts(route["spec"], family).get("dev") == "mu-oc-tun":
        forwarding = forward_verdict(state, family, protocol, iface, "mu-oc-tun", packet["mark"])
        result = ("TUN", "mu-oc-tun") if forwarding == ("ACCEPT", None) else ("DROP", None)
    return {"result": result, "mark": packet["mark"], "route": route, "forward": forwarding}


def evaluate(state, family, protocol, dst, src=None, port=443, iface="br-lan"):
    return trace_packet(state, family, protocol, dst, src, port, iface)["result"]


def case(name):
    def decorator(fn):
        if ONLY and not any(fragment in name for fragment in ONLY):
            return fn
        started = time.monotonic()
        try:
            detail = fn()
            result = {"name": name, "pass": True, "detail": detail or ""}
        except Exception as exc:
            result = {"name": name, "pass": False, "detail": str(exc), "traceback": traceback.format_exc()}
        result["seconds"] = round(time.monotonic() - started, 2)
        RESULTS.append(result)
        print(f"{'PASS' if result['pass'] else 'FAIL'} {name}: {result['detail']}", flush=True)
        return fn
    return decorator


def main():
    ROOT.mkdir(exist_ok=True)
    WORK.mkdir(exist_ok=True)
    (ROOT / "bin").mkdir(exist_ok=True)
    source = SOURCE.read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    (ROOT / "reviewed-network-rules.sh").write_bytes(source)
    for name in ("iptables", "ip6tables", "ip", "uci", "flock"):
        wrapper = ('#!/bin/bash\n'
                   'if [ -e /dev/fd/9 ]; then export MOCK_FD9_OPEN=yes; fi\n'
                   f'"$MOCK_PYTHON" "$MOCK_DISPATCH" {name} "$@"\n'
                   'mock_rc=$?\n'
                   'if [ -f "$MOCK_SIGNAL_SHELL" ]; then\n'
                   '  read -r mock_signal < "$MOCK_SIGNAL_SHELL" || true\n'
                   '  /usr/bin/rm -f "$MOCK_SIGNAL_SHELL"\n'
                   '  kill -s "$mock_signal" "$PPID"\n'
                   'fi\n'
                   'exit "$mock_rc"\n')
        path = ROOT / "bin" / name
        path.write_text(wrapper, encoding="utf-8", newline="\n")
        path.chmod(0o755)
    syntax = subprocess.run([str(BASH), "-n", str(ROOT / "reviewed-network-rules.sh")], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr

    @case("test-ip packet matrix, custom app ports, default br-lan")
    def packet_test_ip():
        lab = Lab("test-ip")
        lab.install()
        for dst in ("192.168.3.1", "192.168.0.1", "10.0.0.1", "172.16.2.1", "100.64.0.1", "127.0.0.1", "169.254.1.1"):
            for protocol in ("tcp", "udp"):
                for port in (22, 80, 443, 8081, 9090):
                    assert evaluate(lab.state, 4, protocol, dst, port=port) is None, (dst, protocol, port)
                assert evaluate(lab.state, 4, protocol, dst, port=53) == ("REDIRECT", 17874), dst
        for protocol, expected in (("tcp", ("REDIRECT", 17892)), ("udp", ("TPROXY", 17895))):
            assert evaluate(lab.state, 4, protocol, "1.1.1.1") == expected
            assert evaluate(lab.state, 4, protocol, "1.1.1.1", port=53) == ("REDIRECT", 17874)
            for other in ("192.168.3.119", "192.168.3.121", "10.0.0.5"):
                for dst in ("1.1.1.1", "192.168.3.1"):
                    for port in (53, 443):
                        assert evaluate(lab.state, 4, protocol, dst, src=other, port=port) is None
            assert evaluate(lab.state, 4, protocol, "1.1.1.1", iface="wan") is None
        assert not OWNED_CHAINS.intersection(lab.state["tables"]["6:nat"])
        assert lab.state["calls"][0]["command"] == "flock"
        assert lab.state["calls"][0]["fd9_inherited"]
        assert (lab.base / "run" / "rules.scope").read_text().strip() == "test-ip"
        return "management + DNS ordering, other clients/interfaces, TCP/UDP passed"

    @case("all scope IPv4/IPv6 packet and policy matrix")
    def packet_all():
        lab = Lab("all", "all")
        lab.install()
        for family, public, source in ((4, "8.8.8.8", "192.168.3.121"), (6, "2606:4700:4700::1111", "2001:db8:3::121")):
            for protocol, expected in (("tcp", ("REDIRECT", 17892)), ("udp", ("TPROXY", 17895))):
                assert evaluate(lab.state, family, protocol, public, src=source) == expected
                assert evaluate(lab.state, family, protocol, public, src=source, port=53) == ("REDIRECT", 17874)
            assert {"family": family, "pref": 17250, "fwmark": "0x7250/0xffffffff", "table": 7250} in lab.state["rules"]
            assert any(r["family"] == family and r["table"] == 7250 and "local" in r["spec"] for r in lab.state["routes"])
        for dst in ("::1", "fd00:3::1", "fe80::1", "ff02::1", "2001:db8:3::1"):
            for protocol in ("tcp", "udp"):
                assert evaluate(lab.state, 6, protocol, dst) is None, dst
                assert evaluate(lab.state, 6, protocol, dst, port=53) == ("REDIRECT", 17874), dst

    @case("stop preserves firmware firewall and unrelated policies/routes")
    def normal_stop():
        lab = Lab("normal-stop", "all")
        lab.install()
        assert lab.run("stop").returncode == 0
        lab.assert_clean()
        assert lab.run("stop").returncode == 0
        lab.assert_clean()

    @case("reload is idempotent; all -> test-ip removes IPv6; off removes rules")
    def transitions():
        lab = Lab("transitions", "all")
        lab.install()
        original_tables = copy.deepcopy(lab.state["tables"])
        assert lab.run("reload").returncode == 0
        assert lab.state["tables"] == original_tables
        lab.state["uci"]["mu5250_proxy_scope"] = "test-ip"
        assert lab.run("reload").returncode == 0
        assert not OWNED_CHAINS.intersection(lab.state["tables"]["6:nat"])
        lab.state["uci"]["mu5250_proxy_scope"] = "off"
        assert lab.run("reload").returncode == 0
        lab.assert_clean()

    for name, command, args in (
        ("ipv4-policy", "ip", ["-4", "rule", "add", "fwmark", "0x7250/0xffffffff", "table", "7250", "pref", "17250"]),
        ("ipv4-last-hook", "iptables", ["-t", "mangle", "-I", "PREROUTING", "1", "-i", "br-lan", "-j", "MU_OC_UDP"]),
        ("ipv6-route", "ip", ["-6", "route", "add", "local", "::/0", "dev", "lo", "table", "7250"]),
        ("ipv6-last-hook", "ip6tables", ["-t", "mangle", "-I", "PREROUTING", "1", "-i", "br-lan", "-j", "MU_OC_UDP"]),
    ):
        @case(f"failed installation rolls back: {name}")
        def rollback(name=name, command=command, args=args):
            lab = Lab("rollback-" + name, "all")
            lab.inject(command, args)
            result = lab.run()
            assert result.returncode != 0
            assert lab.state["injections"][0]["used"]
            lab.assert_clean()

    for resource in ("chain4", "chain6", "priority4", "priority6"):
        @case(f"unowned {resource} collision refuses before mutation")
        def collision(resource=resource):
            lab = Lab("collision-" + resource, "all")
            family = int(resource[-1])
            if resource.startswith("chain"):
                lab.state["tables"][f"{family}:nat"]["MU_OC_NAT"] = [["-j", "RETURN"]]
            else:
                lab.state["rules"].append({"family": family, "pref": 17250, "fwmark": "0x42/0xffffffff", "table": 42})
            before = copy.deepcopy(lab.state)
            assert lab.run().returncode == 73
            for key in ("tables", "routes", "rules"):
                assert lab.state[key] == before[key], key
            assert not (lab.base / "run" / "rules.owned").exists()

    @case("occupied table7250 is rejected and foreign routes preserved")
    def table_collision():
        lab = Lab("collision-table", "all")
        foreign = {"family": 4, "table": 7250, "spec": ["192.0.2.0/24", "dev", "firmware", "table", "7250"]}
        lab.state["routes"].append(foreign)
        result = lab.run()
        start_rc = result.returncode
        if result.returncode == 0:
            lab.run("stop")
        assert foreign in lab.state["routes"], f"foreign table7250 route erased; start exit={start_rc}"
        assert start_rc != 0, "occupied routing table was accepted"

    @case("stop uses recorded install interface after MU_OC_LAN change")
    def changed_interface():
        lab = Lab("changed-interface")
        lab.install()
        assert lab.run("stop", iface="br-guest").returncode == 0
        lab.assert_clean()

    @case("reload rejects newly occupied IPv6 priority after test-ip")
    def new_priority():
        lab = Lab("new-priority")
        lab.install()
        foreign = {"family": 6, "pref": 17250, "fwmark": "0x7250/0xffffffff", "table": 42}
        lab.state["rules"].append(foreign)
        lab.state["routes"].append({"family": 6, "table": 42, "spec": ["default", "dev", "firmware", "table", "42"]})
        lab.state["uci"]["mu5250_proxy_scope"] = "all"
        result = lab.run("reload")
        occupied = [r for r in lab.state["rules"] if r["family"] == 6 and r["pref"] == 17250]
        assert result.returncode != 0 and occupied == [foreign], f"reload exit={result.returncode}, priority17250 rules={occupied}"

    @case("TERM during final hook installation aborts and removes all state")
    def term_trap():
        lab = Lab("term-trap")
        lab.inject("iptables", ["-t", "mangle", "-I", "PREROUTING", "1", "-i", "br-lan", "-j", "MU_OC_UDP"], action="signal")
        result = lab.run()
        assert lab.state["injections"][0]["used"]
        errors = []
        if result.returncode == 0:
            errors.append(f"reported success after TERM: {result.stdout.strip()}")
        try:
            lab.assert_clean()
        except AssertionError as exc:
            errors.append(str(exc))
        assert not errors, "; ".join(errors)

    @case("router assigned global IPv4 management address bypasses except DNS")
    def ipv4_management():
        lab = Lab("ipv4-management")
        lab.install()
        for protocol in ("tcp", "udp"):
            result = evaluate(lab.state, 4, protocol, "203.0.113.9", port=8081)
            assert result is None, f"router's assigned IPv4 management traffic {protocol}/8081 -> {result}"
            assert evaluate(lab.state, 4, protocol, "203.0.113.9", port=53) == ("REDIRECT", 17874)

    @case("lock failure prevents all rule mutations")
    def lock_failure():
        lab = Lab("lock-failure")
        lab.inject("flock", ["-x", "9"])
        assert lab.run().returncode != 0
        lab.assert_clean()
        assert len(lab.state["calls"]) == 1

    @case("invalid interface rejects before rule mutation")
    def bad_interface():
        lab = Lab("bad-interface")
        assert lab.run(iface_exists=False).returncode != 0
        lab.assert_clean()
        assert not any(c["command"] in ("ip", "iptables", "ip6tables") for c in lab.state["calls"])

    @case("invalid port rejects before rule mutation")
    def bad_port():
        lab = Lab("bad-port")
        lab.state["uci"]["proxy_port"] = "65536"
        assert lab.run().returncode == 64
        lab.assert_clean()

    @case("invalid test IPv4 rejects before replacing working rules")
    def bad_test_ip():
        lab = Lab("bad-test-ip")
        lab.install()
        before = copy.deepcopy(lab.state["tables"])
        lab.state["uci"]["mu5250_test_ip"] = "192.168.3.999"
        result = lab.run("reload")
        assert result.returncode == 64 and lab.state["tables"] == before, f"exit={result.returncode}; previous working rules preserved={lab.state['tables'] == before}"

    @case("failed IPv6 local-address discovery aborts and rolls back")
    def failed_address_discovery():
        lab = Lab("failed-address-discovery", "all")
        lab.inject("ip", ["-o", "-6", "addr", "show"])
        result = lab.run()
        assert lab.state["injections"][0]["used"]
        redirect = evaluate(lab.state, 6, "tcp", "2001:db8:3::1", port=8081)
        assert result.returncode != 0, f"exit=0 despite address query failure; local IPv6 tcp/8081 -> {redirect}"
        lab.assert_clean()

    @case("failed hook removal retains ownership and reports failure")
    def failed_hook_removal():
        lab = Lab("failed-hook-removal")
        lab.install()
        lab.inject("iptables", ["-t", "nat", "-D", "PREROUTING", "-i", "br-lan", "-j", "MU_OC_NAT"])
        result = lab.run("stop")
        remains = [r for r in lab.state["tables"]["4:nat"]["PREROUTING"] if "MU_OC_NAT" in r]
        marker = (lab.base / "run" / "rules.owned").exists()
        assert result.returncode != 0 and marker, f"stop exit={result.returncode}; remaining hooks={remains}; ownership retained={marker}"

    from tun_cases import register_tests
    register_tests(sys.modules[__name__])

    if ONLY and (ROOT / "results.json").exists():
        previous = json.loads((ROOT / "results.json").read_text(encoding="utf-8"))
        if previous["sha256"] == digest:
            updated_names = {r["name"] for r in RESULTS}
            RESULTS[:0] = [r for r in previous["tests"] if r["name"] not in updated_names]
    report = {"source": str(SOURCE), "sha256": digest, "source_unchanged_at_end": SOURCE.read_bytes() == source,
              "shell": str(BASH), "shell_mode": "bash --posix", "python": sys.version,
              "passed": sum(r["pass"] for r in RESULTS), "failed": sum(not r["pass"] for r in RESULTS), "tests": RESULTS}
    (ROOT / "results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"{report['passed']} passed; {report['failed']} failed; source sha256={digest}; source unchanged={report['source_unchanged_at_end']}", flush=True)
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
