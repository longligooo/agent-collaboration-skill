"""Validation for opt-in automatic collaboration; no executable configuration."""
from __future__ import annotations

import copy
import hashlib
import json
import re

PROVIDERS = {"codex", "claude", "agy"}
IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
MENTION = re.compile(r"(?<![\w@])@([^\s@,;:!?()\[\]{}<>，。；：！？]+)")
TERMINAL = {"cancelled", "completed"}


class AutoError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def fail(code: str, message: str):
    raise AutoError(code, message)


def identifier(value, label="identifier"):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        fail("invalid", f"invalid {label}")
    return value


def text(value, label="text", maximum=65536):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        fail("invalid", f"invalid {label}")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        fail("invalid", f"invalid Unicode in {label}")
    if size > maximum:
        fail("invalid", f"invalid {label}")
    return value


def canonical(value):
    try:
        result = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        fail("invalid", "value must be finite JSON")
    try:
        size = len(result.encode())
    except UnicodeEncodeError:
        fail("invalid", "invalid Unicode in JSON")
    if size > 1024 * 1024:
        fail("invalid", "JSON exceeds size limit")
    return result


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def binding(value):
    if not isinstance(value, dict) or set(value) - {"participant_id", "provider_id", "model_id", "role"}:
        fail("invalid", "invalid provider binding fields")
    result = copy.deepcopy(value)
    identifier(result.get("participant_id"), "participant_id")
    if result.get("provider_id") not in PROVIDERS:
        fail("invalid", "unsupported provider")
    if result.get("model_id") is not None:
        model = text(result["model_id"], "model_id", 200)
        if model.startswith("-") or any(c.isspace() or ord(c) < 32 for c in model):
            fail("invalid", "invalid model_id")
    if "role" in result:
        text(result["role"], "role", 120)
    return result


def plan(value, coordinator):
    if not isinstance(value, dict) or set(value) - {"goal", "participants", "tasks", "artifacts", "limitations", "ticket", "spec"}:
        fail("invalid_plan", "unsupported plan fields")
    result = copy.deepcopy(value)
    text(result.get("goal"), "goal")
    for field in ("ticket", "spec"):
        path = text(result.get(field), field, 512)
        if path.startswith(("/", "\\")) or "\\" in path or ":" in path or any(p in {"", ".", ".."} for p in path.split("/")) or not path.endswith(".md"):
            fail("invalid_plan", "ticket/spec must be safe relative Markdown paths")
    roster = result.get("participants")
    tasks = result.get("tasks")
    if not isinstance(roster, list) or not 1 <= len(roster) <= 32:
        fail("invalid_plan", "plan requires 1..32 participants")
    roster = [binding(p) for p in roster]
    names = [p["participant_id"] for p in roster]
    if len(names) != len(set(names)) or "all" in names:
        fail("invalid_plan", "duplicate or reserved participant")
    expected = {k: coordinator.get(k) for k in ("participant_id", "provider_id", "model_id")}
    actual = next((p for p in roster if p["participant_id"] == coordinator["participant_id"]), {})
    if {k: actual.get(k) for k in expected} != expected:
        fail("invalid_plan", "plan must preserve coordinator binding")
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 128:
        fail("invalid_plan", "plan requires 1..128 tasks")
    ids = []
    for task in tasks:
        if not isinstance(task, dict) or set(task) - {"id", "participant_id", "instruction", "depends_on", "acceptance"}:
            fail("invalid_plan", "invalid planned task fields")
        ids.append(identifier(task.get("id"), "planned task id"))
        if task.get("participant_id") not in names:
            fail("invalid_plan", "planned task recipient absent from roster")
        text(task.get("instruction"), "planned task instruction")
        acceptance = task.get("acceptance")
        if not isinstance(acceptance, list) or not 1 <= len(acceptance) <= 32:
            fail("invalid_plan", "task needs acceptance criteria")
        for criterion in acceptance:
            text(criterion, "acceptance criterion", 8192)
        deps = task.setdefault("depends_on", [])
        if not isinstance(deps, list) or any(not isinstance(d, str) for d in deps) or len(set(deps)) != len(deps):
            fail("invalid_plan", "invalid task dependencies")
    if len(ids) != len(set(ids)):
        fail("invalid_plan", "duplicate planned task id")
    graph = {t["id"]: t["depends_on"] for t in tasks}
    seen, visiting = set(), set()
    def visit(node):
        if node not in graph or node in visiting:
            fail("invalid_plan", "unknown or cyclic task dependency")
        if node in seen:
            return
        visiting.add(node)
        for dep in graph[node]:
            visit(dep)
        visiting.remove(node)
        seen.add(node)
    for node in graph:
        visit(node)
    for field in ("artifacts", "limitations"):
        values = result.setdefault(field, [])
        if not isinstance(values, list) or len(values) > 128:
            fail("invalid_plan", f"invalid {field}")
        for entry in values:
            text(entry, field, 8192)
    result["participants"] = roster
    canonical(result)
    return result


def recipients(body, participants, coordinator, supplied=None):
    text(body, "message body")
    if re.search(r"(?<![\w@])@(?=\s|$)", body):
        fail("invalid_recipient", "incomplete mention")
    matches = MENTION.findall(body)
    wanted = []
    for name in matches:
        # A trailing full stop is prose punctuation, not a participant suffix.
        if name.endswith(".") and name not in participants:
            name = name[:-1]
        if name == "all":
            wanted.extend(participants)
        elif name in participants:
            wanted.append(name)
        else:
            fail("invalid_recipient", "unknown or ambiguous mention")
    wanted = list(dict.fromkeys(wanted)) if wanted else [coordinator]
    if supplied is not None:
        if not isinstance(supplied, list) or any(not isinstance(x, str) for x in supplied) or len(set(supplied)) != len(supplied):
            fail("invalid_recipient", "invalid explicit recipients")
        if set(supplied) != set(wanted):
            fail("invalid_recipient", "explicit recipients do not match original mentions")
    return wanted
