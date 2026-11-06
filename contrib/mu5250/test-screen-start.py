"""Linux-only offline tests for the real screen launcher and early init (target <10 s).

Run: python3 contrib/mu5250/test-screen-start.py
Production copies change only application/proc/lock paths and the PATH prefix.
The early init is sourced by Bash, then its real start()/stop() is invoked;
neither host rc.common nor any device or real /proc is accessed. Dependencies
and watcher processes are fixtures. Real flock and sleep exercise concurrency.
Windows validation must compile this file only; Linux execution is required.
"""
from __future__ import annotations

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

if sys.platform == "linux":
    import fcntl


ROOT = Path(__file__).resolve().parent
SYSTEM_PATH = "/usr/bin:/usr/sbin:/bin:/sbin"
BASH = shutil.which("bash", path=SYSTEM_PATH) if sys.platform == "linux" else None
UNSUPPORTED = "UNSUPPORTED: screen-start tests require Linux; no tests executed"
DRIVER = 'stop() { return 0; }; . "$1"; "$2"'

# These fixtures deliberately keep inherited fds open. Reopening fd 9, as the
# full watcher does for its own lock, could conceal a launcher-lock leak.
WATCHER = r'''#!/bin/sh
set -eu
printf '%s\n' "$$" >> "$TEST_ROOT/watcher.invoked"
exec 7>"$TEST_APP/run/screen-network-watch.lock"
flock -n -x 7 || exit 0
printf '%s\n' "$$" >> "$TEST_ROOT/watcher.started"
while [ -e "$TEST_ROOT/publication-held" ]; do sleep 0.01; done
mkdir -p "$TEST_PROC/$$"
printf '/bin/sh\000%s\000' "$0" > "$TEST_PROC/$$/cmdline.next"
mv "$TEST_PROC/$$/cmdline.next" "$TEST_PROC/$$/cmdline"
printf 'fixture watcher running: %s\n' "$$"
printf '%s\n' "$$" > "$TEST_APP/run/screen-network-watch.pid.next"
mv "$TEST_APP/run/screen-network-watch.pid.next" "$TEST_APP/run/screen-network-watch.pid"
while :; do sleep 1; done
'''

BLOCKED_LAUNCHER = r'''#!/bin/sh
set -eu
# With stdin redirected by start(), read must see EOF even while the driver's
# input pipe remains open. This also checks the often missed stdin redirection.
if IFS= read -r unexpected; then exit 97; fi
printf 'blocked\n' > "$TEST_ROOT/launcher.blocked"
IFS= read -r token < "$TEST_ROOT/launcher-gate"
printf 'released\n' > "$TEST_ROOT/launcher.released"
'''


def write(path, value, executable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8", newline="\n")
    if executable:
        path.chmod(0o700)


class Lab:
    def __init__(self, application=True):
        self.temp = tempfile.TemporaryDirectory(prefix="mu-screen-start-")
        self.root = Path(self.temp.name).resolve()
        self.app = self.root / "app"
        self.proc = self.root / "proc"
        self.run = self.app / "run"
        self.launcher = self.app / "bin/screen-start.sh"
        self.watcher = self.app / "bin/screen-network-watch.sh"
        self.init = self.root / "screen-early.init"
        self.bootstrap = self.root / "bootstrap.lock"
        self.processes = []
        self.proc.mkdir()
        (self.root / "test-bin").mkdir()
        # No inherited credentials, shell hooks, or user-specific PATH.
        self.env = {"PATH": SYSTEM_PATH, "LC_ALL": "C", "HOME": str(self.root),
                    "TEST_ROOT": str(self.root), "TEST_APP": str(self.app),
                    "TEST_PROC": str(self.proc), "TEST_BIN": str(self.root / "test-bin")}
        write(self.init, self.production_copy("screen-early.init"))
        if application:
            self.install_application()

    def production_copy(self, name):
        source = (ROOT / name).read_text(encoding="utf-8")
        fixed_path = "export PATH=" + SYSTEM_PATH
        if source.count(fixed_path) != 1 or "/data/openclash" not in source:
            raise AssertionError(f"{name}: production path constants changed")
        for old, new in (
            ("/data/openclash", str(self.app)),
            ("/proc/", str(self.proc) + "/"),
            ("/tmp/mu5250-screen-bootstrap.lock", str(self.bootstrap)),
            (fixed_path, 'export PATH="$TEST_BIN":' + SYSTEM_PATH),
        ):
            source = source.replace(old, new)
        return source

    def install_application(self):
        self.run.mkdir(parents=True, exist_ok=True)
        write(self.launcher, self.production_copy("screen-start.sh"), executable=True)
        write(self.watcher, WATCHER, executable=True)
        write(self.app / "lib/screen-network.manifest", "offline manifest fixture\n")

    def spawn(self, argv, input_pipe=False):
        process = subprocess.Popen(
            argv, env=self.env,
            stdin=subprocess.PIPE if input_pipe else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
        self.processes.append(process)
        return process

    def hook(self, action="start", input_pipe=False):
        started = time.monotonic()
        process = self.spawn([BASH, "--noprofile", "--norc", "-c", DRIVER,
                              "fixture-rc.common", str(self.init), action], input_pipe)
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired as exc:
            raise AssertionError(f"early {action} blocked the boot caller") from exc
        return process, time.monotonic() - started

    def launch(self):
        return self.spawn(["/bin/sh", str(self.launcher)])

    def finish(self, process, timeout=1.5):
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise AssertionError("caller blocked, or background child retained caller pipes: "
                                 + shlex.join(process.args)) from exc
        if process.returncode != 0:
            raise AssertionError(f"{shlex.join(process.args)}: exit {process.returncode}: "
                                 f"{stdout!r} {stderr!r}")
        return stdout, stderr

    def wait_for(self, predicate, timeout=1.5):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                raise AssertionError(f"fixture condition timed out after {timeout}s")
            time.sleep(0.01)

    @staticmethod
    def lock_free(path):
        # No process-table inspection: test the actual kernel lock directly.
        with path.open("a") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        return True

    def lines(self, name):
        path = self.root / name
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def old_process(self, argv=None):
        # A real, owned sentinel proves an untrusted PID is not terminated.
        process = self.spawn([shutil.which("sleep", path=SYSTEM_PATH), "20"])
        if argv is None:
            argv = ["/bin/sh", str(self.watcher)]
        directory = self.proc / str(process.pid)
        directory.mkdir()
        (directory / "cmdline").write_bytes(b"\0".join(os.fsencode(arg) for arg in argv) + b"\0")
        write(self.run / "screen-network-watch.pid", f"{process.pid}\n")
        return process

    def close(self):
        # Signal only session groups created by this fixture, including workers
        # whose immediate caller already exited. No PID is discovered via /proc.
        for process in self.processes:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for process in self.processes:
            try:
                process.communicate(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=0.5)
        self.temp.cleanup()


class ScreenStartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sys.platform != "linux":
            raise RuntimeError(UNSUPPORTED)
        if not BASH:
            raise RuntimeError("Bash is required for the rc.common fixture")
        for name in ("flock", "sleep", "nohup", "cat", "tr", "awk", "mkdir", "mv"):
            if not shutil.which(name, path=SYSTEM_PATH):
                raise RuntimeError(f"real Linux utility required: {name}")
        for name in ("screen-start.sh", "screen-early.init"):
            if not (ROOT / name).is_file():
                raise RuntimeError(f"required production source missing: {name}")

    def lab(self, application=True):
        lab = Lab(application)
        self.addCleanup(lab.close)
        return lab

    def assert_one_watcher(self, lab):
        self.assertEqual(len(lab.lines("watcher.invoked")), 1, "duplicate watcher invocation")
        self.assertEqual(lab.lines("watcher.started"), lab.lines("watcher.invoked"))
        pid = (lab.run / "screen-network-watch.pid").read_text().strip()
        self.assertEqual(pid, lab.lines("watcher.started")[0])
        self.assertEqual((lab.proc / pid / "cmdline").read_bytes(),
                         b"/bin/sh\0" + os.fsencode(lab.watcher) + b"\0")
        self.assertFalse(lab.lock_free(lab.run / "screen-network-watch.lock"),
                         "fixture watcher must remain alive during lock checks")
        self.assertTrue(lab.lock_free(lab.run / "screen-start.lock"),
                        "launcher fd 9 leaked to its live watcher")

    def test_early_start_returns_with_all_fds_redirected_while_launcher_blocks(self):
        lab = self.lab()
        os.mkfifo(lab.root / "launcher-gate", 0o600)
        write(lab.launcher, BLOCKED_LAUNCHER, executable=True)
        process, elapsed = lab.hook(input_pipe=True)
        self.assertLess(elapsed, 1, "early entry delayed boot")
        # Keep driver's stdin writer open until the child proves it saw EOF.
        lab.wait_for(lambda: (lab.root / "launcher.blocked").exists())
        self.assertFalse((lab.root / "launcher.released").exists())
        self.assertEqual(lab.finish(process, timeout=0.5), (b"", b""))
        self.assertTrue(lab.lock_free(lab.bootstrap), "fd 8 leaked into blocked launcher")
        self.assertEqual(lab.lines("watcher.invoked"), [])

    def test_missing_application_returns_without_blocking_or_creating_app_files(self):
        lab = self.lab(application=False)
        process, elapsed = lab.hook()
        self.assertLess(elapsed, 1)
        self.assertEqual(lab.finish(process, timeout=0.5), (b"", b""))
        lab.wait_for(lambda: lab.bootstrap.exists() and not lab.lock_free(lab.bootstrap))
        self.assertFalse(lab.app.exists(), "early worker created files before /data was ready")
        # A second boot entry must also return while the original worker waits.
        duplicate, elapsed = lab.hook()
        self.assertLess(elapsed, 1)
        self.assertEqual(lab.finish(duplicate, timeout=0.5), (b"", b""))
        self.assertEqual(lab.lines("watcher.invoked"), [])

    def test_late_application_is_launched_and_both_startup_locks_are_released(self):
        lab = self.lab(application=False)
        process, _ = lab.hook()
        lab.finish(process, timeout=0.5)
        lab.wait_for(lambda: lab.bootstrap.exists() and not lab.lock_free(lab.bootstrap))
        lab.install_application()
        lab.wait_for(lambda: (lab.run / "screen-network-watch.pid").exists(), timeout=2)
        lab.wait_for(lambda: lab.lock_free(lab.run / "screen-start.lock"))
        self.assert_one_watcher(lab)
        self.assertTrue(lab.lock_free(lab.bootstrap), "fd 8 leaked into launcher/watcher")
        self.assertIn("Screen watcher dispatched", (lab.run / "screen-start.log").read_text())

    def test_valid_existing_watcher_is_adopted_without_truncating_its_log(self):
        lab = self.lab()
        sentinel = lab.old_process()
        previous = b"existing watcher log\nkeep this history\n"
        log = lab.run / "screen-network-watch.log"
        log.write_bytes(previous)
        metadata = log.stat()
        stdout, _ = lab.finish(lab.launch())
        self.assertIn(b"already running", stdout)
        self.assertEqual(log.read_bytes(), previous)
        self.assertEqual((log.stat().st_ino, log.stat().st_mtime_ns),
                         (metadata.st_ino, metadata.st_mtime_ns))
        self.assertIsNone(sentinel.poll())
        self.assertEqual(lab.lines("watcher.invoked"), [])
        self.assertTrue(lab.lock_free(lab.run / "screen-start.lock"))

    def test_concurrent_and_repeated_launchers_spawn_once_without_leaking_fd9(self):
        lab = self.lab()
        gate = lab.root / "publication-held"
        write(gate, "hold publication until competitors exit\n")
        first = lab.launch()
        lab.wait_for(lambda: bool(lab.lines("watcher.started")))
        # Publication is deliberately blocked, so a missing dispatcher lock
        # cannot accidentally pass through the existing-PID adoption branch.
        competitors = [lab.launch() for _ in range(6)]
        for process in competitors:
            lab.finish(process, timeout=0.5)
        self.assertFalse(lab.lock_free(lab.run / "screen-start.lock"))
        gate.unlink()
        lab.finish(first)
        self.assert_one_watcher(lab)
        log = lab.run / "screen-network-watch.log"
        previous = log.read_bytes()
        lab.finish(lab.launch())
        self.assert_one_watcher(lab)
        self.assertEqual(log.read_bytes(), previous)

    def test_untrusted_old_pid_is_not_killed_and_full_argv_is_required(self):
        # A substring/path-only or first-two-arguments-only match must not adopt.
        for variant in ("extra-argument", "similar-path", "wrong-interpreter", "missing-proc"):
            with self.subTest(variant=variant):
                lab = self.lab()
                argv = {
                    "extra-argument": ["/bin/sh", str(lab.watcher), "untrusted"],
                    "similar-path": ["/bin/sh", str(lab.watcher) + ".old"],
                    "wrong-interpreter": ["/bin/bash", str(lab.watcher)],
                    "missing-proc": ["/bin/sh", str(lab.watcher)],
                }[variant]
                sentinel = lab.old_process(argv)
                if variant == "missing-proc":
                    (lab.proc / str(sentinel.pid) / "cmdline").unlink()
                lab.finish(lab.launch())
                self.assertIsNone(sentinel.poll(), "untrusted preexisting PID was killed")
                self.assert_one_watcher(lab)
                self.assertNotEqual((lab.run / "screen-network-watch.pid").read_text().strip(),
                                    str(sentinel.pid))

    def test_stop_uses_the_sourced_hook_and_leaves_existing_processes_untouched(self):
        lab = self.lab()
        sentinel = lab.old_process()
        log = lab.run / "screen-network-watch.log"
        log.write_bytes(b"existing log survives stop\n")
        old_pid = (lab.run / "screen-network-watch.pid").read_bytes()
        process, elapsed = lab.hook("stop")
        self.assertLess(elapsed, 1)
        self.assertEqual(lab.finish(process, timeout=0.5), (b"", b""))
        self.assertIsNone(sentinel.poll(), "stop killed an existing watcher")
        self.assertEqual(log.read_bytes(), b"existing log survives stop\n")
        self.assertEqual((lab.run / "screen-network-watch.pid").read_bytes(), old_pid)
        self.assertFalse(lab.bootstrap.exists(), "stop started a bootstrap worker")
        self.assertEqual(lab.lines("watcher.invoked"), [])


if __name__ == "__main__":
    if sys.platform != "linux":
        print(UNSUPPORTED, file=sys.stderr)
        raise SystemExit(2)
    unittest.main(verbosity=2)
