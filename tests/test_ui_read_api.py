"""The read UI consumes explicit conversation/runtime truth, never inferred activity."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "collab.py"
sys.path.insert(0, str(SCRIPT.parent))
import collab
import human_events
import task_discovery
import task_model
import ui_server


class UiReadApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.env_patch = mock.patch.dict(os.environ, {"COLLAB_UI_HOME": str(self.home)})
        self.env_patch.start()
        self.root = self.base / "project"
        self.root.mkdir()
        self.cli("init", "--thread-id", "feature", "--topic", "Review", "--objective", "Ticket test",
                 "--participants", "builder", "lead", "peer", "--coordinator", "lead")
        ui_server.register_project(self.root)
        self.project_id = ui_server.project_id(str(self.root))
        self.server = None

    def tearDown(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        self.env_patch.stop()
        self.temp.cleanup()

    def cli(self, *arguments):
        result = subprocess.run([sys.executable, str(SCRIPT), *arguments, "--root", str(self.root)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def make_turn(self, author="lead"):
        created = self.cli("new-turn", "--thread-id", "feature", "--author", author,
                           "--role", "reviewer", "--kind", "REVIEW", "--status", "OPEN",
                           "--requested-action", "REVISE", "--title", "Review <script>not executable</script>")
        path = self.root / created["document"]
        path.write_text(path.read_text().replace(collab.PLACEHOLDER, "Evidence:"))
        return created["document"]

    def human(self, identifier="human-1", **updates):
        payload = {"event_id": identifier, "actor_type": "human", "author": "user",
                   "body": "Please ask @builder only. <script>not executable</script>",
                   "created_at": "2026-10-30T09:00:00Z", "recipients": ["builder"],
                   "provenance": {"kind": "authorized-import", "source_conversation_id": "source-with-builder",
                                  "source_message_id": identifier, "source_author": "user",
                                  "supplied_authorized": True, "original_text": True}}
        payload.update(updates)
        return human_events.record_human_event(self.root, "feature", payload)

    def detail(self):
        return ui_server.thread_detail(self.project_id, "feature")["thread"]

    def start_server(self):
        self.server = ui_server.ThreadingHTTPServer(("127.0.0.1", 0), ui_server.CollaborationHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self.server.server_address[1]

    def request(self, port, path, *, method="GET"):
        with urlopen(Request(f"http://127.0.0.1:{port}{path}", method=method), timeout=5) as response:
            return json.loads(response.read())

    def test_legacy_turn_not_inferred_human_and_recipient_unknown(self):
        self.make_turn()
        view = self.detail()
        self.assertEqual(view["history"][0]["actor_type"], "agent")
        self.assertTrue(view["history"][0]["is_coordinator"])
        self.assertIsNone(view["history"][0]["provenance"])
        self.assertEqual(view["history"][0]["recipients"], [])
        self.assertEqual(view["history"][0]["recipient_source"], "unknown")
        self.assertIsNone(view["coding_task"])
        self.assertEqual(json.loads((self.root / ".ai-collab/threads/feature/thread.json").read_text())["participants"], ["builder", "lead", "peer"])

    def test_human_source_maps_only_explicit_recipient_without_manifest_mutation(self):
        task_discovery.bind_identity(self.root, "builder", "alex")
        self.human()
        view = self.detail()
        event = view["history"][0]
        self.assertEqual(event["actor_type"], "human")
        self.assertEqual(event["recipients"], [{"agent": "builder", "identity": "alex"}])
        self.assertEqual(event["provenance"]["source_conversation_id"], "source-with-builder")
        self.assertIn("not independently verified", event["attribution"])
        self.assertIn("<script>", event["body"])  # JSON facts preserved; UI must escape, never execute.
        self.assertNotIn("user", view["participants"])
        self.assertIn("user", [item["agent"] for item in view["participant_details"]])

    def test_current_signal_recipient_is_labeled_observed_not_original_provenance(self):
        document = self.make_turn()
        self.cli("publish", "--thread-id", "feature", "--agent", "lead", "--to", "builder", "--document", document)
        event = self.detail()["history"][0]
        self.assertEqual(event["recipients"], [{"agent": "builder", "identity": "builder"}])
        self.assertEqual(event["recipient_source"], "current-signal")

    def test_human_updates_invalidate_task_recovery_context_not_code_version(self):
        task_model.initialize_task(self.root, "feature", "builder", [{"id": "ac-1", "text": "Pass"}])
        before = task_model.capture_context(self.root, "feature")
        self.human()
        after = task_model.capture_context(self.root, "feature")
        self.assertEqual(before["code_version"], after["code_version"])
        self.assertNotEqual(before["messages_fingerprint"], after["messages_fingerprint"])
        self.assertTrue(self.detail()["coding_task"]["recovery"]["needs_recheck"])

    def test_snapshot_identity_grouping_and_human_attribution(self):
        task_discovery.bind_identity(self.root, "builder", "alex")
        self.human()
        snapshot = ui_server.snapshot()
        builder = next(person for person in snapshot["collaborators"] if person["identity"] == "alex")
        self.assertIn({"project_id": self.project_id, "agent": "builder"}, builder["aliases"])
        person = next(person for person in snapshot["collaborators"] if person["identity"] == "user")
        self.assertEqual(person["actor_type"], "human")

    def test_rebinding_cannot_rewrite_captured_human_author_or_mentions(self):
        task_discovery.bind_identity(self.root, "builder", "alex")
        task_discovery.bind_identity(self.root, "user", "person-first")
        self.human()
        task_discovery.bind_identity(self.root, "builder", "bob", replace=True)
        task_discovery.bind_identity(self.root, "user", "person-second", replace=True)
        event = self.detail()["history"][0]
        self.assertEqual(event["identity"], "person-first")
        self.assertEqual(event["recipients"], [{"agent": "builder", "identity": "alex"}])
        people = ui_server.snapshot()["collaborators"]
        self.assertTrue(any(person["identity"] == "person-first" and person["actor_type"] == "human" for person in people))
        self.assertFalse(any(person["identity"] == "person-second" for person in people))

    @unittest.skipIf(os.name == "nt", "symlink creation may need Windows permissions")
    def test_registered_root_rejects_manifest_turn_signal_and_state_escapes(self):
        thread = self.root / ".ai-collab/threads/feature"
        outside = self.base / "outside.json"
        manifest = thread / "thread.json"
        original = manifest.read_bytes()
        payload = json.loads(original)
        payload["objective"] = "OUTSIDE_REGISTERED_ROOT"
        outside.write_text(json.dumps(payload))
        manifest.unlink()
        manifest.symlink_to(outside)
        with self.assertRaises(collab.CollaborationError):
            self.detail()
        snapshot = ui_server.snapshot()
        self.assertNotIn("OUTSIDE_REGISTERED_ROOT", json.dumps(snapshot))
        manifest.unlink()
        manifest.write_bytes(original)
        document = self.make_turn()
        target = self.root / document
        outside_turn = self.base / "outside.md"
        outside_turn.write_text(target.read_text().replace("Evidence:", "PRIVATE_EXTERNAL_TEXT"))
        target.unlink()
        target.symlink_to(outside_turn)
        snapshot = ui_server.snapshot()
        self.assertNotIn("PRIVATE_EXTERNAL_TEXT", json.dumps(snapshot))
        self.assertTrue(snapshot["projects"][0]["threads"][0]["turns"][0]["error"])
        target.unlink()
        target.write_text(outside_turn.read_text().replace("PRIVATE_EXTERNAL_TEXT", "Evidence:"))
        self.cli("publish", "--thread-id", "feature", "--agent", "lead", "--to", "builder", "--document", document)
        signal = thread / "signals/to-builder.json"
        outside_signal = self.base / "outside-signal.json"
        outside_signal.write_bytes(signal.read_bytes())
        signal.unlink()
        signal.symlink_to(outside_signal)
        self.assertEqual(collab.inspect_inbox(self.root, "builder", "feature")["decision"], "INVALID")
        state = thread / "state/builder.json"
        outside_state = self.base / "outside-state.json"
        outside_state.write_text(json.dumps({"protocol_version": 1, "agent": "builder", "processed_message_ids": [], "updated_at": collab.now_iso()}))
        state.symlink_to(outside_state)
        with self.assertRaises(collab.CollaborationError):
            collab.read_state(self.root, "feature", "builder")

    def test_human_record_cli_is_explicit_recording_not_transport_send(self):
        payload = {"event_id": "manual-cli", "actor_type": "human", "author": "user",
                   "body": "Original text explicitly supplied for manual entry", "created_at": "2026-10-30T09:00:00Z",
                   "recipients": ["builder"], "provenance": {"kind": "manual", "supplied_authorized": True, "original_text": True}}
        source = self.root / ".ai-collab/human-input.json"
        source.write_text(json.dumps(payload))
        result = self.cli("human-record", "--thread-id", "feature", "--event-file", ".ai-collab/human-input.json")
        self.assertEqual(result["result"], "RECORDED")
        self.assertEqual(collab.inspect_inbox(self.root, "builder", "feature")["decision"], "EMPTY")
        event = self.detail()["history"][0]
        self.assertEqual(event["actor_type"], "human")
        self.assertIsNone(event["provenance"]["source_conversation_id"])
        self.assertIn("Manual", event["attribution"])

    def test_api_is_read_only_and_allowlisted_and_no_pending_inferred_active(self):
        self.make_turn()
        port = self.start_server()
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))
        detail = self.request(port, f"/api/thread?project_id={self.project_id}&thread_id=feature")
        self.assertEqual(detail["thread"]["thread_id"], "feature")
        activity = self.request(port, "/api/activity")
        self.assertEqual(activity["runs"], [])
        self.assertEqual(activity["identities"], [])
        for path in ["/api/activity?root=/", "/api/thread?project_id=not-registered&thread_id=feature",
                     f"/api/thread?project_id={self.project_id}&thread_id=../outside",
                     f"/api/thread?project_id={self.project_id}&thread_id=feature&thread_id=other"]:
            with self.assertRaises(HTTPError) as raised:
                self.request(port, path)
            self.assertEqual(raised.exception.code, 400)
        with self.assertRaises(HTTPError) as raised:
            self.request(port, "/api/activity", method="POST")
        self.assertEqual(raised.exception.code, 404)
        self.assertEqual(before, sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*")))


if __name__ == "__main__":
    unittest.main()
