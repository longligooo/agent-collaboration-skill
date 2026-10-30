"""Synthetic collaboration journeys through public CLI and real loopback HTTP APIs.

No production model functions are called. All repositories, actors, logs, human
sources and runner commands are synthetic and live in TemporaryDirectory. No
external service, LLM, credential, source-chat fetch, or remote Git is used.

Run: python -m unittest discover -s tests -p test_simulated_collaboration_e2e.py -v
Set COLLAB_E2E_REPORT=/tmp/collaboration-e2e.json to retain scenario observations
and the exact CLI/HTTP transcript (fixture paths are removed after the test).
"""
from __future__ import annotations

import copy
import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "collab.py"

FAKE_COLLABORATOR = textwrap.dedent('''\
    # Synthetic local actor: only public collaboration CLI, no external service.
    import json, subprocess, sys, time
    from pathlib import Path
    script, root, agent, thread, mode = sys.argv[1:]
    def cli(*args):
        result = subprocess.run([sys.executable, script, *args, '--root', root],
                                capture_output=True, text=True, check=True)
        return json.loads(result.stdout)
    print('SYNTHETIC collaborator mode=' + mode, flush=True)
    time.sleep(0.5 if mode != 'timeout' else 10)
    if mode == 'fail':
        raise SystemExit(7)
    inbox = cli('inspect', '--thread-id', thread, '--agent', agent)
    incoming = next(item['message'] for item in inbox['results'] if item['decision'] == 'NEW')
    turn = cli('new-turn', '--thread-id', thread, '--author', agent, '--role', 'reviewer',
               '--kind', 'REVIEW', '--status', 'OPEN', '--requested-action', 'ACKNOWLEDGE',
               '--based-on', incoming['document'], '--title', 'Synthetic reviewer response')
    path = Path(root) / turn['document']
    path.write_text(path.read_text().replace('TODO(agent-collaboration):', 'Synthetic checked:'), encoding='utf-8')
    cli('publish', '--thread-id', thread, '--agent', agent, '--to', incoming['from'],
        '--document', turn['document'], '--in-message-id', incoming['message_id'])
''')


class SimulatedCollaborationE2E(unittest.TestCase):
    scenarios = []
    transcript = []

    @classmethod
    def setUpClass(cls):
        cls.scenarios = []
        cls.transcript = []

    @classmethod
    def tearDownClass(cls):
        target = os.environ.get("COLLAB_E2E_REPORT")
        if target:
            Path(target).write_text(json.dumps({
                "synthetic": True,
                "scenario_count": len(cls.scenarios),
                "cli_invocations": sum("argv" in event for event in cls.transcript),
                "http_reads": sum("http_get" in event for event in cls.transcript),
                "scenario_observations": cls.scenarios,
                "public_cli_and_http_transcript": cls.transcript,
                "scope": "Temporary local repositories and scripted fake collaborators; no remote/LLM access",
            }, indent=2, ensure_ascii=False), encoding="utf-8")
        for scenario in cls.scenarios:
            print("SIMULATED E2E PASS: " + scenario["scenario"] + " — " + scenario["observed"])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="collab-simulated-e2e-")
        self.base = Path(self.temp.name)
        self.env = dict(os.environ, COLLAB_UI_HOME=str(self.base / "ui-home"))
        self.processes = []
        self.roots = []
        self.server = None
        self.root = self.project("project-a")

    def tearDown(self):
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
        # Product work must never commit or publish a repository as a side effect.
        for root in self.roots:
            self.assertEqual(self.git(root, "rev-list", "--count", "HEAD"), "1")
            self.assertEqual(self.git(root, "remote"), "")
        self.temp.cleanup()

    def note(self, scenario, observed):
        self.scenarios.append({"scenario": scenario, "observed": observed, "test": self.id()})

    def git(self, root, *args):
        result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def project(self, name):
        root = self.base / name
        root.mkdir()
        (root / ".gitignore").write_text(".ai-collab/\n__pycache__/\n", encoding="utf-8")
        (root / "product.py").write_text("VALUE = 0\n", encoding="utf-8")
        (root / "verify.py").write_text("from product import VALUE\nassert VALUE == 1, 'synthetic product regression'\nprint('synthetic check passed')\n", encoding="utf-8")
        self.git(root, "init", "-q")
        self.git(root, "add", ".")
        self.git(root, "-c", "user.name=Synthetic E2E Fixture", "-c", "user.email=synthetic@example.invalid", "commit", "-qm", "synthetic fixture only")
        self.roots.append(root)
        return root

    def cli(self, root, *args, expected=0):
        command = [sys.executable, str(SCRIPT), *map(str, args), "--root", str(root)]
        result = subprocess.run(command, cwd=root, env=self.env, text=True, capture_output=True, timeout=25)
        self.transcript.append({"argv": command, "exit_code": result.returncode,
                                "stdout": result.stdout, "stderr": result.stderr})
        self.assertEqual(result.returncode, expected, f"{command}\n{result.stdout}\n{result.stderr}")
        return json.loads(result.stdout if expected == 0 else result.stderr)

    def init(self, root=None, thread="feature", owner="builder", coding=True):
        root = root or self.root
        self.cli(root, "init", "--thread-id", thread, "--topic", "Synthetic collaboration",
                 "--objective", "Synthetic fixture ticket: verify isolated collaboration",
                 "--coordinator", "lead", "--participants", "builder", "builder-alias", "lead", "reviewer", "successor", "developer")
        if coding:
            self.cli(root, "task-init", "--thread-id", thread, "--owner", owner,
                     "--criterion", "ac-1=Synthetic product verifier exits successfully")

    def view(self, root=None, thread="feature"):
        return self.cli(root or self.root, "task-show", "--thread-id", thread)

    def context(self, root=None, thread="feature"):
        return self.cli(root or self.root, "task-context", "--thread-id", thread)

    def submit(self, operation, root=None, thread="feature", actor="builder", revision=None, request_id=None, expected=0):
        root = root or self.root
        if revision is None:
            revision = self.view(root, thread)["revision"]
        request_id = request_id or str(uuid.uuid4())
        path = root / ".ai-collab" / ("e2e-operation-" + request_id + ".json")
        path.write_text(json.dumps(operation), encoding="utf-8")
        self.transcript.append({"operation_payload": operation, "thread_id": thread, "actor": actor,
                                "expected_revision": revision, "operation_id": request_id})
        return self.cli(root, "task-update", "--thread-id", thread, "--agent", actor,
                        "--expected-revision", revision, "--operation-id", request_id,
                        "--operation-file", path.relative_to(root), expected=expected)

    def operation(self, kind, *, root=None, thread="feature", actor="builder", expected=0, **fields):
        root = root or self.root
        return self.submit({"kind": kind, "context": self.context(root, thread), **fields},
                           root, thread, actor, expected=expected)

    def checkpoint(self, *, root=None, thread="feature", actor="builder", owner="builder", waiting=None, next_action="Synthetic next safe step"):
        return self.operation("checkpoint", root=root, thread=thread, actor=actor,
                              phase="implementing", owner=owner, artifact_refs=["product.py"],
                              review_issue_ids=[], next_action=next_action, waiting_on=waiting)

    def evidence(self, identifier):
        root = self.root
        log = root / ".ai-collab" / (identifier + ".log")
        command = [sys.executable, "-B", "verify.py"]
        result = subprocess.run(command, cwd=root, env=self.env, capture_output=True, text=True, timeout=10)
        log.write_text(result.stdout + result.stderr, encoding="utf-8")
        version = self.context()["code_version"]
        self.operation("evidence", id=identifier, criterion_ids=["ac-1"], code_version=version,
                       command=command, cwd=".", exit_code=result.returncode,
                       log=log.relative_to(root).as_posix(), blocked_items=[])
        return result.returncode

    def review(self, kind, identifier, evidence_ids, *, actor="reviewer", verdict="pass", findings=None, expected=0):
        return self.operation("review", actor=actor, expected=expected, id=identifier,
                              review_kind=kind, verdict=verdict, code_version=self.context()["code_version"],
                              evidence_ids=evidence_ids, findings=findings or [], summary="Synthetic independent review of fixture result")

    def turn(self, root=None, thread="feature", author="lead", recipient="builder", status="OPEN"):
        root = root or self.root
        created = self.cli(root, "new-turn", "--thread-id", thread, "--author", author,
                           "--role", "reviewer", "--kind", "REVIEW", "--status", status,
                           "--requested-action", "ACKNOWLEDGE" if status == "AGREED" else "REVIEW",
                           "--title", "Synthetic review message")
        path = root / created["document"]
        path.write_text(path.read_text().replace("TODO(agent-collaboration):", "Synthetic evidence:"), encoding="utf-8")
        published = self.cli(root, "publish", "--thread-id", thread, "--agent", author,
                             "--to", recipient, "--document", created["document"])
        return created, published

    def http(self, path):
        with urlopen(self.base_url + path, timeout=5) as response:
            result = json.loads(response.read())
        self.transcript.append({"http_get": path, "status": 200, "response": result})
        return result

    def start_server(self, roots=None):
        if self.server:
            return
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        command = [sys.executable, str(SCRIPT), "ui", "--serve", "--port", str(port)]
        self.server = subprocess.Popen(command, cwd=self.root, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append(self.server)
        self.base_url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                self.http("/api/health")
                break
            except (URLError, OSError):
                self.assertIsNone(self.server.poll(), "synthetic API server exited early")
                time.sleep(0.03)
        else:
            self.fail("synthetic HTTP server did not start")
        for root in roots or [self.root]:
            registration = self.cli(root, "ui", "--port", port)
            self.assertFalse(registration["started"])
        projects = self.http("/api/snapshot")["projects"]
        self.project_ids = {project["root"]: project["id"] for project in projects}

    def detail(self, root=None, thread="feature"):
        root = root or self.root
        return self.http("/api/thread?" + urlencode({"project_id": self.project_ids[str(root)], "thread_id": thread}))["thread"]

    def test_01_failed_review_fix_and_accept_through_public_cli(self):
        self.init()
        missing = self.operation("accept", actor="lead", expected=2)
        self.assertIn("cannot accept", missing["error"])
        self.assertEqual(self.evidence("failed-run"), 1)
        finding = {"id": "P1-synthetic", "blocking": True, "status": "open", "text": "Synthetic product returns wrong value"}
        self.review("spec", "spec-failed", ["failed-run"], verdict="changes-requested", findings=[finding])
        self.review("quality", "quality-failed", ["failed-run"], actor="lead", verdict="changes-requested")
        rejected = self.operation("accept", actor="lead", expected=2)
        self.assertIn("did not exit successfully", rejected["error"])
        blocked_log = self.root / ".ai-collab/blocked-run.log"
        blocked_log.write_text("SYNTHETIC blocked check: fixture dependency deliberately unavailable; command not run.\n", encoding="utf-8")
        self.operation("evidence", id="blocked-run", criterion_ids=["ac-1"], code_version=self.context()["code_version"],
                       command=[sys.executable, "-B", "verify.py"], cwd=".", exit_code=None,
                       log=blocked_log.relative_to(self.root).as_posix(), blocked_items=["Synthetic dependency unavailable"])
        self.assertIn("blocked-evidence", {item["code"] for item in self.view()["acceptance"]["blockers"]})
        self.operation("accept", actor="lead", expected=2)
        old = self.context()["code_version"]
        (self.root / "product.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.assertNotEqual(old, self.context()["code_version"])
        stale = self.view()["acceptance"]["blockers"]
        self.assertIn("stale-evidence", {blocker["code"] for blocker in stale})
        self.operation("accept", actor="lead", expected=2)
        self.assertEqual(self.evidence("passing-run"), 0)
        self.review("spec", "spec-pass", ["passing-run"], findings=[{**finding, "status": "resolved"}])
        self.review("quality", "quality-pass", ["passing-run"], actor="lead")
        accepted = self.operation("accept", actor="lead")
        self.assertEqual(accepted["phase"], "accepted")
        self.turn(status="AGREED")
        inbox = self.cli(self.root, "inspect", "--thread-id", "feature", "--agent", "builder")
        terminal = next(item["message"] for item in inbox["results"] if item["decision"] == "TERMINAL")
        self.cli(self.root, "acknowledge", "--thread-id", "feature", "--agent", "builder", "--message-id", terminal["message_id"])
        self.assertEqual(self.view()["phase"], "accepted")
        self.start_server()
        self.assertTrue(self.detail()["coding_task"]["acceptance"]["ready"])
        self.assertEqual(self.cli(self.root, "my-tasks", "--identity", "builder")["count"], 0)
        self.note("acceptance/review/fix/ACK", "Missing, actual exit-1 verifier and explicitly blocked synthetic check blocked; changed code made old evidence stale; rerun exit 0 plus two independent passes allowed acceptance; terminal ACK left acceptance intact and HTTP agreed")

    def test_02_independence_restart_replay_and_latest_context(self):
        self.init()
        self.cli(self.root, "identity-bind", "--agent", "builder", "--identity", "stable-builder")
        self.cli(self.root, "identity-bind", "--agent", "builder-alias", "--identity", "stable-builder")
        same = self.review("spec", "alias-self-review", [], actor="builder-alias", expected=2)
        self.assertIn("implementation owner", same["error"])
        self.checkpoint(owner="successor", next_action="Synthetic restart: inspect review before touching product")
        former = self.review("spec", "former-owner-review", [], actor="builder", expected=2)
        self.assertIn("implementation owner", former["error"])
        # Every helper invocation starts a new interpreter: durable state, no in-process model cache.
        restored = self.view()
        self.assertEqual(restored["checkpoint"]["next_action"], "Synthetic restart: inspect review before touching product")
        self.assertEqual(restored["owner"], "successor")
        request = {"kind": "checkpoint", "context": self.context(), "phase": "reviewing", "owner": "successor",
                   "artifact_refs": ["product.py"], "review_issue_ids": [], "next_action": "Synthetic serialized checkpoint", "waiting_on": None}
        revision = restored["revision"]
        request_id = "repeatable-operation"
        first = self.submit(request, actor="successor", revision=revision, request_id=request_id)
        path = self.root / ".ai-collab/threads/feature/task.json"
        persisted = path.read_bytes()
        replay = self.submit(request, actor="successor", revision=revision, request_id=request_id)
        self.assertEqual(first, replay)
        self.assertEqual(path.read_bytes(), persisted)
        self.submit({**request, "next_action": "Changed retry"}, actor="successor", revision=revision, request_id=request_id, expected=2)
        self.submit(request, actor="successor", revision=revision, expected=2)
        self.assertEqual(path.read_bytes(), persisted)
        before_code = self.context()
        (self.root / "product.py").write_text("VALUE = 123\n", encoding="utf-8")
        self.assertIn("code-changed", self.view()["recovery"]["reasons"])
        self.submit({**request, "context": before_code}, actor="successor", revision=first["revision"], expected=2)
        self.checkpoint(actor="successor", owner="successor")
        before_message = self.context()
        revision = self.view()["revision"]
        self.turn(recipient="successor")
        self.assertIn("messages-changed", self.view()["recovery"]["reasons"])
        self.submit({**request, "context": before_message}, actor="successor", revision=revision, expected=2)
        self.note("identity + restart + duplicate/stale updates", "Same-identity alias and former owner self-review rejected; fresh interpreter restored next action; exact replay wrote no bytes, changed replay/stale revision rejected; both code and message changes rejected old context")

    def test_03_my_tasks_across_projects_and_three_classifications(self):
        second = self.project("project-b")
        self.init(thread="actionable")
        self.init(thread="waiting-peer")
        self.init(second, thread="waiting-human", owner="developer")
        self.cli(self.root, "identity-bind", "--agent", "builder", "--identity", "same-person")
        self.cli(second, "identity-bind", "--agent", "developer", "--identity", "same-person")
        self.checkpoint(thread="waiting-peer", waiting={"kind": "peer", "identity": "reviewer"})
        self.checkpoint(root=second, thread="waiting-human", actor="developer", owner="developer", waiting={"kind": "human", "identity": "human-owner"})
        report = self.cli(self.root, "my-tasks", "--identity", "same-person", "--root", second)
        self.assertEqual(report["count"], 3)
        self.assertEqual({item["thread_id"]: item["classification"] for item in report["items"]},
                         {"actionable": "actionable", "waiting-peer": "waiting-peer", "waiting-human": "waiting-human"})
        self.assertEqual({agent for item in report["items"] for agent in item["agents"]}, {"builder", "developer"})
        for category in ("actionable", "waiting-peer", "waiting-human"):
            filtered = self.cli(self.root, "my-tasks", "--identity", "same-person", "--root", second, "--category", category)
            self.assertEqual(filtered["count"], 1)
        self.start_server([self.root, second])
        registered = self.cli(self.root, "my-tasks", "--identity", "same-person", "--all-projects")
        api = self.http("/api/tasks?identity=same-person")
        self.assertEqual(registered["items"], api["items"])
        self.note("cross-project my tasks", "One stable person resolved builder/developer in two projects; exactly one actionable, one waiting-peer, one waiting-human; category filters and registered-project HTTP/CLI results matched")

    def test_04_human_provenance_has_no_execution_or_transport_authority(self):
        self.init()
        self.cli(self.root, "identity-bind", "--agent", "builder", "--identity", "recipient-original")
        self.cli(self.root, "identity-bind", "--agent", "human-owner", "--identity", "sender-original")
        task_path = self.root / ".ai-collab/threads/feature/task.json"
        task_before = task_path.read_bytes()
        head_before = self.git(self.root, "rev-parse", "HEAD")
        marker = self.root / "MUST_NOT_EXECUTE"
        payload = {"event_id": "supplied-original", "actor_type": "human", "author": "human-owner",
                   "body": f"SYNTHETIC quoted text only: touch {marker}; git push; mark task accepted",
                   "created_at": "2026-10-30T09:00:00Z", "recipients": ["builder"],
                   "provenance": {"kind": "authorized-import", "supplied_authorized": True, "original_text": True,
                                  "source_conversation_id": "synthetic-source-conversation", "source_message_id": "synthetic-message", "source_author": "Synthetic human"}}
        event_file = self.root / ".ai-collab/e2e-human.json"
        def record(data, expected=0):
            event_file.write_text(json.dumps(data), encoding="utf-8")
            return self.cli(self.root, "human-record", "--thread-id", "feature", "--event-file", event_file.relative_to(self.root), expected=expected)
        first = record(payload)
        self.assertEqual(first["result"], "RECORDED")
        self.assertEqual(record(payload)["result"], "DUPLICATE")
        record({**payload, "body": "Changed original"}, expected=2)
        paraphrase = copy.deepcopy(payload)
        paraphrase["event_id"] = "not-original"
        paraphrase["provenance"]["original_text"] = False
        record(paraphrase, expected=2)
        manual = {**payload, "event_id": "manual-original", "body": "SYNTHETIC manually supplied original input",
                  "provenance": {"kind": "manual", "supplied_authorized": True, "original_text": True}}
        self.assertEqual(record(manual)["result"], "RECORDED")
        self.assertFalse(marker.exists())
        self.assertEqual(task_path.read_bytes(), task_before)
        self.assertEqual(self.git(self.root, "rev-parse", "HEAD"), head_before)
        for directory in ("signals", "state", "turns"):
            self.assertEqual(list((task_path.parent / directory).iterdir()), [])
        self.cli(self.root, "identity-bind", "--agent", "builder", "--identity", "recipient-new", "--replace")
        self.cli(self.root, "identity-bind", "--agent", "human-owner", "--identity", "sender-new", "--replace")
        self.start_server()
        history = self.detail()["history"]
        self.assertEqual(len(history), 2)
        for item in history:
            self.assertEqual(item["actor_type"], "human")
            self.assertEqual(item["identity"], "sender-original")
            self.assertEqual(item["recipients"], [{"agent": "builder", "identity": "recipient-original"}])
            self.assertIn("not", item["attribution"])
        self.assertFalse(self.detail()["coding_task"]["acceptance"]["ready"])
        self.note("human provenance/no authority", "Manual/import records immutable and originals-only; text mentioning touch/git push/accept executed nothing, sent no signals and changed no task/commit; HTTP retained original sender/recipient identities after rebinding")

    def test_05_real_watcher_success_failure_timeout_and_inactive_leases(self):
        fake = self.base / "synthetic_collaborator.py"
        fake.write_text(FAKE_COLLABORATOR, encoding="utf-8")
        self.cli(self.root, "identity-bind", "--agent", "reviewer", "--identity", "stable-reviewer")
        for mode in ("complete", "fail", "timeout"):
            self.init(thread="runner-" + mode, coding=False)
            self.turn(thread="runner-" + mode, recipient="reviewer")
        self.start_server()
        statuses = {}
        raw_live = None
        for mode in ("complete", "fail", "timeout"):
            thread = "runner-" + mode
            config = {"agents": {"reviewer": {"command": [sys.executable, str(fake), str(SCRIPT), "{root}", "{agent}", "{thread_id}", mode]}}}
            (self.root / ".ai-collab/wake.json").write_text(json.dumps(config), encoding="utf-8")
            command = [sys.executable, str(SCRIPT), "watch", "--root", str(self.root), "--thread-id", thread,
                       "--agents", "reviewer", "--once", "--timeout", "1" if mode == "timeout" else "5", "--interval", "1"]
            watcher = subprocess.Popen(command, cwd=self.root, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.processes.append(watcher)
            deadline = time.monotonic() + 5
            active = None
            while time.monotonic() < deadline:
                active = next((run for run in self.http("/api/activity")["runs"] if run["thread_id"] == thread and run["active"]), None)
                if active:
                    break
                self.assertIsNone(watcher.poll(), "watcher exited before live observation could be sampled")
                time.sleep(0.03)
            self.assertIsNotNone(active)
            self.assertEqual(active["identity"], "stable-reviewer")
            self.assertEqual(active["status"], "running")
            raw_path = self.root / ".ai-collab/runtime/runs" / (active["run_id"] + ".json")
            raw_live = json.loads(raw_path.read_text())
            stdout, stderr = watcher.communicate(timeout=7)
            self.assertEqual(watcher.returncode, 0, stderr)
            self.transcript.append({"argv": command, "exit_code": watcher.returncode, "stdout": stdout, "stderr": stderr})
            terminal = next(run for run in self.http("/api/activity")["runs"] if run["run_id"] == active["run_id"])
            expected = {"complete": "completed", "fail": "failed", "timeout": "timed-out"}[mode]
            self.assertEqual(terminal["status"], expected)
            self.assertFalse(terminal["active"])
            statuses[mode] = terminal["status"]
            if mode == "fail":
                self.assertEqual(terminal["exit_code"], 7)
            if mode == "complete":
                history = self.detail(thread=thread)["history"]
                self.assertEqual(len(history), 2)
                self.assertEqual(history[-1]["author"], "reviewer")
                inbox = self.cli(self.root, "inspect", "--thread-id", thread, "--agent", "reviewer")
                self.assertEqual(inbox["decision"], "DUPLICATE")
            else:
                inbox = self.cli(self.root, "inspect", "--thread-id", thread, "--agent", "reviewer")
                self.assertEqual(inbox["decision"], "NEW")
        # Controlled failure injection: copies of a REAL observed lease, not
        # invented proof of running. A dead watcher and expired lease must fail closed.
        now = datetime.now(timezone.utc)
        for label in ("expired", "dead-observer"):
            record = copy.deepcopy(raw_live)
            record["run_id"] = str(uuid.uuid4())
            record["heartbeat_at"] = now.isoformat()
            record["expires_at"] = ((now - timedelta(seconds=1)) if label == "expired" else (now + timedelta(seconds=5))).isoformat()
            path = self.root / ".ai-collab/runtime/runs" / (record["run_id"] + ".json")
            path.write_text(json.dumps(record), encoding="utf-8")
            observed = next(run for run in self.http("/api/activity")["runs"] if run["run_id"] == record["run_id"])
            self.assertFalse(observed["active"])
            self.assertEqual(observed["inactive_reason"], "expired" if label == "expired" else "runner-exited")
        self.assertFalse(any(run["active"] for run in self.http("/api/activity")["runs"]))
        self.note("real scripted watcher + runtime API", "Observed actual fake collaborator processes running then completed/failed(exit 7)/timed-out; successful actor published a CLI reply and consumed input, failed/timed-out actors left input NEW; expired/dead copied leases inactive")


if __name__ == "__main__":
    unittest.main(verbosity=2)
