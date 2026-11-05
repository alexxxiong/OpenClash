"""Offline command dispatcher. No subprocesses, sockets, or host network access."""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import sys


def route_parts(spec, family):
    """Canonical route selectors; tolerate kernel-added proto/scope attributes."""
    kind = "local" if spec[0] == "local" else "unicast"
    prefix = spec[1] if kind == "local" else spec[0]
    if prefix == "default":
        prefix = "0.0.0.0/0" if family == 4 else "::/0"
    result = {"kind": kind, "prefix": str(ipaddress.ip_network(prefix, strict=False))}
    for option in ("dev", "via", "metric", "proto", "scope", "table"):
        if option in spec:
            result[option] = spec[spec.index(option) + 1]
    return result


def main():
    state_path = Path(os.environ["MOCK_STATE"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    command, *args = sys.argv[1:]
    if command in ("iptables", "ip6tables") and args[:2] == ["-w", "5"]:
        args = args[2:]
    event = {"command": command, "args": args}
    state["calls"].append(event)
    rc, output = 0, ""
    try:
        injection = next((item for item in state["injections"]
                          if not item.get("used") and item["command"] == command
                          and item["args"] == args), None)
        if injection:
            injection["used"] = True
        if injection and injection["action"] == "fail":
            raise ValueError("injected command failure")
        if command == "flock":
            if args != ["-x", "9"]:
                raise ValueError("unsupported lock invocation")
            event["fd9_inherited"] = os.environ.get("MOCK_FD9_OPEN") == "yes"
            if not event["fd9_inherited"]:
                raise ValueError("lock descriptor not open")
        elif command == "uci":
            assert args[:2] == ["-q", "get"], args
            key = args[2].split(".")[-1]
            if key not in state["uci"]:
                raise ValueError("missing uci key")
            output = str(state["uci"][key])
        elif command in ("iptables", "ip6tables"):
            family = 4 if command == "iptables" else 6
            assert args[0] == "-t", args
            table, op, chain, *rule = args[1:]
            chains = state["tables"][f"{family}:{table}"]
            if op == "-N":
                if chain in chains:
                    raise ValueError("chain exists")
                chains[chain] = []
            elif chain not in chains:
                raise ValueError("chain missing")
            elif op == "-L":
                output = f"Chain {chain}"
            elif op == "-F":
                chains[chain] = []
            elif op == "-X":
                if chains[chain] or any("-j" in r and r[r.index("-j") + 1] == chain
                                        for rules in chains.values() for r in rules):
                    raise ValueError("chain in use or not empty")
                del chains[chain]
            elif op in ("-A", "-I"):
                position = int(rule.pop(0)) - 1 if op == "-I" else len(chains[chain])
                for option in ("-s", "-d"):
                    if option in rule:
                        network = ipaddress.ip_network(rule[rule.index(option) + 1], strict=False)
                        if network.version != family:
                            raise ValueError("wrong address family")
                target = rule[rule.index("-j") + 1]
                if target not in ("RETURN", "REDIRECT", "TPROXY", "MARK", "ACCEPT", "DROP") and target not in chains:
                    raise ValueError("target chain missing")
                if target == "MARK":
                    value, mask = rule[rule.index("--set-xmark") + 1].split("/")
                    assert 0 <= int(value, 0) <= 0xffffffff and 0 <= int(mask, 0) <= 0xffffffff
                chains[chain].insert(position, rule)
            elif op == "-C":
                if rule not in chains[chain]:
                    raise ValueError("rule missing")
            elif op == "-D":
                if rule not in chains[chain]:
                    raise ValueError("rule missing")
                chains[chain].remove(rule)
            else:
                raise AssertionError((command, args))
        elif command == "ip":
            if args[:3] in (["-o", "-6", "addr"], ["-o", "-4", "addr"]):
                family = int(args[1][1:])
                output = "\n".join(f"{i} br-lan    inet{'6' if family == 6 else ''} {a} scope global"
                                   for i, a in enumerate(state["addresses"][str(family)], 1))
            else:
                family = int(args[0][1:])
                kind, op, *spec = args[1:]
                if kind == "rule" and op == "show":
                    output = "\n".join(f"{r['pref']}: from all fwmark {r['fwmark']} lookup {r['table']}"
                                       for r in state["rules"] if r["family"] == family)
                elif kind == "rule":
                    values = dict(zip(spec[::2], spec[1::2]))
                    rule = {"family": family, "pref": int(values["pref"]),
                            "fwmark": values["fwmark"], "table": int(values["table"])}
                    if op == "add":
                        state["rules"].append(rule)  # Linux permits duplicate priorities.
                    elif op == "del":
                        if rule not in state["rules"]:
                            raise ValueError("policy rule missing")
                        state["rules"].remove(rule)
                    else:
                        raise AssertionError(args)
                elif kind == "route":
                    table_value = spec[spec.index("table") + 1]
                    table = None if table_value == "all" else int(table_value)
                    if op == "add":
                        route = {"family": family, "table": table, "spec": spec}
                        selectors = route_parts(spec, family)
                        assert "dev" in selectors
                        if selectors["dev"] not in state["interfaces"]:
                            raise ValueError("route device missing")
                        if any(r["family"] == family and r["table"] == table
                               and all(route_parts(r["spec"], family).get(key, "0") == selectors.get(key, "0")
                                       for key in ("prefix", "metric")) for r in state["routes"]):
                            raise ValueError("route exists")
                        state["routes"].append(route)
                    elif op == "flush":
                        state["routes"] = [r for r in state["routes"]
                                           if (r["family"], r["table"]) != (family, table)]
                    elif op == "show":
                        output = "\n".join(" ".join(r["spec"]) for r in state["routes"]
                                           if r["family"] == family and (table is None or r["table"] == table))
                    elif op == "del":
                        selectors = route_parts(spec, family)
                        candidates = [r for r in state["routes"] if r["family"] == family and r["table"] == table
                                      and all(route_parts(r["spec"], family).get(key) == value
                                              for key, value in selectors.items())]
                        if not candidates:
                            raise ValueError("route missing")
                        route = min(candidates, key=lambda r: int(route_parts(r["spec"], family).get("metric", 0)))
                        state["routes"].remove(route)
                    else:
                        raise AssertionError(args)
                else:
                    raise AssertionError(args)
        else:
            raise AssertionError((command, args))
        if injection and injection["action"] == "signal":
            Path(os.environ["MOCK_SIGNAL_FILE"]).write_text(injection.get("signal", "TERM"))
    except (ValueError, AssertionError, KeyError) as exc:
        rc = 1
        event["error"] = str(exc)
        if isinstance(exc, (AssertionError, KeyError)):
            event["mock_error"] = True
    event["rc"] = rc
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    if output:
        print(output)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
