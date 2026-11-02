"""Owned process trees and advisory locks for automatic collaboration.

Windows 10 / Server 2016+: CreateProcessW receives JOB_LIST before initial code
runs, plus an explicit HANDLE_LIST. No Popen-then-assign or suspended orphan gap.
The unnamed non-inherited Job Object has KILL_ON_JOB_CLOSE and no breakaway flags.
This controls normal CreateProcess descendants, not a hostile user's external
service/WMI processes. Installed executables and administrator policies are trusted.

No model calls, login, setup, configuration mutation, shell or dependencies here.
The caller supplies already-allowlisted argv/environment and a registered cwd.
Windows native execution must be tested on Windows; Linux mocks are not proof of
native ABI/host policy behavior. See tests/test_auto_process.py for fixture checks.

Sources: Microsoft Job Objects; UpdateProcThreadAttribute; CreateProcessW;
LockFileEx; Raymond Chen 2023-02-09 (JOB_LIST closes pre-assignment crash gap).
"""
from __future__ import annotations

import ctypes as C
from contextlib import contextmanager
import errno
import math
import ntpath
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import threading
import time

PIPE = subprocess.PIPE
DEVNULL = subprocess.DEVNULL
STDOUT = subprocess.STDOUT


class ProcessError(OSError):
    """Sanitized public failure; never includes argv, environment or process text."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


# Fixed Windows ABI widths even when these structures are inspected on Linux.
DWORD = C.c_uint32
WORD = C.c_uint16
BOOL = C.c_int32
HANDLE = C.c_void_p
SIZE_T = C.c_size_t
LPVOID = C.c_void_p
LPWSTR = C.c_wchar_p


class SECURITY_ATTRIBUTES(C.Structure):
    _fields_ = [("nLength", DWORD), ("lpSecurityDescriptor", LPVOID), ("bInheritHandle", BOOL)]


class STARTUPINFOW(C.Structure):
    _fields_ = [("cb", DWORD), ("lpReserved", LPWSTR), ("lpDesktop", LPWSTR), ("lpTitle", LPWSTR),
                ("dwX", DWORD), ("dwY", DWORD), ("dwXSize", DWORD), ("dwYSize", DWORD),
                ("dwXCountChars", DWORD), ("dwYCountChars", DWORD), ("dwFillAttribute", DWORD),
                ("dwFlags", DWORD), ("wShowWindow", WORD), ("cbReserved2", WORD),
                ("lpReserved2", C.POINTER(C.c_ubyte)), ("hStdInput", HANDLE), ("hStdOutput", HANDLE), ("hStdError", HANDLE)]


class STARTUPINFOEXW(C.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", LPVOID)]


class PROCESS_INFORMATION(C.Structure):
    _fields_ = [("hProcess", HANDLE), ("hThread", HANDLE), ("dwProcessId", DWORD), ("dwThreadId", DWORD)]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(C.Structure):
    _fields_ = [("PerProcessUserTimeLimit", C.c_int64), ("PerJobUserTimeLimit", C.c_int64),
                ("LimitFlags", DWORD), ("MinimumWorkingSetSize", SIZE_T), ("MaximumWorkingSetSize", SIZE_T),
                ("ActiveProcessLimit", DWORD), ("Affinity", SIZE_T), ("PriorityClass", DWORD), ("SchedulingClass", DWORD)]


class IO_COUNTERS(C.Structure):
    _fields_ = [(name, C.c_uint64) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(C.Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", SIZE_T), ("JobMemoryLimit", SIZE_T),
                ("PeakProcessMemoryUsed", SIZE_T), ("PeakJobMemoryUsed", SIZE_T)]


class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(C.Structure):
    _fields_ = [("TotalUserTime", C.c_int64), ("TotalKernelTime", C.c_int64),
                ("ThisPeriodTotalUserTime", C.c_int64), ("ThisPeriodTotalKernelTime", C.c_int64),
                ("TotalPageFaultCount", DWORD), ("TotalProcesses", DWORD), ("ActiveProcesses", DWORD), ("TotalTerminatedProcesses", DWORD)]


class OVERLAPPED(C.Structure):
    # Offset/OffsetHigh occupy the union's full pointer-sized storage on 32/64bit.
    _fields_ = [("Internal", SIZE_T), ("InternalHigh", SIZE_T), ("Offset", DWORD), ("OffsetHigh", DWORD), ("hEvent", HANDLE)]


JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_NO_WINDOW = 0x08000000
STARTF_USESTDHANDLES = 0x100
WAIT_TIMEOUT = 258
WAIT_OBJECT_0 = 0
INFINITE = 0xFFFFFFFF
INVALID_HANDLE_VALUE = C.c_void_p(-1).value


def _validate(argv, cwd, env, *, windows=False):
    if not isinstance(argv, (list, tuple)) or not argv or any(not isinstance(a, str) or "\0" in a for a in argv):
        raise ProcessError("invalid_argv")
    if not argv[0] or not (ntpath.isabs(argv[0]) if windows else os.path.isabs(argv[0])):
        raise ProcessError("absolute_executable_required")
    if windows and ntpath.splitext(argv[0])[1].lower() not in {".exe", ".com"}:
        raise ProcessError("native_windows_executable_required")
    if not isinstance(env, dict) or any(not isinstance(k, str) or not k or "=" in k or "\0" in k or not isinstance(v, str) or "\0" in v for k, v in env.items()):
        raise ProcessError("invalid_environment")
    if windows and len({k.casefold() for k in env}) != len(env):
        raise ProcessError("ambiguous_environment")
    cwd = os.fspath(cwd)
    if not isinstance(cwd, str) or "\0" in cwd or not (ntpath.isabs(cwd) if windows else os.path.isabs(cwd)):
        raise ProcessError("absolute_workspace_required")
    if windows and len(subprocess.list2cmdline(argv).encode("utf-16-le")) // 2 >= 32767:
        raise ProcessError("argv_limit")
    return tuple(argv), cwd, dict(env)


class _WindowsAPI:
    """Small ctypes boundary. Test doubles replace this class, never platform APIs."""
    def __init__(self, kernel=None):
        if kernel is None:
            if os.name != "nt":
                raise ProcessError("windows_api_unavailable")
            kernel = C.WinDLL("kernel32", use_last_error=True)
        self.k = kernel
        signatures = {
            "CreateJobObjectW": (HANDLE, [LPVOID, LPWSTR]),
            "SetInformationJobObject": (BOOL, [HANDLE, C.c_int, LPVOID, DWORD]),
            "QueryInformationJobObject": (BOOL, [HANDLE, C.c_int, LPVOID, DWORD, C.POINTER(DWORD)]),
            "TerminateJobObject": (BOOL, [HANDLE, C.c_uint]),
            "CloseHandle": (BOOL, [HANDLE]),
            "CreatePipe": (BOOL, [C.POINTER(HANDLE), C.POINTER(HANDLE), C.POINTER(SECURITY_ATTRIBUTES), DWORD]),
            "SetHandleInformation": (BOOL, [HANDLE, DWORD, DWORD]),
            "CreateFileW": (HANDLE, [LPWSTR, DWORD, DWORD, C.POINTER(SECURITY_ATTRIBUTES), DWORD, DWORD, HANDLE]),
            "InitializeProcThreadAttributeList": (BOOL, [LPVOID, DWORD, DWORD, C.POINTER(SIZE_T)]),
            "UpdateProcThreadAttribute": (BOOL, [LPVOID, DWORD, SIZE_T, LPVOID, SIZE_T, LPVOID, C.POINTER(SIZE_T)]),
            "DeleteProcThreadAttributeList": (None, [LPVOID]),
            "CreateProcessW": (BOOL, [LPWSTR, LPWSTR, LPVOID, LPVOID, BOOL, DWORD, LPVOID, LPWSTR, C.POINTER(STARTUPINFOW), C.POINTER(PROCESS_INFORMATION)]),
            "WaitForSingleObject": (DWORD, [HANDLE, DWORD]),
            "GetExitCodeProcess": (BOOL, [HANDLE, C.POINTER(DWORD)]),
            "LockFileEx": (BOOL, [HANDLE, DWORD, DWORD, DWORD, DWORD, C.POINTER(OVERLAPPED)]),
            "UnlockFileEx": (BOOL, [HANDLE, DWORD, DWORD, DWORD, C.POINTER(OVERLAPPED)]),
        }
        for name, (restype, argtypes) in signatures.items():
            fn = getattr(kernel, name)
            fn.restype, fn.argtypes = restype, argtypes

    @staticmethod
    def _check(value, code):
        if not value:
            raise ProcessError(code)
        return value

    def create_job(self):
        handle = self.k.CreateJobObjectW(None, None)
        return self._check(handle, "job_create_failed")

    def configure_job(self, job):
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        self._check(self.k.SetInformationJobObject(job, 9, C.byref(info), C.sizeof(info)), "job_policy_failed")

    def close_handle(self, handle):
        self._check(self.k.CloseHandle(handle), "handle_close_failed")

    def pipe(self):
        read, write = HANDLE(), HANDLE()
        sa = SECURITY_ATTRIBUTES(C.sizeof(SECURITY_ATTRIBUTES), None, True)
        self._check(self.k.CreatePipe(C.byref(read), C.byref(write), C.byref(sa), 0), "pipe_create_failed")
        return read.value, write.value

    def noninheritable(self, handle):
        self._check(self.k.SetHandleInformation(handle, 1, 0), "handle_policy_failed")

    def null(self, writing):
        sa = SECURITY_ATTRIBUTES(C.sizeof(SECURITY_ATTRIBUTES), None, True)
        handle = self.k.CreateFileW("NUL", 0x40000000 if writing else 0x80000000, 3, C.byref(sa), 3, 0x80, None)
        if handle in (None, INVALID_HANDLE_VALUE):
            raise ProcessError("null_handle_failed")
        return handle

    @contextmanager
    def attributes(self, job, inherited):
        size = SIZE_T()
        self.k.InitializeProcThreadAttributeList(None, 2, 0, C.byref(size))
        if not 0 < size.value <= 65536:
            raise ProcessError("process_attributes_unavailable")
        memory = C.create_string_buffer(size.value)
        self._check(self.k.InitializeProcThreadAttributeList(memory, 2, 0, C.byref(size)), "process_attributes_failed")
        try:
            jobs = (HANDLE * 1)(job)
            handles = (HANDLE * len(inherited))(*inherited)
            self._check(self.k.UpdateProcThreadAttribute(memory, 0, PROC_THREAD_ATTRIBUTE_JOB_LIST,
                        C.cast(jobs, LPVOID), C.sizeof(jobs), None, None), "job_assignment_unavailable")
            self._check(self.k.UpdateProcThreadAttribute(memory, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                        C.cast(handles, LPVOID), C.sizeof(handles), None, None), "handle_list_failed")
            yield C.cast(memory, LPVOID)
        finally:
            self.k.DeleteProcThreadAttributeList(memory)

    def create(self, argv, cwd, env, job, stdin, stdout, stderr):
        info = STARTUPINFOEXW()
        info.StartupInfo.cb = C.sizeof(info)
        info.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        info.StartupInfo.hStdInput, info.StartupInfo.hStdOutput, info.StartupInfo.hStdError = stdin, stdout, stderr
        inherited = list(dict.fromkeys((stdin, stdout, stderr)))
        command = C.create_unicode_buffer(subprocess.list2cmdline(argv))
        environment = C.create_unicode_buffer("\0".join(k + "=" + v for k, v in sorted(env.items(), key=lambda kv: kv[0].casefold())) + "\0\0")
        process = PROCESS_INFORMATION()
        with self.attributes(job, inherited) as attributes:
            info.lpAttributeList = attributes
            self._check(self.k.CreateProcessW(argv[0], command, None, None, True,
                        EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW,
                        C.cast(environment, LPVOID), cwd, C.cast(C.byref(info), C.POINTER(STARTUPINFOW)),
                        C.byref(process)), "process_create_failed")
        return process.hProcess, process.hThread, process.dwProcessId

    def fdopen(self, handle, writing):
        import msvcrt
        flags = (os.O_WRONLY if writing else os.O_RDONLY) | os.O_BINARY
        try:
            fd = msvcrt.open_osfhandle(handle, flags)  # transfers native handle ownership
        except BaseException:
            self.close_handle(handle)
            raise
        try:
            os.set_inheritable(fd, False)
            return os.fdopen(fd, "wb" if writing else "rb", buffering=0)
        except BaseException:
            os.close(fd)
            raise

    def wait_handle(self, process, milliseconds):
        result = self.k.WaitForSingleObject(process, milliseconds)
        if result == WAIT_TIMEOUT:
            return False
        if result != WAIT_OBJECT_0:
            raise ProcessError("process_wait_failed")
        return True

    def exit_code(self, process):
        code = DWORD()
        self._check(self.k.GetExitCodeProcess(process, C.byref(code)), "process_exit_unknown")
        return code.value

    def terminate_job(self, job):
        self._check(self.k.TerminateJobObject(job, 1), "process_unknown")

    def active_processes(self, job):
        info = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        self._check(self.k.QueryInformationJobObject(job, 1, C.byref(info), C.sizeof(info), None), "process_unknown")
        return info.ActiveProcesses

    def lock_file(self, handle, overlapped):
        self._check(self.k.LockFileEx(handle, 3, 0, 1, 0, C.byref(overlapped)), "lock_busy")

    def unlock_file(self, handle, overlapped):
        self._check(self.k.UnlockFileEx(handle, 0, 1, 0, C.byref(overlapped)), "lock_release_failed")


class WindowsProcess:
    """Popen-compatible subset with owning Job/process handles and binary pipes."""
    def __init__(self, api, job, process, pid, streams, args):
        self._api, self._job, self._process = api, job, process
        self.pid, self.args, self.returncode = pid, args, None
        self.stdin, self.stdout, self.stderr = streams
        self._lock = threading.RLock()
        self._closed = False
        self._stop_verified = False

    def poll(self):
        with self._lock:
            if self.returncode is None:
                if self._process is None:
                    raise ProcessError("process_handle_closed")
                if self._api.wait_handle(self._process, 0):
                    self.returncode = self._api.exit_code(self._process)
            return self.returncode

    def wait(self, timeout=None):
        if timeout is not None and (not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0):
            raise ValueError("invalid timeout")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            value = self.poll()
            if value is not None:
                return value
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise subprocess.TimeoutExpired("owned-process", timeout)
            # Short waits preserve bounded cancellation latency under concurrent wait.
            ms = 50 if remaining is None else max(1, min(50, math.ceil(remaining * 1000)))
            with self._lock:
                if self._process is not None:
                    self._api.wait_handle(self._process, ms)

    def terminate_tree(self, timeout=5):
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("invalid termination timeout")
        deadline = time.monotonic() + timeout
        with self._lock:
            if self._stop_verified:
                return self.returncode
            if self._job is None:
                if self._closed and self._stop_verified:
                    return self.returncode
                raise ProcessError("process_unknown")
            self._api.terminate_job(self._job)
        try:
            self.wait(timeout=max(.001, deadline - time.monotonic()))
            while True:
                with self._lock:
                    active = self._api.active_processes(self._job)
                if active == 0:
                    self._stop_verified = True
                    return self.returncode
                if time.monotonic() >= deadline:
                    raise ProcessError("process_unknown")
                time.sleep(.01)
        except subprocess.TimeoutExpired:
            raise ProcessError("process_unknown") from None

    kill = terminate_tree
    terminate = terminate_tree

    def close(self):
        with self._lock:
            if self._closed:
                if not self._stop_verified:
                    raise ProcessError("process_unknown")
                return
        error = None
        try:
            self.terminate_tree()
        except (OSError, subprocess.TimeoutExpired) as exc:
            error = exc
        finally:
            with self._lock:
                for stream in (self.stdin, self.stdout, self.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
                for attribute in ("_process", "_job"):
                    handle = getattr(self, attribute)
                    if handle is not None:
                        try:
                            self._api.close_handle(handle)
                        except OSError as exc:
                            error = error or exc
                        setattr(self, attribute, None)
                self._closed = True
        if error:
            raise ProcessError("process_unknown") from None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _spawn_windows(argv, cwd, env, stdin, stdout, stderr, *, api=None):
    api = api or _WindowsAPI()
    owned, streams, job, process, thread = set(), [None, None, None], None, None, None
    def keep(handle):
        owned.add(handle)
        return handle
    try:
        job = keep(api.create_job())
        api.configure_job(job)
        children = []
        parents = []
        for index, mode in enumerate((stdin, stdout, stderr)):
            if index == 2 and mode == STDOUT:
                children.append(children[1]); parents.append(None)
            elif mode == DEVNULL:
                children.append(keep(api.null(index != 0))); parents.append(None)
            else:
                read, write = api.pipe(); keep(read); keep(write)
                parent, child = (write, read) if index == 0 else (read, write)
                api.noninheritable(parent)
                children.append(child); parents.append(parent)
        process, thread, pid = api.create(argv, cwd, env, job, *children)
        keep(process); keep(thread)
        # Child is already in the kill-on-close job before any instruction runs.
        api.close_handle(thread); owned.remove(thread); thread = None
        for child in set(children):
            api.close_handle(child); owned.remove(child)
        for index, handle in enumerate(parents):
            if handle is not None:
                # fdopen owns the handle even if constructing the file later fails.
                owned.remove(handle)
                streams[index] = api.fdopen(handle, index == 0)
        owned.remove(job); owned.remove(process)
        return WindowsProcess(api, job, process, pid, streams, argv)
    except BaseException:
        cleanup_unknown = False
        if process is not None and job is not None:
            try:
                api.terminate_job(job)
                deadline = time.monotonic() + 5
                if not api.wait_handle(process, 5000):
                    cleanup_unknown = True
                while api.active_processes(job):
                    if time.monotonic() >= deadline:
                        cleanup_unknown = True
                        break
                    time.sleep(.01)
            except OSError:
                cleanup_unknown = True
        for stream in streams:
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        # Closing job also kills a process created successfully before pipe/stream
        # setup failed. Never resume/launch an uncontained fallback.
        for handle in list(owned):
            try:
                api.close_handle(handle)
            except OSError:
                cleanup_unknown = True
        if cleanup_unknown:
            raise ProcessError("process_unknown") from None
        raise


def _posix_group_alive(group):
    if sys.platform.startswith("linux"):
        try:
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    value = (entry / "stat").read_text()
                    fields = value[value.rfind(")") + 2:].split()
                    if int(fields[2]) == group and fields[0] not in {"Z", "X"}:
                        return True
                except FileNotFoundError:
                    continue
                except (OSError, ValueError, IndexError):
                    raise ProcessError("process_unknown") from None
            return False
        except OSError:
            raise ProcessError("process_unknown") from None
    try:
        os.killpg(group, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        raise ProcessError("process_unknown") from None


class PosixProcess:
    def __init__(self, argv, cwd, env, stdin, stdout, stderr):
        self._process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=stdin, stdout=stdout,
                                         stderr=stderr, start_new_session=True, bufsize=0, close_fds=True)
        self._closed = False
        self._stop_verified = False

    def __getattr__(self, name):
        return getattr(self._process, name)

    def terminate_tree(self, timeout=5):
        if self._stop_verified:
            return self._process.returncode
        if self._closed:
            if self._stop_verified:
                return self._process.returncode
            raise ProcessError("process_unknown")
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("invalid termination timeout")
        deadline = time.monotonic() + timeout
        try:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self._process.wait(timeout=max(.001, deadline - time.monotonic()))
            while _posix_group_alive(self.pid):
                if time.monotonic() >= deadline:
                    raise ProcessError("process_unknown")
                time.sleep(.01)
            self._stop_verified = True
            return self._process.returncode
        except (OSError, subprocess.TimeoutExpired):
            raise ProcessError("process_unknown") from None

    kill = terminate_tree
    terminate = terminate_tree

    def close(self):
        if self._closed:
            if not self._stop_verified:
                raise ProcessError("process_unknown")
            return
        error = None
        try:
            self.terminate_tree()
        except (OSError, subprocess.TimeoutExpired) as exc:
            error = exc
        finally:
            for stream in (self.stdin, self.stdout, self.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError as exc:
                        error = error or exc
            self._closed = True
        if error:
            raise ProcessError("process_unknown") from None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def spawn(argv, *, cwd, env, stdin=PIPE, stdout=PIPE, stderr=DEVNULL):
    """Spawn an owned tree without a shell; binary streams are unbuffered.

    Windows requires a native .exe/.com image, not a .cmd/.bat shell shim. A
    provider adapter may choose a verified native interpreter plus fixed script.
    Unsupported JOB_LIST/nested-job policy fails closed with no unsafe fallback.
    """
    if stdin not in {PIPE, DEVNULL} or stdout not in {PIPE, DEVNULL} or stderr not in {PIPE, DEVNULL, STDOUT}:
        raise ProcessError("unsupported_stdio")
    windows = os.name == "nt"
    argv, cwd, env = _validate(argv, cwd, env, windows=windows)
    if windows:
        return _spawn_windows(argv, cwd, env, stdin, stdout, stderr)
    if os.name != "posix":
        raise ProcessError("platform_unsupported")
    return PosixProcess(argv, cwd, env, stdin, stdout, stderr)


def is_unsafe_link(path):
    """Reject internal-state aliases/reparse points, including Windows junctions.

    Registered roots may first be canonicalized by the registry. For *nested*
    state paths, the lexical absolute path must remain the actual path. Missing
    descendants are allowed only when their existing ancestors are canonical.
    Path equality supplies native Windows case-insensitive comparison.
    """
    path = Path(path)
    try:
        if path.is_symlink() or (getattr(path, "is_junction", lambda: False)()):
            return True
        try:
            if getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
                return True
        except FileNotFoundError:
            pass
        return path.resolve() != path.absolute()
    except (OSError, RuntimeError):
        return True


class AdvisoryLock:
    """Nonblocking owned lock; OS releases it on exit. Does not replace markers."""
    def __init__(self, path):
        self.path = Path(path)
        self.fd = None
        self._api = None
        self._overlapped = None

    def acquire(self):
        if self.fd is not None:
            return self
        if is_unsafe_link(self.path):
            raise ProcessError("unsafe_lock_path")
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ProcessError("unsafe_lock_path")
            os.set_inheritable(fd, False)
            if os.name == "nt":
                import msvcrt
                self._api = _WindowsAPI()
                self._overlapped = OVERLAPPED()
                self._api.lock_file(msvcrt.get_osfhandle(fd), self._overlapped)
            elif os.name == "posix":
                import fcntl
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise ProcessError("lock_busy" if exc.errno in {errno.EACCES, errno.EAGAIN} else "lock_failed") from None
            else:
                raise ProcessError("platform_unsupported")
            self.fd = fd
            return self
        except BaseException:
            os.close(fd)
            raise

    def close(self):
        if self.fd is None:
            return
        fd, self.fd = self.fd, None
        try:
            if self._api is not None:
                import msvcrt
                self._api.unlock_file(msvcrt.get_osfhandle(fd), self._overlapped)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.close()
