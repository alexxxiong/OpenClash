#!/usr/bin/env python3
"""Inspect build artifacts on the host without executing any AArch64 code."""
import argparse
import hashlib
import json
from pathlib import Path
import struct


def inspect(path):
    raw = path.read_bytes()
    assert raw[:7] == b"\x7fELF\x02\x01\x01", path
    hdr = struct.unpack_from("<HHIQQQIHHHHHH", raw, 16)
    kind, machine, _, _, phoff, _, _, _, phsize, phnum, *_ = hdr
    assert machine == 183 and phsize == 56
    ph = [struct.unpack_from("<IIQQQQQQ", raw, phoff + i * phsize) for i in range(phnum)]
    interp = next((raw[p[2]:p[2]+p[5]].rstrip(b"\0").decode() for p in ph if p[0] == 3), None)
    dynamic = next((p for p in ph if p[0] == 2), None)
    tags = []
    if dynamic:
        for offset in range(dynamic[2], dynamic[2] + dynamic[5], 16):
            tag, value = struct.unpack_from("<qQ", raw, offset)
            if not tag:
                break
            tags.append((tag, value))
    return {"name": path.name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "machine": "AArch64", "elf_type": kind, "interpreter": interp,
            "needed_count": sum(t == 1 for t, _ in tags), "dynamic_tags": len(tags)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path,
                        default=Path(__file__).resolve().parent.parent / "dist" / "screen-loader")
    args = parser.parse_args()
    if not __debug__:
        parser.error("run without -O or PYTHONOPTIMIZE; verification uses assertions")
    out = args.out.expanduser().resolve()
    report_path = out / "artifact-verification.json"
    report_path.unlink(missing_ok=True)
    names = ("ptrace-load", "dummy-target", "libdummy-inject.so", "screen-network.so", "oc-screen-ready")
    results = [inspect(out / name) for name in names]
    helper, target, library, payload, readiness = results
    assert helper["elf_type"] == 2 and helper["interpreter"] is None and helper["needed_count"] == 0
    assert readiness["elf_type"] == 2 and readiness["interpreter"] is None and readiness["needed_count"] == 0
    assert target["elf_type"] == 3 and target["interpreter"] == "/lib/ld-musl-aarch64.so.1"
    for shared in (library, payload):
        assert shared["elf_type"] == 3 and shared["interpreter"] is None and shared["dynamic_tags"] > 0
    manifest = json.loads((out / "build-manifest.json").read_text(encoding="utf-8-sig"))
    assert manifest["target"] == "aarch64-linux-musl"
    assert len(manifest["outputs"]) == len(names)
    assert {entry["name"] for entry in manifest["outputs"]} == set(names)
    for result in results:
        entry = next(item for item in manifest["outputs"] if item["name"] == result["name"])
        assert result["sha256"] == entry["sha256"] and result["bytes"] == entry["bytes"]
    report = {"artifact_checks_passed": True, "target_executables_run": False,
              "ptrace_runtime_validated": False, "results": results}
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
