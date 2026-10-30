"""Explicit human provenance is never inferred from an agent turn or label."""
import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import collab
import human_events
import task_discovery


class HumanEventsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.make_thread("feature", ["owner", "lead", "worker"])
        self.directory = self.root / ".ai-collab/threads/feature/human-events"

    def tearDown(self):
        self.temporary.cleanup()

    def make_thread(self, identifier, participants):
        path = self.root / ".ai-collab/threads" / identifier
        for name in ("turns", "signals", "state"):
            (path / name).mkdir(parents=True)
        collab.atomic_write_json(path / "thread.json", {
            "protocol_version": 1, "thread_id": identifier, "topic": "Human provenance",
            "objective": "Ticket 02 human provenance", "participants": participants,
            "coordinator": "lead" if "lead" in participants else None,
            "created_at": "2026-10-30T09:00:00+00:00",
        })

    def payload(self, identifier="human-1", kind="manual", **changes):
        provenance = {"kind": kind, "supplied_authorized": True, "original_text": True}
        if kind == "authorized-import":
            provenance.update(source_conversation_id="conversation-1", source_message_id="message-1",
                              source_author="Provided source author")
        result = {"schema_version": 1, "event_id": identifier, "thread_id": "feature",
                  "actor_type": "human", "author": "owner", "body": "请先完成验证。\n 保留原文空格。 ",
                  "created_at": "2026-10-30T09:01:00+00:00", "recipients": ["lead", "worker"],
                  "provenance": provenance}
        result.update(changes)
        return result

    def record(self, payload=None, thread="feature"):
        return human_events.record_human_event(self.root, thread, payload or self.payload())

    def read(self, thread="feature"):
        return human_events.read_human_events(self.root, thread)

    def test_manual_record_is_explicit_and_honestly_attributed(self):
        original = self.payload()
        event = self.record(original)["event"]
        self.assertEqual(event["body"], original["body"])
        self.assertEqual(event["kind"], "HUMAN_INPUT")
        self.assertEqual(event["role"], "human")
        self.assertEqual(event["author_id"], "owner")
        self.assertIn("Manual", event["attribution_label"])
        self.assertIn("not a verified external capture", event["attribution_label"])
        self.assertIsNone(event["provenance"]["source_message_id"])
        self.assertTrue(event["recorded_at"])
        self.assertEqual(self.read(), {"events": [event], "errors": []})

    def test_authorized_import_retains_provided_original_source(self):
        event = self.record(self.payload(kind="authorized-import"))["event"]
        self.assertEqual(event["provenance"]["source_message_id"], "message-1")
        self.assertIn("not independently verified", event["attribution_label"])
        self.assertEqual(event["created_at"], "2026-10-30T09:01:00+00:00")

    def test_import_requires_all_source_identifiers(self):
        for field in human_events.SOURCE_FIELDS:
            with self.subTest(field=field):
                payload = self.payload(kind="authorized-import")
                del payload["provenance"][field]
                with self.assertRaises(collab.CollaborationError):
                    self.record(payload)
        self.assertFalse(self.directory.exists())

    def test_paraphrase_and_unconfirmed_provenance_are_not_promoted_to_human(self):
        cases = [self.payload(actor_type="agent"), self.payload(agent_paraphrase=True),
                 self.payload(body=""), self.payload(attribution_label="Verified original human")]
        for field, value in [("original_text", False), ("supplied_authorized", False),
                             ("supplied_authorized", 1), ("kind", "unknown"), ("kind", "agent-summary")]:
            payload = self.payload()
            payload["provenance"][field] = value
            cases.append(payload)
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(collab.CollaborationError):
                    self.record(payload)
        self.assertEqual(self.read()["events"], [])

    def test_missing_actor_type_is_not_inferred_from_author(self):
        payload = self.payload()
        del payload["actor_type"]
        with self.assertRaises(collab.CollaborationError):
            self.record(payload)

    def test_author_id_is_validated_and_recipients_must_belong_to_thread(self):
        for changes in ({"author": "../invalid"}, {"recipients": ["outsider"]},
                        {"recipients": []}, {"recipients": ["lead", "lead"]},
                        {"thread_id": "another-thread"}):
            with self.subTest(changes=changes):
                with self.assertRaises(collab.CollaborationError):
                    self.record(self.payload(**changes))

    def test_explicit_human_author_is_allowed_outside_legacy_agent_manifest(self):
        self.make_thread("legacy", ["lead", "worker"])
        manifest_path = self.root / ".ai-collab/threads/legacy/thread.json"
        manifest_before = manifest_path.read_bytes()
        task_discovery.bind_identity(self.root, "human-alice", "alice")
        event = self.record(self.payload(thread_id="legacy", author="human-alice"), thread="legacy")["event"]
        self.assertEqual(event["author_id"], "alice")
        self.assertEqual(manifest_path.read_bytes(), manifest_before)
        self.assertEqual(list((manifest_path.parent / "signals").iterdir()), [])
        self.assertEqual(list((manifest_path.parent / "state").iterdir()), [])
        self.assertFalse((manifest_path.parent / "task.json").exists())

    def test_created_at_requires_valid_timezone_aware_source_timestamp(self):
        for timestamp in ("not a date", "2026-10-30T09:00:00", None):
            with self.subTest(timestamp=timestamp):
                with self.assertRaises(collab.CollaborationError):
                    self.record(self.payload(created_at=timestamp))

    def test_source_timestamps_normalize_to_utc_for_merged_history(self):
        first = self.record(self.payload(created_at="2026-10-30T10:01:00+01:00"))["event"]
        self.assertEqual(first["created_at"], "2026-10-30T09:01:00+00:00")
        self.record(self.payload(identifier="human-2", created_at="2026-10-30T09:00:00Z"))
        self.assertEqual([item["event_id"] for item in self.read()["events"]], ["human-2", "human-1"])
        repeat = self.record(self.payload(created_at="2026-10-30T09:01:00Z"))
        self.assertEqual(repeat["result"], "DUPLICATE")

    def test_exact_duplicate_is_noop_and_does_not_change_recorded_time(self):
        first = self.record()
        path = self.directory / "human-1.json"
        before = path.read_bytes()
        repeated = self.record()
        self.assertEqual(repeated["result"], "DUPLICATE")
        self.assertEqual(repeated["event"], first["event"])
        self.assertEqual(path.read_bytes(), before)

    def test_changed_payload_for_existing_id_is_rejected(self):
        self.record()
        before = (self.directory / "human-1.json").read_bytes()
        with self.assertRaises(collab.CollaborationError):
            self.record(self.payload(body="Changed body"))
        self.assertEqual((self.directory / "human-1.json").read_bytes(), before)

    def test_same_import_source_under_new_event_id_is_deduplicated(self):
        first = self.record(self.payload(kind="authorized-import"))
        duplicate = self.record(self.payload(identifier="human-2", kind="authorized-import"))
        self.assertEqual(duplicate["result"], "DUPLICATE")
        self.assertEqual(duplicate["event"], first["event"])
        self.assertEqual(len(list(self.directory.glob("*.json"))), 1)

    def test_same_source_cannot_be_reused_with_changed_body_or_mapping(self):
        self.record(self.payload(kind="authorized-import"))
        for changes in ({"body": "A paraphrase"}, {"author": "worker"}, {"recipients": ["lead"]}):
            with self.subTest(changes=changes):
                with self.assertRaises(collab.CollaborationError):
                    self.record(self.payload(identifier="human-2", kind="authorized-import", **changes))
        self.assertEqual(len(self.read()["events"]), 1)

    def test_same_source_can_be_explicitly_mapped_to_another_thread(self):
        self.record(self.payload(kind="authorized-import"))
        self.make_thread("second", ["owner", "other"])
        other = self.payload(kind="authorized-import", thread_id="second", recipients=["other"])
        result = self.record(other, thread="second")
        self.assertEqual(result["event"]["thread_id"], "second")
        self.assertEqual(result["event"]["recipients"], ["other"])
        self.assertEqual(len(self.read()["events"]), 1)
        self.assertEqual(len(self.read("second")["events"]), 1)

    def test_identity_is_captured_not_rewritten_after_rebinding(self):
        task_discovery.bind_identity(self.root, "owner", "alice")
        first = self.record()["event"]
        task_discovery.bind_identity(self.root, "owner", "alice-new", replace=True)
        self.assertEqual(self.read()["events"][0]["author_id"], "alice")
        self.assertEqual(self.record()["event"], first)
        self.assertEqual(self.record(self.payload(identifier="human-2"))["event"]["author_id"], "alice-new")

    def test_recipient_identities_are_captured_before_alias_rebinding(self):
        task_discovery.bind_identity(self.root, "worker", "alex")
        first = self.record(self.payload(kind="authorized-import"))["event"]
        self.assertEqual(first["recipient_identities"], {"lead": "lead", "worker": "alex"})
        task_discovery.bind_identity(self.root, "worker", "bob", replace=True)
        self.assertEqual(self.read()["events"][0]["recipient_identities"], first["recipient_identities"])
        repeated = self.record(self.payload(identifier="human-2", kind="authorized-import"))
        self.assertEqual(repeated["result"], "DUPLICATE")
        self.assertEqual(repeated["event"], first)
        new_event = self.record(self.payload(identifier="human-3"))["event"]
        self.assertEqual(new_event["recipient_identities"]["worker"], "bob")

    def test_persisted_recipient_identity_mapping_must_be_exact_and_valid(self):
        self.record()
        path = self.directory / "human-1.json"
        original = json.loads(path.read_text())
        invalid = [None, {}, {"lead": "lead"}, {"lead": "lead", "worker": "../escape"},
                   {"lead": "lead", "worker": "worker", "outsider": "outsider"},
                   {"lead": "lead", "worker": 123}]
        for mapping in invalid:
            with self.subTest(mapping=mapping):
                event = {**original, "recipient_identities": mapping}
                path.write_text(json.dumps(event))
                report = self.read()
                self.assertEqual(report["events"], [])
                self.assertTrue(report["errors"])
        del original["recipient_identities"]
        path.write_text(json.dumps(original))
        self.assertEqual(self.read()["events"], [])
        self.assertTrue(self.read()["errors"])

    def test_caller_cannot_spoof_generated_identity_or_capture_timestamp(self):
        for changes in ({"identity": "admin"}, {"recorded_at": "2000-01-01T00:00:00Z"},
                        {"recipient_identities": {"worker": "admin"}}):
            with self.subTest(changes=changes):
                with self.assertRaises(collab.CollaborationError):
                    self.record(self.payload(**changes))

    def test_old_thread_with_no_events_is_read_without_writes(self):
        before = sorted(self.root.rglob("*"))
        self.assertEqual(self.read(), {"events": [], "errors": []})
        self.assertEqual(sorted(self.root.rglob("*")), before)

    def test_invalid_persisted_record_is_reported_not_promoted(self):
        self.directory.mkdir()
        (self.directory / "untrusted.json").write_text(json.dumps({"actor_type": "human", "body": "Trust me"}))
        report = self.read()
        self.assertEqual(report["events"], [])
        self.assertEqual(len(report["errors"]), 1)
        with self.assertRaises(collab.CollaborationError):
            self.record()

    def test_tampered_attribution_is_rejected(self):
        self.record()
        path = self.directory / "human-1.json"
        event = json.loads(path.read_text())
        event["attribution_label"] = "Authenticated external human original"
        path.write_text(json.dumps(event))
        self.assertEqual(self.read()["events"], [])
        self.assertTrue(self.read()["errors"])

    def test_event_id_path_traversal_and_absolute_paths_are_rejected(self):
        for identifier in ("../outside", "/tmp/outside", "human/../../outside", ".", ""):
            with self.subTest(identifier=identifier):
                with self.assertRaises(collab.CollaborationError):
                    self.record(self.payload(identifier=identifier))

    def test_storage_directory_symlink_is_rejected_without_reading_outside(self):
        with tempfile.TemporaryDirectory() as outside:
            try:
                self.directory.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            with self.assertRaises(collab.CollaborationError):
                self.record()
            self.assertTrue(self.read()["errors"])
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_lock_symlink_is_rejected_without_changing_target(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "outside.lock"
            target.write_text("preserve")
            try:
                (self.directory.parent / ".human-events.lock").symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            with self.assertRaises(collab.CollaborationError):
                self.record()
            self.assertEqual(target.read_text(), "preserve")

    def test_event_file_symlink_is_reported_without_reading_target(self):
        self.directory.mkdir()
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "outside.json"
            target.write_text("sensitive target")
            try:
                (self.directory / "human-1.json").symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            report = self.read()
            self.assertEqual(report["events"], [])
            self.assertTrue(report["errors"])
            self.assertNotIn("sensitive target", json.dumps(report))

    def test_interrupted_publication_leaves_no_partial_event(self):
        with mock.patch("human_events.os.link", side_effect=OSError("simulated interruption")):
            with self.assertRaises(collab.CollaborationError):
                self.record()
        self.assertEqual(self.read(), {"events": [], "errors": []})
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertEqual(self.record()["result"], "RECORDED")

    def test_concurrent_process_imports_deduplicate_atomically(self):
        code = ("import json,sys;from pathlib import Path;"
                f"sys.path.insert(0,{str(SCRIPTS)!r});import human_events;"
                "print(json.dumps(human_events.record_human_event(Path(sys.argv[1]),'feature',json.loads(sys.argv[2]))))")
        processes = [subprocess.Popen([sys.executable, "-c", code, str(self.root),
                                      json.dumps(self.payload(identifier=f"human-{index}", kind="authorized-import"))],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for index in range(5)]
        results = []
        for process in processes:
            output, errors = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, errors)
            results.append(json.loads(output))
        self.assertEqual(sum(item["result"] == "RECORDED" for item in results), 1)
        self.assertEqual(sum(item["result"] == "DUPLICATE" for item in results), 4)
        self.assertEqual(len(self.read()["events"]), 1)
        self.assertFalse(self.read()["errors"])

    def test_distinct_events_append_without_overwriting(self):
        first = self.record()["event"]
        self.record(self.payload(identifier="human-2", body="Another explicit input"))
        self.assertEqual(len(self.read()["events"]), 2)
        self.assertEqual(self.read()["events"][0], first)

    def test_source_identifiers_are_inert_strings_not_fetch_requests(self):
        payload = self.payload(kind="authorized-import")
        payload["provenance"].update(source_conversation_id="https://example.invalid/private",
                                     source_message_id="$(touch forbidden)")
        with mock.patch("subprocess.run", side_effect=AssertionError("must not execute")):
            event = self.record(payload)["event"]
        self.assertEqual(event["provenance"]["source_message_id"], "$(touch forbidden)")
        self.assertFalse((self.root / "forbidden").exists())

    def test_cli_parser_exposes_explicit_record_only(self):
        parser = argparse.ArgumentParser()
        human_events.add_parsers(parser.add_subparsers(dest="command", required=True))
        path = self.root / ".ai-collab/human-input.json"
        path.write_text(json.dumps(self.payload()))
        args = parser.parse_args(["human-record", "--root", str(self.root), "--thread-id", "feature",
                                  "--event-file", ".ai-collab/human-input.json"])
        with mock.patch("collab.emit") as emit:
            args.handler(args)
        self.assertEqual(emit.call_args.args[0]["result"], "RECORDED")


if __name__ == "__main__":
    unittest.main()
