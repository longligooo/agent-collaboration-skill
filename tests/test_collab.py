import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "collab.py"


class CollaborationCliTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def run_cli(self, *arguments, expected=0):
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            expected,
            msg=f"stdout={completed.stdout}\nstderr={completed.stderr}",
        )
        stream = completed.stdout if expected == 0 else completed.stderr
        return json.loads(stream)

    def initialize(self, thread_id="design-thread"):
        return self.run_cli(
            "init",
            "--root",
            str(self.root),
            "--thread-id",
            thread_id,
            "--topic",
            "Design thread",
            "--objective",
            "Agree on a design",
            "--coordinator",
            "cursor",
            "--participants",
            "cursor",
            "codex",
            "claude-code",
        )

    def create_completed_turn(
        self,
        *,
        author="cursor",
        role="proposer",
        kind="PROPOSAL",
        status="OPEN",
        requested_action="REVIEW",
        round_number=None,
        based_on=None,
    ):
        arguments = [
            "new-turn",
            "--root",
            str(self.root),
            "--thread-id",
            "design-thread",
            "--author",
            author,
            "--role",
            role,
            "--kind",
            kind,
            "--status",
            status,
            "--requested-action",
            requested_action,
            "--title",
            "Test turn",
        ]
        if round_number is not None:
            arguments.extend(["--round", str(round_number)])
        for dependency in based_on or []:
            arguments.extend(["--based-on", dependency])
        result = self.run_cli(*arguments)
        document = self.root / result["document"]
        content = document.read_text(encoding="utf-8")
        document.write_text(
            content.replace("TODO(agent-collaboration):", "Completed:"),
            encoding="utf-8",
            newline="\n",
        )
        return result["document"]

    def test_full_publish_inspect_and_acknowledge_flow(self):
        self.initialize()
        document = self.create_completed_turn()
        validation = self.run_cli(
            "validate-artifact",
            "--root",
            str(self.root),
            "--document",
            document,
        )
        self.assertEqual(validation["result"], "VALID")

        publication = self.run_cli(
            "publish",
            "--root",
            str(self.root),
            "--thread-id",
            "design-thread",
            "--agent",
            "cursor",
            "--to",
            "codex",
            "--document",
            document,
            "--requested-action",
            "REVIEW",
        )
        self.assertEqual(publication["result"], "PUBLISHED")

        inspection = self.run_cli(
            "inspect", "--root", str(self.root), "--agent", "codex"
        )
        self.assertEqual(inspection["decision"], "NEW")

        self.run_cli(
            "acknowledge",
            "--root",
            str(self.root),
            "--thread-id",
            "design-thread",
            "--agent",
            "codex",
            "--message-id",
            publication["message_id"],
        )
        duplicate = self.run_cli(
            "inspect", "--root", str(self.root), "--agent", "codex"
        )
        self.assertEqual(duplicate["decision"], "DUPLICATE")

    def test_terminal_signal_is_reported(self):
        self.initialize()
        document = self.create_completed_turn(
            kind="DECISION",
            role="coordinator",
            status="AGREED",
            requested_action="ACKNOWLEDGE",
        )
        self.run_cli(
            "publish",
            "--root",
            str(self.root),
            "--thread-id",
            "design-thread",
            "--agent",
            "cursor",
            "--to",
            "claude-code",
            "--document",
            document,
        )
        inspection = self.run_cli(
            "inspect", "--root", str(self.root), "--agent", "claude-code"
        )
        self.assertEqual(inspection["decision"], "TERMINAL")

    def test_turns_are_immutable(self):
        self.initialize()
        self.create_completed_turn(round_number=1)
        result = self.run_cli(
            "new-turn",
            "--root",
            str(self.root),
            "--thread-id",
            "design-thread",
            "--author",
            "cursor",
            "--role",
            "proposer",
            "--kind",
            "PROPOSAL",
            "--status",
            "OPEN",
            "--requested-action",
            "REVIEW",
            "--round",
            "1",
            "--title",
            "Duplicate turn",
            expected=2,
        )
        self.assertIn("immutable", result["error"])

    def test_round_number_is_unique_across_authors_and_kinds(self):
        self.initialize()
        self.create_completed_turn(round_number=1)
        result = self.run_cli(
            "new-turn",
            "--root",
            str(self.root),
            "--thread-id",
            "design-thread",
            "--author",
            "codex",
            "--role",
            "reviewer",
            "--kind",
            "REVIEW",
            "--status",
            "OPEN",
            "--requested-action",
            "REVISE",
            "--round",
            "1",
            "--title",
            "Conflicting round",
            expected=2,
        )
        self.assertIn("round 1 already exists", result["error"])

    def test_signal_cannot_be_moved_to_another_thread(self):
        self.initialize()
        self.initialize("other-thread")
        document = self.create_completed_turn()
        self.run_cli(
            "publish",
            "--root",
            str(self.root),
            "--thread-id",
            "design-thread",
            "--agent",
            "cursor",
            "--to",
            "codex",
            "--document",
            document,
        )
        source = (
            self.root
            / ".ai-collab/threads/design-thread/signals/to-codex.json"
        )
        destination = (
            self.root
            / ".ai-collab/threads/other-thread/signals/to-codex.json"
        )
        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

        inspection = self.run_cli(
            "inspect",
            "--root",
            str(self.root),
            "--agent",
            "codex",
            "--thread-id",
            "other-thread",
        )
        self.assertEqual(inspection["decision"], "INVALID")
        self.assertIn("signal path", inspection["results"][0]["error"])

    def test_repository_path_escape_is_rejected(self):
        self.initialize()
        result = self.run_cli(
            "new-turn",
            "--root",
            str(self.root),
            "--thread-id",
            "design-thread",
            "--author",
            "cursor",
            "--role",
            "proposer",
            "--kind",
            "PROPOSAL",
            "--status",
            "OPEN",
            "--requested-action",
            "REVIEW",
            "--based-on",
            "../outside.md",
            "--title",
            "Unsafe turn",
            expected=2,
        )
        self.assertIn("escapes", result["error"])

    def test_publish_marks_incoming_message_after_response(self):
        self.initialize()
        first_document = self.create_completed_turn()
        first_publication = self.run_cli(
            "publish",
            "--root",
            str(self.root),
            "--thread-id",
            "design-thread",
            "--agent",
            "cursor",
            "--to",
            "codex",
            "--document",
            first_document,
        )
        response_document = self.create_completed_turn(
            author="codex",
            role="reviewer",
            kind="REVIEW",
            status="NO_BLOCKING_FINDINGS",
            requested_action="ACKNOWLEDGE",
            based_on=[first_document],
        )
        self.run_cli(
            "publish",
            "--root",
            str(self.root),
            "--thread-id",
            "design-thread",
            "--agent",
            "codex",
            "--to",
            "cursor",
            "--document",
            response_document,
            "--in-message-id",
            first_publication["message_id"],
        )
        inspection = self.run_cli(
            "inspect", "--root", str(self.root), "--agent", "codex"
        )
        self.assertEqual(inspection["decision"], "DUPLICATE")


if __name__ == "__main__":
    unittest.main()
