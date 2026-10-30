"""Regression coverage for the optional coding-task acceptance/recovery profile."""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "collab.py"
sys.path.insert(0, str(SCRIPT.parent))
import collab  # noqa: E402
import task_model  # noqa: E402


class CodingTaskTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.git("init", "-q")
        (self.root / ".gitignore").write_text(".ai-collab/\nignored.txt\n", encoding="utf-8")
        (self.root / "product.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "fixture")
        self.run_cli("init", "--root", str(self.root), "--thread-id", "feature",
                     "--topic", "Feature", "--objective", "Ticket feature: acceptance",
                     "--participants", "builder", "lead", "peer")
        self.task_path = self.root / ".ai-collab/threads/feature/task.json"
        self.criteria = [{"id": "ac-1", "text": "The regression suite passes"}]

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *arguments):
        completed = subprocess.run(["git", *arguments], cwd=self.root, text=True,
                                   capture_output=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout.strip()

    def run_cli(self, *arguments, expected=0):
        completed = subprocess.run([sys.executable, str(SCRIPT), *arguments],
                                   cwd=self.root, text=True, capture_output=True, check=False)
        self.assertEqual(completed.returncode, expected,
                         f"stdout={completed.stdout}\nstderr={completed.stderr}")
        return json.loads(completed.stdout if expected == 0 else completed.stderr)

    def initialize(self):
        return task_model.initialize_task(self.root, "feature", "builder", self.criteria)

    def context(self):
        return task_model.capture_context(self.root, "feature")

    def operation(self, kind, agent="builder", **values):
        operation = {"kind": kind, "context": self.context(), **values}
        current = task_model.load_task(self.root, "feature")
        return task_model.apply_operation(self.root, "feature", agent, operation,
                                          current["revision"], str(uuid.uuid4()))

    def evidence(self, identity="evidence-1", **overrides):
        log = self.root / ".ai-collab/logs" / f"{identity}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("Ran the fixture check: OK\n", encoding="utf-8")
        values = {
            "id": identity,
            "criterion_ids": ["ac-1"],
            "code_version": self.context()["code_version"],
            "command": ["python", "-m", "unittest", "discover", "-s", "tests"],
            "cwd": ".",
            "exit_code": 0,
            "log": log.relative_to(self.root).as_posix(),
            "blocked_items": [],
        }
        values.update(overrides)
        return self.operation("evidence", **values)

    def review(self, review_kind, identity=None, agent="lead", **overrides):
        values = {
            "id": identity or f"{review_kind}-1",
            "review_kind": review_kind,
            "verdict": "pass",
            "code_version": self.context()["code_version"],
            "evidence_ids": ["evidence-1"],
            "findings": [],
            "summary": "Current implementation and evidence meet this review's scope.",
        }
        values.update(overrides)
        return self.operation("review", agent=agent, **values)

    def prepare_ready(self):
        self.initialize()
        self.evidence()
        self.review("spec")
        self.review("quality")
        self.assertTrue(task_model.task_view(self.root, "feature")["acceptance"]["ready"])

    def checkpoint(self, **overrides):
        values = {
            "phase": "implementing", "owner": "builder",
            "artifact_refs": ["product.py"], "review_issue_ids": ["P1-1"],
            "next_action": "Read the latest messages, then finish the regression check.",
            "waiting_on": None,
        }
        values.update(overrides)
        return self.operation("checkpoint", **values)

    def assert_not_ready(self):
        gate = task_model.task_view(self.root, "feature")["acceptance"]
        self.assertFalse(gate["ready"])
        self.assertTrue(gate["blockers"])
        for blocker in gate["blockers"]:
            self.assertTrue(blocker["code"])
            self.assertTrue(blocker["message"])

    def test_legacy_thread_has_no_profile_and_read_creates_no_files(self):
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))
        self.assertIsNone(task_model.load_task(self.root, "feature"))
        self.assertIsNone(task_model.task_view(self.root, "feature"))
        after = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))
        self.assertEqual(before, after)

    def test_initialization_requires_criteria_unique_ids_and_known_owner(self):
        for owner, criteria in [("outsider", self.criteria), ("builder", []),
                                ("builder", self.criteria + self.criteria)]:
            with self.subTest(owner=owner, criteria=criteria):
                with self.assertRaises(collab.CollaborationError):
                    task_model.initialize_task(self.root, "feature", owner, criteria)
                self.assertFalse(self.task_path.exists())
        task = self.initialize()
        self.assertEqual(task["schema_version"], 1)
        self.assertEqual(task["revision"], 1)
        self.assertEqual(task["phase"], "planned")
        self.assertEqual(task["owner"], "builder")
        self.assertEqual(task["criteria"], self.criteria)
        with self.assertRaises(collab.CollaborationError):
            self.initialize()

    def test_missing_evidence_and_reviews_cannot_be_accepted(self):
        self.initialize()
        before = self.task_path.read_bytes()
        self.assert_not_ready()
        with self.assertRaises(collab.CollaborationError):
            self.operation("accept", agent="lead")
        self.assertEqual(before, self.task_path.read_bytes())

    def test_full_current_evidence_and_two_reviews_allow_acceptance(self):
        self.prepare_ready()
        self.operation("accept", agent="lead")
        view = task_model.task_view(self.root, "feature")
        self.assertEqual(view["phase"], "accepted")
        self.assertTrue(view["acceptance"]["ready"])

    def test_failed_or_blocked_check_never_satisfies_criterion(self):
        for exit_code, blocked in [(1, []), (None, ["Dependency unavailable"]),
                                   (0, ["Browser verification not run"])]:
            with self.subTest(exit_code=exit_code, blocked=blocked):
                if self.task_path.exists():
                    self.task_path.unlink()
                self.initialize()
                self.evidence(exit_code=exit_code, blocked_items=blocked)
                self.review("spec")
                self.review("quality")
                self.assert_not_ready()
                with self.assertRaises(collab.CollaborationError):
                    self.operation("accept", agent="lead")

    def test_every_criterion_needs_evidence(self):
        self.criteria.append({"id": "ac-2", "text": "Migration verified"})
        self.initialize()
        self.evidence()
        self.review("spec")
        self.review("quality")
        self.assert_not_ready()

    def test_both_spec_and_quality_review_are_required(self):
        self.initialize()
        self.evidence()
        self.review("spec")
        self.assert_not_ready()
        self.review("quality")
        self.assertTrue(task_model.task_view(self.root, "feature")["acceptance"]["ready"])

    def test_owner_cannot_supply_own_acceptance_review(self):
        self.initialize()
        self.evidence()
        with self.assertRaises(collab.CollaborationError):
            self.review("spec", agent="builder")
        self.assert_not_ready()

    def test_latest_changes_requested_review_blocks_earlier_pass(self):
        self.prepare_ready()
        self.review("quality", identity="quality-2", verdict="changes-requested")
        self.assert_not_ready()

    def test_open_blocking_findings_block_even_with_pass_verdict(self):
        self.initialize()
        self.evidence()
        self.review("spec")
        self.review("quality", findings=[{
            "id": "P1-1", "blocking": True, "status": "open",
            "text": "The rollback path is not verified.",
        }])
        self.assert_not_ready()

    def test_nonblocking_findings_do_not_block(self):
        self.initialize()
        self.evidence()
        self.review("spec")
        self.review("quality", findings=[{
            "id": "P2-1", "blocking": False, "status": "open", "text": "Optional follow-up.",
        }])
        self.assertTrue(task_model.task_view(self.root, "feature")["acceptance"]["ready"])

    def test_later_review_cannot_silently_drop_blocking_finding(self):
        self.initialize()
        self.evidence()
        self.review("spec")
        finding = {"id": "P1-1", "blocking": True, "status": "open", "text": "Fix rollback."}
        self.review("quality", findings=[finding])
        self.review("quality", identity="quality-2", findings=[])
        self.assert_not_ready()
        self.review("quality", identity="quality-3", findings=[{**finding, "status": "resolved"}])
        self.assertTrue(task_model.task_view(self.root, "feature")["acceptance"]["ready"])

    def test_checkpoint_waiting_on_human_or_peer_blocks_acceptance(self):
        self.prepare_ready()
        for kind in ("human", "peer"):
            with self.subTest(kind=kind):
                self.checkpoint(phase="reviewing", waiting_on={"kind": kind, "identity": "lead"})
                self.assert_not_ready()
                with self.assertRaises(collab.CollaborationError):
                    self.operation("accept", agent="lead")
        self.checkpoint(phase="reviewing", waiting_on=None)
        self.assertTrue(task_model.task_view(self.root, "feature")["acceptance"]["ready"])

    def test_blocked_checkpoint_blocks_acceptance(self):
        self.prepare_ready()
        self.checkpoint(phase="blocked")
        self.assert_not_ready()

    def test_blocked_checkpoint_is_not_cleared_by_evidence_or_review(self):
        self.initialize()
        self.checkpoint(phase="blocked", waiting_on=None)
        self.evidence()
        self.review("spec")
        self.review("quality")
        self.assertEqual(task_model.load_task(self.root, "feature")["phase"], "blocked")
        self.assertEqual(task_model.load_task(self.root, "feature")["checkpoint"]["phase"], "blocked")
        self.assert_not_ready()

    def test_former_implementation_owner_cannot_self_review_after_handoff(self):
        self.initialize()
        self.evidence()
        self.checkpoint(owner="peer", phase="reviewing", review_issue_ids=[])
        task = task_model.load_task(self.root, "feature")
        self.assertEqual(task["owner"], "peer")
        self.assertIn("builder", task["implementation_owners"])
        self.assertIn("peer", task["implementation_owners"])
        with self.assertRaises(collab.CollaborationError):
            self.review("spec", agent="builder")
        self.assert_not_ready()
        self.review("spec", agent="lead")
        self.review("quality", agent="lead")
        self.assertTrue(task_model.task_view(self.root, "feature")["acceptance"]["ready"])

    def test_stable_identity_alias_cannot_self_review(self):
        import task_discovery
        self.initialize()
        task_discovery.bind_identity(self.root, "builder", "alex")
        task_discovery.bind_identity(self.root, "lead", "alex")
        self.evidence()
        with self.assertRaises(collab.CollaborationError):
            self.review("spec", agent="lead")
        self.assert_not_ready()

    def test_identity_rebinding_invalidates_previously_independent_review(self):
        import task_discovery
        self.prepare_ready()
        task_discovery.bind_identity(self.root, "builder", "alex")
        task_discovery.bind_identity(self.root, "lead", "alex")
        self.assert_not_ready()
        with self.assertRaises(collab.CollaborationError):
            self.operation("accept", agent="peer")

    def test_persisted_evidence_missing_command_fails_closed(self):
        self.prepare_ready()
        task = json.loads(self.task_path.read_text(encoding="utf-8"))
        del task["evidence"][0]["command"]
        self.task_path.write_text(json.dumps(task), encoding="utf-8")
        with self.assertRaises(collab.CollaborationError):
            task_model.task_view(self.root, "feature")
        with self.assertRaises(collab.CollaborationError):
            self.operation("accept", agent="lead")

    def test_unrelated_pass_cannot_hide_blocked_check_for_same_criterion(self):
        self.initialize()
        self.evidence(command=["python", "integration.py"], exit_code=None,
                      blocked_items=["Integration dependency unavailable"])
        self.evidence("evidence-2", command=["python", "unit.py"])
        self.review("spec", evidence_ids=["evidence-1", "evidence-2"])
        self.review("quality", evidence_ids=["evidence-1", "evidence-2"])
        self.assert_not_ready()
        blockers = task_model.task_view(self.root, "feature")["acceptance"]["blockers"]
        self.assertTrue(any(item.get("evidence_id") == "evidence-1" for item in blockers))
        self.evidence("evidence-3", command=["python", "integration.py"])
        self.review("spec", identity="spec-2", evidence_ids=["evidence-2", "evidence-3"])
        self.review("quality", identity="quality-2", evidence_ids=["evidence-2", "evidence-3"])
        self.assertTrue(task_model.task_view(self.root, "feature")["acceptance"]["ready"])

    def test_failed_evidence_can_be_superseded_by_reverified_current_evidence(self):
        self.initialize()
        self.evidence(exit_code=1)
        self.evidence("evidence-2")
        self.review("spec", evidence_ids=["evidence-2"])
        self.review("quality", evidence_ids=["evidence-2"])
        self.assertTrue(task_model.task_view(self.root, "feature")["acceptance"]["ready"])

    def test_changed_tracked_bytes_make_evidence_and_reviews_stale(self):
        self.prepare_ready()
        before = self.context()["code_version"]
        (self.root / "product.py").write_text("VALUE = 2\n", encoding="utf-8")
        self.assertNotEqual(before, self.context()["code_version"])
        self.assert_not_ready()
        with self.assertRaises(collab.CollaborationError):
            self.operation("accept", agent="lead")

    def test_new_head_invalidates_evidence_even_when_tree_unchanged(self):
        self.prepare_ready()
        before = self.context()["code_version"]
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "--allow-empty", "-qm", "new head")
        after = self.context()["code_version"]
        self.assertNotEqual(before["head_sha"], after["head_sha"])
        self.assert_not_ready()

    def test_untracked_source_counts_but_ignored_and_protocol_files_do_not(self):
        self.initialize()
        initial = self.context()["code_version"]
        (self.root / "ignored.txt").write_text("ignored\n", encoding="utf-8")
        (self.root / ".ai-collab/notes.txt").write_text("protocol\n", encoding="utf-8")
        self.assertEqual(initial, self.context()["code_version"])
        (self.root / "new_source.py").write_text("pass\n", encoding="utf-8")
        self.assertNotEqual(initial, self.context()["code_version"])

    def test_changed_log_invalidates_evidence_without_changing_code(self):
        self.prepare_ready()
        before = self.context()["code_version"]
        (self.root / ".ai-collab/logs/evidence-1.log").write_text("altered\n", encoding="utf-8")
        self.assertEqual(before, self.context()["code_version"])
        self.assert_not_ready()

    def test_deleted_log_invalidates_evidence(self):
        self.prepare_ready()
        (self.root / ".ai-collab/logs/evidence-1.log").unlink()
        self.assert_not_ready()

    def test_review_must_reference_existing_evidence(self):
        self.initialize()
        with self.assertRaises(collab.CollaborationError):
            self.review("spec", evidence_ids=["missing"])

    def test_new_evidence_requires_new_reviews(self):
        self.prepare_ready()
        self.evidence("evidence-2")
        self.assert_not_ready()

    def test_stale_context_rejected_before_mutating_state(self):
        self.initialize()
        stale = self.context()
        before = self.task_path.read_bytes()
        (self.root / "product.py").write_text("VALUE = 3\n", encoding="utf-8")
        with self.assertRaises(collab.CollaborationError):
            task_model.apply_operation(self.root, "feature", "builder", {
                "kind": "cancel", "context": stale,
            }, 1, str(uuid.uuid4()))
        self.assertEqual(before, self.task_path.read_bytes())

    def test_stale_revision_rejected_and_reused_id_with_changed_content_rejected(self):
        self.initialize()
        operation = {"kind": "checkpoint", "context": self.context(),
                     "phase": "implementing", "owner": "builder", "artifact_refs": [],
                     "review_issue_ids": [], "next_action": "Run tests", "waiting_on": None}
        identity = str(uuid.uuid4())
        result = task_model.apply_operation(self.root, "feature", "builder", operation, 1, identity)
        unchanged = self.task_path.read_bytes()
        duplicate = task_model.apply_operation(self.root, "feature", "builder", operation, 1, identity)
        self.assertEqual(result, duplicate)
        self.assertEqual(unchanged, self.task_path.read_bytes())
        changed = copy.deepcopy(operation)
        changed["next_action"] = "Do something else"
        with self.assertRaises(collab.CollaborationError):
            task_model.apply_operation(self.root, "feature", "builder", changed, 1, identity)
        with self.assertRaises(collab.CollaborationError):
            task_model.apply_operation(self.root, "feature", "builder", operation, 1, str(uuid.uuid4()))
        self.assertEqual(unchanged, self.task_path.read_bytes())

    def test_checkpoint_survives_process_restart(self):
        self.initialize()
        self.checkpoint(waiting_on={"kind": "peer", "identity": "lead"})
        code = ("import json,sys; from pathlib import Path; "
                f"sys.path.insert(0,{str(SCRIPT.parent)!r}); import task_model; "
                f"print(json.dumps(task_model.task_view(Path({str(self.root)!r}),'feature')))" )
        completed = subprocess.run([sys.executable, "-c", code], text=True, capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        restored = json.loads(completed.stdout)
        self.assertEqual(restored["checkpoint"]["owner"], "builder")
        self.assertEqual(restored["checkpoint"]["artifact_refs"], ["product.py"])
        self.assertEqual(restored["checkpoint"]["review_issue_ids"], ["P1-1"])
        self.assertEqual(restored["checkpoint"]["waiting_on"]["kind"], "peer")
        self.assertFalse(restored["recovery"]["needs_recheck"])

    def test_code_change_after_checkpoint_requires_recheck(self):
        self.initialize()
        self.checkpoint()
        (self.root / "product.py").write_text("VALUE = 4\n", encoding="utf-8")
        recovery = task_model.task_view(self.root, "feature")["recovery"]
        self.assertTrue(recovery["needs_recheck"])
        self.assertTrue(recovery["reasons"])

    def test_message_change_after_checkpoint_requires_recheck(self):
        self.initialize()
        self.checkpoint()
        stale = self.context()
        created = self.run_cli("new-turn", "--root", str(self.root), "--thread-id", "feature",
                               "--author", "lead", "--role", "reviewer", "--kind", "REVIEW",
                               "--status", "OPEN", "--requested-action", "REVISE",
                               "--title", "New information")
        path = self.root / created["document"]
        path.write_text(path.read_text(encoding="utf-8").replace(collab.PLACEHOLDER, "Complete:"),
                        encoding="utf-8")
        self.run_cli("publish", "--root", str(self.root), "--thread-id", "feature",
                     "--agent", "lead", "--to", "builder", "--document", created["document"])
        self.assertNotEqual(stale["messages_fingerprint"], self.context()["messages_fingerprint"])
        self.assertTrue(task_model.task_view(self.root, "feature")["recovery"]["needs_recheck"])
        with self.assertRaises(collab.CollaborationError):
            task_model.apply_operation(self.root, "feature", "builder", {
                "kind": "cancel", "context": stale,
            }, 2, str(uuid.uuid4()))

    def test_accepted_task_becomes_resumable_when_current_code_changes(self):
        self.prepare_ready()
        self.operation("accept", agent="lead")
        (self.root / "product.py").write_text("VALUE = 5\n", encoding="utf-8")
        view = task_model.task_view(self.root, "feature")
        self.assert_not_ready()
        self.assertNotEqual(view["phase"], "accepted")

    def test_recorded_command_is_never_executed(self):
        self.initialize()
        marker = self.root / "UNAUTHORIZED_EXECUTION"
        self.evidence(command=f"touch {marker}")
        self.assertFalse(marker.exists())

    def test_paths_cannot_escape_repository(self):
        self.initialize()
        for overrides in [{"log": "../outside.log"}, {"cwd": "../"},
                          {"cwd": str(self.root)}, {"log": str(self.root / "product.py")}]:
            with self.subTest(overrides=overrides):
                with self.assertRaises(collab.CollaborationError):
                    self.evidence(**overrides)

    def test_evidence_ids_are_immutable(self):
        self.initialize()
        self.evidence()
        with self.assertRaises(collab.CollaborationError):
            self.evidence(exit_code=1)

    def test_nonparticipant_cannot_mutate_task(self):
        self.initialize()
        with self.assertRaises(collab.CollaborationError):
            self.operation("cancel", agent="outsider")

    def test_task_lock_symlink_cannot_escape_repository(self):
        self.initialize()
        previous = self.task_path.read_bytes()
        lock_path = self.task_path.parent / ".task.lock"
        lock_path.unlink()
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "outside.lock"
            target.write_bytes(b"do not modify")
            try:
                lock_path.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is unavailable")
            with self.assertRaises(collab.CollaborationError):
                self.checkpoint()
            self.assertEqual(target.read_bytes(), b"do not modify")
        self.assertEqual(self.task_path.read_bytes(), previous)

    def test_interrupted_atomic_replace_preserves_previous_checkpoint(self):
        self.initialize()
        self.checkpoint()
        previous = self.task_path.read_bytes()
        with mock.patch("collab.os.replace", side_effect=OSError("simulated interruption")):
            with self.assertRaises(OSError):
                self.checkpoint(next_action="This write must not become visible")
        self.assertEqual(self.task_path.read_bytes(), previous)
        self.assertEqual(task_model.load_task(self.root, "feature")["revision"], 2)
        self.assertEqual(list(self.task_path.parent.glob(".task.json.*.tmp")), [])

    def completed_turn(self, status="AGREED", requested_action="ACKNOWLEDGE"):
        created = self.run_cli("new-turn", "--root", str(self.root), "--thread-id", "feature",
                               "--author", "lead", "--role", "reviewer", "--kind", "REVIEW",
                               "--status", status, "--requested-action", requested_action,
                               "--title", "Acceptance review")
        path = self.root / created["document"]
        path.write_text(path.read_text(encoding="utf-8").replace(collab.PLACEHOLDER, "Complete:"),
                        encoding="utf-8")
        return created["document"]

    def test_new_terminal_publication_cannot_bypass_coding_acceptance(self):
        self.initialize()
        document = self.completed_turn()
        result = self.run_cli("publish", "--root", str(self.root), "--thread-id", "feature",
                              "--agent", "lead", "--to", "builder", "--document", document,
                              expected=2)
        self.assertIn("accepted", result["error"])
        self.assertFalse((self.task_path.parent / "signals/to-builder.json").exists())
        self.assert_not_ready()

    def test_acknowledgement_of_old_terminal_mail_does_not_accept_task(self):
        document = self.completed_turn()
        result = self.run_cli("publish", "--root", str(self.root), "--thread-id", "feature",
                              "--agent", "lead", "--to", "builder", "--document", document)
        signal = json.loads((self.task_path.parent / "signals/to-builder.json").read_text())
        self.initialize()
        self.run_cli("acknowledge", "--root", str(self.root), "--thread-id", "feature",
                     "--agent", "builder", "--message-id", signal["message_id"])
        self.assertEqual(task_model.load_task(self.root, "feature")["phase"], "planned")
        self.assert_not_ready()
        state = collab.read_state(self.root, "feature", "builder")
        self.assertIn(signal["message_id"], state["processed_message_ids"])

    def test_cli_context_show_and_atomic_checkpoint_round_trip(self):
        initial = self.run_cli("task-init", "--root", str(self.root), "--thread-id", "feature",
                               "--owner", "builder", "--criterion", "ac-1=Tests pass")
        context = self.run_cli("task-context", "--root", str(self.root), "--thread-id", "feature")
        operation = {"kind": "checkpoint", "context": context, "phase": "implementing",
                     "owner": "builder", "artifact_refs": ["product.py"], "review_issue_ids": [],
                     "next_action": "Run the authorized tests", "waiting_on": None}
        path = self.root / ".ai-collab/checkpoint.json"
        path.write_text(json.dumps(operation), encoding="utf-8")
        self.run_cli("task-update", "--root", str(self.root), "--thread-id", "feature",
                     "--agent", "builder", "--expected-revision", str(initial["revision"]),
                     "--operation-id", str(uuid.uuid4()), "--operation-file", ".ai-collab/checkpoint.json")
        view = self.run_cli("task-show", "--root", str(self.root), "--thread-id", "feature")
        self.assertEqual(view["phase"], "implementing")
        self.assertFalse(view["recovery"]["needs_recheck"])

    def test_malformed_task_file_is_not_silently_reinitialized(self):
        self.initialize()
        self.task_path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(collab.CollaborationError):
            task_model.load_task(self.root, "feature")
        with self.assertRaises(collab.CollaborationError):
            self.initialize()


if __name__ == "__main__":
    unittest.main()
