"""Linux integration tests for the real, read-only screen-ready executable.

Run: MU_OC_READY_TEST_BINARY=/absolute/path/screen-ready python3 test-screen-ready.py
The binary is required; an unavailable Linux runner/binary is an error, not a skip.
No device, production /proc, compiler, or network is used. ProcFixture is also
importable by an external Linux runner to prepare the same sparse-memory fixtures.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import select
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest


PID = 4242
STARTTIME = 424242
EXE = "/usr/bin/zte_topsw_devui"
HANDLER = 0x47A840
VTABLE = 0xE64908
STATE = 0x227153C
HEARTBEAT = STATE + 12
TIMER_LIST = 0x2279C78
HEAD, TAIL = 0x3000000, 0x3001000
DISPLAY_END = 0x404B000
MAP_LINES = (
    f"00400000-01000000 r-xp 00000000 00:01 123 {EXE}\n",
    "02271000-02280000 rw-p 00000000 00:00 0\n",
    "03000000-03002000 rw-p 00000000 00:00 0\n",
    "04000000-0404b000 rw-s 00000000 00:01 456 /dev/dri/card0\n",
)


class ProcFixture:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mu-oc-screen-ready-")
        self.root = Path(self.temp.name).resolve()
        self.proc = self.root / "proc"
        self.process = self.proc / str(PID)
        self.process.mkdir(parents=True)
        self.stat_path = self.process / "stat"
        self.mem_path = self.process / "mem"
        self.stat_bytes = self.make_stat()
        self.stat_path.write_bytes(self.stat_bytes)
        (self.process / "status").write_text(
            f"Name:\tzte_topsw_devui\nState:\tS (sleeping)\nTgid:\t{PID}\n"
            f"Pid:\t{PID}\nPPid:\t1\nTracerPid:\t0\n"
            "Uid:\t0\t0\t0\t0\nGid:\t0\t0\t0\t0\n",
            encoding="ascii",
        )
        (self.process / "exe").symlink_to(EXE)  # Deliberately may dangle.
        self.set_maps(MAP_LINES)
        self.mem_fd = os.open(self.mem_path, os.O_CREAT | os.O_RDWR, 0o640)
        os.ftruncate(self.mem_fd, DISPLAY_END)
        self.put(HANDLER, "I", 0xA9B97BFD)
        self.put(VTABLE, "QQ", 0x4F76AC, 0x4F77D4)
        self.put(STATE + 3, "B", 1)
        self.tick = 1000
        self.put(HEARTBEAT, "I", self.tick)
        self.put(TIMER_LIST, "QQQ", 32, HEAD, TAIL)
        for node in (HEAD, TAIL):
            self.put(node, "8xQ16x", HANDLER)
        for path in self.process.iterdir():
            if not path.is_symlink():
                path.chmod(0o640)

    @staticmethod
    def make_stat(starttime=STARTTIME):
        # Fields 1..22 inclusive, including the state field. Comm deliberately
        # contains spaces and ')' so a parser must find the closing delimiter.
        numeric = [1, PID, PID, 0, -1, 4194304, 0, 0, 0, 0,
                   1, 1, 0, 0, 20, 0, 1, 0, starttime]
        assert len(numeric) == 19
        return (f"{PID} (zte (ui) devui) S " + " ".join(map(str, numeric)) + "\n").encode("ascii")

    def put(self, address, fmt, *values):
        data = struct.pack("<" + fmt, *values)
        if os.pwrite(self.mem_fd, data, address) != len(data):
            raise OSError("short fixture memory write")

    def set_maps(self, lines):
        (self.process / "maps").write_text("".join(lines), encoding="ascii")

    def set_stat(self, starttime):
        self.stat_bytes = self.make_stat(starttime)
        # In-place same-length update models PID reuse without an inode race.
        fd = os.open(self.stat_path, os.O_WRONLY)
        try:
            if os.pwrite(fd, self.stat_bytes, 0) != len(self.stat_bytes):
                raise OSError("short fixture stat write")
        finally:
            os.close(fd)

    def snapshot(self, heartbeat=False, changing_stat=False):
        """Hash every logical byte, including sparse holes; ignore only planned writes.

        atime is intentionally excluded because reads may change it. Mutable
        bytes are checked against the exact writer's final value after execution.
        Modes, sizes, inodes, symlink targets, and the directory inventory must
        remain unchanged even for files deliberately updated by the fixture.
        """
        result = {}
        for path in [self.root, *sorted(self.root.rglob("*"))]:
            info = path.lstat()
            name = str(path.relative_to(self.root))
            entry = (stat.S_IMODE(info.st_mode), info.st_ino)
            if path.is_symlink():
                result[name] = ("link", *entry, os.readlink(path))
            elif path.is_dir():
                result[name] = ("dir", *entry)
            else:
                digest = hashlib.sha256()
                if not (changing_stat and path == self.stat_path):
                    with path.open("rb") as stream:
                        offset = 0
                        while chunk := stream.read(1024 * 1024):
                            if heartbeat and path == self.mem_path:
                                lo = max(0, HEARTBEAT - offset)
                                hi = min(len(chunk), HEARTBEAT + 4 - offset)
                                if lo < hi:
                                    chunk = chunk[:lo] + b"\0" * (hi - lo) + chunk[hi:]
                            digest.update(chunk)
                            offset += len(chunk)
                mutable = (heartbeat and path == self.mem_path) or (changing_stat and path == self.stat_path)
                result[name] = ("file", *entry, info.st_size, digest.hexdigest(),
                                None if mutable else info.st_mtime_ns)
        return result

    def close(self):
        os.close(self.mem_fd)
        self.temp.cleanup()


class StatReadWatch:
    """Synchronize PID reuse with an actual helper stat read, not a launch delay."""
    def __init__(self, path):
        libc = ctypes.CDLL(None, use_errno=True)
        libc.inotify_init1.argtypes = [ctypes.c_int]
        libc.inotify_init1.restype = ctypes.c_int
        libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        libc.inotify_add_watch.restype = ctypes.c_int
        self.fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1")
        if libc.inotify_add_watch(self.fd, os.fsencode(path), 0x00000001) < 0:  # IN_ACCESS
            os.close(self.fd)
            raise OSError(ctypes.get_errno(), "inotify_add_watch")

    def wait(self):
        if not select.select([self.fd], [], [], 3)[0]:
            raise AssertionError("helper never read the fixture stat")
        os.read(self.fd, 4096)

    def close(self):
        os.close(self.fd)


class ScreenReadyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sys.platform != "linux":
            raise RuntimeError("screen-ready integration tests require Linux; no Windows execution or silent skip")
        value = os.environ.get("MU_OC_READY_TEST_BINARY")
        if not value:
            raise RuntimeError("set MU_OC_READY_TEST_BINARY to the compiled Linux screen-ready executable")
        cls.binary = Path(value).expanduser().resolve()
        if not cls.binary.is_file() or not os.access(cls.binary, os.X_OK):
            raise RuntimeError(f"MU_OC_READY_TEST_BINARY is not executable: {cls.binary}")

    def setUp(self):
        self.lab = ProcFixture()
        self.addCleanup(self.lab.close)

    def invoke(self, expected, reason, *, heartbeat=False, reuse_pid=False,
               wait_ms=500, args=None):
        lab = self.lab
        before = lab.snapshot(heartbeat=heartbeat, changing_stat=reuse_pid)
        stop = threading.Event()
        errors = []
        updates = []
        watch = StatReadWatch(lab.stat_path) if reuse_pid else None

        def writer():
            try:
                if watch:
                    watch.wait()
                    # Allow the first identity read to finish. No heartbeat is
                    # advanced until starttime changes, so readiness cannot win.
                    if stop.wait(0.075):
                        return
                    lab.set_stat(STARTTIME + 1)
                    updates.append("stat")
                while not stop.wait(0.05):
                    lab.tick = (lab.tick + 50) & 0xFFFFFFFF
                    lab.put(HEARTBEAT, "I", lab.tick)
                    updates.append("heartbeat")
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=writer, daemon=True) if heartbeat or reuse_pid else None
        command = [str(self.binary)]
        if args is None:
            command += ["--pid", str(PID), "--proc-root", str(lab.proc)]
            if wait_ms is not None:
                command += ["--wait-ms", str(wait_ms)]
        else:
            command += list(args)
        try:
            if thread:
                thread.start()
            started = time.monotonic()
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=7)
                duration = time.monotonic() - started
            finally:
                stop.set()
                if thread:
                    thread.join(timeout=4)
        finally:
            if watch:
                watch.close()
        self.assertTrue(thread is None or not thread.is_alive(), "fixture writer failed to stop")
        self.assertEqual(errors, [], f"fixture writer failed: {errors}")
        self.assertEqual(before, lab.snapshot(heartbeat=heartbeat, changing_stat=reuse_pid),
                         "helper changed fixture bytes, modes, metadata, or inventory")
        self.assertEqual(lab.stat_path.read_bytes(), lab.stat_bytes)
        self.assertEqual(os.pread(lab.mem_fd, 4, HEARTBEAT), struct.pack("<I", lab.tick))
        self.assertEqual(result.returncode, expected, f"stdout={result.stdout!r}, stderr={result.stderr!r}")
        value = json.loads(result.stdout)  # Extra output and multiple JSON records fail.
        self.assertIs(type(value), dict)
        self.assertIs(value.get("ready"), expected == 0)
        if reason is None:
            self.assertIsInstance(value.get("reason"), str)
            self.assertTrue(value["reason"])
            self.assertNotEqual(value["reason"], "ready")
        else:
            self.assertEqual(value.get("reason"), reason)
        if expected == 0:
            self.assertIn("heartbeat", updates, "ready must follow a fixture heartbeat update")
        if reuse_pid:
            self.assertIn("stat", updates, "PID reuse must occur during the helper's check")
        return duration

    def test_progressing_ui_is_ready_including_counter_wrap_and_old_process(self):
        for starttime, tick in [(STARTTIME, 1000), (1, 0xFFFFFFF0)]:
            with self.subTest(starttime=starttime, tick=tick):
                self.lab.stat_bytes = self.lab.make_stat(starttime)
                self.lab.stat_path.write_bytes(self.lab.stat_bytes)
                self.lab.tick = tick
                self.lab.put(HEARTBEAT, "I", tick)
                # Omission exercises the public 1200 ms default too.
                self.invoke(0, "ready", heartbeat=True, wait_ms=None)

    def test_uninitialized_timer_state_is_retryable(self):
        for enabled in (0, 2):
            with self.subTest(enabled=enabled):
                self.lab.put(STATE + 3, "B", enabled)
                self.invoke(75, "waiting-for-timers", heartbeat=True)

    def test_missing_or_incorrect_framebuffer_is_retryable(self):
        for display in ("", MAP_LINES[-1].replace("0404b000", "0404a000"),
                        MAP_LINES[-1].replace("rw-s", "rw-p")):
            with self.subTest(display=display):
                self.lab.set_maps((*MAP_LINES[:-1], display))
                self.invoke(75, "waiting-for-display", heartbeat=True)

    def test_empty_or_invalid_timer_list_is_retryable(self):
        for node_size, head, tail in ((32, 0, 0), (31, HEAD, TAIL),
                                      (32, HEAD, 0), (32, HEAD + 1, TAIL)):
            with self.subTest(node_size=node_size, head=head, tail=tail):
                self.lab.put(TIMER_LIST, "QQQ", node_size, head, tail)
                self.invoke(75, "waiting-for-timers", heartbeat=True)

    def test_callback_must_be_executable_on_both_timer_nodes(self):
        for node, callback in ((HEAD, 0), (HEAD, STATE), (HEAD, HANDLER + 1),
                               (TAIL, 0xDEADBEEF)):
            with self.subTest(node=node, callback=callback):
                for valid_node in (HEAD, TAIL):
                    self.lab.put(valid_node, "8xQ16x", HANDLER)
                self.lab.put(node + 8, "Q", callback)
                self.invoke(75, "waiting-for-timers", heartbeat=True)

    def test_frozen_heartbeat_times_out_regardless_of_counter_age(self):
        for tick in (0, 0xFFFFFFFE):
            with self.subTest(tick=tick):
                self.lab.tick = tick
                self.lab.put(HEARTBEAT, "I", tick)
                duration = self.invoke(75, "waiting-for-heartbeat", wait_ms=300)
                self.assertGreaterEqual(duration, 0.27, "frozen heartbeat returned before its deadline")
                self.assertLess(duration, 2, "wait-ms did not bound the heartbeat check")
        duration = self.invoke(75, "waiting-for-heartbeat", wait_ms=0)
        self.assertLess(duration, 1, "zero wait should sample without waiting for a heartbeat")

    def test_pid_reuse_during_check_is_an_identity_failure(self):
        self.invoke(69, "ui-process-changed", heartbeat=True, reuse_pid=True, wait_ms=1200)

    def test_foreign_exe_uid_tgid_tracer_or_zero_starttime_is_rejected(self):
        status_path = self.lab.process / "status"
        status = status_path.read_text(encoding="ascii")
        exe = self.lab.process / "exe"
        for target in ("/bin/sleep", EXE + " (deleted)"):
            with self.subTest(exe=target):
                exe.unlink()
                exe.symlink_to(target)
                self.invoke(69, "ui-identity-unavailable")
        exe.unlink()
        exe.symlink_to(EXE)
        for old, new in (("Uid:\t0\t0\t0\t0", "Uid:\t0\t0\t1000\t0"),
                         (f"Tgid:\t{PID}", f"Tgid:\t{PID + 1}"),
                         ("TracerPid:\t0", "TracerPid:\t7")):
            with self.subTest(status=new):
                status_path.write_text(status.replace(old, new), encoding="ascii")
                self.invoke(69, "ui-identity-unavailable")
        status_path.write_text(status, encoding="ascii")
        self.lab.stat_bytes = self.lab.make_stat(0)
        self.lab.stat_path.write_bytes(self.lab.stat_bytes)
        self.invoke(69, "ui-identity-unavailable")

    def test_wrong_instruction_or_either_vtable_slot_is_unsupported(self):
        for address, fmt, value in ((HANDLER, "I", 0xD503201F),
                                    (VTABLE, "Q", 0x4F76AD),
                                    (VTABLE + 8, "Q", 0x4F77D5)):
            with self.subTest(address=hex(address)):
                self.lab.put(HANDLER, "I", 0xA9B97BFD)
                self.lab.put(VTABLE, "QQ", 0x4F76AC, 0x4F77D4)
                self.lab.put(address, fmt, value)
                self.invoke(69, "unsupported-ui")

    def test_bad_arguments_are_usage_errors(self):
        valid = ("--pid", str(PID), "--proc-root", str(self.lab.proc))
        cases = [(), ("--pid",), ("--proc-root", str(self.lab.proc)),
                 ("--pid", "-1"), ("--pid", "0"), ("--pid", "1"),
                 ("--pid", "2147483648"), ("--pid", "4242x"),
                 (*valid, "--pid", str(PID)), (*valid, "--proc-root", "relative"),
                 (*valid, "--wait-ms", "-1"), (*valid, "--wait-ms", "5001"),
                 (*valid, "--wait-ms", "x"), (*valid, "--wait-ms"),
                 (*valid, "--unknown"), (*valid, "extra")]
        for args in cases:
            with self.subTest(args=args):
                self.invoke(64, None, args=args)


if __name__ == "__main__":
    unittest.main(verbosity=2)
