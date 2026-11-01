"""Latest-activity ordering uses actual projections without changing duration."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import collab
import human_events
import ui_read_model
import ui_server


class ThreadActivitySortTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.registry = self.base / "registry.json"

    def tearDown(self):
        self.temporary.cleanup()

    def thread(self, project_name, thread_id="same", created_at="2026-01-01T00:00:00Z"):
        root = self.base / project_name
        directory = root / ".ai-collab" / "threads" / thread_id
        directory.mkdir(parents=True)
        (directory / "thread.json").write_text(json.dumps({
            "protocol_version": 1, "thread_id": thread_id, "topic": thread_id,
            "objective": "Synthetic sorting acceptance", "participants": ["lead", "builder"],
            "coordinator": "lead", "created_at": created_at,
        }))
        ui_server.register_project(root, self.registry)
        return root

    def turn(self, root, timestamp, *, round=1, status="CLOSED"):
        directory = root / ".ai-collab/threads/same/turns"
        directory.mkdir(exist_ok=True)
        metadata = {"round": round, "author": "lead", "role": "reviewer", "kind": "REVIEW",
                    "status": status, "requested_action": "ACK", "created_at": timestamp}
        path = directory / f"round-{round:04d}-from-lead-review.md"
        path.write_text(collab.META_START + json.dumps(metadata) + collab.META_END + "\n# Review\n\n## Summary\nSynthetic turn\n")

    def scan(self, root):
        return ui_server.scan_project({"root": str(root)})["threads"][0]

    @unittest.skipUnless(shutil.which("node"), "Node required for production UI comparator")
    def test_116_plus_1_new_project_thread_is_first_without_losing_collisions(self):
        first = self.thread("a-old", "same")
        for number in range(115):
            self.thread("a-old", f"thread-{number:03}")
        second = self.thread("z-new", "same", "2026-10-30T09:00:00Z")
        snapshot = ui_server.snapshot(self.registry)
        self.assertEqual(len(snapshot["projects"][0]["threads"]), 116)
        self.assertEqual(snapshot["overview"]["threads"], 117)
        model = str(ROOT / "ui/view-model.js")
        source = f"""
const assert=require('node:assert/strict'), M=require({json.dumps(model)});
const snapshot={json.dumps(snapshot)};
const input=snapshot.projects.flatMap(p=>p.threads), before=JSON.stringify(input);
const sorted=M.sortThreads(input);
assert.equal(sorted.length,117);
assert.equal(sorted[0].project_id,{json.dumps(ui_server.project_id(str(second)))});
assert.equal(sorted[0].thread_id,'same');
assert.equal(new Set(sorted.map(t=>JSON.stringify([t.project_id,t.thread_id]))).size,117);
assert.equal(JSON.stringify(input),before);
assert.equal(sorted.filter(t=>t.thread_id==='same').length,2);
"""
        result = subprocess.run([shutil.which("node"), "-"], input=source, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        person = next(person for person in snapshot["collaborators"] if person["identity"] == "lead")
        self.assertEqual(person["threads"][0]["project_id"], ui_server.project_id(str(second)))
        self.assertEqual(self.scan(first)["updated_at"], "")

    def test_human_only_activity_newest_and_closed_duration_unchanged(self):
        root = self.thread("project")
        self.turn(root, "2026-01-02T00:00:00Z")
        before = self.scan(root)
        with mock.patch.object(collab, "now_iso", return_value="2026-10-30T10:00:00Z"):
            human_events.record_human_event(root, "same", {
                "event_id": "new-human", "actor_type": "human", "author": "owner",
                "body": "Synthetic local original", "created_at": "2026-10-30T09:00:00Z",
                "recipients": ["builder"], "provenance": {"kind": "manual", "supplied_authorized": True, "original_text": True},
            })
        after = self.scan(root)
        self.assertEqual(after["last_activity_at"], "2026-10-30T10:00:00+00:00")
        self.assertEqual(after["updated_at"], before["updated_at"])
        self.assertEqual(after["started_at"], before["started_at"])
        self.assertEqual(after["status"], "CLOSED")
        detail = ui_server.thread_detail(ui_server.project_id(str(root)), "same", self.registry)["thread"]
        self.assertEqual(detail["last_activity_at"], after["last_activity_at"])
        self.assertEqual(detail["history"][-1]["created_at"], "2026-10-30T09:00:00+00:00")

    def test_all_agent_timestamps_count_without_changing_last_round_semantics(self):
        root = self.thread("project")
        self.turn(root, "2026-05-01T00:00:00Z", round=1)
        self.turn(root, "2026-02-01T00:00:00Z", round=2)
        record = self.scan(root)
        self.assertEqual(record["updated_at"], "2026-02-01T00:00:00Z")
        self.assertEqual(record["last_activity_at"], "2026-05-01T00:00:00+00:00")
        self.assertEqual(record["round"], 2)

    def test_invalid_dates_ignored_and_equal_instants_have_deterministic_scoped_ties(self):
        records = [
            {"project_id": "b", "thread_id": "same", "last_activity_at": "2026-10-30T09:00:00Z"},
            {"project_id": "a", "thread_id": "z", "last_activity_at": "2026-10-30T10:00:00+01:00"},
            {"project_id": "a", "thread_id": "same", "last_activity_at": "2026-10-30T09:00:00"},
            {"project_id": "invalid", "thread_id": "same", "last_activity_at": "not-a-date"},
            {"project_id": "early", "thread_id": "same", "last_activity_at": "1960-01-01T00:00:00Z"},
        ]
        sorted_records = sorted(records, key=ui_read_model.thread_sort_key)
        self.assertEqual([(t["project_id"], t["thread_id"]) for t in sorted_records],
                         [("a", "same"), ("a", "z"), ("b", "same"), ("early", "same"), ("invalid", "same")])
        for invalid in (None, 12, {}, "", "bad", "2026-02-30T00:00:00Z"):
            self.assertIsNone(ui_read_model.activity_timestamp(invalid))
        self.assertEqual(ui_read_model.last_activity_at({"started_at": "2026-01-01", "updated_at": "bad", "turns": [{"created_at": "9999-01-01", "error": "invalid"}]}), "2026-01-01T00:00:00+00:00")
        if shutil.which("node"):
            source = f"""
const assert=require('node:assert/strict'),M=require({json.dumps(str(ROOT / 'ui/view-model.js'))});
assert.deepEqual(M.sortThreads({json.dumps(records)}),{json.dumps(sorted_records)});
for(const value of [null,12,{{}},'', 'bad','2026-02-30T00:00:00Z','0000-01-01','2026-01-01T24:00:00Z']) assert(Number.isNaN(M.activityTime(value)));
assert.equal(M.lastActivityAt({{last_activity_at:'bad',started_at:'2026-01-01',updated_at:'2026-01-02T00:00:00Z'}}),'2026-01-02T00:00:00Z');
"""
            result = subprocess.run([shutil.which("node"), "-"], input=source, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_turn_timestamp_does_not_break_history_or_activity(self):
        root = self.thread("project")
        self.turn(root, "2026-02-01T00:00:00Z", round=1)
        self.turn(root, 42, round=2)
        self.turn(root, "not-a-date", round=3)
        view = ui_server.thread_detail(ui_server.project_id(str(root)), "same", self.registry)["thread"]
        self.assertEqual(len(view["history"]), 3)
        self.assertEqual(view["last_activity_at"], "2026-02-01T00:00:00+00:00")
        self.assertEqual([turn["round"] for turn in view["history"]], [1, 2, 3])

    def test_invalid_manifest_is_isolated_and_valid_thread_remains_ordered(self):
        root = self.thread("project", "invalid", "not-a-date")
        self.thread("project", "valid", "2026-01-01T00:00:00Z")
        threads = ui_server.scan_project({"root": str(root)})["threads"]
        self.assertEqual([thread["thread_id"] for thread in threads], ["valid", "invalid"])
        self.assertTrue(threads[1]["error"])
        self.assertEqual(threads[1]["last_activity_at"], "")


if __name__ == "__main__":
    unittest.main()
