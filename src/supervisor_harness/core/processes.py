"""What a command the harness starts may leave running: nothing.

Measured in Minesweeper run 5. An implementer's probe script started ``npx vite
preview`` through Node's ``child_process``, and ``run_command`` ran the probe.
Two ``node`` processes, ``npx-cli.js`` and the ``vite.js`` it started, were
still serving ports 4173 and 4199 forty-five minutes after the run ended. They
held files under the worktree's ``node_modules``, so the worktree was removed
only partly, and the scorer scored the husk.

The tree kill missed them because they were no longer in the tree it walked.
``taskkill /T`` follows parent ids down from the process the harness started.
Node's timeout on the probe's own ``execSync`` -- or a ``child.kill()`` -- stops
the ``cmd.exe`` it ran ``npx.cmd`` in and nothing below it, so ``npx`` and
``vite`` were left with a dead parent, and no parent id led to them. A command
that returned on its own was followed by no kill at all. Measured the other
way too: a direct ``npx vite preview`` that ran into the timeout with its shim
still alive was stopped, all five processes, within half a second.

So membership is decided when a process is created, not by walking ancestry
when it has to die:

* **Windows**: the command starts suspended, joins a Job Object, then runs.
  Everything it starts is in the job, and stays there whatever happens to its
  parent -- a job is left only by a breakaway the job does not allow. The job
  is terminated at the timeout *and* when the command returns, and it is
  created ``KILL_ON_JOB_CLOSE``, so the harness dying takes it down as well.
* **Elsewhere**: the command leads its own process group, as before, and the
  group is killed when the command returns as well as at the timeout. An
  orphan keeps its group, so this reaches it. A process that calls
  ``setsid`` leaves the group, and nothing short of a cgroup can follow it;
  for those, closing the run's worktree stops whatever is still working inside
  it (`stop_processes_in`).
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

#: How long a stopped process is given to be gone before its files are needed.
_EXIT_WAIT_SECONDS = 5.0


class BoundedProcess:
    """A command and everything it starts, which `stop` ends together."""

    def __init__(self, argv: list[str], cwd: Path | str, env: dict[str, str]) -> None:
        self._job: int | None = None
        if sys.platform == "win32":
            self.proc = subprocess.Popen(  # noqa: S603 - tokenised, no shell, allow-listed
                argv, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                errors="replace", env=env,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | _CREATE_SUSPENDED,
            )
            self._job = _enter_job(self.proc)
        else:
            self.proc = subprocess.Popen(  # noqa: S603 - tokenised, no shell, allow-listed
                argv, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                errors="replace", env=env, start_new_session=True,
            )

    def stop(self) -> None:
        """End the command, if it is still running, and everything it started."""
        if sys.platform == "win32":
            if self._job is not None:
                _end_job(self._job)
                self._job = None
            elif self.proc.poll() is None:
                # No job (see `_enter_job`): the old walk by parent id is what is left.
                subprocess.run(  # noqa: S603 - fixed argv, no shell
                    ["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],  # noqa: S607
                    capture_output=True, check=False,
                )
        else:
            # The group outlives its leader while any member is left, and its
            # id is not handed to a new process until then. Plain try/except
            # rather than contextlib.suppress: the project's guard counts every
            # `suppress` as a broad one (test_enforce_versus_observe).
            try:  # noqa: SIM105
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass  # the group is empty, or not ours to signal
        try:  # noqa: SIM105
            self.proc.kill()
        except OSError:
            pass  # already gone


def alive(pid: int) -> bool:
    """Whether process ``pid`` is still running."""
    if sys.platform == "win32":
        return _alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return True
    return stat.rpartition(")")[2].split()[:1] != ["Z"]


def stop_processes_in(tree: Path) -> list[str]:
    """Stop every process whose program or working directory is inside ``tree``.

    For a directory about to be deleted: on Windows, a running process's
    working directory and open files cannot be, so one survivor leaves a husk.
    Never this process or its ancestors. Returns one line per process stopped,
    ``pid N name``, so what was stopped is said rather than done quietly.
    Implemented for Windows and Linux; elsewhere it stops nothing.
    """
    root = _normal(str(tree))
    spared = _ancestry()
    stopped: list[str] = []
    for pid, name, places in _process_places():
        if pid in spared or not any(_inside(_normal(p), root) for p in places if p):
            continue
        if _terminate(pid):
            stopped.append(f"pid {pid} {name}")
    return stopped


# -- discovering processes ------------------------------------------------------


def _normal(path: str) -> str:
    path = os.path.normcase(os.path.normpath(path.removeprefix("\\\\?\\")))
    return path.rstrip("\\/")


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root + os.sep)


def _ancestry() -> set[int]:
    parents = {pid: ppid for pid, ppid in _parents()}
    spared, pid = {os.getpid()}, os.getpid()
    while (pid := parents.get(pid, 0)) and pid not in spared:
        spared.add(pid)
    return spared


def _parents() -> list[tuple[int, int]]:
    if sys.platform == "win32":
        return [(pid, ppid) for pid, ppid, _ in _snapshot()]
    if sys.platform != "linux":
        return [(os.getpid(), os.getppid())]
    pairs = []
    for pid in _linux_pids():
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
            pairs.append((pid, int(stat.rpartition(")")[2].split()[1])))
        except (OSError, ValueError, IndexError):
            continue
    return pairs


def _process_places() -> list[tuple[int, str, tuple[str, ...]]]:
    """Each process, its name, and where it lives: its program and its working directory."""
    if sys.platform == "win32":
        return [(pid, name, (_image_path(pid), _working_directory(pid)))
                for pid, _, name in _snapshot()]
    if sys.platform != "linux":
        return []
    found = []
    for pid in _linux_pids():
        places = []
        for link in ("exe", "cwd"):
            try:  # noqa: SIM105
                places.append(os.readlink(f"/proc/{pid}/{link}"))
            except OSError:
                pass  # not ours to read, or already gone
        try:
            name = Path(f"/proc/{pid}/comm").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found.append((pid, name.strip(), tuple(places)))
    return found


def _linux_pids() -> list[int]:
    try:
        return [int(entry) for entry in os.listdir("/proc") if entry.isdigit()]
    except OSError:
        return []


def _terminate(pid: int) -> bool:
    if sys.platform == "win32":
        return _terminate_windows(pid)
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return False
    deadline = time.monotonic() + _EXIT_WAIT_SECONDS
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return True


# -- Windows --------------------------------------------------------------------

_CREATE_SUSPENDED = 0x00000004

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")

    _PROCESS_TERMINATE = 0x0001
    _PROCESS_VM_READ = 0x0010
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_QUERY_INFORMATION = 0x0400
    _PROCESS_SUSPEND_RESUME = 0x0800
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _SYNCHRONIZE = 0x00100000
    _STILL_ACTIVE = 259
    _ERROR_ACCESS_DENIED = 5
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _TH32CS_SNAPPROCESS = 0x00000002
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class _BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BasicLimits),
                    ("IoInfo", ctypes.c_uint64 * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class _ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]

    class _BasicInformation(ctypes.Structure):
        _fields_ = [("ExitStatus", wintypes.LONG),
                    ("PebBaseAddress", ctypes.c_void_p),
                    ("AffinityMask", ctypes.c_size_t),
                    ("BasePriority", wintypes.LONG),
                    ("UniqueProcessId", ctypes.c_size_t),
                    ("InheritedFromUniqueProcessId", ctypes.c_size_t)]

    def _declare(fn: Any, restype: Any, *argtypes: Any) -> Any:
        fn.restype, fn.argtypes = restype, list(argtypes)
        return fn

    _H = wintypes.HANDLE
    _OpenProcess = _declare(_kernel32.OpenProcess, _H, wintypes.DWORD, wintypes.BOOL,
                            wintypes.DWORD)
    _CloseHandle = _declare(_kernel32.CloseHandle, wintypes.BOOL, _H)
    _GetExitCodeProcess = _declare(_kernel32.GetExitCodeProcess, wintypes.BOOL, _H,
                                   ctypes.POINTER(wintypes.DWORD))
    _TerminateProcess = _declare(_kernel32.TerminateProcess, wintypes.BOOL, _H, wintypes.UINT)
    _WaitForSingleObject = _declare(_kernel32.WaitForSingleObject, wintypes.DWORD, _H,
                                    wintypes.DWORD)
    _CreateJobObjectW = _declare(_kernel32.CreateJobObjectW, _H, ctypes.c_void_p,
                                 wintypes.LPCWSTR)
    _SetInformationJobObject = _declare(_kernel32.SetInformationJobObject, wintypes.BOOL, _H,
                                        ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    _AssignProcessToJobObject = _declare(_kernel32.AssignProcessToJobObject, wintypes.BOOL,
                                         _H, _H)
    _TerminateJobObject = _declare(_kernel32.TerminateJobObject, wintypes.BOOL, _H,
                                   wintypes.UINT)
    _CreateToolhelp32Snapshot = _declare(_kernel32.CreateToolhelp32Snapshot, _H,
                                         wintypes.DWORD, wintypes.DWORD)
    _Process32FirstW = _declare(_kernel32.Process32FirstW, wintypes.BOOL, _H,
                                ctypes.POINTER(_ProcessEntry))
    _Process32NextW = _declare(_kernel32.Process32NextW, wintypes.BOOL, _H,
                               ctypes.POINTER(_ProcessEntry))
    _QueryFullProcessImageNameW = _declare(_kernel32.QueryFullProcessImageNameW,
                                           wintypes.BOOL, _H, wintypes.DWORD,
                                           wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    _ReadProcessMemory = _declare(_kernel32.ReadProcessMemory, wintypes.BOOL, _H,
                                  ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                  ctypes.POINTER(ctypes.c_size_t))
    _IsWow64Process = _declare(_kernel32.IsWow64Process, wintypes.BOOL, _H,
                               ctypes.POINTER(wintypes.BOOL))
    _NtResumeProcess = _declare(_ntdll.NtResumeProcess, wintypes.LONG, _H)
    _NtQueryInformationProcess = _declare(_ntdll.NtQueryInformationProcess, wintypes.LONG,
                                          _H, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG,
                                          ctypes.POINTER(wintypes.ULONG))

    @contextlib.contextmanager
    def _opened(pid: int, access: int) -> Any:
        handle = _OpenProcess(access, False, pid)
        try:
            yield handle
        finally:
            if handle:
                _CloseHandle(handle)

    def _enter_job(proc: subprocess.Popen[str]) -> int | None:
        """Put the suspended ``proc`` in a new kill-on-close job, then let it run.

        The process was created suspended so that nothing it starts can be
        started before it is in the job. It is resumed whatever happens here:
        a job that cannot be made or joined costs the guarantee, not the
        command. Then ``stop`` falls back to the walk by parent id.
        """
        job = _CreateJobObjectW(None, None)
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        with _opened(proc.pid, _PROCESS_SET_QUOTA | _PROCESS_TERMINATE
                     | _PROCESS_SUSPEND_RESUME) as handle:
            joined = bool(job and handle and _SetInformationJobObject(
                job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits),
                ctypes.sizeof(limits)) and _AssignProcessToJobObject(job, handle))
            resumed = bool(handle) and _NtResumeProcess(handle) >= 0
        if not resumed:
            if job:
                _CloseHandle(job)
            proc.kill()
            raise OSError(f"could not start {proc.args!r}: the suspended process "
                          "could not be resumed")
        if not joined:
            if job:
                _CloseHandle(job)
            return None
        return int(job)

    def _end_job(job: int) -> None:
        _TerminateJobObject(job, 1)
        _CloseHandle(job)

    def _alive_windows(pid: int) -> bool:
        with _opened(pid, _PROCESS_QUERY_LIMITED_INFORMATION) as handle:
            if not handle:
                return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
            code = wintypes.DWORD()
            return bool(_GetExitCodeProcess(handle, ctypes.byref(code))) and \
                code.value == _STILL_ACTIVE

    def _terminate_windows(pid: int) -> bool:
        with _opened(pid, _PROCESS_TERMINATE | _SYNCHRONIZE) as handle:
            if not handle or not _TerminateProcess(handle, 1):
                return False
            _WaitForSingleObject(handle, int(_EXIT_WAIT_SECONDS * 1000))
            return True

    def _snapshot() -> list[tuple[int, int, str]]:
        """Every process: its id, its parent's id, and its program's file name."""
        snap = _CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if not snap or snap == _INVALID_HANDLE_VALUE:
            return []
        found = []
        try:
            entry = _ProcessEntry()
            entry.dwSize = ctypes.sizeof(entry)
            more = _Process32FirstW(snap, ctypes.byref(entry))
            while more:
                found.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
                more = _Process32NextW(snap, ctypes.byref(entry))
        finally:
            _CloseHandle(snap)
        return found

    def _image_path(pid: int) -> str:
        with _opened(pid, _PROCESS_QUERY_LIMITED_INFORMATION) as handle:
            if not handle:
                return ""
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not _QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return ""
            return buffer.value

    def _working_directory(pid: int) -> str:
        """Read from the process's own parameters block, as Process Explorer does.

        Windows has no call for another process's working directory. The block
        is found through the process environment block, at offsets that have
        not moved since Windows XP. A process of the other bitness is skipped:
        its directory lives in a block laid out differently.
        """
        with _opened(pid, _PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ) as handle:
            if not handle or _wow64(handle) != _wow64(None):
                return ""
            info = _BasicInformation()
            if _NtQueryInformationProcess(handle, 0, ctypes.byref(info), ctypes.sizeof(info),
                                          None) < 0 or not info.PebBaseAddress:
                return ""
            wide = ctypes.sizeof(ctypes.c_void_p) == 8
            params = _read_pointer(handle, info.PebBaseAddress + (0x20 if wide else 0x10))
            if not params:
                return ""
            directory = params + (0x38 if wide else 0x24)
            length = ctypes.c_uint16()
            if not _read(handle, directory, length):
                return ""
            text = _read_pointer(handle, directory + (8 if wide else 4))
            if not text or not length.value:
                return ""
            buffer = ctypes.create_string_buffer(length.value)
            if not _read(handle, text, buffer):
                return ""
            return buffer.raw.decode("utf-16-le", errors="replace")

    def _wow64(handle: Any) -> bool:
        flag = wintypes.BOOL()
        target = handle if handle is not None else ctypes.c_void_p(-1)  # this process
        return bool(_IsWow64Process(target, ctypes.byref(flag))) and bool(flag.value)

    def _read(handle: Any, address: int, into: Any) -> bool:
        done = ctypes.c_size_t()
        return bool(_ReadProcessMemory(handle, address, ctypes.byref(into),
                                       ctypes.sizeof(into), ctypes.byref(done))) and \
            done.value == ctypes.sizeof(into)

    def _read_pointer(handle: Any, address: int) -> int:
        pointer = ctypes.c_void_p()
        return (pointer.value or 0) if _read(handle, address, pointer) else 0
