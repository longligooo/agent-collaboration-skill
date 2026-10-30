import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import collab
import task_discovery
import task_model
import ui_server


class TaskDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.real_task_view = task_model.task_view
        self.module_patch = mock.patch.object(task_model, "task_view", return_value=None)
        self.model = types.SimpleNamespace(task_view=self.module_patch.start())
        self.env = mock.patch.dict(os.environ, {"COLLAB_UI_HOME": str(self.base / "registry")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.module_patch.stop()
        self.temp.cleanup()

    def project(self, name, participants=("alice", "bob"), thread="work"):
        root = self.base / name
        root.mkdir(exist_ok=True)
        target = collab.thread_directory(root, thread)
        for child in ("turns", "signals", "state"):
            (target / child).mkdir(parents=True, exist_ok=True)
        collab.atomic_write_json(target / "thread.json", {
            "protocol_version": 1, "thread_id": thread, "topic": name,
            "objective": "Ticket: test", "participants": list(participants),
            "created_at": "2026-10-30T00:00:00+00:00",
        })
        return root

    def publish(self, root, status="OPEN", author="bob", recipient="alice"):
        args = types.SimpleNamespace(root=str(root), thread_id="work", author=author,
                                     role="proposer", kind="DISCUSSION", status=status,
                                     requested_action="ACKNOWLEDGE" if status == "AGREED" else "REVIEW",
                                     round=None, based_on=[], title="Test")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            collab.cmd_new_turn(args)
        document = json.loads(output.getvalue())["document"]
        path = root / document
        path.write_text(path.read_text().replace(collab.PLACEHOLDER, "Complete:"))
        args = types.SimpleNamespace(root=str(root), thread_id="work", agent=author,
                                     to=recipient, document=document, requested_action=None,
                                     in_message_id=None, message_id=str(uuid.uuid4()))
        with contextlib.redirect_stdout(io.StringIO()):
            collab.cmd_publish(args)
        return args.message_id

    def coding_view(self, owner="alice", phase="implementing", waiting_on=None, stale=False):
        return {"profile": "coding", "phase": phase, "owner": owner, "revision": 2,
                "criteria": [], "evidence": [], "reviews": [],
                "checkpoint": {"owner": owner, "phase": phase, "waiting_on": waiting_on,
                               "next_action": "Review current sources", "artifact_refs": [], "review_issue_ids": []},
                "acceptance": {"ready": not stale, "blockers": [{"code": "stale"}] if stale else []},
                "recovery": {"needs_recheck": stale, "reasons": ["code_changed"] if stale else []}}

    def test_cross_project_aliases_and_explicit_name_collisions(self):
        one = self.project("one", ("alice", "bob"))
        two = self.project("two", ("reviewer", "alice"))
        task_discovery.bind_identity(one, "alice", "person-a")
        task_discovery.bind_identity(two, "reviewer", "person-a")
        task_discovery.bind_identity(two, "alice", "person-b")
        report = task_discovery.query_tasks([one, two, one], "person-a")
        self.assertEqual(report["count"], 2)
        self.assertEqual([item["agents"] for item in report["items"]], [["alice"], ["reviewer"]])
        self.assertEqual(task_discovery.query_tasks([one, two], "person-b")["count"], 1)
        self.assertEqual(task_discovery.query_tasks([one, two], "alice")["count"], 0)

    def test_bind_is_idempotent_and_remapping_is_explicit(self):
        root = self.project("one")
        task_discovery.bind_identity(root, "alice", "stable")
        path = root / ".ai-collab/identities.json"
        before = path.read_bytes()
        self.assertEqual(task_discovery.bind_identity(root, "alice", "stable")["result"], "UNCHANGED")
        self.assertEqual(path.read_bytes(), before)
        with self.assertRaises(collab.CollaborationError):
            task_discovery.bind_identity(root, "alice", "different")
        task_discovery.bind_identity(root, "alice", "different", replace=True)
        self.assertEqual(task_discovery.load_identities(root), {"alice": "different"})
        for invalid in ("../escape", "session/name", "", "Agent"):
            with self.assertRaises(collab.CollaborationError):
                task_discovery.bind_identity(root, "alice", invalid)

    def test_parallel_bindings_do_not_lose_updates(self):
        root = self.project("one")
        errors = []
        def bind(agent):
            try:
                task_discovery.bind_identity(root, agent, "identity-" + agent)
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=bind, args=(agent,)) for agent in ("alice", "bob")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(task_discovery.load_identities(root)), 2)

    def test_invalid_mapping_does_not_guess_identity(self):
        root = self.project("one")
        (root / ".ai-collab/identities.json").write_text('{"schema_version": 99, "participants": {}}')
        report = task_discovery.query_tasks([root], "alice")
        self.assertEqual(report["items"], [])
        self.assertEqual(len(report["errors"]), 1)

    @unittest.skipIf(os.name == "nt", "symlink creation may require Windows privileges")
    def test_identity_file_cannot_escape_project(self):
        root = self.project("one")
        outside = self.base / "outside.json"
        outside.write_text("{}")
        (root / ".ai-collab/identities.json").symlink_to(outside)
        with self.assertRaises(collab.CollaborationError):
            task_discovery.bind_identity(root, "alice", "stable")
        self.assertEqual(outside.read_text(), "{}")

    def test_legacy_missing_data_read_only_and_pending_ack(self):
        root = self.project("legacy")
        report = task_discovery.query_tasks([root], "alice")
        self.assertEqual(report["items"][0]["profile"], "legacy")
        self.assertEqual(report["items"][0]["classification"], "waiting-peer")
        self.assertFalse((root / ".ai-collab/identities.json").exists())
        message_id = self.publish(root, "AGREED")
        before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        report = task_discovery.query_tasks([root], "alice")
        self.assertEqual(report["items"][0]["classification"], "actionable")
        self.assertTrue(report["items"][0]["terminal"])
        self.assertEqual(before, {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()})
        collab.mark_processed(root, "work", "alice", message_id)
        self.assertEqual(task_discovery.query_tasks([root], "alice")["count"], 0)
        self.assertEqual(task_discovery.query_tasks([root], "alice", include_terminal=True)["count"], 1)

    def test_duplicates_are_not_actionable(self):
        root = self.project("one")
        message_id = self.publish(root)
        self.assertEqual(task_discovery.query_tasks([root], "alice")["items"][0]["classification"], "actionable")
        collab.mark_processed(root, "work", "alice", message_id)
        self.assertEqual(task_discovery.query_tasks([root], "alice")["items"][0]["classification"], "waiting-peer")

    def test_classifications_and_filters(self):
        root = self.project("one")
        for waiting, expected in ((None, "actionable"), ({"kind": "peer", "identity": "bob"}, "waiting-peer"),
                                  ({"kind": "human", "identity": "human"}, "waiting-human")):
            self.model.task_view.return_value = self.coding_view(waiting_on=waiting)
            report = task_discovery.query_tasks([root], "alice", category=expected)
            self.assertEqual(report["count"], 1)
            self.assertEqual(report["items"][0]["classification"], expected)
            self.assertEqual(report["items"][0]["next_action"], "Review current sources")
            other = next(value for value in task_discovery.CATEGORIES if value != expected)
            self.assertEqual(task_discovery.query_tasks([root], "alice", category=other)["count"], 0)
        with self.assertRaises(collab.CollaborationError):
            task_discovery.query_tasks([root], "alice", category="unknown")

    def test_owner_is_resolved_through_stable_identity(self):
        root = self.project("one")
        task_discovery.bind_identity(root, "alice", "stable")
        self.model.task_view.return_value = self.coding_view()
        self.assertEqual(task_discovery.query_tasks([root], "stable")["items"][0]["classification"], "actionable")
        self.assertEqual(task_discovery.query_tasks([root], "bob")["items"][0]["classification"], "waiting-peer")

    def test_peer_waiting_on_me_is_actionable(self):
        root = self.project("one")
        task_discovery.bind_identity(root, "alice", "stable")
        self.model.task_view.return_value = self.coding_view(owner="bob", waiting_on={"kind": "peer", "identity": "stable"})
        self.assertEqual(task_discovery.query_tasks([root], "stable")["items"][0]["classification"], "actionable")
        self.assertEqual(task_discovery.query_tasks([root], "bob")["items"][0]["classification"], "waiting-peer")

    def test_live_core_model_is_shared_and_survives_reload(self):
        root = self.project("one")
        self.model.task_view.side_effect = self.real_task_view
        task_model.initialize_task(root, "work", "alice", [{"id": "tests", "text": "Tests pass"}])
        task_discovery.bind_identity(root, "alice", "stable")
        report = task_discovery.query_tasks([root], "stable")
        item = report["items"][0]
        self.assertEqual(item["profile"], "coding")
        self.assertFalse(item["coding_task"]["acceptance"]["ready"])
        self.assertEqual(item["coding_task"], self.real_task_view(root, "work"))
        self.assertEqual(task_discovery.load_identities(root), {"alice": "stable"})
        self.assertEqual(task_discovery.query_tasks([root], "stable")["count"], 1)

    def test_accepted_stale_reappears_but_cancelled_is_terminal(self):
        root = self.project("one")
        self.model.task_view.return_value = self.coding_view(phase="accepted")
        self.assertEqual(task_discovery.query_tasks([root], "alice")["count"], 0)
        self.model.task_view.return_value = self.coding_view(phase="accepted", stale=True)
        self.assertEqual(task_discovery.query_tasks([root], "alice")["count"], 1)
        self.model.task_view.return_value = self.coding_view(phase="cancelled", stale=True)
        self.assertEqual(task_discovery.query_tasks([root], "alice")["count"], 0)

    def test_acceptance_publication_does_not_leave_owner_task_unfinished(self):
        root = self.project("one")
        view = self.coding_view(phase="accepted")
        view["recovery"] = {"needs_recheck": True, "reasons": ["messages-changed"]}
        self.model.task_view.return_value = view
        self.publish(root, "AGREED", author="alice", recipient="bob")
        self.assertEqual(task_discovery.query_tasks([root], "alice")["count"], 0)
        self.assertEqual(task_discovery.query_tasks([root], "bob")["count"], 1)

    def test_invalid_signal_requires_human_not_automatic_execution(self):
        root = self.project("one")
        (collab.thread_directory(root, "work") / "signals/to-alice.json").write_text("{}")
        report = task_discovery.query_tasks([root], "alice")
        self.assertEqual(report["items"][0]["classification"], "waiting-human")
        self.assertEqual(report["items"][0]["pending"][0]["decision"], "INVALID")

    def test_unavailable_project_and_malformed_thread_are_reported(self):
        root = self.project("one")
        (collab.thread_directory(root, "work") / "thread.json").write_text("{}")
        report = task_discovery.query_tasks([self.base / "missing", root], "alice")
        self.assertEqual(report["count"], 0)
        self.assertEqual(len(report["errors"]), 2)

    def test_cli_contract_and_registry(self):
        root = self.project("one")
        ui_server.register_project(root)
        parser = argparse.ArgumentParser()
        task_discovery.add_parsers(parser.add_subparsers(dest="command", required=True))
        args = parser.parse_args(["my-tasks", "--all-projects", "--identity", "alice"])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            args.handler(args)
        self.assertEqual(json.loads(output.getvalue())["count"], 1)
        args = parser.parse_args(["my-tasks", "--root", str(root), "--root", str(root), "--identity", "alice"])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            args.handler(args)
        self.assertEqual(json.loads(output.getvalue())["count"], 1)

    def test_read_api_uses_only_registered_projects(self):
        root = self.project("one")
        self.project("unregistered")
        ui_server.register_project(root)
        server = ui_server.ThreadingHTTPServer(("127.0.0.1", 0), ui_server.CollaborationHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with urlopen(base + "/api/tasks?identity=alice", timeout=3) as response:
                report = json.loads(response.read())
            self.assertEqual(report["count"], 1)
            self.assertEqual(report["items"][0]["project_root"], str(root))
            for query in ("", "?identity=alice&root=/tmp", "?identity=alice&identity=bob",
                          "?identity=alice&include_terminal=maybe", "?identity=alice&category=unknown"):
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + "/api/tasks" + query, timeout=3)
                self.assertEqual(error.exception.code, 400)
            request = Request(base + "/api/tasks", data=b'{}', method="POST")
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=3)
            self.assertEqual(error.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
