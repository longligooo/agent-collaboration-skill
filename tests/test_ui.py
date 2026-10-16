import ast
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock
from urllib.request import Request, urlopen


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "collab.py"
sys.path.insert(0, str(SCRIPT.parent))
import ui_server  # noqa: E402


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def request_json(port, route, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    method = "GET" if payload is None else "POST"
    headers = {} if payload is None else {"Content-Type": "application/json"}
    request = Request(f"http://127.0.0.1:{port}{route}", data=data, headers=headers, method=method)
    with urlopen(request, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


class UiTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.env = os.environ.copy()
        self.env["COLLAB_UI_HOME"] = str(self.home)
        os.environ["COLLAB_UI_HOME"] = str(self.home)
        self.servers = []
        self.pids = []

    def tearDown(self):
        for httpd in self.servers:
            httpd.shutdown()
            httpd.server_close()
        for pid in self.pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        self.temporary.cleanup()

    def run_cli(self, *arguments, expected=0, env=None):
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            text=True,
            capture_output=True,
            check=False,
            env=env or self.env,
        )
        self.assertEqual(
            completed.returncode,
            expected,
            msg=f"stdout={completed.stdout}\nstderr={completed.stderr}",
        )
        return json.loads(completed.stdout if expected == 0 else completed.stderr)

    def make_project(self, name, author, recipient, coordinator=None):
        root = self.base / name
        root.mkdir()
        participants = [author, recipient]
        arguments = [
            "init", "--root", str(root), "--thread-id", "design",
            "--topic", f"{name} topic", "--objective", f"Work in {name}",
            "--participants", *participants,
        ]
        if coordinator:
            arguments.extend(["--coordinator", coordinator])
        self.run_cli(*arguments)
        created = self.run_cli(
            "new-turn", "--root", str(root), "--thread-id", "design",
            "--author", author, "--role", "proposer", "--kind", "PROPOSAL",
            "--status", "OPEN", "--requested-action", "REVIEW", "--title", f"{name} proposal",
        )
        document = root / created["document"]
        document.write_text(
            document.read_text(encoding="utf-8").replace("TODO(agent-collaboration):", "Ready:"),
            encoding="utf-8",
        )
        self.run_cli(
            "publish", "--root", str(root), "--thread-id", "design",
            "--agent", author, "--to", recipient, "--document", created["document"],
        )
        return root

    def start_server(self):
        port = free_port()
        httpd = ui_server.make_server(port)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.servers.append(httpd)
        for _ in range(50):
            if ui_server.read_health(port):
                return port
            time.sleep(0.05)
        self.fail("in-process UI did not become ready")

    def test_default_port_is_23451(self):
        self.assertEqual(ui_server.UI_PORT, 23451)

    def test_snapshot_groups_one_participant_across_projects(self):
        first = self.make_project("alpha", "frontend", "lead", coordinator="lead")
        second = self.make_project("beta", "backend", "lead")
        port = self.start_server()
        self.assertTrue(request_json(port, "/api/register", {"root": str(first)})["created"])
        again = request_json(port, "/api/register", {"root": str(first)})
        self.assertFalse(again["created"])
        request_json(port, "/api/register", {"root": str(second)})

        page = urlopen(f"http://127.0.0.1:{port}/app.js", timeout=3)
        script = page.read()
        self.assertIn("总览".encode("utf-8"), script)
        self.assertIn("刷新".encode("utf-8"), script)
        self.assertNotIn(b"setInterval", script)
        css = urlopen(f"http://127.0.0.1:{port}/app.css", timeout=3)
        self.assertIn(b"color-scheme: dark", css.read())
        body = request_json(port, "/api/snapshot")
        self.assertEqual(body["overview"]["projects"], 2)
        self.assertEqual(body["overview"]["pending"], 2)
        lead = next(person for person in body["collaborators"] if person["id"] == "lead")
        self.assertEqual(sorted(project["name"] for project in lead["projects"]), ["alpha", "beta"])
        self.assertEqual(lead["pending"], 2)
        alpha = next(project for project in body["projects"] if project["name"] == "alpha")
        self.assertEqual(alpha["threads"][0]["coordinator"], "lead")
        self.assertIn("Ready:", alpha["threads"][0]["turns"][0]["summary"])

    def test_command_starts_one_process_then_only_registers(self):
        first = self.make_project("one", "frontend", "lead")
        second = self.make_project("two", "backend", "lead")
        port = free_port()
        started = self.run_cli("ui", "--root", str(first), "--port", str(port))
        self.assertTrue(started["started"])
        self.pids.append(started["pid"])
        health = request_json(port, "/api/health")
        self.assertEqual(health["pid"], started["pid"])
        registered = self.run_cli("ui", "--root", str(second), "--port", str(port))
        self.assertFalse(registered["started"])
        self.assertEqual(request_json(port, "/api/health")["pid"], started["pid"])
        roots = set(request_json(port, "/api/health")["projects"])
        self.assertEqual(roots, {str(first.resolve()), str(second.resolve())})

    def test_ui_server_does_not_import_fcntl_at_import_time(self):
        tree = ast.parse(Path(ui_server.__file__).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
                self.assertNotIn("fcntl", names)
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "fcntl")

    def test_windows_lock_uses_byte_range_and_spawn_avoids_setsid(self):
        calls = []
        fake = types.SimpleNamespace(
            LK_LOCK=0,
            LK_UNLCK=1,
            locking=lambda fd, mode, nbytes: calls.append((mode, nbytes)),
        )
        handle = tempfile.TemporaryFile()
        self.addCleanup(handle.close)
        with mock.patch.dict(sys.modules, {"msvcrt": fake}):
            with mock.patch.object(ui_server.os, "name", "nt"):
                ui_server.lock_file(handle)
                ui_server.unlock_file(handle)
                spawned = ui_server.background_popen_kwargs()
        self.assertEqual(calls, [(fake.LK_LOCK, 1), (fake.LK_UNLCK, 1)])
        self.assertNotIn("start_new_session", spawned)
        self.assertEqual(spawned["creationflags"] & 0x00000200, 0x00000200)
        self.assertEqual(spawned["creationflags"] & 0x08000000, 0x08000000)


if __name__ == "__main__":
    unittest.main()
