#!/usr/bin/env python3
"""Cross-compile the loader, dummy fixtures and sibling UI payload; never run them."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


SOURCE_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = SOURCE_DIR.parent / "dist" / "screen-loader"
TARGET = "aarch64-linux-musl"
COMMON = [
    "cc", "-target", TARGET, "-std=c11", "-O2", "-g", "-Wall", "-Wextra",
    "-Werror", "-Wformat=2", "-fstack-protector-strong", "-D_FORTIFY_SOURCE=2",
]
BUILDS = (
    ("oc-screen-ready", ("-static",), ("../screen-ready.c",)),
    ("ptrace-load", ("-static",), ("ptrace_load.c", "elf_image.c")),
    ("dummy-target", ("-dynamic", "-fPIE", "-pie"), ("dummy_target.c", "dummy_loop.S")),
    ("libdummy-inject.so", ("-shared", "-fPIC", "-Wl,-z,now", "-Wl,-z,relro"),
     ("dummy_library.c",)),
    ("screen-network.so", ("-shared", "-fPIC", "-Wl,-z,now", "-Wl,-z,relro"),
     ("../screen-network.c",)),
)


def file_record(path, name):
    raw = path.read_bytes()
    return {"name": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zig", help="Zig executable path or command (default: ZIG environment variable, then PATH)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help="output directory (default: ../dist/screen-loader beside this source directory)")
    args = parser.parse_args()
    requested_zig = args.zig or os.environ.get("ZIG") or "zig"
    zig = shutil.which(os.path.expanduser(requested_zig))
    if zig is None:
        parser.error(f"Zig executable not found: {requested_zig!r}; use --zig, set ZIG, or add zig to PATH")
    zig = str(Path(zig).resolve())
    source_names = sorted({name for _, _, names in BUILDS for name in names}
                          | {"elf_image.h", "sha256.h", "trace_state.h"})
    for name in source_names:
        if not (SOURCE_DIR / name).is_file():
            parser.error(f"missing source: {SOURCE_DIR / name}")
    # Capture the input hashes before compiling. The payload belongs to the parent.
    inputs = [file_record(SOURCE_DIR / name, name) for name in source_names]
    version = subprocess.run([zig, "version"], check=True, capture_output=True, text=True).stdout.strip()
    out = args.out.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    # A failed rebuild must not retain a successful manifest/report from an older build.
    for name in ("build-manifest.json", "artifact-verification.json"):
        (out / name).unlink(missing_ok=True)
    outputs = []
    for name, flags, sources in BUILDS:
        print(f"Building {name}", flush=True)
        subprocess.run([zig, *COMMON, *flags, "-o", str(out / name),
                        *(str(SOURCE_DIR / source) for source in sources)], check=True)
        outputs.append(file_record(out / name, name))
    manifest = {"zig": version, "target": TARGET, "runtime_tests_executed": False,
                "inputs": inputs, "outputs": outputs}
    manifest_path = out / "build-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Built {len(outputs)} artifacts; no target code executed. Manifest: {manifest_path}")


if __name__ == "__main__":
    main()
