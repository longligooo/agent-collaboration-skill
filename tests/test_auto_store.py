"""Fixture-only state-machine and crash/concurrency tests; no provider calls."""
import concurrent.futures
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from auto_model import AutoError, plan as validate_plan, recipients
from auto_store import AutoStore


class AutoStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = 1000.0
        self.store = AutoStore(self.root, "project", clock=lambda: self.now)
        self.coordinator = {"participant_id": "lead", "provider_id": "codex"}
        self.plan = {"goal": "Build safely", "ticket": ".scratch/feature/issues/01-work.md", "spec": ".scratch/feature/spec.md",
                     "participants": [self.coordinator, {"participant_id": "worker", "provider_id": "claude"}],
                     "tasks": [{"id": "work", "participant_id": "worker", "instruction": "Implement", "acceptance": ["Tests pass"]}]}

    def create(self, key="create"):
        return self.store.create_task("Example", "PRIVATE original", self.coordinator, idempotency_key=key)

    def ready(self):
        task = self.create()
        planner = self.store.claim("runtime")
        task = self.store.submit_plan(task["id"], self.plan, expected_revision=task["revision"])
        self.store.finish(planner["id"], planner["token"], "succeeded")
        task = self.store.get_task(task["id"])
        task = self.store.approve(task["id"], expected_revision=task["revision"], plan_revision=task["plan_revision"], plan_hash=task["plan_hash"], idempotency_key="approve")
        return self.store.bind_thread(task["id"], "thread", expected_revision=task["revision"])

    def worker(self, task, key="work"):
        return self.store.enqueue(task["id"], "worker", "worker", {"planned_task_id": "work"}, idempotency_key=key)

    def error(self, code, callable, *args, **kwargs):
        with self.assertRaises(AutoError) as raised:
            callable(*args, **kwargs)
        self.assertEqual(code, raised.exception.code)

    def test_create_is_durable_and_idempotent(self):
        task = self.create()
        self.assertEqual(task, self.create())
        reopened = AutoStore(self.root, "project")
        self.assertEqual(task, reopened.get_task(task["id"]))
        self.assertEqual(1, len(reopened.list_jobs()))
        self.error("idempotency_conflict", self.store.create_task, "Changed", "PRIVATE original", self.coordinator, idempotency_key="create")
        self.error("invalid", self.store.create_task, "Title", "Text", self.coordinator, idempotency_key=None)

    def test_no_worker_or_manifest_before_approval(self):
        task = self.create()
        self.error("invalid_recipient", self.worker, task)
        self.error("approval_required", self.store.bind_thread, task["id"], "thread", expected_revision=task["revision"])
        self.assertFalse((self.root / ".ai-collab/threads").exists())
        task = self.store.submit_plan(task["id"], self.plan, expected_revision=task["revision"])
        self.error("approval_conflict", self.store.approve, task["id"], expected_revision=task["revision"], plan_revision=task["plan_revision"], plan_hash="stale", idempotency_key="approve")
        self.assertEqual("awaiting_approval", self.store.get_task(task["id"])["state"])

    def test_revised_plan_invalidates_old_approval_and_revision(self):
        task = self.create()
        old = self.store.submit_plan(task["id"], self.plan, expected_revision=task["revision"])
        revised = copy.deepcopy(self.plan); revised["goal"] = "Different goal"
        task = self.store.submit_plan(task["id"], revised, expected_revision=old["revision"])
        self.assertEqual(2, task["plan_revision"])
        self.error("revision_conflict", self.store.approve, task["id"], expected_revision=old["revision"], plan_revision=old["plan_revision"], plan_hash=old["plan_hash"], idempotency_key="old")
        self.error("approval_conflict", self.store.approve, task["id"], expected_revision=task["revision"], plan_revision=old["plan_revision"], plan_hash=old["plan_hash"], idempotency_key="old2")

    def test_human_plan_edit_changes_revision_and_rejects_stale_approval(self):
        task = self.create(); task = self.store.submit_plan(task["id"], self.plan, expected_revision=task["revision"])
        changed = copy.deepcopy(self.plan); changed["participants"][1]["provider_id"] = "agy"
        edited = self.store.edit_plan(task["id"], changed, expected_revision=task["revision"], plan_revision=task["plan_revision"], plan_hash=task["plan_hash"], idempotency_key="edit")
        self.assertEqual(task["plan_revision"] + 1, edited["plan_revision"])
        self.assertNotEqual(task["plan_hash"], edited["plan_hash"])
        self.assertIsNone(edited["approved_plan_revision"])
        self.assertEqual(edited, self.store.edit_plan(task["id"], changed, expected_revision=task["revision"], plan_revision=task["plan_revision"], plan_hash=task["plan_hash"], idempotency_key="edit"))
        self.error("revision_conflict", self.store.approve, task["id"], expected_revision=task["revision"], plan_revision=task["plan_revision"], plan_hash=task["plan_hash"], idempotency_key="stale")
        self.error("approval_conflict", self.store.edit_plan, task["id"], changed, expected_revision=edited["revision"], plan_revision=edited["plan_revision"], plan_hash="wrong", idempotency_key="stale-edit")
        changed["participants"][0]["provider_id"] = "agy"
        self.error("invalid_plan", self.store.edit_plan, task["id"], changed, expected_revision=edited["revision"], plan_revision=edited["plan_revision"], plan_hash=edited["plan_hash"], idempotency_key="bad-coordinator")

    def test_human_plan_edit_key_conflict_and_finalized_rejection(self):
        task = self.create(); task = self.store.submit_plan(task["id"], self.plan, expected_revision=task["revision"])
        changed = copy.deepcopy(self.plan); changed["goal"] = "Edited goal"
        self.store.edit_plan(task["id"], changed, expected_revision=task["revision"], plan_revision=task["plan_revision"], plan_hash=task["plan_hash"], idempotency_key="edit")
        changed["goal"] = "Second goal"
        self.error("idempotency_conflict", self.store.edit_plan, task["id"], changed, expected_revision=task["revision"], plan_revision=task["plan_revision"], plan_hash=task["plan_hash"], idempotency_key="edit")
        current = self.store.get_task(task["id"])
        current = self.store.approve(current["id"], expected_revision=current["revision"], plan_revision=current["plan_revision"], plan_hash=current["plan_hash"], idempotency_key="approve")
        current = self.store.bind_thread(current["id"], "thread", expected_revision=current["revision"])
        self.error("approval_conflict", self.store.edit_plan, current["id"], changed, expected_revision=current["revision"], plan_revision=current["plan_revision"], plan_hash=current["plan_hash"], idempotency_key="post-finalization")

    def test_human_edit_and_approval_race_has_exactly_one_winner(self):
        task = self.create(); task = self.store.submit_plan(task["id"], self.plan, expected_revision=task["revision"])
        changed = copy.deepcopy(self.plan); changed["goal"] = "Edited goal"
        def mutate(action):
            store = AutoStore(self.root, "project")
            kwargs = dict(expected_revision=task["revision"], plan_revision=task["plan_revision"], plan_hash=task["plan_hash"], idempotency_key=action)
            try:
                return store.edit_plan(task["id"], changed, **kwargs) if action == "edit" else store.approve(task["id"], **kwargs)
            except AutoError as exc:
                return exc.code
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(mutate, ("edit", "approve")))
        self.assertEqual(1, sum(isinstance(result, dict) for result in results))
        self.assertEqual(1, results.count("revision_conflict"))
        current = self.store.get_task(task["id"])
        if current["state"] == "ready":
            self.assertEqual(task["plan_hash"], current["approved_plan_hash"])
        else:
            self.assertEqual("awaiting_approval", current["state"])
            self.assertIsNone(current["approved_plan_hash"])
            self.assertNotEqual(task["plan_hash"], current["plan_hash"])
        self.assertEqual([], self.store.list_jobs(task["id"], states=["claimed"]))

    def test_finalize_is_immutable(self):
        task = self.ready()
        self.error("immutable_thread", self.store.bind_thread, task["id"], "other", expected_revision=task["revision"])
        self.error("invalid_state", self.store.submit_plan, task["id"], self.plan, expected_revision=task["revision"])
        self.error("invalid_state", self.store.revise, task["id"], "Change", expected_revision=task["revision"], idempotency_key="revise")

    def test_plan_rejects_bad_roster_and_dependency_cycle(self):
        malformed = copy.deepcopy(self.plan); malformed["participants"].append(self.coordinator)
        self.error("invalid_plan", validate_plan, malformed, self.coordinator)
        malformed = copy.deepcopy(self.plan); malformed["tasks"][0]["depends_on"] = ["work"]
        self.error("invalid_plan", validate_plan, malformed, self.coordinator)
        malformed = copy.deepcopy(self.plan); malformed["ticket"] = "../outside.md"
        self.error("invalid_plan", validate_plan, malformed, self.coordinator)
        malformed = copy.deepcopy(self.plan); malformed["tasks"][0].pop("acceptance")
        self.error("invalid_plan", validate_plan, malformed, self.coordinator)
        malformed = copy.deepcopy(self.plan); malformed["participants"][0]["provider_id"] = "agy"
        self.error("invalid_plan", validate_plan, malformed, self.coordinator)

    def test_fifo_global_claim_serializes_checkout(self):
        first = self.create("one"); second = self.create("two")
        job = self.store.claim("r1")
        self.assertEqual(first["id"], job["task_id"])
        self.assertIsNone(self.store.claim("r2"))
        self.store.finish(job["id"], job["token"], "failed")
        self.assertEqual(second["id"], self.store.claim("r2")["task_id"])

    def test_expiry_quarantines_root_until_process_proof_and_retry(self):
        first = self.create("one"); self.create("two")
        job = self.store.claim("r1", lease_seconds=5)
        self.now += 6
        self.error("stale_claim", self.store.finish, job["id"], job["token"], "succeeded")
        self.assertEqual(1, len(self.store.expire_claims()))
        self.assertIsNone(self.store.claim("r2"))
        self.error("process_unknown", self.store.resolve_unknown, job["id"], process_stopped=False, evidence="Uncertain")
        task = self.store.get_task(first["id"])
        self.error("unsafe_retry", self.store.retry, task["id"], job["id"], expected_revision=task["revision"], idempotency_key="retry")
        self.store.resolve_unknown(job["id"], process_stopped=True, evidence="Runtime verified owned process group ended")
        task = self.store.get_task(first["id"])
        retried = self.store.retry(task["id"], job["id"], expected_revision=task["revision"], idempotency_key="retry")
        self.assertEqual("queued", retried["job"]["state"])
        next_job = self.store.claim("r2")
        self.assertEqual(job["id"], next_job["id"])
        self.assertNotEqual(job["token"], next_job["token"])
        self.assertEqual(2, next_job["attempt"])
        self.error("stale_claim", self.store.heartbeat, job["id"], job["token"])

    def test_recover_spawn_gap_quarantines_even_no_pid(self):
        self.create(); job = self.store.claim("old")
        self.assertNotIn("pid", job)
        self.assertEqual(1, len(self.store.recover_owner("old")))
        self.assertIsNone(self.store.claim("new"))
        self.error("stale_claim", self.store.attach_process, job["id"], job["token"], 123, "identity")

    def test_process_attachment_is_fenced_and_immutable(self):
        self.create(); job = self.store.claim("r")
        attached = self.store.attach_process(job["id"], job["token"], 123, "birth-token")
        self.assertEqual(123, attached["pid"])
        self.error("process_conflict", self.store.attach_process, job["id"], job["token"], 124, "birth-token")

    def test_success_is_not_acceptance_or_completion(self):
        task = self.ready(); self.worker(task)
        job = self.store.claim("r")
        self.store.finish(job["id"], job["token"], "succeeded")
        task = self.store.get_task(task["id"])
        self.assertEqual("ready", task["state"])
        self.assertEqual({"status": "unverified"}, task["acceptance"])

    def test_pause_resume_and_cancel_safe_boundaries(self):
        task = self.ready(); self.worker(task)
        job = self.store.claim("r")
        task = self.store.get_task(task["id"])
        task = self.store.control(task["id"], "pause", expected_revision=task["revision"], idempotency_key="pause")
        self.assertEqual("pausing", task["state"])
        self.assertIsNone(self.store.claim("other"))
        self.store.finish(job["id"], job["token"], "succeeded")
        task = self.store.get_task(task["id"])
        self.assertEqual("paused", task["state"])
        task = self.store.control(task["id"], "resume", expected_revision=task["revision"], idempotency_key="resume")
        self.assertEqual("ready", task["state"])
        task = self.store.control(task["id"], "cancel", expected_revision=task["revision"], idempotency_key="cancel")
        self.assertEqual("cancelled", task["state"])

    def test_cancel_unknown_requires_stop_confirmation(self):
        task = self.create(); job = self.store.claim("r")
        task = self.store.control(task["id"], "cancel", expected_revision=task["revision"], idempotency_key="cancel")
        self.assertEqual("cancelling", task["state"])
        self.store.recover_owner("r")
        self.assertEqual("cancelling", self.store.get_task(task["id"])["state"])
        self.store.resolve_unknown(job["id"], process_stopped=True, evidence="Process group verified dead")
        self.assertEqual("cancelled", self.store.get_task(task["id"])["state"])

    def test_provider_change_waits_for_boundary_and_keeps_identity(self):
        task = self.ready(); self.worker(task)
        job = self.store.claim("r"); task = self.store.get_task(task["id"])
        result = self.store.request_provider_change(task["id"], "worker", "agy", "model", expected_revision=task["revision"], idempotency_key="change")
        self.assertEqual("queued", result["change"]["state"])
        self.assertEqual("claude", self.store.get_job(job["id"])["binding"]["provider_id"])
        self.store.finish(job["id"], job["token"], "succeeded")
        task = self.store.get_task(task["id"])
        self.assertEqual("agy", next(p for p in task["participants"] if p["participant_id"] == "worker")["provider_id"])
        self.assertEqual([], task["queued_provider_changes"])
        self.worker(task, "second"); job = self.store.claim("r")
        self.assertEqual("agy", job["binding"]["provider_id"])

    def test_message_mentions_snapshot_and_no_live_injection(self):
        task = self.ready()
        body = "@all 原文\nKeep whitespace  "
        event = self.store.send_message(task["id"], body, idempotency_key="message")
        self.assertEqual(body, event["body"])
        self.assertEqual(["lead", "worker"], event["recipients"])
        self.worker(task); job = self.store.claim("r")
        claimed = self.store.job_messages(job["id"], job["token"])
        self.assertEqual([event["event_id"]], [e["event_id"] for e in claimed])
        later = self.store.send_message(task["id"], "@worker later", idempotency_key="later")
        self.assertEqual(1, len(self.store.job_messages(job["id"], job["token"])))
        self.store.mark_delivered(job["id"], job["token"], [event["event_id"]])
        event = self.store.messages(task["id"])["events"][0]
        states = {d["recipient"]: d["state"] for d in event["delivery"]}
        self.assertEqual({"lead": "queued", "worker": "delivered"}, states)
        self.assertEqual("queued", self.store.messages(task["id"])["events"][1]["delivery"][0]["state"])
        self.assertNotIn(body, json.dumps(self.store.events(task["id"])))
        self.assertFalse((self.root / ".ai-collab/threads").exists())

    def test_message_default_unknown_and_explicit_mismatch(self):
        task = self.create()
        event = self.store.send_message(task["id"], "hello", idempotency_key="message")
        self.assertEqual(["lead"], event["recipients"])
        self.error("invalid_recipient", self.store.send_message, task["id"], "@worker wait", idempotency_key="bad")
        self.error("invalid_recipient", self.store.send_message, task["id"], "hello", ["worker"], idempotency_key="bad2")
        self.assertEqual(["lead"], recipients("@all @lead", ["lead"], "lead"))

    def test_unknown_delivery_not_resent_on_retry(self):
        task = self.create()
        self.store.send_message(task["id"], "hello", idempotency_key="message")
        job = self.store.claim("r"); self.store.recover_owner("r")
        delivery = self.store.messages(task["id"])["events"][0]["delivery"][0]
        self.assertEqual("failed", delivery["state"])
        self.assertEqual("delivery_unknown", delivery["error_code"])
        self.store.resolve_unknown(job["id"], process_stopped=True, evidence="Owned process stopped")
        task = self.store.get_task(task["id"])
        self.store.retry(task["id"], job["id"], expected_revision=task["revision"], idempotency_key="retry")
        job = self.store.claim("new")
        self.assertEqual([], self.store.job_messages(job["id"], job["token"]))

    def test_concurrent_claim_has_one_winner(self):
        self.create("one"); self.create("two")
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            jobs = list(pool.map(lambda i: AutoStore(self.root, "project").claim(str(i)), range(8)))
        self.assertEqual(1, sum(job is not None for job in jobs))

    def test_concurrent_approve_revision_race_has_one_winner(self):
        task = self.create(); task = self.store.submit_plan(task["id"], self.plan, expected_revision=task["revision"])
        def approve(i):
            try:
                return AutoStore(self.root, "project").approve(task["id"], expected_revision=task["revision"], plan_revision=task["plan_revision"], plan_hash=task["plan_hash"], idempotency_key=str(i))
            except AutoError as exc:
                return exc.code
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            results = list(pool.map(approve, range(4)))
        self.assertEqual(1, sum(isinstance(r, dict) for r in results))
        self.assertEqual(3, results.count("revision_conflict"))

    def test_dependency_jobs_wait_and_validate_recipient(self):
        self.plan["tasks"].append({"id": "review", "participant_id": "lead", "instruction": "Review", "acceptance": ["Evidence reviewed"], "depends_on": ["work"]})
        task = self.ready()
        self.error("invalid_job", self.store.enqueue, task["id"], "lead", "worker", {"planned_task_id": "work"}, idempotency_key="wrong")
        dependent = self.store.enqueue(task["id"], "lead", "worker", {"planned_task_id": "review"}, idempotency_key="review")
        self.assertIsNone(self.store.claim("r"))
        first = self.worker(task)
        job = self.store.claim("r")
        self.assertEqual(first["id"], job["id"])
        self.store.finish(job["id"], job["token"], "succeeded")
        job = self.store.claim("r")
        self.assertEqual(dependent["id"], job["id"])
        self.store.finish(job["id"], job["token"], "succeeded")
        task = self.store.mark_execution_finished(task["id"])
        self.assertEqual("blocked", task["state"])
        self.assertEqual("awaiting_verification", task["blocked_reason"])
        self.assertEqual({"status": "unverified"}, task["acceptance"])

    def test_message_cycle_before_approval_only_coordinator(self):
        task = self.create()
        planner = self.store.claim("r")
        task = self.store.submit_plan(task["id"], self.plan, expected_revision=task["revision"])
        self.store.finish(planner["id"], planner["token"], "succeeded")
        event = self.store.send_message(task["id"], "please clarify", idempotency_key="message")
        queued = self.store.enqueue(task["id"], "lead", "message", {"event_id": event["event_id"]}, idempotency_key="msg-job")
        job = self.store.claim("r")
        self.assertEqual(queued["id"], job["id"])
        self.assertEqual("awaiting_approval", self.store.get_task(task["id"])["state"])
        self.assertEqual(1, len(self.store.job_messages(job["id"], job["token"])))
        self.store.mark_delivered(job["id"], job["token"], [event["event_id"]])
        self.store.finish(job["id"], job["token"], "succeeded")
        self.assertEqual("awaiting_approval", self.store.get_task(task["id"])["state"])

    def test_message_job_skipped_when_worker_already_delivered(self):
        task = self.ready()
        event = self.store.send_message(task["id"], "@worker hello", idempotency_key="message")
        self.worker(task)
        pending = self.store.enqueue(task["id"], "worker", "message", {"event_id": event["event_id"]}, idempotency_key="msg-job")
        job = self.store.claim("r")
        self.store.mark_delivered(job["id"], job["token"], [event["event_id"]])
        self.store.finish(job["id"], job["token"], "succeeded")
        self.assertIsNone(self.store.claim("r"))
        self.assertEqual("cancelled", self.store.get_job(pending["id"])["state"])

    def test_retry_has_new_process_identity(self):
        task = self.create(); job = self.store.claim("r")
        self.store.attach_process(job["id"], job["token"], 123, "old")
        self.store.finish(job["id"], job["token"], "failed")
        task = self.store.get_task(task["id"])
        self.store.retry(task["id"], job["id"], expected_revision=task["revision"], idempotency_key="retry")
        job = self.store.claim("r2")
        self.assertNotIn("pid", job)
        self.assertEqual(456, self.store.attach_process(job["id"], job["token"], 456, "new")["pid"])

    def test_replaced_root_and_database_hardlinks_rejected(self):
        alias = self.root / "db-alias"
        os.link(self.store.path, alias)
        self.error("unsafe_path", self.store.list_tasks)
        alias.unlink()
        if os.name != "nt":
            os.chmod(self.store.path, 0o644)
            self.error("unsafe_path", self.store.list_tasks)
            os.chmod(self.store.path, 0o600)
        target = self.root / "outside"
        target.write_text("secret")
        Path(str(self.store.path) + "-journal").symlink_to(target)
        self.error("unsafe_path", self.store.list_tasks)

    def test_invalid_unicode_and_incomplete_mentions(self):
        task = self.create()
        self.error("invalid", self.store.send_message, task["id"], "\ud800", idempotency_key="surrogate")
        self.error("invalid_recipient", self.store.send_message, task["id"], "hello @", idempotency_key="incomplete")

    def test_database_ignored_before_any_supervisor_starts(self):
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True, capture_output=True)
        (self.root / "README.md").write_text("Fixture repository")
        self.create()
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True, capture_output=True)
        staged = subprocess.check_output(["git", "-C", str(self.root), "ls-files"], text=True)
        self.assertEqual("README.md\n", staged)
        ignored = subprocess.check_output(["git", "-C", str(self.root), "check-ignore", str(self.store.path)], text=True)
        self.assertIn("state.sqlite3", ignored)

    def test_preexisting_conflicting_ignore_fails_before_database_creation(self):
        for name, symlink in (("incompatible", False), ("linked", True)):
            root = self.root / name; root.mkdir()
            directory = root / ".ai-collab/local/automatic"; directory.mkdir(parents=True)
            ignore = directory / ".gitignore"
            if symlink:
                target = self.root / "ignore-target"; target.write_text("*\n")
                ignore.symlink_to(target)
            else:
                ignore.write_text("!state.sqlite3\n")
            self.error("unsafe_path", AutoStore, root, "project")
            self.assertFalse((directory / "state.sqlite3").exists())
            self.assertEqual("!state.sqlite3\n", ignore.read_text()) if not symlink else self.assertTrue(ignore.is_symlink())

    def test_changed_privacy_ignore_blocks_existing_store(self):
        self.store.directory.joinpath(".gitignore").write_text("!state.sqlite3\n")
        self.error("unsafe_path", self.create)

    def test_service_guards_are_transactional_and_skipped_on_replay(self):
        for operation in ("revise", "edit_plan", "request_provider_change"):
            with self.subTest(operation=operation):
                task = self.create("create-" + operation)
                if operation != "revise":
                    task = self.store.submit_plan(task["id"], self.plan, expected_revision=task["revision"])
                if operation == "request_provider_change":
                    task = self.store.approve(task["id"], expected_revision=task["revision"], plan_revision=task["plan_revision"], plan_hash=task["plan_hash"], idempotency_key="approve-guard")
                calls = []
                def guard(current):
                    calls.append(current["id"])
                    self.assertEqual(task["revision"], current["revision"])
                    # A second writer cannot enter while the guard is evaluating.
                    connection = sqlite3.connect(self.store.path, timeout=0, isolation_level=None)
                    try:
                        with self.assertRaises(sqlite3.OperationalError):
                            connection.execute("BEGIN IMMEDIATE")
                    finally:
                        connection.close()
                kwargs = {"expected_revision": task["revision"], "idempotency_key": "guard-" + operation, "guard": guard}
                if operation == "revise":
                    args = [task["id"], "New original"]
                elif operation == "edit_plan":
                    args = [task["id"], self.plan]
                    kwargs.update(plan_revision=task["plan_revision"], plan_hash=task["plan_hash"])
                else:
                    args = [task["id"], "worker", "agy"]
                method = getattr(self.store, operation)
                first = method(*args, **kwargs)
                def must_not_run(_):
                    self.fail("guard must not run again on stored command replay")
                kwargs["guard"] = must_not_run
                self.assertEqual(first, method(*args, **kwargs))
                self.assertEqual([task["id"]], calls)

    def test_internal_junction_guard_precedes_database_open(self):
        import auto_process
        root = self.root / "junction-fixture"; root.mkdir()
        rejected = root / ".ai-collab/local"
        with patch("auto_store.is_unsafe_link", side_effect=lambda p: Path(p) == rejected or auto_process.is_unsafe_link(p)):
            self.error("unsafe_path", AutoStore, root, "project")
        self.assertFalse((root / ".ai-collab/local/automatic/state.sqlite3").exists())
        sidecar = Path(str(self.store.path) + "-wal")
        with patch("auto_store.is_unsafe_link", side_effect=lambda p: Path(p) == sidecar or auto_process.is_unsafe_link(p)):
            self.error("unsafe_path", self.store.list_tasks)

    def test_private_file_and_wrong_project_rejected(self):
        if os.name != "nt":
            self.assertEqual(0, self.store.path.stat().st_mode & 0o077)
        self.error("store_mismatch", AutoStore, self.root, "different")

    def test_symlink_state_rejected(self):
        target = self.root / "other"; target.mkdir()
        alias = self.root / "alias"; alias.symlink_to(target, target_is_directory=True)
        self.error("unsafe_path", AutoStore, alias, "project")
        root2 = self.root / "second"; root2.mkdir(); (root2 / ".ai-collab").symlink_to(target, target_is_directory=True)
        self.error("unsafe_path", AutoStore, root2, "project")


if __name__ == "__main__":
    unittest.main()
