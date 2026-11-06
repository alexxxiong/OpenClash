"""Linux-only offline integration tests for the actual screen watcher (~14 s).

Run: python3 contrib/mu5250/test-screen-watch.py
Only paths and the fixed PATH are rewritten in a temporary watcher copy. Real
Bash, flock, cat, sha256sum, gzip/zcat and sleep execute; no ARM tools, device,
network or credentials are involved. Full CLI arguments and output are printed
for every case, including failures, before temporary fixtures are removed.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "screen-network-watch.sh"
SYSTEM_PATH = "/usr/bin:/usr/sbin:/bin:/sbin"
BASH = shutil.which("bash") if sys.platform == "linux" else None
UNSUPPORTED = "UNSUPPORTED: screen-watch integration tests require Linux; no tests executed"

HELPER = r'''#!/bin/sh
set -eu
name=${0##*/}
case "$name" in
    pidof) kind=pidof ;;
    oc-screen-ready) kind=ready ;;
    oc-screen-load) kind=load ;;
    screen-status-poll.sh) kind=poller ;;
    *) exit 96 ;;
esac
n=$(cat "$TEST_ROOT/$kind.count" 2>/dev/null || printf '0')
n=$((n + 1))
printf '%s\000' "$@" > "$TEST_ROOT/$kind.$n.argv"
case "$kind" in
    pidof)
        [ "$#" = 1 ] && [ "$1" = zte_topsw_devui ] || exit 96
        printf '123\n'
        code=0 ;;
    ready)
        [ "$#" = 2 ] && [ "$1" = --pid ] && [ "$2" = 123 ] || exit 96
        code=$(cat "$TEST_ROOT/ready.exit")
        case "$code" in
            0) printf '{"ready":true,"reason":"ready"}\n' ;;
            75) printf '{"ready":false,"reason":"waiting-for-heartbeat"}\n' ;;
            *) printf '{"ready":false,"reason":"unsupported-ui"}\n' ;;
        esac ;;
    load)
        code=$(cat "$TEST_ROOT/load.exit")
        if [ "$code" = 0 ]; then
            printf 'mu_screen_install returned 0x0; fixture success\n'
        else
            printf 'fixture loader failure\n' >&2
        fi ;;
    poller)
        # A valid existing poller must be adopted; any invocation fails tests.
        printf 'unexpected fixture poller launch\n' >&2
        code=96 ;;
esac
printf '%s\n' "$code" > "$TEST_ROOT/$kind.$n.exit"
printf '%s\n' "$n" > "$TEST_ROOT/$kind.count"
exit "$code"
'''


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8", newline="\n")


def executable(path, value):
    write(path, value)
    path.chmod(0o700)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Lab:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mu-screen-watch-")
        self.root = Path(self.temp.name)
        self.base = self.root / "app"
        self.run = self.base / "run"
        self.proc = self.root / "proc"
        self.bin = self.root / "test-bin"
        self.ui = self.root / "zte_topsw_devui"
        self.library = self.base / "lib/screen-network-0123456789ab.so"
        self.script = self.root / "screen-network-watch.sh"
        self.process = None
        self.output = None
        self.started = None
        # Inherit no shell startup hooks or credentials from the caller.
        self.env = {"PATH": SYSTEM_PATH, "LC_ALL": "C", "HOME": str(self.root),
                    "TEST_ROOT": str(self.root), "TEST_BIN": str(self.bin)}

    def prepare(self):
        self.run.mkdir(parents=True)
        executable(self.bin / "pidof", HELPER)
        for name in ("oc-screen-load", "oc-screen-ready", "screen-status-poll.sh"):
            executable(self.base / "bin" / name, HELPER)
        write(self.ui, "offline stock UI fixture\n")
        write(self.library, "offline library fixture\n")
        write(self.base / "lib/screen-network.manifest",
              f"{digest(self.ui)} {digest(self.base / 'bin/oc-screen-load')} "
              f"{digest(self.library)} {self.library}\n")
        write(self.base / "lib/screen-ready.sha256",
              digest(self.base / "bin/oc-screen-ready") + "\n")
        self.proc.mkdir()
        compressed = subprocess.run(
            ["gzip", "-c"], input=b"# CONFIG_PID_NS is not set\n",
            capture_output=True, check=True, timeout=3, env=self.env,
        )
        (self.proc / "config.gz").write_bytes(compressed.stdout)
        write(self.proc / "123/maps", "")
        write(self.run / "screen-status-poll.pid", "124\n")
        cmdline = self.proc / "124/cmdline"
        cmdline.parent.mkdir()
        cmdline.write_bytes(b"/bin/sh\0" +
                            os.fsencode(self.base / "bin/screen-status-poll.sh") + b"\0")
        write(self.root / "ready.exit", "0\n")
        write(self.root / "load.exit", "0\n")

        source = SOURCE.read_text(encoding="utf-8")
        fixed_path = "export PATH=" + SYSTEM_PATH
        if source.count(fixed_path) != 1:
            raise AssertionError("watcher fixed PATH changed; review fixture substitution")
        for value in ("/data/openclash", "/proc/", "/usr/bin/zte_topsw_devui"):
            if value not in source:
                raise AssertionError(f"watcher fixture constant disappeared: {value}")
        # No readiness/control logic or sleep duration is changed.
        source = source.replace("/data/openclash", str(self.base))
        source = source.replace("/proc/", str(self.proc) + "/")
        source = source.replace("/usr/bin/zte_topsw_devui", str(self.ui))
        source = source.replace(fixed_path, 'export PATH="$TEST_BIN":' + SYSTEM_PATH)
        write(self.script, source)

    def start(self):
        self.output = (self.root / "watcher.out").open("w", encoding="utf-8")
        self.started = time.monotonic()
        self.process = subprocess.Popen(
            [BASH, "--noprofile", "--norc", str(self.script)], env=self.env,
            stdin=subprocess.DEVNULL, stdout=self.output, stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    def text(self, path):
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def calls(self, kind):
        result = []
        for path in sorted(self.root.glob(f"{kind}.*.argv"),
                           key=lambda p: int(p.name.split(".")[1])):
            raw = path.read_bytes()
            # A completed record is NUL terminated, including empty arguments.
            if raw.endswith(b"\0"):
                result.append([os.fsdecode(arg) for arg in raw[:-1].split(b"\0")])
        return result

    def alive(self):
        if self.process.poll() is not None:
            raise AssertionError(f"watcher exited {self.process.returncode}: "
                                 + self.text(self.root / "watcher.out"))

    def wait_for(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while True:
            self.alive()
            if predicate():
                return time.monotonic() - self.started
            if time.monotonic() >= deadline:
                raise AssertionError(f"watcher condition timed out after {timeout}s")
            time.sleep(0.02)

    def observe(self, duration):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            self.alive()
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))

    def stop(self):
        if self.process is not None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=1)
        if self.output is not None:
            self.output.close()

    def report(self):
        print(f"\nCLI: {shlex.join([BASH, '--noprofile', '--norc', str(self.script)])}",
              flush=True)
        for path in sorted(self.root.glob("*.argv")):
            raw = path.read_bytes()
            args = [os.fsdecode(arg) for arg in raw.removesuffix(b"\0").split(b"\0")]
            code = self.text(path.with_suffix(".exit")).strip() or "incomplete"
            print(f"{path.name}: {args!r}; exit={code}", flush=True)
        for path in [self.root / "watcher.out", *sorted(self.run.glob("*"))]:
            if path.is_file():
                print(f"--- {path.relative_to(self.root)} ---\n{self.text(path)}", flush=True)

    def close(self):
        try:
            self.stop()
            self.report()
        finally:
            self.temp.cleanup()


class ScreenWatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sys.platform != "linux":
            raise RuntimeError(UNSUPPORTED)
        if not BASH:
            raise RuntimeError("Bash is required on Linux (default PATH discovery)")
        for name in ("flock", "cat", "sha256sum", "gzip", "zcat", "grep",
                     "awk", "tr", "cut", "sleep", "mkdir"):
            if not shutil.which(name, path=SYSTEM_PATH):
                raise RuntimeError(f"real Linux utility required: {name}")

    def setUp(self):
        self.lab = Lab()
        self.addCleanup(self.lab.close)
        self.lab.prepare()

    def assert_common(self, ready_count, load_count):
        lab = self.lab
        lab.alive()
        self.assertEqual(lab.calls("poller"), [], "valid PID 124 poller was not adopted")
        self.assertTrue(lab.calls("pidof"))
        self.assertTrue(all(args == ["zte_topsw_devui"] for args in lab.calls("pidof")))
        self.assertEqual(lab.calls("ready"), [["--pid", "123"]] * ready_count)
        expected_load = [
            "--confirm-no-pidns", "--expect-exe", str(lab.ui),
            "--expect-sha256", digest(lab.ui), "--pid", "123", "--so", str(lab.library),
            "--expect-so-sha256", digest(lab.library), "--timeout-ms", "3000",
            "--safe-wait-ms", "5000", "--entry", "mu_screen_install", "--arg0", "0",
            "--ack-loader-risk",
        ]
        self.assertEqual(lab.calls("load"), [expected_load] * load_count)

    def test_ready_immediately_loads_once_under_three_seconds(self):
        lab = self.lab
        lab.start()
        elapsed = lab.wait_for(lambda: lab.text(lab.run / "screen-network.active")
                               == f"123 {lab.library}\n")
        self.assertLess(elapsed, 3, "ready UI still has a fixed startup delay")
        self.assertEqual(lab.text(lab.run / "screen-network.active"), f"123 {lab.library}\n")
        self.assert_common(1, 1)

    def test_three_retryable_probes_preserve_the_single_load_attempt(self):
        lab = self.lab
        write(lab.root / "ready.exit", "75\n")
        lab.start()
        lab.wait_for(lambda: lab.text(lab.root / "ready.count").strip() == "3", timeout=3.5)
        self.assert_common(3, 0)
        self.assertFalse((lab.run / "screen-network.active").exists())
        self.assertEqual([lab.text(lab.root / f"ready.{n}.exit") for n in (1, 2, 3)],
                         ["75\n"] * 3)
        # Atomic release cannot expose an empty control file to the helper.
        write(lab.root / "ready.next", "0\n")
        (lab.root / "ready.next").replace(lab.root / "ready.exit")
        lab.wait_for(lambda: lab.text(lab.run / "screen-network.active")
                     == f"123 {lab.library}\n", timeout=2)
        self.assert_common(4, 1)

    def test_mapped_and_active_adopts_without_probe_or_loader_under_three_seconds(self):
        lab = self.lab
        write(lab.proc / "123/maps", f"1000-2000 r-xp 0 00:00 1 {lab.library}\n")
        active = f"123 {lab.library}\n"
        write(lab.run / "screen-network.active", active)
        lab.start()
        elapsed = lab.wait_for(lambda: "Adopted existing screen extension for UI PID 123"
                               in lab.text(lab.root / "watcher.out"))
        self.assertLess(elapsed, 3)
        self.assertEqual(lab.text(lab.run / "screen-network.active"), active)
        self.assert_common(0, 0)

    def test_failed_loader_is_not_retried_after_five_seconds_on_same_pid(self):
        lab = self.lab
        write(lab.root / "load.exit", "7\n")
        lab.start()
        lab.wait_for(lambda: "no automatic retry" in lab.text(lab.root / "watcher.out"))
        lab.observe(5.25)  # Real sleep: cross the watcher's next five-second iteration.
        self.assert_common(1, 1)
        self.assertGreaterEqual(len(lab.calls("pidof")), 3, "second watcher iteration not observed")
        self.assertFalse((lab.run / "screen-network.active").exists())

    def test_non75_readiness_refusal_never_loads(self):
        lab = self.lab
        write(lab.root / "ready.exit", "69\n")
        lab.start()
        lab.wait_for(lambda: "readiness refused for PID 123 (exit 69)"
                     in lab.text(lab.root / "watcher.out"))
        lab.observe(5.25)
        self.assert_common(1, 0)
        self.assertGreaterEqual(len(lab.calls("pidof")), 2)
        self.assertFalse((lab.run / "screen-network.active").exists())


if __name__ == "__main__":
    if sys.platform != "linux":
        print(UNSUPPORTED, file=sys.stderr)
        raise SystemExit(2)
    unittest.main(verbosity=2)
