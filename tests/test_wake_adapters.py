"""Real harmless fake CLIs exercised through the bundled shell entry points."""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import collab
import runtime_activity
import task_discovery


@unittest.skipIf(os.name == "nt", "bundled adapters are POSIX shell scripts")
class WakeAdapterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.processes = []
        self.child_pids = set()
        self.env = dict(os.environ)
        self.env.update(PATH=str(self.bin) + os.pathsep + self.env.get("PATH", ""),
                        COLLAB_ONCE="1", FAKE_ROOT=str(self.root),
                        PRIVATE_ENV_SECRET="private-environment-secret")
        self.cli("init", "--thread-id", "work", "--topic", "Synthetic adapter test",
                 "--objective", "Fixture only", "--participants", "lead", "worker")
        created = json.loads(self.cli("new-turn", "--thread-id", "work", "--author", "lead",
            "--role", "reviewer", "--kind", "REVIEW", "--status", "OPEN",
            "--requested-action", "IMPLEMENT", "--title", "Synthetic mail").stdout)
        document = self.root / created["document"]
        document.write_text(document.read_text().replace("TODO(agent-collaboration):", "Synthetic:"))
        self.message_id = json.loads(self.cli("publish", "--thread-id", "work", "--agent", "lead",
            "--to", "worker", "--document", created["document"]).stdout)["message_id"]
        task_discovery.bind_identity(self.root, "worker", "stable-worker")

    def tearDown(self):
        (self.root / "release").touch()
        for pid in self.child_pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for process in self.processes:
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
        self.temp.cleanup()

    def cli(self, *args):
        result = subprocess.run([sys.executable, str(SCRIPTS / "collab.py"), *args,
            "--root", str(self.root)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def fake(self, tool):
        path = self.bin / ("claude" if tool == "claude" else "agent")
        path.write_text(f"#!{sys.executable}\n" + '''
import json, os, sys, time
from pathlib import Path
root = Path(os.environ['FAKE_ROOT'])
(root / 'argv.json').write_text(json.dumps(sys.argv[1:]))
(root / 'cwd.txt').write_text(os.getcwd())
print('raw-output-private-secret', flush=True)
print('raw-error-private-secret', file=sys.stderr, flush=True)
start = time.monotonic()
while not (root / 'release').exists() and time.monotonic() - start < 15:
    time.sleep(0.02)
raise SystemExit(int(os.environ.get('FAKE_EXIT', '0')))
''')
        path.chmod(0o700)

    def launch(self, tool, **env):
        environment = {**self.env, **env}
        process = subprocess.Popen(["/bin/sh", str(SCRIPTS / "adapters" / (tool + "-wake.sh")),
            str(self.root), "worker"], env=environment, cwd=self.root, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.processes.append(process)
        return process

    def active(self):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            report = runtime_activity.activity_snapshot([self.root])
            active = [run for run in report["runs"] if run["active"]]
            if active:
                self.child_pids.update(run["process_pid"] for run in active)
                return active[0]
            time.sleep(0.01)
        self.fail("adapter did not become observable: " + str(report))

    def finish(self, process):
        (self.root / "release").touch()
        out, err = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, err)  # Existing shell polling ignores vendor exit.
        return out, err

    def assert_private(self):
        files = list((self.root / ".ai-collab/runtime/runs").glob("*.json"))
        self.assertTrue(files)
        for path in files:
            raw = path.read_text()
            for secret in ("private-environment-secret", "raw-output-private-secret", "raw-error-private-secret",
                           "private-session-id", "private-chat-id", "Synthetic mail", "--allowedTools", "--trust"):
                self.assertNotIn(secret, raw)
        self.assertFalse((self.root / ".ai-collab/logs").exists())

    def test_both_wrappers_observe_real_lifecycle_and_preserve_resume_arguments(self):
        for tool, setting, session in (("claude", "CLAUDE_SESSION_ID", "private-session-id"),
                                       ("cursor", "CURSOR_CHAT_ID", "private-chat-id")):
            with self.subTest(tool=tool):
                (self.root / "release").unlink(missing_ok=True)
                self.fake(tool)
                process = self.launch(tool, **{setting: session})
                run = self.active()
                self.assertEqual(run["observer"], "wake-adapter")
                self.assertEqual(run["identity"], "stable-worker")
                self.assertEqual(run["message_id"], self.message_id)
                self.assertIsNone(run["log"])
                self.assertTrue(runtime_activity.process_alive(run["process_pid"]))
                out, err = self.finish(process)
                argv = json.loads((self.root / "argv.json").read_text())
                self.assertIn("--resume", argv)
                self.assertEqual(argv[argv.index("--resume") + 1], session)
                prompt = argv[1] if tool == "claude" else argv[-1]
                self.assertIn(self.message_id, prompt)
                self.assertIn("Do not run git commit or git push", prompt)
                if tool == "claude":
                    self.assertEqual(argv[-4:], ["--permission-mode", "acceptEdits", "--allowedTools", "Bash,Read,Edit,Write,Glob,Grep"])
                else:
                    self.assertEqual(argv[-3:-1], ["--force", "--trust"])
                self.assertEqual((self.root / "cwd.txt").read_text(), str(self.root))
                self.assertIn("raw-output-private-secret", out)
                self.assertIn("raw-error-private-secret", err)
                finished = next(r for r in runtime_activity.activity_snapshot([self.root])["runs"] if r["run_id"] == run["run_id"])
                self.assertEqual(finished["status"], "completed")
                self.assertFalse(finished["active"])
                self.assertEqual(finished["exit_code"], 0)
                self.assert_private()

    def test_vendor_failure_is_terminal_not_a_busy_task_phase(self):
        self.fake("claude")
        process = self.launch("claude", FAKE_EXIT="7")
        self.active()
        self.finish(process)
        run = runtime_activity.activity_snapshot([self.root])["runs"][0]
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["exit_code"], 7)
        self.assertFalse(run["active"])
        self.assertEqual(collab.inbox_report(self.root, "worker")["items"][0]["decision"], "NEW")
        self.assert_private()

    def test_missing_vendor_binary_has_no_running_observation_or_secret_error(self):
        # Restricted fake PATH guarantees no real vendor CLI can be invoked.
        (self.bin / "python3").symlink_to(sys.executable)
        (self.bin / "dirname").symlink_to(shutil.which("dirname"))
        process = self.launch("cursor", PATH=str(self.bin))
        out, err = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(out, "")
        self.assertIn("FileNotFoundError", err)
        run = runtime_activity.activity_snapshot([self.root])["runs"][0]
        self.assertEqual(run["status"], "failed")
        self.assertIsNone(run["process_pid"])
        self.assertFalse(run["active"])
        self.assertEqual(run["error"], "FileNotFoundError")
        self.assert_private()

    def test_invalid_notification_has_no_fabricated_message_id(self):
        signal_path = self.root / ".ai-collab/threads/work/signals/to-worker.json"
        signal_path.write_text('{"invalid":true}')
        self.fake("cursor")
        process = self.launch("cursor")
        run = self.active()
        self.assertIsNone(run["message_id"])
        self.finish(process)
        self.assertIn("INVALID", json.loads((self.root / "argv.json").read_text())[-1])
        self.assert_private()

    def test_idle_and_stop_never_launch_or_manufacture_activity(self):
        self.fake("claude")
        self.cli("acknowledge", "--thread-id", "work", "--agent", "worker", "--message-id", self.message_id)
        process = self.launch("claude")
        process.communicate(timeout=5)
        self.assertFalse((self.root / "argv.json").exists())
        self.assertEqual(runtime_activity.activity_snapshot([self.root])["runs"], [])
        (self.root / ".ai-collab/STOP").touch()
        process = self.launch("cursor")
        out, _ = process.communicate(timeout=5)
        self.assertEqual(out.strip(), "stopped")
        self.assertFalse((self.root / "argv.json").exists())
        self.assertEqual(runtime_activity.activity_snapshot([self.root])["runs"], [])

    def test_telemetry_storage_failure_preserves_vendor_invocation_without_busy_state(self):
        runtime = self.root / ".ai-collab/runtime"
        runtime.mkdir()
        (runtime / "runs").write_text("synthetic unavailable directory")
        self.fake("claude")
        process = self.launch("claude")
        end = time.monotonic() + 5
        while not (self.root / "argv.json").exists() and time.monotonic() < end:
            time.sleep(0.01)
        self.assertTrue((self.root / "argv.json").exists())
        self.assertEqual(runtime_activity.activity_snapshot([self.root])["runs"], [])
        self.finish(process)
        self.assertEqual(runtime_activity.activity_snapshot([self.root])["runs"], [])

    def test_observer_crash_and_expired_wrapper_lease_are_inactive(self):
        self.fake("cursor")
        process = self.launch("cursor")
        run = self.active()
        path = self.root / ".ai-collab/runtime/runs" / (run["run_id"] + ".json")
        # A wrapper-derived real observation must also obey fail-closed expiry.
        persisted = json.loads(path.read_text())
        persisted["expires_at"] = "2000-01-01T00:00:00+00:00"
        collab.atomic_write_json(path, persisted)
        expired = runtime_activity.activity_snapshot([self.root])["runs"][0]
        self.assertFalse(expired["active"])
        self.assertEqual(expired["inactive_reason"], "expired")
        persisted["expires_at"] = run["expires_at"]
        collab.atomic_write_json(path, persisted)
        os.kill(run["runner_pid"], signal.SIGKILL)
        end = time.monotonic() + 3
        while runtime_activity.process_alive(run["runner_pid"]) and time.monotonic() < end:
            time.sleep(0.01)
        crashed = runtime_activity.activity_snapshot([self.root])["runs"][0]
        self.assertFalse(crashed["active"])
        self.assertEqual(crashed["inactive_reason"], "runner-exited")
        self.finish(process)

    def test_exception_message_never_copies_command_or_secrets_into_telemetry(self):
        with mock.patch.object(runtime_activity.subprocess, "Popen", side_effect=OSError("raw-output-private-secret")):
            with self.assertRaises(OSError):
                runtime_activity.run_observed(["secret-command"], root=self.root,
                    thread_id="work", agent="worker", message_id=self.message_id, log=None,
                    cwd=self.root, env=self.env, stdin=None, stdout=None, stderr=None,
                    timeout=None, observer="wake-adapter")
        run = runtime_activity.activity_snapshot([self.root])["runs"][0]
        self.assertEqual(run["error"], "OSError")
        self.assert_private()


if __name__ == "__main__":
    unittest.main()
