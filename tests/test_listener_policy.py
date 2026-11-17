from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import listener_policy as policy


class ListenerPolicyTests(unittest.TestCase):
    @patch.object(policy.collab, "load_manifest", return_value={"participants": ["me", "one", "two"]})
    @patch.object(policy.session_feedback, "targets", return_value={"targets": [
        {"participant": "me", "client": "codex", "vendor": "codex"},
        {"participant": "one", "client": "codex", "vendor": "listener"},
        {"participant": "two", "client": "cursor", "vendor": "listener"}]})
    def test_route_relative_to_selected_peers(self, targets, manifest):
        root = Path(".")
        direct = policy.plan(root, "t", "me", "codex", ["one"])
        self.assertEqual((direct["mode"], direct["interval_minutes"]), ("direct", None))
        relay = policy.plan(root, "t", "me", "codex", ["one", "two"])
        self.assertEqual(("shared_relay", "luna", 2, ["two"], ["one"]),
            (relay["mode"], relay["relay_model"], relay["interval_minutes"], relay["poll_peers"], relay["direct_peers"]))
        local = policy.plan(root, "t", "me", "cursor", ["one"])
        self.assertEqual(("self", 5, 60), (local["mode"], local["interval_minutes"], local["idle_minutes"]))
        custom = policy.plan(root, "t", "me", "codex", ["two"], relay_interval=3, idle_minutes=45)
        self.assertEqual((3, 45), (custom["interval_minutes"], custom["idle_minutes"]))

    @patch.object(policy.collab, "inspect_inbox")
    def test_repeat_does_not_extend_idle_and_published_closure_stops(self, inspect):
        start = datetime(2026, 11, 17, tzinfo=timezone.utc)
        now = start + timedelta(minutes=60)
        config = {"thread_id": "tmp", "participant": "me", "mode": "shared_relay",
                  "poll_peers": ["two"], "idle_minutes": 60}
        message = {"thread_id": "tmp", "round": 3, "from": "two", "to": "me",
                   "message_id": "id", "status": "OPEN", "created_at": now.isoformat()}
        inspect.return_value = {"results": [{"decision": "NEW", "message": message}]}
        root = Path("root-with-no-stop")
        self.assertEqual("idle_timeout", policy.step(root, config, start.isoformat(), None, "id", now=now)["reason"])
        self.assertEqual(now.isoformat(), policy.step(root, config, start.isoformat(), None, now=now)["last_new_at"])
        self.assertEqual("stop", policy.step(root, config, start.isoformat(), None, completed=True, now=now)["state"])
        message["status"] = "CLOSED"
        inspect.return_value["results"][0]["decision"] = "TERMINAL"
        self.assertEqual("stop_after_delivery", policy.step(root, config, start.isoformat(), None, now=now)["state"])
