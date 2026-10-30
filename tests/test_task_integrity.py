"""Process-level persistence and fresh-snapshot invariants."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import collab
import task_model

SCRIPT = Path(collab.__file__).resolve()


class TaskIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        subprocess.run([sys.executable, str(SCRIPT), "init", "--root", str(self.root),
                        "--thread-id", "task", "--topic", "Recovery", "--objective", "Ticket recovery",
                        "--participants", "owner", "reviewer"], check=True, capture_output=True)
        task_model.initialize_task(self.root, "task", "owner", [{"id": "ac-1", "text": "Pass"}])

    def tearDown(self):
        self.temp.cleanup()

    def command(self, operation_id):
        return [sys.executable, str(SCRIPT), "task-update", "--root", str(self.root),
                "--thread-id", "task", "--agent", "owner", "--expected-revision", "1",
                "--operation-id", operation_id, "--operation-file", ".ai-collab/op.json"]

    def write_operation(self):
        operation = {"kind": "checkpoint", "context": task_model.capture_context(self.root, "task"),
                     "phase": "implementing", "owner": "owner", "artifact_refs": [],
                     "review_issue_ids": [], "next_action": "Run authorized tests", "waiting_on": None}
        (self.root / ".ai-collab/op.json").write_text(json.dumps(operation), encoding="utf-8")
        return operation

    def test_concurrent_revision_writers_do_not_lose_updates(self):
        self.write_operation()
        first = subprocess.Popen(self.command("first"), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        second = subprocess.Popen(self.command("second"), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        outputs = [first.communicate(timeout=15), second.communicate(timeout=15)]
        self.assertEqual(sorted([first.returncode, second.returncode]), [0, 2], outputs)
        task = task_model.load_task(self.root, "task")
        self.assertEqual(task["revision"], 2)
        self.assertEqual(len(task["operations"]), 1)

    def test_concurrent_identical_operation_is_applied_once(self):
        self.write_operation()
        first = subprocess.Popen(self.command("same"), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        second = subprocess.Popen(self.command("same"), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        outputs = [first.communicate(timeout=15), second.communicate(timeout=15)]
        self.assertEqual([first.returncode, second.returncode], [0, 0], outputs)
        self.assertEqual(task_model.load_task(self.root, "task")["revision"], 2)

    @unittest.skipIf(os.name == "nt", "POSIX process termination lock recovery")
    def test_crashed_process_releases_task_lock(self):
        code = (f"import sys,time;sys.path.insert(0,{str(SCRIPT.parent)!r});"
                f"import task_model;from pathlib import Path;"
                f"lock=task_model._locked(Path({str(self.root)!r}),'task');lock.__enter__();"
                "print('locked',flush=True);time.sleep(30)")
        process = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "locked")
        finally:
            process.terminate()
            process.communicate(timeout=5)
        self.write_operation()
        result = subprocess.run(self.command("after-restart"), capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(task_model.load_task(self.root, "task")["revision"], 2)

    @unittest.skipIf(os.name == "nt", "symlink creation requires Windows permission")
    def test_directory_symlink_fingerprint_is_bounded_and_tracks_link_target(self):
        (self.root / "source-a").mkdir()
        (self.root / "source-b").mkdir()
        link = self.root / "link"
        link.symlink_to("source-a", target_is_directory=True)
        before = task_model.code_version(self.root)
        link.unlink()
        link.symlink_to("source-b", target_is_directory=True)
        self.assertNotEqual(before, task_model.code_version(self.root))
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        link.unlink()
        link.symlink_to(".", target_is_directory=True)
        subprocess.run(["git", "-C", str(self.root), "add", "link"], check=True)
        self.assertIsInstance(task_model.code_version(self.root)["worktree_fingerprint"], str)

    def test_invalid_operation_kind_is_a_structured_error_without_mutation(self):
        operation = self.write_operation()
        operation["kind"] = []
        with self.assertRaises(collab.CollaborationError):
            task_model.apply_operation(self.root, "task", "owner", operation, 1, "bad")
        self.assertEqual(task_model.load_task(self.root, "task")["revision"], 1)


if __name__ == "__main__":
    unittest.main()
