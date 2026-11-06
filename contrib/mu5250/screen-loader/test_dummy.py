#!/usr/bin/env python3
"""Opt-in native AArch64 Linux tests. Never invoked by build.ps1; never kills a target.

Run only in an expendable Linux test environment. Timeout test deliberately abandons
a constructor while it may own loader locks; the process must not be reused afterwards.
"""
import argparse
import hashlib
import os
from pathlib import Path
import platform
import struct
import subprocess
import tempfile
import time


def wait_until(condition, seconds=4):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError("bounded wait expired")


def control(path):
    raw = path.read_bytes()
    assert len(raw) >= 16
    return raw[1], struct.unpack_from("<Q", raw, 8)[0]


def quit_cooperatively(proc, path):
    if proc.poll() is None and path.exists():
        with path.open("r+b", buffering=0) as stream:
            stream.write(b"Q")
    try:
        code = proc.wait(timeout=4)
    except subprocess.TimeoutExpired:
        print(f"WARNING: dummy PID {proc.pid} still alive; no kill sent; control file {path}")
        return False
    assert code == 0, f"dummy exited with {code}"
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "out")
    ap.add_argument("--include-unsafe-timeout-test", action="store_true")
    ap.add_argument("--confirm-no-pidns", action="store_true", help="parent has verified CONFIG_PID_NS disabled; both ns/pid files must be ENOENT")
    args = ap.parse_args()
    if platform.system() != "Linux" or platform.machine() not in ("aarch64", "arm64"):
        ap.error("native AArch64 Linux only; no AArch64 executable will run on Windows")
    if os.geteuid() != 0 or os.getuid() != 0:
        ap.error("the loader explicitly requires root and a root-owned dummy")
    out = args.out.resolve()
    loader, target, library = (out / n for n in ("ptrace-load", "dummy-target", "libdummy-inject.so"))
    target_sha, so_sha = (hashlib.sha256(p.read_bytes()).hexdigest() for p in (target, library))
    root = Path(tempfile.mkdtemp(prefix="ptrace-dummy-"))
    os.chmod(root, 0o700)
    print(f"Evidence: {root}")
    for p in (loader, target, library):
        assert p.stat().st_uid == 0 and not p.stat().st_mode & 0o022
    for name, symbols in ((target, []), (library, ["dummy_entry", "dummy_uninstall"])):
        for symbol in symbols or [None]:
            subprocess.run([str(loader), "--inspect-elf", str(name)] + ([symbol] if symbol else []), check=True, timeout=5)
    # Prove that the C parser uses PT_DYNAMIC, not section headers.
    stripped = bytearray(library.read_bytes())
    stripped[40:48] = b"\0" * 8  # e_shoff
    stripped[58:64] = b"\0" * 6  # e_shentsize/e_shnum/e_shstrndx
    no_sections = root / "no-sections.so"
    no_sections.write_bytes(stripped)
    subprocess.run([str(loader), "--inspect-elf", str(no_sections), "dummy_entry"], check=True, timeout=5)
    # SHA implementation is checked against Python hashlib on the same bytes.
    inspected = subprocess.run([str(loader), "--inspect-elf", str(library)], capture_output=True, text=True, check=True, timeout=5)
    assert f"sha256={so_sha}" in inspected.stdout
    malformed = root / "bad-elf.so"
    malformed.write_bytes(b"not an ELF")
    assert subprocess.run([str(loader), "--inspect-elf", str(malformed)], capture_output=True, timeout=5).returncode == 1

    def start(name, hang=False):
        marker, ctl = root / (name + ".marker"), root / (name + ".control")
        log = (root / (name + ".target.log")).open("wb")
        proc = subprocess.Popen([str(target), str(marker), str(ctl)] + (["--hang-constructor"] if hang else []), stdout=log, stderr=log)
        log.close()
        wait_until(lambda: ctl.exists() and ctl.stat().st_size >= 16)
        wait_until(lambda: control(ctl)[1] > 0)
        return proc, marker, ctl

    def command(proc):
        return [str(loader), "--expect-exe", str(target), "--expect-sha256", target_sha,
                "--pid", str(proc.pid), "--so", str(library), "--expect-so-sha256", so_sha,
                "--timeout-ms", "1500", "--safe-wait-ms", "3000"] + (["--confirm-no-pidns"] if args.confirm_no_pidns else [])

    def run(proc, name, extra=(), expected=0):
        completed = subprocess.run(command(proc) + list(extra), capture_output=True, text=True, timeout=15)
        (root / (name + ".loader.log")).write_text(completed.stdout + completed.stderr)
        assert completed.returncode == expected, (name, completed.returncode, completed.stderr)
        return completed

    proc, marker, ctl = start("success")
    try:
        run(proc, "preflight", ["--check-only"])
        assert not marker.exists()
        wrong = command(proc)
        wrong[wrong.index(target_sha)] = "0" * 64
        result = subprocess.run(wrong + ["--ack-loader-risk"], capture_output=True, text=True, timeout=10)
        assert result.returncode == 1 and not marker.exists()
        result = run(proc, "load-entry", ["--ack-loader-risk", "--entry", "dummy_entry", "--arg0", "0x1234"])
        assert "RESTORED:" in result.stderr and "DETACHED" in result.stderr
        wait_until(lambda: marker.exists())
        text = marker.read_text()
        assert f"INJECTOR_DUMMY_CTOR_v1 pid={proc.pid}" in text
        assert "INJECTOR_DUMMY_ENTRY_v1" in text and "arg0=0x0000000000001234" in text
        assert f"dummy_entry returned 0x{0x1234 ^ 0x0123456789abcdef:x}" in result.stderr
        before = control(ctl)[1]
        wait_until(lambda: control(ctl)[1] > before)
        assert control(ctl)[0] == 0 and proc.poll() is None
        status = Path(f"/proc/{proc.pid}/status").read_text()
        assert next(line.split()[1] for line in status.splitlines() if line.startswith("TracerPid:")) == "0"
        run(proc, "uninstall-entry", ["--ack-loader-risk", "--entry", "dummy_uninstall", "--arg0", "0"])
        assert "INJECTOR_DUMMY_UNINSTALL_v1" in marker.read_text()
        assert control(ctl)[0] == 0
        print("PASS: blocked nanosleep target, constructor, dlsym entry/uninstall, canaries, heartbeat, detach, hash refusal")
    finally:
        assert quit_cooperatively(proc, ctl)

    if args.include_unsafe_timeout_test:
        proc, marker, ctl = start("timeout", hang=True)
        try:
            result = run(proc, "timeout", ["--ack-loader-risk"], expected=3)
            assert "RESTORED:" in result.stderr and "DETACHED" in result.stderr
            assert "UNSAFE LIFECYCLE:" in result.stderr
            assert marker.exists()
            before = control(ctl)[1]
            wait_until(lambda: control(ctl)[1] > before)
            assert control(ctl)[0] == 0 and proc.poll() is None
            status = Path(f"/proc/{proc.pid}/status").read_text()
            assert next(line.split()[1] for line in status.splitlines() if line.startswith("TracerPid:")) == "0"
            print("PASS: user-space constructor timeout recovered registers, heartbeat and detach; loader state intentionally NOT certified safe")
        finally:
            quit_cooperatively(proc, ctl)
    print("Dummy test suite completed; inspect evidence before any separate real-process review.")


if __name__ == "__main__":
    main()
