"""Process fixtures only. Linux WinAPI mocks do not claim native Windows QA.

Native Windows check (no AI):
    py -3 -m unittest discover -s tests -p test_auto_process.py -v
Expected Windows-only native cases run (not skipped), echo/quoting/timeout/tree
cleanup pass, and no provider is launched. Run with normal user rights on Windows
10+/Server2016+. Restrictive parent Job policy must fail closed, never break away.
"""
import ctypes as C
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import auto_process as ap


class FakeStream(io.BytesIO):
    def __init__(self, api, handle):
        super().__init__()
        self.api, self.handle = api, handle
    def close(self):
        if not self.closed:
            self.api.close_handle(self.handle)
        super().close()


class FakeWindowsAPI:
    def __init__(self, fail=None):
        self.handles = {}; self.next = 100; self.events = []
        self.fail = fail; self.running = False; self.active = 0; self.code = None
        self.parent_handles = set(); self.job = None; self.fd_count = 0
    def alloc(self, kind):
        self.next += 1; self.handles[self.next] = kind; return self.next
    def failure(self, stage):
        if self.fail == stage: raise ap.ProcessError("fixture_" + stage)
    def create_job(self):
        self.failure("job"); self.job = self.alloc("job"); self.events.append("create_job"); return self.job
    def configure_job(self, job):
        self.events.append("kill_on_close"); self.failure("policy")
    def pipe(self):
        self.failure("pipe"); return self.alloc("read"), self.alloc("write")
    def noninheritable(self, handle):
        self.parent_handles.add(handle); self.failure("inheritance")
    def null(self, writing):
        self.failure("null"); return self.alloc("null")
    def create(self, argv, cwd, env, job, stdin, stdout, stderr):
        self.events.append(("create_in_job", job, (stdin, stdout, stderr), argv))
        assert "kill_on_close" in self.events
        assert job not in (stdin, stdout, stderr)
        assert not self.parent_handles.intersection((stdin, stdout, stderr))
        self.failure("create")
        self.running = True; self.active = 2
        return self.alloc("process"), self.alloc("thread"), 1234
    def fdopen(self, handle, writing):
        self.fd_count += 1
        if self.fail == "fdopen" or self.fail == "fdopen" + str(self.fd_count):
            self.close_handle(handle)  # same ownership-on-failure contract as API
            raise ap.ProcessError("fixture_fdopen")
        return FakeStream(self, handle)
    def close_handle(self, handle):
        assert handle in self.handles, ("double close", handle)
        kind = self.handles.pop(handle)
        self.events.append(("close", kind))
        if kind == "job":
            self.running = False; self.active = 0
    def wait_handle(self, handle, ms):
        if not self.running: return True
        if ms: time.sleep(min(ms / 1000, .01))
        return False
    def exit_code(self, handle): return self.code if self.code is not None else 1
    def terminate_job(self, job):
        self.events.append("terminate_job")
        self.failure("terminate")
        self.running = False; self.active = 0; self.code = 1
    def active_processes(self, job):
        self.failure("query"); return self.active


class NativeFunction:
    def __init__(self, fn): self.fn = fn
    def __call__(self, *args): return self.fn(*args)


class RecordingKernel:
    """Runs ctypes boundary itself against shaped fake native functions."""
    def __init__(self, fail_attribute=None):
        self.attributes = {}; self.deleted = 0; self.calls = []
        self.fail_attribute = fail_attribute
        names = ("CreateJobObjectW SetInformationJobObject QueryInformationJobObject TerminateJobObject CloseHandle CreatePipe SetHandleInformation CreateFileW InitializeProcThreadAttributeList UpdateProcThreadAttribute DeleteProcThreadAttributeList CreateProcessW WaitForSingleObject GetExitCodeProcess LockFileEx UnlockFileEx").split()
        for name in names: setattr(self, name, NativeFunction(getattr(self, "fn_" + name, lambda *_: True)))
    def fn_InitializeProcThreadAttributeList(self, memory, count, flags, size):
        self.calls.append(("initialize", bool(memory), count))
        C.cast(size, C.POINTER(ap.SIZE_T)).contents.value = 128
        return bool(memory)
    def fn_UpdateProcThreadAttribute(self, memory, flags, key, values, size, *_):
        self.calls.append(("attribute", key))
        if key == self.fail_attribute: return False
        count = size // C.sizeof(ap.HANDLE)
        self.attributes[key] = list(C.cast(values, C.POINTER(ap.HANDLE * count)).contents)
        return True
    def fn_DeleteProcThreadAttributeList(self, memory): self.deleted += 1
    def fn_SetInformationJobObject(self, job, kind, pointer, size):
        info = C.cast(pointer, C.POINTER(ap.JOBOBJECT_EXTENDED_LIMIT_INFORMATION)).contents
        self.policy = (job, kind, info.BasicLimitInformation.LimitFlags, size)
        return True
    def fn_CreateProcessW(self, exe, command, process_sa, thread_sa, inherit, flags, environment, cwd, startup, process):
        info = C.cast(startup, C.POINTER(ap.STARTUPINFOEXW)).contents
        self.created = {"exe": exe, "command": command.value, "inherit": inherit, "flags": flags,
                        "cb": info.StartupInfo.cb, "startup_flags": info.StartupInfo.dwFlags,
                        "handles": [info.StartupInfo.hStdInput, info.StartupInfo.hStdOutput, info.StartupInfo.hStdError],
                        "process_sa": process_sa, "thread_sa": thread_sa, "cwd": cwd}
        assert ap.PROC_THREAD_ATTRIBUTE_JOB_LIST in self.attributes
        assert ap.PROC_THREAD_ATTRIBUTE_HANDLE_LIST in self.attributes
        out = C.cast(process, C.POINTER(ap.PROCESS_INFORMATION)).contents
        out.hProcess = 500; out.hThread = 501; out.dwProcessId = 55; out.dwThreadId = 56
        return True
    def fn_LockFileEx(self, handle, flags, reserved, low, high, overlapped):
        value = C.cast(overlapped, C.POINTER(ap.OVERLAPPED)).contents
        self.lock = (handle, flags, reserved, low, high, value.Offset, value.OffsetHigh, value.hEvent)
        return True


class WindowsMockTests(unittest.TestCase):
    def make(self, api=None, stderr=ap.PIPE):
        api = api or FakeWindowsAPI()
        process = ap._spawn_windows((r"C:\Tools\fixture.exe", "arg"), r"C:\Work", {"PATH": r"C:\Tools"}, ap.PIPE, ap.PIPE, stderr, api=api)
        return api, process

    def test_job_precedes_run_and_only_child_stdio_is_inherited(self):
        api, process = self.make()
        creation = next(e for e in api.events if isinstance(e, tuple) and e[0] == "create_in_job")
        self.assertEqual(api.job, creation[1])
        self.assertEqual(3, len(set(creation[2])))
        self.assertNotIn(api.job, creation[2])
        self.assertTrue(all(api.handles[h] not in {"thread", "null"} for h in api.handles))
        self.assertIsNone(process.poll())
        process.stdin.write(b"fixture input")
        process.close(); process.close()
        self.assertEqual({}, api.handles)
        self.assertEqual(0, api.active)
        self.assertEqual(1, process.returncode)

    def test_stderr_stdout_reuses_handle_without_double_close(self):
        api, process = self.make(stderr=ap.STDOUT)
        creation = next(e for e in api.events if isinstance(e, tuple) and e[0] == "create_in_job")
        self.assertEqual(creation[2][1], creation[2][2])
        self.assertIsNone(process.stderr)
        process.close()
        self.assertEqual({}, api.handles)

    def test_null_stream_cleanup(self):
        api = FakeWindowsAPI()
        process = ap._spawn_windows((r"C:\fixture.exe",), r"C:\Work", {}, ap.DEVNULL, ap.DEVNULL, ap.DEVNULL, api=api)
        self.assertEqual((None, None, None), (process.stdin, process.stdout, process.stderr))
        process.close(); self.assertEqual({}, api.handles)

    def test_each_creation_failure_cleans_all_owned_handles(self):
        for stage in ("job", "policy", "pipe", "inheritance", "null", "create", "fdopen", "fdopen2"):
            with self.subTest(stage=stage):
                api = FakeWindowsAPI(stage)
                with self.assertRaises(ap.ProcessError):
                    self.make(api, stderr=ap.DEVNULL)
                self.assertFalse(api.running)
                self.assertEqual({}, api.handles)
                if stage == "fdopen": self.assertIn("terminate_job", api.events)

    def test_third_stream_failure_closes_already_converted_streams(self):
        api = FakeWindowsAPI("fdopen3")
        with self.assertRaises(ap.ProcessError): self.make(api)
        self.assertEqual({}, api.handles); self.assertFalse(api.running)

    def test_actual_exit_259_is_not_confused_with_still_active(self):
        api, process = self.make(); api.running = False; api.code = 259
        self.assertEqual(259, process.poll())
        process.close()
        self.assertEqual(259, process.returncode)

    def test_wait_timeout_does_not_claim_process_stopped(self):
        api, process = self.make()
        try:
            with self.assertRaises(subprocess.TimeoutExpired): process.wait(timeout=.01)
            self.assertIsNone(process.returncode)
            self.assertEqual(2, api.active)
            process.terminate_tree(); self.assertEqual(1, process.wait(timeout=0))
        finally: process.close()

    def test_failed_termination_is_unknown_even_after_handle_cleanup(self):
        api, process = self.make(); api.fail = "terminate"
        with self.assertRaisesRegex(ap.ProcessError, "process_unknown"): process.close()
        self.assertEqual({}, api.handles)
        self.assertEqual(0, api.active)  # last job handle close still applies kill policy
        with self.assertRaisesRegex(ap.ProcessError, "process_unknown"): process.terminate_tree()
        with self.assertRaisesRegex(ap.ProcessError, "process_unknown"): process.close()

    def test_failed_job_query_is_not_stop_proof(self):
        api, process = self.make(); api.fail = "query"
        with self.assertRaises(ap.ProcessError): process.terminate_tree()
        with self.assertRaises(ap.ProcessError): process.close()
        self.assertEqual({}, api.handles)

    def test_concurrent_wait_and_close_no_double_close_or_orphan(self):
        api, process = self.make(); results = []
        thread = threading.Thread(target=lambda: results.append(process.wait(timeout=2)))
        thread.start(); process.close(); thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual([1], results)
        self.assertEqual({}, api.handles)

    def test_native_attribute_and_quoting_contract(self):
        kernel = RecordingKernel(); api = ap._WindowsAPI(kernel)
        api.configure_job(100)
        args = (r"C:\Program Files\fixture.exe", 'space and "quote"', "backslash\\", "文字")
        process, thread, pid = api.create(args, r"C:\Work", {"PATH": "fixture"}, 100, 10, 11, 12)
        self.assertEqual((500, 501, 55), (process, thread, pid))
        self.assertEqual([100], kernel.attributes[ap.PROC_THREAD_ATTRIBUTE_JOB_LIST])
        self.assertEqual([10, 11, 12], kernel.attributes[ap.PROC_THREAD_ATTRIBUTE_HANDLE_LIST])
        self.assertEqual(subprocess.list2cmdline(args), kernel.created["command"])
        self.assertEqual(args[0], kernel.created["exe"])
        self.assertEqual(C.sizeof(ap.STARTUPINFOEXW), kernel.created["cb"])
        self.assertEqual(ap.STARTF_USESTDHANDLES, kernel.created["startup_flags"])
        self.assertEqual(ap.EXTENDED_STARTUPINFO_PRESENT | ap.CREATE_UNICODE_ENVIRONMENT | ap.CREATE_NO_WINDOW, kernel.created["flags"])
        self.assertIsNone(kernel.created["process_sa"]); self.assertIsNone(kernel.created["thread_sa"])
        self.assertEqual(ap.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, kernel.policy[2])
        self.assertEqual(1, kernel.deleted)

    def test_attribute_failure_never_calls_create_and_deletes_attribute_list(self):
        for attribute in (ap.PROC_THREAD_ATTRIBUTE_JOB_LIST, ap.PROC_THREAD_ATTRIBUTE_HANDLE_LIST):
            kernel = RecordingKernel(attribute); api = ap._WindowsAPI(kernel)
            with self.assertRaises(ap.ProcessError):
                api.create((r"C:\fixture.exe",), r"C:\Work", {}, 100, 10, 11, 12)
            self.assertFalse(hasattr(kernel, "created")); self.assertEqual(1, kernel.deleted)

    def test_windows_structures_use_fixed_abi_widths(self):
        self.assertEqual(4, C.sizeof(ap.DWORD)); self.assertEqual(2, C.sizeof(ap.WORD))
        self.assertEqual(48, C.sizeof(ap.JOBOBJECT_BASIC_ACCOUNTING_INFORMATION))
        if C.sizeof(C.c_void_p) == 8:
            self.assertEqual(104, C.sizeof(ap.STARTUPINFOW)); self.assertEqual(112, C.sizeof(ap.STARTUPINFOEXW))
            self.assertEqual(144, C.sizeof(ap.JOBOBJECT_EXTENDED_LIMIT_INFORMATION)); self.assertEqual(32, C.sizeof(ap.OVERLAPPED))
        else:
            self.assertEqual(68, C.sizeof(ap.STARTUPINFOW)); self.assertEqual(72, C.sizeof(ap.STARTUPINFOEXW))
            self.assertEqual(20, C.sizeof(ap.OVERLAPPED))

    def test_windows_native_lock_is_nonblocking_exclusive_zero_offset(self):
        kernel = RecordingKernel(); api = ap._WindowsAPI(kernel)
        api.lock_file(77, ap.OVERLAPPED())
        self.assertEqual((77, 3, 0, 1, 0, 0, 0, None), kernel.lock)

    def test_reparse_and_junction_path_contract_on_linux(self):
        from types import SimpleNamespace
        fake = unittest.mock.Mock()
        fake.is_symlink.return_value = False
        fake.is_junction.return_value = False
        fake.lstat.return_value = SimpleNamespace(st_file_attributes=0x400)
        with patch.object(ap, "Path", return_value=fake):
            self.assertTrue(ap.is_unsafe_link("nested-state"))
            fake.lstat.return_value = SimpleNamespace(st_file_attributes=0)
            fake.is_junction.return_value = True
            self.assertTrue(ap.is_unsafe_link("nested-state"))
            fake.is_junction.return_value = False
            fake.resolve.return_value = "real-location"
            fake.absolute.return_value = "lexical-alias"
            self.assertTrue(ap.is_unsafe_link("nested-state"))
            fake.absolute.return_value = "real-location"
            self.assertFalse(ap.is_unsafe_link("nested-state"))

    def test_windows_validation_no_batch_shell_or_environment_ambiguity(self):
        for executable in ("fixture.exe", r"C:\fixture.cmd", r"C:\fixture.bat", r"C:\fixture.ps1"):
            with self.assertRaises(ap.ProcessError): ap._validate([executable], r"C:\Work", {}, windows=True)
        with self.assertRaisesRegex(ap.ProcessError, "ambiguous_environment"):
            ap._validate([r"C:\fixture.exe"], r"C:\Work", {"Path": "a", "PATH": "b"}, windows=True)
        with self.assertRaisesRegex(ap.ProcessError, "invalid_environment"):
            ap._validate([r"C:\fixture.exe"], r"C:\Work", {"BAD=KEY": "x"}, windows=True)
        with self.assertRaisesRegex(ap.ProcessError, "argv_limit"):
            ap._validate([r"C:\fixture.exe", "x" * 33000], r"C:\Work", {}, windows=True)


@unittest.skipUnless(os.name == "posix", "POSIX fixture")
class PosixProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
    def process(self, script, **kwargs):
        return ap.spawn([sys.executable, "-c", script], cwd=self.root, env=dict(os.environ), **kwargs)
    def test_binary_pipe_echo_and_exit(self):
        process = self.process("import sys;sys.stdout.buffer.write(sys.stdin.buffer.read());sys.stderr.write('err')", stderr=ap.PIPE)
        try:
            process.stdin.write(b"fixture\n"); process.stdin.close()
            self.assertEqual(b"fixture\n", process.stdout.read()); self.assertEqual(b"err", process.stderr.read())
            self.assertEqual(0, process.wait(timeout=3))
        finally: process.close()
        self.assertTrue(process.stdout.closed)
    def test_owned_group_kills_descendant(self):
        process = self.process("import subprocess,sys,time; c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);print(c.pid,flush=True);time.sleep(60)")
        child = int(process.stdout.readline())
        process.terminate_tree(timeout=3)
        self.assertFalse(ap._posix_group_alive(process.pid))
        process.close(); process.close()
    def test_wait_timeout_and_close_kills(self):
        process = self.process("import time;time.sleep(60)")
        with self.assertRaises(subprocess.TimeoutExpired): process.wait(timeout=.01)
        process.close(); self.assertIsNotNone(process.returncode)
    def test_advisory_lock_conflict_release_and_symlink(self):
        path = self.root / "lock"
        with ap.AdvisoryLock(path):
            with self.assertRaisesRegex(ap.ProcessError, "lock_busy"): ap.AdvisoryLock(path).acquire()
        with ap.AdvisoryLock(path): pass
        alias = self.root / "alias"; alias.symlink_to(path)
        with self.assertRaisesRegex(ap.ProcessError, "unsafe_lock_path"): ap.AdvisoryLock(alias).acquire()
    def test_verified_reaping_never_signals_recycled_process_group(self):
        process = self.process("pass")
        process.wait(timeout=3)
        process.terminate_tree()
        with patch.object(ap.os, "killpg", side_effect=AssertionError("must not signal recycled group")):
            process.terminate_tree(); process.close()

    def test_invalid_input_and_spawn_failure(self):
        with self.assertRaises(ap.ProcessError): ap.spawn("shell string", cwd=self.root, env={})
        with self.assertRaises(ap.ProcessError): ap.spawn([sys.executable], cwd=self.root, env={}, stdout=None)
        with self.assertRaises(FileNotFoundError): ap.spawn([str(self.root / "absent")], cwd=self.root, env={})


@unittest.skipUnless(os.name == "nt", "requires native Windows; Linux mocks are not native evidence")
class WindowsNativeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {k: v for k, v in os.environ.items() if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
    def test_native_job_pipes_quoting_and_cleanup(self):
        args = ['space and "quote"', "文字", "trailing\\"]
        process = ap.spawn([sys.executable, "-c", "import sys,json;print(json.dumps(sys.argv[1:]));print(sys.stdin.read())", *args], cwd=self.root, env=self.env)
        try:
            process.stdin.write(b"fixture"); process.stdin.close()
            self.assertEqual(args, json.loads(process.stdout.readline().decode()))
            self.assertEqual(b"fixture", process.stdout.read().strip())
            self.assertEqual(0, process.wait(timeout=5))
        finally: process.close()
    def test_native_job_descendant_termination_and_lock(self):
        process = ap.spawn([sys.executable, "-c", "import subprocess,sys,time;c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);print(c.pid,flush=True);time.sleep(60)"], cwd=self.root, env=self.env)
        try:
            child = int(process.stdout.readline()); self.assertGreater(child, 0)
            with self.assertRaises(subprocess.TimeoutExpired): process.wait(timeout=.01)
            process.terminate_tree(timeout=5)
            self.assertEqual(0, process._api.active_processes(process._job))
        finally: process.close()
        with ap.AdvisoryLock(self.root / "lock"):
            with self.assertRaises(ap.ProcessError): ap.AdvisoryLock(self.root / "lock").acquire()
        with ap.AdvisoryLock(self.root / "lock"): pass


if __name__ == "__main__":
    unittest.main()
