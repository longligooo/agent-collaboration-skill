"""Local-only human captures and git-sync's non-destructive privacy boundary."""
import json
import shutil
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
import task_model


@unittest.skipUnless(shutil.which("git"), "git is required")
class HumanPrivacyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "repo"
        self.remote = self.base / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True)
        subprocess.run(["git", "clone", "-q", str(self.remote), str(self.root)], check=True, capture_output=True)
        for key, value in (("user.name", "Test"), ("user.email", "test@example.invalid")):
            self.git("config", key, value)
        self.write("product.txt", "initial\n")
        self.git("add", "product.txt")
        self.git("commit", "-qm", "synthetic fixture")
        self.git("push", "-qu", "origin", "HEAD")
        self.thread = ".ai-collab/threads/feature"
        self.legacy = self.thread + "/human-events/human-1.json"
        self.local = ".ai-collab/local/human-events/feature/human-1.json"
        self.write(self.thread + "/thread.json", json.dumps({
            "protocol_version": 1, "thread_id": "feature", "topic": "Synthetic privacy",
            "objective": "Ticket 04 privacy", "participants": ["lead", "worker"],
            "coordinator": "lead", "created_at": "2026-10-30T09:00:00+00:00",
        }))
        self.payload = {"event_id": "human-1", "actor_type": "human", "author": "human-owner",
                        "body": "SYNTHETIC_PRIVATE_ORIGINAL", "created_at": "2026-10-30T09:01:00Z",
                        "recipients": ["worker"], "provenance": {"kind": "manual", "supplied_authorized": True, "original_text": True}}

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        result = subprocess.run(["git", *args], cwd=self.root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def remote_git(self, *args):
        result = subprocess.run(["git", "--git-dir", str(self.remote), *args], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def publish(self):
        with mock.patch("collab.log_event") as log:
            collab.git_publish_thread(self.root, "feature", "synthetic shareable thread")
        return [call.args for call in log.call_args_list], [call.kwargs for call in log.call_args_list]

    def test_new_capture_is_local_ignored_and_still_changes_recovery_fingerprint(self):
        before = task_model.capture_context(self.root, "feature")
        human_events.record_human_event(self.root, "feature", self.payload)
        self.assertTrue((self.root / self.local).exists())
        self.assertFalse((self.root / self.legacy).exists())
        self.assertEqual(self.git("check-ignore", self.local), self.local)
        self.assertNotEqual(before["messages_fingerprint"], task_model.capture_context(self.root, "feature")["messages_fingerprint"])
        self.assertEqual(before["code_version"], task_model.capture_context(self.root, "feature")["code_version"])
        self.assertEqual(self.publish()[0], [])
        files = self.remote_git("ls-tree", "-r", "--name-only", "HEAD")
        self.assertIn(self.thread + "/thread.json", files)
        self.assertNotIn("human-events", files)
        self.assertNotIn(".human-events.lock", files)
        self.assertTrue((self.root / self.local).exists())

    def test_local_event_symlink_rejected_by_history_and_fingerprint(self):
        outside = self.base / "synthetic-outside.json"
        outside.write_text("SYNTHETIC_EXTERNAL_DO_NOT_READ")
        path = self.root / self.local
        path.parent.mkdir(parents=True)
        try:
            path.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        report = human_events.read_human_events(self.root, "feature")
        self.assertTrue(report["errors"])
        self.assertNotIn(outside.read_text(), json.dumps(report))
        with self.assertRaises(collab.CollaborationError):
            task_model.capture_context(self.root, "feature")

    def test_local_store_alias_is_rejected_even_inside_repository(self):
        actual = self.root / "synthetic-alias"
        actual.mkdir()
        path = self.root / ".ai-collab/local"
        try:
            path.symlink_to(actual, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with self.assertRaises(collab.CollaborationError):
            task_model.capture_context(self.root, "feature")

    def test_legacy_read_and_duplicate_preserve_bytes_without_migration(self):
        event = human_events.record_human_event(self.root, "feature", self.payload)["event"]
        local_path = self.root / self.local
        legacy_path = self.write(self.legacy, local_path.read_text())
        local_path.unlink()  # Fixture representing only pre-fix storage.
        old_bytes = legacy_path.read_bytes()
        self.assertEqual(human_events.read_human_events(self.root, "feature")["events"], [event])
        self.assertEqual(human_events.record_human_event(self.root, "feature", self.payload)["result"], "DUPLICATE")
        self.assertFalse(local_path.exists())
        self.assertEqual(legacy_path.read_bytes(), old_bytes)
        self.assertEqual(self.publish()[0], [])
        self.assertNotIn("human-events", self.remote_git("ls-tree", "-r", "--name-only", "HEAD"))

    def test_identical_local_and_legacy_copy_is_one_event_conflict_is_error(self):
        event = human_events.record_human_event(self.root, "feature", self.payload)["event"]
        legacy = self.write(self.legacy, (self.root / self.local).read_text())
        report = human_events.read_human_events(self.root, "feature")
        self.assertEqual(report, {"events": [event], "errors": []})
        changed = json.loads(legacy.read_text())
        changed["body"] = "conflicting synthetic original"
        legacy.write_text(json.dumps(changed))
        self.assertTrue(human_events.read_human_events(self.root, "feature")["errors"])
        with self.assertRaises(collab.CollaborationError):
            human_events.record_human_event(self.root, "feature", self.payload)

    def test_staged_new_private_and_business_files_are_not_committed_or_reset(self):
        private = self.write(self.legacy, "SYNTHETIC_STAGED_PRIVATE")
        local = self.write(self.local, "SYNTHETIC_LOCAL_PRIVATE")
        self.write("product.txt", "staged business edit\n")
        self.git("add", "-f", self.legacy, self.local, "product.txt")
        before = {name: self.git("show", ":" + name) for name in (self.legacy, self.local, "product.txt")}
        self.assertEqual(self.publish()[0], [])
        self.assertEqual({name: self.git("show", ":" + name) for name in before}, before)
        self.assertEqual(private.read_text(), "SYNTHETIC_STAGED_PRIVATE")
        self.assertEqual(local.read_text(), "SYNTHETIC_LOCAL_PRIVATE")
        files = self.remote_git("ls-tree", "-r", "--name-only", "HEAD")
        self.assertNotIn("human-events", files)
        self.assertEqual(self.remote_git("show", "HEAD:product.txt"), "initial")

    def test_previously_published_private_modification_is_excluded_without_deletion(self):
        self.write(self.legacy, "ALREADY_PUBLISHED_SYNTHETIC_ORIGINAL")
        self.git("add", "-f", self.thread)
        self.git("commit", "-qm", "legacy synthetic history already shared")
        self.git("push", "-q")
        private = self.write(self.legacy, "UNSHARED_SYNTHETIC_MODIFICATION")
        self.git("add", "-f", self.legacy)
        staged_before = self.git("show", ":" + self.legacy)
        self.write(self.thread + "/turns/new.md", "shareable synthetic turn")
        self.assertEqual(self.publish()[0], [])
        self.assertEqual(self.git("show", ":" + self.legacy), staged_before)
        self.assertEqual(private.read_text(), "UNSHARED_SYNTHETIC_MODIFICATION")
        self.assertEqual(self.remote_git("show", "HEAD:" + self.legacy), "ALREADY_PUBLISHED_SYNTHETIC_ORIGINAL")
        self.assertEqual(self.remote_git("show", "HEAD:" + self.thread + "/turns/new.md"), "shareable synthetic turn")

    def test_private_unpushed_ancestor_blocks_even_after_later_deletion(self):
        baseline = self.remote_git("rev-parse", "HEAD")
        self.write(self.legacy, "SYNTHETIC_UNPUSHED_PRIVATE")
        self.git("add", "-f", self.legacy)
        self.git("commit", "-qm", "private synthetic ancestor")
        self.git("rm", self.legacy)
        self.git("commit", "-qm", "fixture deletion does not erase ancestor")
        local_head = self.git("rev-parse", "HEAD")
        events, fields = self.publish()
        self.assertIn(("GIT_ERROR",), events)
        self.assertEqual(fields[0]["operation"], "privacy-check")
        self.assertEqual(self.remote_git("rev-parse", "HEAD"), baseline)
        self.assertEqual(self.git("rev-parse", "HEAD"), local_head)

    def test_private_other_thread_and_local_paths_block_outgoing_history(self):
        for name in (".ai-collab/threads/other/human-events/input.json", ".ai-collab/local/import.json"):
            with self.subTest(name=name):
                clean = self.git("rev-parse", "HEAD")
                self.write(name, "SYNTHETIC_PRIVATE_OTHER_THREAD")
                self.git("add", "-f", name)
                self.git("commit", "-qm", "private synthetic ancestor")
                self.assertEqual(self.publish()[1][0]["operation"], "privacy-check")
                # Test-only recovery; production never rewrites this history.
                self.git("reset", "--soft", clean)
                self.git("reset", "--", name)

    def test_remote_reset_does_not_make_stale_tracking_ref_a_privacy_bypass(self):
        old_remote = self.remote_git("rev-parse", "HEAD")
        self.write(self.legacy, "SYNTHETIC_ONCE_PUBLISHED_PRIVATE")
        self.git("add", "-f", self.legacy)
        self.git("commit", "-qm", "synthetic private history")
        self.git("push", "-q")
        ref = self.remote_git("symbolic-ref", "HEAD")
        self.remote_git("update-ref", ref, old_remote)
        self.assertEqual(self.publish()[1][0]["operation"], "privacy-check")
        self.assertEqual(self.remote_git("rev-parse", "HEAD"), old_remote)

    def test_distinct_push_url_is_checked_instead_of_fetch_remote(self):
        clean = self.git("rev-parse", "HEAD")
        self.write(self.legacy, "SYNTHETIC_FETCH_REMOTE_PRIVATE")
        self.git("add", "-f", self.legacy)
        self.git("commit", "-qm", "synthetic fetch-only private history")
        self.git("push", "-q")
        recipient = self.base / "different-recipient.git"
        subprocess.run(["git", "clone", "--bare", "-q", str(self.remote), str(recipient)], check=True)
        branch = self.git("symbolic-ref", "HEAD")
        subprocess.run(["git", "--git-dir", str(recipient), "update-ref", branch, clean], check=True)
        self.git("config", "remote.origin.pushurl", str(recipient))
        self.assertEqual(self.publish()[1][0]["operation"], "privacy-check")
        self.assertEqual(subprocess.check_output(["git", "--git-dir", str(recipient), "rev-parse", "HEAD"], text=True).strip(), clean)

    def test_multiple_push_urls_fail_closed(self):
        second = self.base / "second.git"
        subprocess.run(["git", "clone", "--bare", "-q", str(self.remote), str(second)], check=True)
        self.git("config", "--add", "remote.origin.pushurl", str(self.remote))
        self.git("config", "--add", "remote.origin.pushurl", str(second))
        before = self.git("rev-parse", "HEAD")
        self.assertEqual(self.publish()[1][0]["operation"], "privacy-check")
        self.assertEqual(self.git("rev-parse", "HEAD"), before)

    def test_push_instead_of_is_resolved_and_checked(self):
        clean = self.git("rev-parse", "HEAD")
        recipient = self.base / "rewritten-recipient.git"
        subprocess.run(["git", "clone", "--bare", "-q", str(self.remote), str(recipient)], check=True)
        self.git("config", "url." + str(recipient) + ".pushInsteadOf", str(self.remote))
        self.assertEqual(self.publish()[0], [])
        self.assertEqual(self.remote_git("rev-parse", "HEAD"), clean)
        recipient_files = subprocess.check_output(["git", "--git-dir", str(recipient), "ls-tree", "-r", "--name-only", "HEAD"], text=True)
        self.assertIn(self.thread + "/thread.json", recipient_files)

    def test_missing_upstream_fails_closed_without_changing_index(self):
        self.git("branch", "--unset-upstream")
        before = self.git("ls-files", "--stage")
        self.assertEqual(self.publish()[1][0]["operation"], "privacy-check")
        self.assertEqual(self.git("ls-files", "--stage"), before)

    def test_configured_extra_push_refspecs_fail_closed(self):
        self.git("branch", "private-branch")
        self.git("config", "remote.origin.push", "refs/heads/*:refs/heads/*")
        before = self.remote_git("rev-parse", "HEAD")
        self.assertEqual(self.publish()[1][0]["operation"], "privacy-check")
        self.assertEqual(self.remote_git("rev-parse", "HEAD"), before)

    def test_automatic_tags_are_not_implicitly_published(self):
        branch = self.git("symbolic-ref", "--short", "HEAD")
        self.git("tag", "-am", "synthetic tag", "private-tag")
        self.git("config", "push.followTags", "true")
        self.assertEqual(self.publish()[0], [])
        self.assertEqual(self.remote_git("for-each-ref", "--format=%(refname)"), "refs/heads/" + branch)

    def test_alternate_push_remote_policies_fail_closed(self):
        recipient = self.base / "fork.git"
        subprocess.run(["git", "clone", "--bare", "-q", str(self.remote), str(recipient)], check=True)
        self.git("remote", "add", "fork", str(recipient))
        branch = self.git("symbolic-ref", "--short", "HEAD")
        before = self.remote_git("rev-parse", "HEAD")
        for key in ("branch." + branch + ".pushRemote", "remote.pushDefault"):
            with self.subTest(key=key):
                self.git("config", key, "fork")
                self.assertEqual(self.publish()[1][0]["operation"], "privacy-check")
                self.assertEqual(self.remote_git("rev-parse", "HEAD"), before)
                self.assertEqual(subprocess.check_output(["git", "--git-dir", str(recipient), "rev-parse", "HEAD"], text=True).strip(), before)
                self.git("config", "--unset", key)

    def test_simple_policy_does_not_publish_to_differently_named_upstream(self):
        self.git("branch", "-m", "different-local-name")
        before = self.remote_git("rev-parse", "HEAD")
        self.assertEqual(self.publish()[1][0]["operation"], "privacy-check")
        self.assertEqual(self.remote_git("rev-parse", "HEAD"), before)

    def test_replacement_ref_cannot_hide_private_outgoing_history(self):
        clean = self.git("rev-parse", "HEAD")
        clean_tree = self.git("rev-parse", "HEAD^{tree}")
        self.write(self.legacy, "SYNTHETIC_REPLACED_PRIVATE_HISTORY")
        self.git("add", "-f", self.legacy)
        self.git("commit", "-qm", "synthetic private commit")
        private_commit = self.git("rev-parse", "HEAD")
        substitute = self.git("commit-tree", clean_tree, "-p", clean, "-m", "synthetic benign replacement")
        self.git("replace", private_commit, substitute)
        self.assertEqual(self.publish()[1][0]["operation"], "privacy-check")
        self.assertEqual(self.remote_git("rev-parse", "HEAD"), clean)


if __name__ == "__main__":
    unittest.main()
