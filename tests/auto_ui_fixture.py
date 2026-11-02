#!/usr/bin/env python3
"""SIMULATED browser QA harness, not evidence of AI/runtime integration.

Run: python tests/auto_ui_fixture.py --port 0
Use printed local URL and ephemeral token in the automatic-task UI. Every project,
credential, plan and provider in this harness is a disposable fixture. Only the
real HTTP gateway, service adapter, SQLite state and UI are exercised. No provider
executables or models run. Ctrl-C/SIGTERM deletes the temporary fixture data.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import auto_api
from auto_service import AutoService
import collab
import ui_server


class FixtureProviders:
    """Static, prominently synthetic catalogue. Never shells out."""
    def discover(self, refresh=False):
        return [{"id": provider, "installed": True,
                 "auth_status": "authenticated" if provider == "claude" else "unknown",
                 "headless_status": "available",
                 "planning_isolation": "model_tools_denied" if provider == "claude" else "unverified",
                 "execution_status": "file_tools_only" if provider == "claude" else "blocked",
                 "models": [{"id": "fixture-claude-small", "label": "SIMULATED · small model"},
                            {"id": "fixture-claude-large", "label": "SIMULATED · large model"}] if provider == "claude" else [],
                 "catalog_source": "SIMULATED fixture, no CLI discovery", "catalog_checked_at": collab.now_iso(),
                 "catalog_status": "fresh" if provider == "claude" else "unknown",
                 "unavailable_reason": None if provider == "claude" else "fixture_execution_disabled",
                 "fixture": True} for provider in ("codex", "claude", "agy")]


class FixtureService(AutoService):
    """Simulates only planner output/finalization; worker jobs never execute."""
    def __init__(self):
        super().__init__(FixtureProviders())
        self.fixture_lock = threading.RLock()

    def project_capabilities(self, store):
        return {"execution_workspace": {"kind": "managed_snapshot", "supported": True,
                "requires_clean": False, "reason": None, "fixture": True,
                "capture_timing": "first_approved_worker", "exclusions": []},
                "application": {"status": "pending", "automatic": False}}

    @staticmethod
    def fixture_plan(task):
        directory = ".scratch/automatic-" + task["id"]
        lead = copy.deepcopy(task["coordinator"])
        worker = "fixture-builder" if lead["participant_id"] != "fixture-builder" else "fixture-helper"
        return {"goal": "SIMULATED plan for browser QA; no AI has run",
                "ticket": directory + "/issues/01-approved-plan.md", "spec": directory + "/spec.md",
                "participants": [lead, {"participant_id": worker, "provider_id": "claude", "model_id": "fixture-claude-small", "role": "implementer"}],
                "tasks": [{"id": "fixture-work", "participant_id": worker,
                           "instruction": "SIMULATED work item. This harness never executes it.",
                           "acceptance": ["Explicit fixture-only behavior; no model output presented as real"]}],
                "artifacts": [], "limitations": ["SIMULATED provider data and planner output", "Workers remain queued; no actual execution or live delivery"]}

    def simulate_plan(self, store, task_id):
        task = store.get_task(task_id)
        if task["state"] != "planning":
            return
        # Intentionally bypass the runtime in this fixture. Inject only a schema-
        # validated draft; never claim an unrelated queued worker to simulate it.
        store.submit_plan(task_id, self.fixture_plan(task), expected_revision=task["revision"])

    def simulate_finalize(self, store, task_id):
        task = store.get_task(task_id)
        if task["state"] != "ready" or task["thread_id"]:
            return
        plan = task["plan"]
        expected = ".scratch/automatic-" + task_id
        if plan["ticket"] != expected + "/issues/01-approved-plan.md" or plan["spec"] != expected + "/spec.md":
            return  # never write user-edited arbitrary paths, even in a fixture
        for field in ("spec", "ticket"):
            path = store.root / plan[field]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# SIMULATED fixture metadata\n\nNo AI ran. No production acceptance.\n", encoding="utf-8")
        thread_id = "fixture-" + task_id[5:]
        with contextlib.redirect_stdout(io.StringIO()):
            collab.cmd_init(argparse.Namespace(root=str(store.root), thread_id=thread_id,
                topic="SIMULATED · browser QA only", objective="Fixture ticket " + plan["ticket"],
                coordinator=task["coordinator"]["participant_id"], participants=[p["participant_id"] for p in task["participants"]]))
        task = store.bind_thread(task_id, thread_id, expected_revision=task["revision"])
        for item in plan["tasks"]:
            store.enqueue(task_id, item["participant_id"], "worker", {"planned_task_id": item["id"]}, idempotency_key="fixture-work-" + item["id"])

    def dispatch(self, operation, params, root, body, key):
        with self.fixture_lock:
            result = super().dispatch(operation, params, root, body, key)
            if operation in {"create_task", "revise"}:
                task_id = result["task"]["id"]
                self.simulate_plan(self.store(root, params["project_id"]), task_id)
            elif operation == "approve":
                self.simulate_finalize(self.store(root, params["project_id"]), params["task_id"])
            return result


class FixtureHandler(ui_server.CollaborationHandler):
    def end_headers(self):
        self.send_header("X-Collab-Fixture", "SIMULATED; no model or worker execution")
        super().end_headers()

    def serve_file(self, path):
        if path not in {"", "/", "/index.html"}:
            return super().serve_file(path)
        body = (ui_server.UI_DIR / "index.html").read_text(encoding="utf-8")
        # Visible label only in this test server; never changes production assets.
        banner = '<div style="position:fixed;bottom:0;left:0;right:0;z-index:99999;background:#7f1d1d;color:white;padding:6px 12px;font:12px sans-serif;text-align:center;pointer-events:none">SIMULATED UI QA · fake providers/plans · no model calls · workers never execute</div>'
        body = body.replace("</body>", banner + "</body>").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0, help="Loopback port; 0 selects a free port")
    parser.add_argument("--no-seed", action="store_true", help="Start with no sample task")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port must be 0..65535")
    old_home = os.environ.get("COLLAB_UI_HOME")
    with tempfile.TemporaryDirectory(prefix="collab-SIMULATED-ui-") as directory:
        root = Path(directory).resolve() / "SIMULATED-QA-No-AI"
        root.mkdir()
        os.environ["COLLAB_UI_HOME"] = str(Path(directory) / "fixture-ui-home")
        ui_server.register_project(root)
        project_id = ui_server.project_id(str(root))
        service, credentials = FixtureService(), auto_api.Credentials()
        seeded = None
        if not args.no_seed:
            seeded = service.dispatch("create_task", {"project_id": project_id}, root,
                {"title": "SIMULATED · awaiting team approval", "instruction": "Fixture-only original human text. No model is called.",
                 "coordinator": {"participant_id": "lead", "provider_id": "claude", "model_id": "fixture-claude-small"}}, "fixture-seed-task")["task"]["id"]
        server = ui_server.ThreadingHTTPServer(("127.0.0.1", args.port), FixtureHandler)
        server.auto_gateway = auto_api.Gateway(credentials, lambda: ui_server.load_registry(ui_server.registry_path()), service.dispatch)
        port = server.server_address[1]
        print(json.dumps({"fixture": True, "warning": "SIMULATED: no model or worker execution",
                          "url": f"http://127.0.0.1:{port}/#/auto/{project_id}" + ("/" + seeded if seeded else ""),
                          "port": port, "token": credentials.token, "csrf_token": credentials.csrf,
                          "project_id": project_id, "task_id": seeded, "temporary_root": str(root)}, ensure_ascii=False), flush=True)
        def stop(*_):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, stop)
        try:
            server.serve_forever(poll_interval=.2)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            if old_home is None:
                os.environ.pop("COLLAB_UI_HOME", None)
            else:
                os.environ["COLLAB_UI_HOME"] = old_home


if __name__ == "__main__":
    main()
