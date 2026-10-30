import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import collab
import runtime_activity
import task_discovery


class RuntimeActivityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.threads = []
        self.failures = []

    def tearDown(self):
        for thread in self.threads:
            thread.join(timeout=5)
        self.temp.cleanup()

    def observed(self, code="pass", thread_id="work", agent="alice", timeout=2):
        log_path = self.root / ".ai-collab/logs" / (thread_id + ".log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w") as stream:
            return runtime_activity.run_observed(
                [sys.executable, "-c", code], root=self.root, thread_id=thread_id,
                agent=agent, message_id=str(uuid.uuid4()), log=str(log_path.relative_to(self.root)),
                cwd=self.root, env=dict(os.environ), stdin=subprocess.DEVNULL,
                stdout=stream, stderr=subprocess.STDOUT, timeout=timeout,
                heartbeat_interval=0.04, ttl=0.3,
            )

    def background(self, code="import time; time.sleep(0.6)", **kwargs):
        def run():
            try:
                self.observed(code, **kwargs)
            except Exception as exc:
                self.failures.append(exc)
        thread = threading.Thread(target=run)
        self.threads.append(thread)
        thread.start()
        return thread

    def wait_active(self, count=1):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            report = runtime_activity.activity_snapshot([self.root])
            if len([r for r in report["runs"] if r["active"]]) >= count:
                return report
            time.sleep(0.01)
        self.fail("real observed process did not become active: " + str(report))

    def record(self, **changes):
        now = datetime.now(timezone.utc)
        record = {"schema_version": 1, "run_id": str(uuid.uuid4()), "thread_id": "work",
                  "agent": "alice", "identity": "stable", "message_id": str(uuid.uuid4()),
                  "observer": "watcher", "machine_token": runtime_activity.machine_token(),
                  "runner_pid": os.getpid(), "process_pid": os.getpid(),
                  "runner_start_token": runtime_activity.process_token(os.getpid()),
                  "process_start_token": runtime_activity.process_token(os.getpid()),
                  "started_at": now.isoformat(), "heartbeat_at": now.isoformat(),
                  "expires_at": (now + timedelta(seconds=2)).isoformat(), "finished_at": None,
                  "status": "running", "exit_code": None, "error": None,
                  "log": ".ai-collab/logs/test.log"}
        record.update(changes)
        runtime_activity._write(self.root, record)
        return record

    def test_actual_process_heartbeat_and_completion(self):
        task_discovery.bind_identity(self.root, "alice", "stable")
        thread = self.background("import time; print('started', flush=True); time.sleep(0.35)")
        report = self.wait_active()
        run = report["runs"][0]
        self.assertEqual(run["identity"], "stable")
        self.assertEqual(run["observer"], "watcher")
        self.assertIsNotNone(run["process_pid"])
        self.assertTrue(run["heartbeat_at"].endswith("+00:00"))
        self.assertEqual(report["identities"][0]["runs"], [run["run_id"]])
        first_heartbeat = run["heartbeat_at"]
        time.sleep(0.07)
        self.assertNotEqual(runtime_activity.activity_snapshot([self.root])["runs"][0]["heartbeat_at"], first_heartbeat)
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        finished = runtime_activity.activity_snapshot([self.root])["runs"][0]
        self.assertFalse(finished["active"])
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["exit_code"], 0)
        self.assertIsNotNone(finished["finished_at"])
        self.assertIn("started", (self.root / finished["log"]).read_text())
        self.assertEqual(self.failures, [])

    def test_concurrent_runs_same_identity_across_threads(self):
        task_discovery.bind_identity(self.root, "alice", "stable")
        task_discovery.bind_identity(self.root, "reviewer", "stable")
        first = self.background(thread_id="first")
        second = self.background(thread_id="second", agent="reviewer")
        report = self.wait_active(2)
        self.assertEqual(len(report["identities"]), 1)
        identity = report["identities"][0]
        self.assertTrue(identity["active"])
        self.assertEqual(len(identity["runs"]), 2)
        self.assertEqual({r["thread_id"] for r in report["runs"]}, {"first", "second"})
        self.assertEqual(len({r["run_id"] for r in report["runs"]}), 2)
        first.join()
        second.join()
        self.assertFalse(runtime_activity.activity_snapshot([self.root])["identities"][0]["active"])
        self.assertEqual(self.failures, [])

    def test_failure_and_timeout_are_terminal_and_child_reaped(self):
        completed = self.observed("raise SystemExit(7)")
        self.assertEqual(completed.returncode, 7)
        failed = runtime_activity.activity_snapshot([self.root])["runs"][0]
        self.assertEqual(failed["status"], "failed")
        self.assertFalse(failed["active"])
        with self.assertRaises(subprocess.TimeoutExpired):
            self.observed("import time; time.sleep(10)", thread_id="timeout", timeout=0.1)
        timed = next(r for r in runtime_activity.activity_snapshot([self.root])["runs"] if r["thread_id"] == "timeout")
        self.assertEqual(timed["status"], "timed-out")
        self.assertFalse(timed["active"])
        self.assertFalse(runtime_activity.process_alive(timed["process_pid"]))

    def test_launch_failure_has_no_running_state(self):
        log = self.root / "log"
        with log.open("w") as stream, self.assertRaises(FileNotFoundError):
            runtime_activity.run_observed([str(self.root / "missing-command")], root=self.root,
                thread_id="work", agent="alice", message_id=str(uuid.uuid4()), log="log", cwd=self.root,
                env=dict(os.environ), stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT, timeout=1)
        run = runtime_activity.activity_snapshot([self.root])["runs"][0]
        self.assertEqual(run["status"], "failed")
        self.assertIsNone(run["process_pid"])
        self.assertIsNone(run["started_at"])
        self.assertFalse(run["active"])

    def test_interrupted_runner_cancels_and_reaps_child(self):
        writer = runtime_activity._write
        def interrupt(root, record):
            if record["status"] == "running":
                raise KeyboardInterrupt()
            return writer(root, record)
        with mock.patch.object(runtime_activity, "_write", side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            self.observed("import time; time.sleep(10)")
        run = runtime_activity.activity_snapshot([self.root])["runs"][0]
        self.assertEqual(run["status"], "cancelled")
        self.assertFalse(runtime_activity.process_alive(run["process_pid"]))

    def test_expired_foreign_missing_or_reused_pid_is_inactive(self):
        record = self.record()
        path = self.root / ".ai-collab/runtime/runs" / (record["run_id"] + ".json")
        cases = [
            ({"expires_at": "2000-01-01T00:00:00+00:00"}, "expired"),
            ({"machine_token": "another-machine"}, "foreign-host"),
            ({"runner_pid": 999999999}, "runner-exited"),
            ({"process_pid": 999999999}, "process-exited"),
            ({"runner_start_token": "wrong-start"}, "runner-identity-changed"),
            ({"process_start_token": "wrong-start"}, "process-identity-changed"),
            ({"heartbeat_at": "2000-01-01T00:00:00+00:00", "started_at": "2000-01-01T00:00:00+00:00"}, "expired"),
        ]
        for changes, reason in cases:
            with self.subTest(reason=reason):
                collab.atomic_write_json(path, {**record, **changes})
                run = runtime_activity.activity_snapshot([self.root])["runs"][0]
                self.assertFalse(run["active"])
                self.assertEqual(run["inactive_reason"], reason)

    def test_machine_token_is_stable_when_mac_lookup_uses_random_fallback(self):
        with mock.patch.object(runtime_activity.uuid, "getnode", side_effect=[(1 << 40) | 11, (1 << 40) | 22]):
            self.assertEqual(runtime_activity.machine_token(), runtime_activity.machine_token())

    def test_same_identity_aggregates_only_supplied_projects(self):
        first = self.record()
        with tempfile.TemporaryDirectory() as other:
            original = self.root
            self.root = Path(other)
            second = self.record(thread_id="other-work")
            self.root = original
            report = runtime_activity.activity_snapshot([original, Path(other), original])
            self.assertEqual(len(report["runs"]), 2)
            self.assertEqual(len(report["identities"]), 1)
            self.assertEqual(set(report["identities"][0]["runs"]), {first["run_id"], second["run_id"]})
            self.assertEqual(len({r["project_id"] for r in report["runs"]}), 2)
            self.assertEqual(len(runtime_activity.activity_snapshot([original])["runs"]), 1)

    def test_unavailable_and_missing_records_never_infer_running(self):
        (self.root / ".ai-collab/threads/work").mkdir(parents=True)
        (self.root / ".ai-collab/threads/work/task.json").write_text('{"phase":"implementing"}')
        report = runtime_activity.activity_snapshot([self.root, self.root / "missing"])
        self.assertEqual(report["runs"], [])
        self.assertEqual(report["identities"], [])
        self.assertEqual(len(report["errors"]), 1)
        self.assertFalse((self.root / ".ai-collab/runtime").exists())

    def test_telemetry_write_failure_does_not_cancel_authorized_command(self):
        for exit_code in (0, 7):
            with self.subTest(exit_code=exit_code):
                marker = "finished-" + str(exit_code)
                code = "import time; from pathlib import Path; time.sleep(0.1); Path(%r).write_text('done'); raise SystemExit(%d)" % (marker, exit_code)
                with mock.patch.object(runtime_activity, "_write", side_effect=OSError("telemetry disk unavailable")):
                    result = self.observed(code)
                self.assertEqual(result.returncode, exit_code)
                self.assertEqual((self.root / marker).read_text(), "done")
        self.assertEqual(runtime_activity.activity_snapshot([self.root])["runs"], [])

    def test_failed_terminal_write_stops_display_via_process_liveness(self):
        writer = runtime_activity._write
        def fail_after_first(root, record):
            if list((root / ".ai-collab/runtime/runs").glob("*.json")):
                raise OSError("telemetry disk unavailable")
            writer(root, record)
        with mock.patch.object(runtime_activity, "_write", side_effect=fail_after_first):
            result = self.observed("import time; time.sleep(0.12)")
        self.assertEqual(result.returncode, 0)
        run = runtime_activity.activity_snapshot([self.root])["runs"][0]
        self.assertEqual(run["status"], "running")  # Last persisted record, not live truth.
        self.assertFalse(run["active"])
        self.assertEqual(run["inactive_reason"], "process-exited")

    def test_malformed_terminal_records_do_not_hide_valid_live_observation(self):
        thread = self.background()
        live = self.wait_active()["runs"][0]
        for field, value in (("started_at", ["bad"]), ("finished_at", {"bad": 1}), ("status", ["completed"]),
                             ("heartbeat_at", 3), ("expires_at", None)):
            self.record(**{"status": "completed", field: value})
        report = runtime_activity.activity_snapshot([self.root])
        self.assertEqual(len(report["errors"]), 5)
        self.assertEqual([r["run_id"] for r in report["runs"]], [live["run_id"]])
        self.assertTrue(report["runs"][0]["active"])
        thread.join()

    def test_corrupt_or_identity_mismatched_records_are_rejected(self):
        record = self.record()
        directory = self.root / ".ai-collab/runtime/runs"
        (directory / (record["run_id"] + ".json")).write_text('{')
        collab.atomic_write_json(directory / "wrong-name.json", record)
        report = runtime_activity.activity_snapshot([self.root])
        self.assertEqual(report["runs"], [])
        self.assertEqual(len(report["errors"]), 2)

    @unittest.skipIf(os.name == "nt", "symlink creation requires privileges on Windows")
    def test_runtime_symlinks_cannot_escape_project_or_start_command(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside)
            (self.root / ".ai-collab").mkdir()
            (self.root / ".ai-collab/runtime").symlink_to(target, target_is_directory=True)
            with mock.patch.object(runtime_activity.subprocess, "Popen") as popen:
                with self.assertRaises(collab.CollaborationError):
                    self.observed()
                popen.assert_not_called()
            self.assertEqual(runtime_activity.activity_snapshot([self.root])["runs"], [])
            self.assertEqual(list(target.iterdir()), [])

    @unittest.skipIf(os.name == "nt", "SIGKILL crash observation is POSIX-specific")
    def test_crashed_observer_stops_activity_before_lease_expiry(self):
        code = """
import os, subprocess, sys, uuid
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import runtime_activity
root = Path(sys.argv[2])
with (root / 'crash.log').open('w') as log:
    runtime_activity.run_observed([sys.executable, '-c', 'import time; time.sleep(15)'],
        root=root, thread_id='crash', agent='alice', message_id=str(uuid.uuid4()),
        log='crash.log', cwd=root, env=dict(os.environ), stdin=subprocess.DEVNULL,
        stdout=log, stderr=subprocess.STDOUT, timeout=20, heartbeat_interval=0.1, ttl=5)
"""
        runner = subprocess.Popen([sys.executable, "-c", code, str(SCRIPTS), str(self.root)])
        child_pid = None
        try:
            run = self.wait_active()["runs"][0]
            child_pid = run["process_pid"]
            runner.kill()
            runner.wait(timeout=3)
            current = runtime_activity.activity_snapshot([self.root])["runs"][0]
            self.assertFalse(current["active"])
            self.assertEqual(current["inactive_reason"], "runner-exited")
        finally:
            if runner.poll() is None:
                runner.kill()
                runner.wait()
            if child_pid and runtime_activity.process_alive(child_pid):
                os.kill(child_pid, signal.SIGTERM)
                deadline = time.monotonic() + 2
                while runtime_activity.process_alive(child_pid) and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(runtime_activity.process_alive(child_pid), "orphan test child did not exit")

    def test_existing_watcher_wake_is_observable_without_new_entrypoint(self):
        args = types.SimpleNamespace(timeout=2)
        watcher = collab.Watcher(self.root, {"agents": {"alice": {"command": [sys.executable, "-c", "import time; time.sleep(0.3)"]}}}, ["alice"], args)
        item = {"agent": "alice", "thread_id": "work", "decision": "NEW", "message": {
            "message_id": str(uuid.uuid4()), "document": "turn.md", "requested_action": "REVIEW", "round": 1}}
        def run():
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    watcher.wake(item)
            except Exception as exc:
                self.failures.append(exc)
        thread = threading.Thread(target=run)
        self.threads.append(thread)
        thread.start()
        report = self.wait_active()
        self.assertEqual(report["runs"][0]["message_id"], item["message"]["message_id"])
        thread.join(timeout=3)
        self.assertEqual(watcher.wakes, 1)
        self.assertEqual(self.failures, [])
        self.assertFalse(runtime_activity.activity_snapshot([self.root])["runs"][0]["active"])
        self.assertFalse((self.root / ".ai-collab/threads/work/runtime").exists())


if __name__ == "__main__":
    unittest.main()
