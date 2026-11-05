"""TUN, policy-routing, and owned FORWARD-chain regression cases."""
import copy


def register_tests(h):
    Lab, case = h.Lab, h.case

    def network(state):
        return copy.deepcopy({key: state[key] for key in ("tables", "routes", "rules")})

    def assert_reserved_mutations(lab):
        for call in lab.state["calls"]:
            args = call["args"]
            if call["command"] == "ip" and len(args) > 2 and args[1] in ("route", "rule") and args[2] not in ("show", "list"):
                assert args[2] in ("add", "del"), f"global/flush mutation: {call}"
                assert args[args.index("table") + 1] == "7250", call
                if args[1] == "rule":
                    assert args[args.index("pref") + 1] == "17250", call
                    assert args[args.index("fwmark") + 1] == "0x7250/0xffffffff", call
            if call["command"] in ("iptables", "ip6tables") and args[2] in ("-F", "-X", "-N", "-A", "-I", "-D"):
                assert args[3] in h.OWNED_CHAINS | {"PREROUTING", "FORWARD"}, call

    def assert_tun_flow(lab, family, source=None):
        public = "1.1.1.1" if family == 4 else "2606:4700:4700::1111"
        trace = h.trace_packet(lab.state, family, "udp", public, src=source)
        assert trace["mark"] == 0x7250, trace
        assert trace["result"] == ("TUN", "mu-oc-tun"), trace
        assert trace["route"] == {"family": family, "table": 7250,
                                  "spec": ["default", "dev", "mu-oc-tun", "table", "7250"]}, trace
        assert trace["forward"] == ("ACCEPT", None), trace
        assert h.trace_packet(lab.state, family, "udp", public, initial_mark=0x10000000)["mark"] == 0x7250
        assert h.evaluate(lab.state, family, "tcp", public, src=source) == ("REDIRECT", 17892)
        for protocol in ("tcp", "udp"):
            dns = h.trace_packet(lab.state, family, protocol, public, src=source, port=53)
            assert dns["result"] == ("REDIRECT", 17874) and dns["mark"] == 0 and dns["route"] is None, dns

    @case("TUN test-ip: MARK -> policy7250 -> TUN -> owned FORWARD; management and DNS bypass")
    def tun_test_ip():
        lab = Lab("tun-test-ip", engine="tun")
        lab.install()
        assert_tun_flow(lab, 4)
        for dst in ("192.168.3.1", "192.168.0.1", "10.0.0.1", "172.16.2.1", "100.64.0.1", "203.0.113.9"):
            for protocol in ("tcp", "udp"):
                for port in (22, 80, 443, 8081, 9090):
                    trace = h.trace_packet(lab.state, 4, protocol, dst, port=port)
                    assert trace["result"] is None and trace["mark"] == 0 and trace["route"] is None, trace
                assert h.evaluate(lab.state, 4, protocol, dst, port=53) == ("REDIRECT", 17874)
        for source in ("192.168.3.119", "192.168.3.121", "10.0.0.5"):
            for protocol in ("tcp", "udp"):
                for port in (53, 443):
                    trace = h.trace_packet(lab.state, 4, protocol, "1.1.1.1", src=source, port=port)
                    assert trace["result"] is None and trace["mark"] == 0 and trace["route"] is None, trace
        for protocol in ("tcp", "udp", "icmp"):
            assert h.evaluate(lab.state, 4, protocol, "1.1.1.1", iface="wan") is None
        assert h.evaluate(lab.state, 4, "icmp", "1.1.1.1") is None
        assert h.evaluate(lab.state, 6, "udp", "2606:4700:4700::1111") is None
        for table in ("nat", "mangle", "filter"):
            assert not h.OWNED_CHAINS.intersection(lab.state["tables"][f"6:{table}"])
        assert not any(r["family"] == 6 and r["table"] == 7250 for r in lab.state["routes"] + lab.state["rules"])
        assert (lab.base / "run/rules.engine").read_text().strip() == "tun"
        # Mutation checks prove the evaluator needs both MARK and the filter hook.
        no_forward = copy.deepcopy(lab.state)
        no_forward["tables"]["4:filter"]["FORWARD"] = [["-j", "FIRMWARE_FORWARD"]]
        assert h.evaluate(no_forward, 4, "udp", "1.1.1.1") == ("DROP", None)
        no_mark = copy.deepcopy(lab.state)
        rules = no_mark["tables"]["4:mangle"]["MU_OC_UDP"]
        rules[:] = [r for r in rules if "MARK" not in r]
        assert h.trace_packet(no_mark, 4, "udp", "1.1.1.1")["route"] is None
        assert_reserved_mutations(lab)

    @case("TUN all: dualstack routes and strict marked UDP/stateful UDP forwarding")
    def tun_all():
        lab = Lab("tun-all", "all", "tun")
        lab.install()
        for family, source in ((4, "192.168.3.121"), (6, "2001:db8:3::121")):
            assert_tun_flow(lab, family, source)
            assert {"family": family, "pref": 17250, "fwmark": "0x7250/0xffffffff", "table": 7250} in lab.state["rules"]
            assert lab.state["tables"][f"{family}:filter"]["FORWARD"][0] == ["-j", "MU_OC_FWD"]
            assert h.forward_verdict(lab.state, family, mark=0x7250) == ("ACCEPT", None)
            for wrong_mark in (0, 0x7251, 0x17250):
                assert h.forward_verdict(lab.state, family, mark=wrong_mark) == ("DROP", None)
            for protocol in ("tcp", "icmp"):
                assert h.forward_verdict(lab.state, family, protocol=protocol, mark=0x7250) == ("DROP", None)
            for iface, output in (("br-guest", "mu-oc-tun"), ("br-lan", "wan"), ("wan", "br-lan")):
                assert h.forward_verdict(lab.state, family, iface=iface, out_iface=output, mark=0x7250) == ("DROP", None)
            for ctstate in ("ESTABLISHED", "RELATED"):
                assert h.forward_verdict(lab.state, family, iface="mu-oc-tun", out_iface="br-lan", ctstate=ctstate) == ("ACCEPT", None)
                assert h.forward_verdict(lab.state, family, protocol="tcp", iface="mu-oc-tun", out_iface="br-lan", ctstate=ctstate) == ("DROP", None)
            for ctstate in ("NEW", "INVALID", "UNTRACKED"):
                assert h.forward_verdict(lab.state, family, iface="mu-oc-tun", out_iface="br-lan", ctstate=ctstate) == ("DROP", None)
        for dst in ("::1", "fd00:3::1", "fe80::1", "ff02::1", "2001:db8:3::1"):
            for protocol in ("tcp", "udp"):
                trace = h.trace_packet(lab.state, 6, protocol, dst, port=8081)
                assert trace["result"] is None and trace["mark"] == 0, trace
                assert h.evaluate(lab.state, 6, protocol, dst, port=53) == ("REDIRECT", 17874)
        assert (lab.base / "run/rules.families").read_text().split() == ["4", "6"]
        assert_reserved_mutations(lab)

    @case("TUN stop: recorded engine/interface; exact routes; foreign table7250 and global/firmware unchanged")
    def tun_stop():
        lab = Lab("tun-stop", "all", "tun")
        lab.install()
        for family, prefix in ((4, "192.0.2.0/24"), (6, "2001:db8:ffff::/48")):
            foreign = {"family": family, "table": 7250, "spec": [prefix, "dev", "firmware", "table", "7250"]}
            lab.state["routes"].append(foreign)
            lab.initial["routes"].append(copy.deepcopy(foreign))
        # Kernel-added display attributes must not prevent exact route removal.
        for route in lab.state["routes"]:
            if route["table"] == 7250 and route["spec"][:3] == ["default", "dev", "mu-oc-tun"]:
                route["spec"].extend(["proto", "static", "scope", "link"])
        lab.state["uci"]["mu5250_udp_engine"] = "tproxy"
        assert lab.run("stop", iface="br-guest", tun_exists=False).returncode == 0
        lab.assert_clean()
        assert lab.run("stop").returncode == 0
        lab.assert_clean()
        assert_reserved_mutations(lab)

    @case("TUN transitions: legacy -> TUN dualstack -> legacy -> TUN test-ip -> off")
    def transitions():
        lab = Lab("tun-transitions")
        lab.install()
        for engine, scope in (("tun", "all"), ("tproxy", "all"), ("tun", "test-ip")):
            lab.state["uci"].update({"mu5250_udp_engine": engine, "mu5250_proxy_scope": scope})
            result = lab.run("reload")
            assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
            assert (lab.base / "run/rules.engine").read_text().strip() == engine
            families = (4, 6) if scope == "all" else (4,)
            for family in families:
                public = "1.1.1.1" if family == 4 else "2606:4700:4700::1111"
                expected = ("TUN", "mu-oc-tun") if engine == "tun" else ("TPROXY", 17895)
                assert h.evaluate(lab.state, family, "udp", public) == expected
                assert ("MU_OC_FWD" in lab.state["tables"][f"{family}:filter"]) == (engine == "tun")
            if scope == "test-ip":
                assert not any(r["family"] == 6 and r["table"] == 7250 for r in lab.state["rules"] + lab.state["routes"])
            routes = [r for r in lab.state["routes"] if r["table"] == 7250]
            assert len(routes) == len(families)
            assert all(("mu-oc-tun" in r["spec"]) == (engine == "tun") for r in routes)
        installed = network(lab.state)
        assert lab.run("reload").returncode == 0
        assert network(lab.state) == installed, "TUN reload not idempotent"
        lab.state["uci"]["mu5250_proxy_scope"] = "off"
        assert lab.run("reload").returncode == 0
        lab.assert_clean()
        assert_reserved_mutations(lab)

    @case("TUN missing interface rejects start and preserves working legacy reload")
    def missing_tun():
        lab = Lab("tun-missing", engine="tun")
        assert lab.run(tun_exists=False).returncode != 0
        lab.assert_clean()
        lab.state["uci"]["mu5250_udp_engine"] = "tproxy"
        lab.install()
        installed = network(lab.state)
        lab.state["uci"]["mu5250_udp_engine"] = "tun"
        assert lab.run("reload", tun_exists=False).returncode != 0
        assert network(lab.state) == installed
        assert (lab.base / "run/rules.engine").read_text().strip() == "tproxy"

    @case("TUN invalid UDP engine rejected before mutation")
    def invalid_engine():
        lab = Lab("tun-invalid-engine", engine="unknown")
        assert lab.run().returncode == 64
        lab.assert_clean()

    for family in (4, 6):
        @case(f"TUN unowned IPv{family} forward-chain collision rejected without firmware changes")
        def collision(family=family):
            lab = Lab(f"tun-forward-collision{family}", "all", "tun")
            lab.state["tables"][f"{family}:filter"]["MU_OC_FWD"] = [["-j", "ACCEPT"]]
            lab.state["tables"][f"{family}:filter"]["FORWARD"].insert(0, ["-j", "MU_OC_FWD"])
            before = network(lab.state)
            assert lab.run().returncode == 73
            assert network(lab.state) == before
            assert not (lab.base / "run/rules.owned").exists()

    faults = (
        ("mark4", "iptables", ["-t", "mangle", "-A", "MU_OC_UDP", "-p", "udp", "-j", "MARK", "--set-xmark", "0x7250/0xffffffff"]),
        ("forward-hook4", "iptables", ["-t", "filter", "-I", "FORWARD", "1", "-j", "MU_OC_FWD"]),
        ("route4", "ip", ["-4", "route", "add", "default", "dev", "mu-oc-tun", "table", "7250"]),
        ("policy4", "ip", ["-4", "rule", "add", "fwmark", "0x7250/0xffffffff", "table", "7250", "pref", "17250"]),
        ("route6", "ip", ["-6", "route", "add", "default", "dev", "mu-oc-tun", "table", "7250"]),
        ("forward-hook6", "ip6tables", ["-t", "filter", "-I", "FORWARD", "1", "-j", "MU_OC_FWD"]),
        ("last-hook6", "ip6tables", ["-t", "mangle", "-I", "PREROUTING", "1", "-i", "br-lan", "-j", "MU_OC_UDP"]),
    )
    for name, command, args in faults:
        @case(f"TUN failed installation rollback including FORWARD and exact routes: {name}")
        def rollback(name=name, command=command, args=args):
            lab = Lab("tun-rollback-" + name, "all", "tun")
            lab.inject(command, args)
            result = lab.run()
            assert result.returncode != 0 and lab.state["injections"][0]["used"]
            lab.assert_clean()
            assert_reserved_mutations(lab)

    @case("TUN TERM after IPv6 final hook exits143 and removes FORWARD/route state")
    def term():
        lab = Lab("tun-term", "all", "tun")
        lab.inject("ip6tables", faults[-1][2], action="signal")
        assert lab.run().returncode == 143
        assert lab.state["injections"][0]["used"]
        lab.assert_clean()

    @case("TUN failed FORWARD hook deletion retains ownership and successful retry cleans all")
    def forward_delete_failure():
        lab = Lab("tun-forward-delete-failure", "all", "tun")
        lab.install()
        lab.inject("iptables", ["-t", "filter", "-D", "FORWARD", "-j", "MU_OC_FWD"])
        assert lab.run("stop").returncode != 0
        assert (lab.base / "run/rules.owned").exists()
        assert (lab.base / "run/rules.engine").read_text().strip() == "tun"
        assert lab.run("stop").returncode == 0
        lab.assert_clean()

    cleanup_faults = (
        ("tun-route4", "tun", ["-4", "route", "del", "default", "dev", "mu-oc-tun", "table", "7250"]),
        ("legacy-route4", "tproxy", ["-4", "route", "del", "local", "0.0.0.0/0", "dev", "lo", "table", "7250"]),
        ("tun-policy6", "tun", ["-6", "rule", "del", "fwmark", "0x7250/0xffffffff", "table", "7250", "pref", "17250"]),
    )
    for name, engine, args in cleanup_faults:
        @case(f"TUN exact cleanup failures retain ownership until retry: {name}")
        def cleanup_failure(name=name, engine=engine, args=args):
            lab = Lab("tun-cleanup-failure-" + name, "all", engine)
            lab.install()
            lab.inject("ip", args)
            result = lab.run("stop")
            remaining = [r for r in lab.state["routes"] + lab.state["rules"] if r["table"] == 7250]
            retained = (lab.base / "run/rules.owned").exists()
            assert result.returncode != 0 and retained, f"stop exit={result.returncode}; ownership retained={retained}; leftovers={remaining}"
            assert lab.run("stop").returncode == 0
            lab.assert_clean()

    for old_engine, new_engine in (("tproxy", "tun"), ("tun", "tproxy")):
        @case(f"TUN engine transition preserves unowned opposite-engine route: {old_engine}->{new_engine}")
        def opposite_route(old_engine=old_engine, new_engine=new_engine):
            lab = Lab(f"tun-opposite-route-{old_engine}", engine=old_engine)
            lab.install()
            spec = (["default", "dev", "mu-oc-tun", "table", "7250"] if new_engine == "tun" else
                    ["local", "0.0.0.0/0", "dev", "lo", "table", "7250"])
            # A distinct metric permits coexistence with the active default.
            # Neither the installed engine nor the exact added route owns this.
            foreign = {"family": 4, "table": 7250, "spec": spec + ["metric", "77"]}
            lab.state["routes"].append(foreign)
            before = network(lab.state)
            lab.state["uci"]["mu5250_udp_engine"] = new_engine
            lab.inject("ip", ["-4", "route", "add"] + spec)
            result = lab.run("reload")
            assert foreign in lab.state["routes"], f"foreign {new_engine} route deleted during failed transition; exit={result.returncode}"
            assert result.returncode == 73 and network(lab.state) == before, "unowned opposite-engine route was not rejected before teardown"

    @case("TUN legacy->TUN transition preserves unowned forward chain and firmware hook")
    def transition_forward_collision():
        lab = Lab("tun-transition-forward-collision", engine="tproxy")
        lab.install()
        lab.state["tables"]["4:filter"]["MU_OC_FWD"] = [["-j", "ACCEPT"]]
        lab.state["tables"]["4:filter"]["FORWARD"].insert(0, ["-j", "MU_OC_FWD"])
        before = network(lab.state)
        lab.state["uci"]["mu5250_udp_engine"] = "tun"
        result = lab.run("reload")
        assert network(lab.state) == before, f"foreign forward chain/hook or working rules changed; reload exit={result.returncode}"
        assert result.returncode == 73
