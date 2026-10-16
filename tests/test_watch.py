import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "collab.py"
THREAD = "auto-thread"

FAKE_AGENT = textwrap.dedent(
    """
    import json
    import subprocess
    import sys
    from pathlib import Path

    script, root, agent, thread, agree_at = sys.argv[1:6]


    def cli(*arguments):
        completed = subprocess.run(
            [sys.executable, script, *arguments],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(completed.stdout)


    inbox = cli("inspect", "--root", root, "--agent", agent, "--thread-id", thread)
    for result in inbox["results"]:
        message = result.get("message", {})
        if result["decision"] == "TERMINAL":
            cli(
                "acknowledge", "--root", root, "--thread-id", thread,
                "--agent", agent, "--message-id", message["message_id"],
            )
        elif result["decision"] == "NEW":
            agreed = message["round"] + 1 >= int(agree_at)
            turn = cli(
                "new-turn", "--root", root, "--thread-id", thread,
                "--author", agent, "--role", "reviewer",
                "--kind", "DECISION" if agreed else "REVIEW",
                "--status", "AGREED" if agreed else "OPEN",
                "--requested-action", "ACKNOWLEDGE" if agreed else "REVIEW",
                "--based-on", message["document"], "--title", "Automatic turn",
            )
            document = Path(root) / turn["document"]
            document.write_text(
                document.read_text(encoding="utf-8").replace(
                    "TODO(agent-collaboration):", "Done:"
                ),
                encoding="utf-8",
            )
            cli(
                "publish", "--root", root, "--thread-id", thread,
                "--agent", agent, "--to", message["from"],
                "--document", turn["document"],
                "--in-message-id", message["message_id"],
            )
    """
)


class WatchTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        self.root.mkdir()
        self.fake_agent = self.base / "fake_agent.py"
        self.fake_agent.write_text(FAKE_AGENT, encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def run_cli(self, *arguments, expected=0, root=None):
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=root or self.root,
            text=True,
            capture_output=True,
            check=False,
            timeout=120,
        )
        self.assertEqual(
            completed.returncode,
            expected,
            msg=f"stdout={completed.stdout}\nstderr={completed.stderr}",
        )
        return completed

    def run_json(self, *arguments, expected=0):
        completed = self.run_cli(*arguments, expected=expected)
        return json.loads(completed.stdout if expected == 0 else completed.stderr)

    def watch(self, *arguments, expected=0, root=None):
        completed = self.run_cli(
            "watch",
            "--root",
            str(root or self.root),
            "--thread-id",
            THREAD,
            "--interval",
            "1",
            *arguments,
            expected=expected,
            root=root,
        )
        return [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]

    def fake_command(self, agree_at=4):
        return [
            sys.executable,
            str(self.fake_agent),
            str(SCRIPT),
            "{root}",
            "{agent}",
            "{thread_id}",
            str(agree_at),
        ]

    def write_config(self, agents, root=None):
        path = (root or self.root) / ".ai-collab" / "wake.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"agents": agents}), encoding="utf-8")

    def start_thread(self, participants=("cursor", "codex"), to="codex", root=None):
        root = root or self.root
        self.run_cli(
            "init", "--root", str(root), "--thread-id", THREAD,
            "--topic", "Automatic thread", "--objective", "Agree without forwarding",
            "--coordinator", "cursor", "--participants", *participants,
            root=root,
        )
        turn = json.loads(
            self.run_cli(
                "new-turn", "--root", str(root), "--thread-id", THREAD,
                "--author", "cursor", "--role", "proposer", "--kind", "PROPOSAL",
                "--status", "OPEN", "--requested-action", "REVIEW",
                "--title", "Opening proposal",
                root=root,
            ).stdout
        )
        document = root / turn["document"]
        document.write_text(
            document.read_text(encoding="utf-8").replace("TODO(agent-collaboration):", "Done:"),
            encoding="utf-8",
        )
        self.run_cli(
            "publish", "--root", str(root), "--thread-id", THREAD,
            "--agent", "cursor", "--to", to, "--document", turn["document"],
            root=root,
        )

    def turn_files(self, root=None):
        return sorted(
            path.name
            for path in ((root or self.root) / ".ai-collab/threads" / THREAD / "turns").glob("*.md")
        )

    def test_agents_complete_multiple_rounds_without_forwarding(self):
        self.start_thread()
        self.write_config(
            {"cursor": {"command": self.fake_command()}, "codex": {"command": self.fake_command()}}
        )
        events = self.watch()
        self.assertEqual(events[-1]["event"], "DONE")
        self.assertEqual(events[-1]["status"], "AGREED")
        self.assertEqual(
            self.turn_files(),
            [
                "round-0001-from-cursor-proposal.md",
                "round-0002-from-codex-review.md",
                "round-0003-from-cursor-review.md",
                "round-0004-from-codex-decision.md",
            ],
        )
        woken = [(event["agent"], event["decision"]) for event in events if event["event"] == "WAKE"]
        self.assertEqual(
            woken,
            [("codex", "NEW"), ("cursor", "NEW"), ("codex", "NEW"), ("cursor", "TERMINAL")],
        )
        for agent in ("cursor", "codex"):
            inbox = self.run_json("inspect", "--root", str(self.root), "--agent", agent)
            self.assertIn(inbox["decision"], ("DUPLICATE", "EMPTY"))
        logs = list((self.root / ".ai-collab/logs" / THREAD).glob("*.log"))
        self.assertEqual(len(logs), 4)
        self.assertFalse((self.root / ".ai-collab/watch.lock").exists())

    def test_agent_that_never_responds_stalls_the_thread(self):
        self.start_thread()
        self.write_config({"codex": {"command": [sys.executable, "-c", "pass"]}})
        events = self.watch("--max-attempts", "2", expected=3)
        self.assertEqual([e["event"] for e in events].count("WAKE"), 2)
        self.assertIn("STUCK", [e["event"] for e in events])
        self.assertEqual(events[-1]["event"], "STALLED")

    def test_missing_command_is_reported_without_crashing(self):
        self.start_thread()
        self.write_config({"codex": {"command": ["definitely-not-an-installed-agent", "{prompt}"]}})
        events = self.watch("--max-attempts", "1", expected=3)
        exited = [e for e in events if e["event"] == "EXITED"]
        self.assertIn("command not found", exited[0]["error"])

    def test_round_limit_stops_endless_ping_pong(self):
        self.start_thread()
        self.write_config(
            {
                "cursor": {"command": self.fake_command(agree_at=99)},
                "codex": {"command": self.fake_command(agree_at=99)},
            }
        )
        events = self.watch("--max-rounds", "3", expected=3)
        self.assertIn("ROUND_LIMIT", [e["event"] for e in events])
        self.assertEqual(len(self.turn_files()), 3)

    def test_stop_file_halts_before_waking(self):
        self.start_thread()
        self.write_config({"codex": {"command": self.fake_command()}})
        (self.root / ".ai-collab/threads" / THREAD / "STOP").write_text("", encoding="utf-8")
        events = self.watch()
        self.assertEqual([e["event"] for e in events], ["WATCHING", "STOPPED"])

    def test_live_lock_prevents_second_watcher(self):
        self.start_thread()
        self.write_config({"codex": {"command": self.fake_command()}})
        (self.root / ".ai-collab/watch.lock").write_text(
            json.dumps({"pid": os.getpid()}), encoding="utf-8"
        )
        completed = self.run_cli(
            "watch", "--root", str(self.root), "--thread-id", THREAD, expected=2
        )
        self.assertIn("another watcher", completed.stderr)

    def test_notify_only_participant_is_notified_once(self):
        self.start_thread(participants=("cursor", "human"), to="human")
        marker = self.base / "notified.txt"
        self.write_config(
            {
                "human": {
                    "notify_only": True,
                    "command": [
                        sys.executable,
                        "-c",
                        "import sys; open(sys.argv[1], 'a').write(sys.argv[2] + '\\n')",
                        str(marker),
                        "{message_id}",
                    ],
                }
            }
        )
        events = self.watch("--idle-timeout", "2", expected=3)
        self.assertEqual([e["event"] for e in events].count("WAKE"), 1)
        self.assertEqual(events[-1]["event"], "IDLE_TIMEOUT")
        self.assertEqual(len(marker.read_text(encoding="utf-8").splitlines()), 1)

    def test_prompt_placeholders_are_rendered_once(self):
        self.start_thread()
        capture = self.base / "prompt.txt"
        config = {
            "prompt": "agent={agent} thread={thread_id} literal={unknown}",
            "agents": {
                "codex": {
                    "command": [
                        sys.executable,
                        "-c",
                        "import sys; open(sys.argv[1], 'w').write(sys.argv[2])",
                        str(capture),
                        "{prompt}",
                    ]
                }
            },
        }
        (self.root / ".ai-collab/wake.json").write_text(json.dumps(config), encoding="utf-8")
        self.watch("--once")
        self.assertEqual(
            capture.read_text(encoding="utf-8"),
            f"agent=codex thread={THREAD} literal={{unknown}}",
        )

    def test_init_wake_writes_known_commands(self):
        result = self.run_json(
            "init-wake", "--root", str(self.root), "--agents", "cursor", "claude-code", "aider"
        )
        self.assertEqual(result["unconfigured_agents"], ["aider"])
        config = json.loads((self.root / ".ai-collab/wake.json").read_text(encoding="utf-8"))
        self.assertEqual(config["agents"]["cursor"]["command"][0], "cursor-agent")
        self.assertEqual(config["agents"]["claude-code"]["command"][0], "claude")
        self.run_json("init-wake", "--root", str(self.root), "--agents", "codex", expected=2)

    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_git_sync_pushes_each_processed_turn(self):
        remote = self.base / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        shutil.rmtree(self.root)
        subprocess.run(
            ["git", "clone", "-q", str(remote), str(self.root)], check=True, capture_output=True
        )
        for key, value in (("user.name", "Test"), ("user.email", "test@example.com")):
            subprocess.run(["git", "config", key, value], cwd=self.root, check=True)
        (self.root / "README.md").write_text("repo\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=self.root, check=True)
        subprocess.run(["git", "push", "-q", "-u", "origin", "HEAD"], cwd=self.root, check=True)

        self.start_thread()
        self.write_config(
            {"cursor": {"command": self.fake_command()}, "codex": {"command": self.fake_command()}}
        )
        events = self.watch("--git-sync")
        self.assertEqual(events[-1]["event"], "DONE")
        self.assertNotIn("GIT_ERROR", [e["event"] for e in events])
        log = subprocess.run(
            ["git", "--git-dir", str(remote), "log", "--format=%s", "--all"],
            capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        self.assertIn(f"collab({THREAD}): sync local changes", log)
        self.assertEqual(sum("processed round" in line for line in log), 4)
        remote_files = subprocess.run(
            ["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        self.assertEqual(
            sorted(Path(name).name for name in remote_files if "/turns/" in name),
            self.turn_files(),
        )


if __name__ == "__main__":
    unittest.main()
