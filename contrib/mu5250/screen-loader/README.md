# MU5250 AArch64-musl screen loader sources

This source package preserves the reviewed loader and dummy fixtures from
`stock-screen-analysis-20261107/injector`. It includes no binaries, generated
`out/` directory or historical `REVIEW-READY.md`. The C, assembly, headers and
`test_dummy.py` are copied byte-for-byte; the build and artifact-verification
scripts support this packaged location and the sibling `../screen-network.c`.

## Recorded runtime evidence

The parent's **2026-11-07** records identify this loader SHA-256:

```text
a8dbf1ca860b90d3fd70c8c48681dd77a955ef1c464741b815caae5f3c930085
```

That build passed the parent's real native dummy load/entry/uninstall experiment
and UI install/uninstall calls. Evidence remains in the workspace's
`stock-screen-analysis-20261107/integration/` and is not shipped here:

- `loader-dummy-test.json`: loader hashes, constructor and entry markers,
  `dummy_entry` / `dummy_uninstall` return logs, zero canary failure, heartbeat
  observations and cooperative `DUMMY_EXIT result=0`.
- `ui-extension-manifest.json`: the loader hash above, UI executable hash
  `571a201d18ba5ae80ce9f2b92948e63e9f73f2ead22b1920d3f58a6716f07028`,
  and the tested payload hash
  recorded there. Payload revisions have separate hashes; use the current
  device manifest rather than assuming a historical payload is still active.
- `ui-install.log` and `ui-uninstall.log`: `mu_screen_install` and
  `mu_screen_uninstall` each returned `0x0`, exact architectural state restoration
  passed, the selected TID detached and `RESULT=0` / `UI_EXTENSION_EXIT=0`.
- `ui-live.txt`: the parent's extension log records timer installation, removal
  without restarting the UI, and adding the OpenClash switch to the Network page.

These are historical parent-run results for those artifacts. They do not certify
every native harness test, timeout recovery, later payload edits or rebuilt
artifacts. **Full reboot acceptance has not been verified.** This packaging work
uses offline builds and inspection only. Debug paths and compiler versions can
change binary hashes; do not assign the historical hash to a fresh build.

## Source inventory

| File | Purpose |
| --- | --- |
| `ptrace_load.c` | Root-only selected-TID loader, target identity checks, syscall bootstrap, remote calls and verified restoration. |
| `elf_image.c`, `elf_image.h` | Bounded ELF64 AArch64 reader using program headers, `PT_DYNAMIC`, `DT_HASH` / `DT_GNU_HASH`; no section-header dependency. |
| `sha256.h` | Dependency-free SHA-256 for the expected executable and shared library. |
| `trace_state.h` | GPR/FPSIMD layouts, exact architectural comparison and external SIGTRAP sender policy. |
| `dummy_target.c`, `dummy_loop.S` | Dynamic musl target with direct nanosleep, shared heartbeat and GPR/SIMD canaries. |
| `dummy_library.c` | Constructor/entry/uninstall markers and an optional deliberately hanging constructor. |
| `build.py` | Portable Python 3.8+ cross-build of loader, dummy target/library and sibling UI payload. |
| `verify_build.py` | Host-only ELF and manifest/hash checks for all four outputs. |
| `test_dummy.py` | Opt-in native AArch64 Linux harness; cooperative dummy cleanup. |

The UI payload source is owned by the parent at `../screen-network.c`; it is
compiled in place and is not copied or edited by this package.

## Offline build

Use Python 3.8+ and Zig. The historical build used **Zig 0.16.0**. Compiler
selection is `--zig` first, then the `ZIG` environment variable, then `zig` on
`PATH`. A value is one executable path or command name, not a shell command with
arguments. Paths containing spaces work without embedded quote characters.
Scripts resolve sources relative to themselves, so the current directory does
not matter. No toolchain download or device connection is performed.

From this directory in PowerShell:

```powershell
$env:ZIG = 'C:\path to toolchain\zig.exe'
python .\build.py
python .\verify_build.py
```

On Linux/macOS, or with an explicit compiler/output directory:

```sh
ZIG=/path/to/zig python3 build.py --out /absolute/build-dir
python3 verify_build.py --out /absolute/build-dir
# Alternatively: python3 build.py --zig /path/to/zig --out /absolute/build-dir
```

The default output is `../dist/screen-loader/`, covered by the existing MU5250
`dist/` ignore rule, keeping generated artifacts outside this source package.
An explicit relative `--out` resolves against the caller's current directory.

All outputs target `aarch64-linux-musl` with `-O2 -g -Wall -Wextra -Werror
-Wformat=2`, stack protection and fortification:

- `ptrace-load`: static executable, no ELF interpreter or `DT_NEEDED`.
- `dummy-target`: dynamic PIE, interpreter `/lib/ld-musl-aarch64.so.1`.
- `libdummy-inject.so`: shared dummy fixture, immediate binding and RELRO.
- `screen-network.so`: sibling screen payload, shared PIC, immediate binding and RELRO.

`build-manifest.json` records compiler version, input and output hashes/sizes,
and `runtime_tests_executed: false`. A failed rebuild invalidates prior manifests
and verification reports. `verify_build.py` independently reads all four ELF
headers and dynamic tags, checks output hashes/sizes against the manifest and
writes `artifact-verification.json`. Run it without Python optimization flags.
Neither script executes any target artifact or validates ptrace/GUI behavior.

## Current bootstrap and exact restoration

1. Verify root target UIDs, canonical executable/SO paths, SHA-256, executable
   start time/inode, namespaces, supported auxv and musl mappings. Resolve the
   target's `mmap`, `dlopen` and `dlsym` from its mapped libc, not local addresses.
2. Observe an allowed blocked syscall within `--safe-wait-ms`: nanosleep (101),
   clock_nanosleep (115), ppoll (73), epoll_pwait (22) or epoll_pwait2 (441).
   `PTRACE_SEIZE` controls only the selected TID, with
   `PTRACE_O_TRACESYSGOOD | PTRACE_O_TRACEEXEC | PTRACE_O_TRACEEXIT`.
   `PTRACE_INTERRUPT` must produce the expected stop. Recheck the blocked context,
   target identity and live libc instruction bytes.
3. Snapshot all 31 GPRs, SP, PC and PSTATE; FPSIMD V0..V31, FPSR and FPCR;
   the 8-byte TLS register; `NT_ARM_SYSTEM_CALL`; and the 64-bit kernel signal
   mask. The original syscall-number regset must be `-1`, after kernel restart
   preparation. Reject unsupported SVE/SME/MTE/GCS state.
4. Temporarily block catchable signals. Invoke direct `mmap` (222) at an existing
   libc `svc #0` using **two `PTRACE_SYSCALL` resumes**, for entry and exit. Require
   syscall stops marked **`SIGTRAP | 0x80`** by **`PTRACE_O_TRACESYSGOOD`**, no ptrace
   event, PC=`svc+4`, matching SP/x8, and syscall number 222 at entry.
   **`PTRACE_SINGLESTEP` is not used.** No PSTATE.SS normalization is applied to
   saved/restored state. AArch64 syscall stops can expose tracer-specific x7
   behavior; complete GPR restoration remains mandatory.
5. Verify the retained mapping is RW/NX: a 2 MiB scratch stack plus two pages.
   Write strings only into scratch. Remote C calls use an aligned scratch stack,
   `PTRACE_CONT`, and LR pointing at a scratch NX sentinel. Accept return only at
   exact `SIGSEGV/SEGV_ACCERR`, fault address/PC=sentinel, and the expected SP;
   suppress this artificial fault. No executable trampoline or original stack
   writes are used.
6. Restore and read back every saved architectural field before detach:
   **GPR/SP/PC/PSTATE: all 272 bytes, no ignored bits; FP/NEON: all 520 architectural
   bytes of the 528-byte regset; TLS: exactly 8 bytes; syscall number: exact saved
   value; signal mask: exact 64-bit value.** Only the eight nonarchitectural FP
   padding bytes are ignored in comparison and logged separately. Any SET/GET,
   length or architectural mismatch fails restoration. Parent dummy and both UI
   logs record zero mismatches and successful detach.

Unexpected SIGTRAP forwarding requires a recognized sender code and positive,
independent sender PID. Injector or unverified traps are suppressed while the
operation still fails. Identifiable external signals are forwarded on detach;
signal timing/group-stop preservation is best effort. No `PTRACE_O_EXITKILL`,
kill, service restart, `dlclose`, remote thread creation or text patch is used.

## CLI and native dummy harness

```text
ptrace-load --expect-exe /canonical/exe --expect-sha256 HEX --pid PID
  --so /canonical/library.so --expect-so-sha256 HEX
  [--tid TID] [--timeout-ms 1000..30000] [--safe-wait-ms 0..10000]
  [--entry SYMBOL --arg0 UINT64] [--check-only | --ack-loader-risk]
  [--confirm-no-pidns]
ptrace-load --inspect-elf /absolute/file [SYMBOL]
```

Default TID is PID. `--check-only` verifies identity/hash/ELF/mappings without
attachment; it does not test safe points or restoration. Injection requires
`--ack-loader-risk`. `dlopen` uses `RTLD_NOW | RTLD_LOCAL`. Optional entry symbols
must be defined ordinary function exports in the verified SO, and target `dlsym`
must resolve to its verified mapping/export address. `--entry` and `--arg0` must
be provided together. For `mu_screen_install` / `mu_screen_uninstall`, use arg0=0
and interpret the low 32 return bits as a signed C int. Loader exit 0 means the
call returned; assess the entry return and UI effect separately. Every invocation
adds a dlopen reference; uninstall does not unload the library.

Native tests require root on an expendable AArch64 Linux environment with
compatible musl and ptrace permissions, common mount/PID namespaces, no seccomp
and no unsupported extended architectural state. QEMU user-mode execution does
not validate native kernel regsets. Executable and library files must be
root-owned, canonical absolute paths and not group/world writable. No native
tests are run by the build scripts.

```sh
python3 test_dummy.py --out /absolute/build-dir
# Separate expendable dummy only: deliberately interrupts a hanging constructor.
python3 test_dummy.py --out /absolute/build-dir --include-unsafe-timeout-test
```

The harness also checks sectionless ELF parsing, SHA against Python hashlib,
malformed ELF/wrong executable hash refusal, constructor/entry/uninstall markers,
entry return, canaries, heartbeat and detach. Its retained default `--out` is
`./out` beside the script; pass `--out` explicitly to match the packaged build.
It requests cooperative exit by writing `Q` at byte zero of the dummy's existing
4096-byte control file without truncation. An unresponsive dummy is retained and
reported. Harness subprocess timeouts can terminate a hung helper; this harness
belongs in an expendable dummy environment.

Only use `--confirm-no-pidns` after independently verifying that the actual
kernel disables `CONFIG_PID_NS`. It permits both self/target `ns/pid` interfaces
to be absent with ENOENT; one-sided absence, other errors or differing existing
identities still fail. Mount namespace checks remain strict. The parent's
recorded runs used this explicit attestation.

## Limits and exit codes

Register restoration is not process-state rollback. Only one TID is controlled;
other threads can hold loader/heap locks, and a blocked syscall is a heuristic
safe point, not proof of reentrancy. Constructors and entries must be reviewed
for the exact UI build and complete promptly. The helper validates the SO's hash,
not the payload's internal GUI guards or behavior. Root writers can still race
the final path-based dlopen.

An unfinished remote operation triggers interrupt/restore/verify/detach recovery.
Even with exact registers and a continuing heartbeat, constructor side effects,
heap/TLS memory and locks cannot be rolled back. Exit 3 is not safe to retry.
Uninterruptible sleep, exec/exit or a failed recovery stop can leave restoration
unconfirmed (exit 4). Scratch is retained, has no hard stack guard, and consumes
roughly 2 MiB per invocation; repeated loads also retain library references.
The source's legacy bootstrap error label still says `mmap/single-step`; the
implemented protocol and `MMAP syscall entry/exit verified` logs use syscall
tracing as described above.

| Exit | Meaning |
| --- | --- |
| 0 | Check/load and optional entry returned as expected; assess entry result separately. |
| 1 | Refusal or operation failure with ordinary cleanup. |
| 2 | Invalid CLI. |
| 3 | In-flight operation rewound; semantic state unsafe. |
| 4 | Cleanup/restoration or target lifecycle uncertain. |
