import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "collab.py"
ADAPTERS = SCRIPT.parent / "adapters"
THREAD = "inbox-thread"


class InboxTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def run_cli(self, *arguments, expected=0, env=None):
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
        self.assertEqual(
            completed.returncode,
            expected,
            msg=f"stdout={completed.stdout}\nstderr={completed.stderr}",
        )
        return completed

    def publish_turn(self, *, author="codex", recipient="cursor", status="OPEN", kind="REVIEW", action="IMPLEMENT"):
        self.run_cli(
            "init", "--root", str(self.root), "--thread-id", THREAD,
            "--topic", "Inbox", "--objective", "Check mail",
            "--coordinator", "cursor", "--participants", "cursor", "codex", "claude-code", "chatgpt",
        )
        created = json.loads(
            self.run_cli(
                "new-turn", "--root", str(self.root), "--thread-id", THREAD,
                "--author", author, "--role", "reviewer", "--kind", kind,
                "--status", status, "--requested-action", action, "--title", "Mail",
            ).stdout
        )
        document = self.root / created["document"]
        document.write_text(
            document.read_text(encoding="utf-8").replace("TODO(agent-collaboration):", "Done:"),
            encoding="utf-8",
        )
        published = json.loads(
            self.run_cli(
                "publish", "--root", str(self.root), "--thread-id", THREAD,
                "--agent", author, "--to", recipient, "--document", created["document"],
            ).stdout
        )
        return published["message_id"]

    def test_inbox_reports_only_messages_that_need_attention(self):
        message_id = self.publish_turn()
        waiting = json.loads(
            self.run_cli("inbox", "--root", str(self.root), "--agent", "cursor").stdout
        )
        self.assertTrue(waiting["needs_attention"])
        self.assertEqual(waiting["count"], 1)
        self.assertEqual(waiting["items"][0]["decision"], "NEW")
        self.assertEqual(waiting["items"][0]["message_id"], message_id)

        idle = self.run_cli("inbox", "--root", str(self.root), "--agent", "claude-code", expected=1)
        self.assertFalse(json.loads(idle.stdout)["needs_attention"])

        text = self.run_cli(
            "inbox", "--root", str(self.root), "--agent", "cursor", "--format", "text"
        ).stdout
        self.assertTrue(text.startswith("needs_attention: yes\n"))
        self.assertIn(message_id, text)

        prompt = self.run_cli(
            "inbox", "--root", str(self.root), "--agent", "cursor", "--format", "prompt"
        ).stdout
        self.assertIn(message_id, prompt)
        self.assertNotIn("needs_attention", prompt)

    def test_processed_and_other_agents_need_no_attention(self):
        message_id = self.publish_turn()
        self.run_cli(
            "acknowledge", "--root", str(self.root), "--thread-id", THREAD,
            "--agent", "cursor", "--message-id", message_id,
        )
        self.run_cli("inbox", "--root", str(self.root), "--agent", "cursor", expected=1)

    def test_terminal_message_still_needs_attention(self):
        self.publish_turn(status="AGREED", kind="DECISION", action="ACKNOWLEDGE")
        report = json.loads(self.run_cli("inbox", "--root", str(self.root), "--agent", "cursor").stdout)
        self.assertEqual(report["items"][0]["decision"], "TERMINAL")

    def test_agent_can_come_from_the_environment(self):
        self.publish_turn(recipient="chatgpt")
        env = os.environ.copy()
        env["COLLAB_AGENT"] = "chatgpt"
        completed = self.run_cli("inbox", "--root", str(self.root), env=env)
        self.assertEqual(json.loads(completed.stdout)["agent"], "chatgpt")
        self.run_cli("inbox", "--root", str(self.root), expected=2)

    def test_cursor_background_script_wakes_the_named_chat(self):
        message_id = self.publish_turn()
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        capture = self.root / "woke.txt"
        fake = bin_dir / "agent"
        fake.write_text(
            textwrap.dedent(
                f"""\
                #!/bin/sh
                printf '%s\\n' "$@" > "{capture}"
                """
            ),
            encoding="utf-8",
        )
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        env = os.environ.copy()
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
        env["COLLAB_ONCE"] = "1"
        env["CURSOR_CHAT_ID"] = "chat-123"
        completed = subprocess.run(
            ["sh", str(ADAPTERS / "cursor-wake.sh"), str(self.root)],
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        woke = capture.read_text(encoding="utf-8")
        self.assertIn("--resume", woke)
        self.assertIn("chat-123", woke)
        self.assertIn(message_id, woke)

    def test_background_script_stays_quiet_when_idle(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        capture = self.root / "woke.txt"
        fake = bin_dir / "claude"
        fake.write_text(
            textwrap.dedent(
                f"""\
                #!/bin/sh
                printf '%s\\n' "$@" > "{capture}"
                """
            ),
            encoding="utf-8",
        )
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        env = os.environ.copy()
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
        env["COLLAB_ONCE"] = "1"
        completed = subprocess.run(
            ["sh", str(ADAPTERS / "claude-wake.sh"), str(self.root)],
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        self.assertFalse(capture.exists())


if __name__ == "__main__":
    unittest.main()
