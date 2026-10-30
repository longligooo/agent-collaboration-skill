"""Optional coding-task acceptance and durable recovery, separate from transport.

All command fields are declarations, never executable instructions. This module is
also the read model for the CLI and local HTTP API.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional

import collab

SCHEMA_VERSION = 1
PHASES = {"planned", "implementing", "verifying", "reviewing", "blocked"}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise collab.CollaborationError(f"{label} must be a non-empty string")
    return value


def _choice(value: Any, allowed: set, label: str) -> str:
    return collab.validate_enum(_text(value, label), allowed, label)


def _strings(value: Any, label: str, *, nonempty: bool = False) -> list:
    if not isinstance(value, list) or (nonempty and not value):
        raise collab.CollaborationError(f"{label} must be a {'non-empty ' if nonempty else ''}list")
    for item in value:
        _text(item, label)
    if len(set(value)) != len(value):
        raise collab.CollaborationError(f"{label} must not contain duplicates")
    return value


def _path(root: Path, thread_id: str) -> Path:
    return collab.resolve_repo_path(root, f".ai-collab/threads/{collab.validate_id(thread_id, 'thread_id')}/task.json")


def _file_digest(path: Path) -> str:
    try:
        if path.is_symlink():
            return _digest({"link": os.readlink(path)})
        if not path.exists():
            return "missing"
        if not path.is_file():
            return "directory"
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as exc:
        raise collab.CollaborationError(f"cannot fingerprint {path}: {exc}") from exc


def code_version(root: Path) -> Dict[str, Any]:
    """Read HEAD and actual tracked/untracked content; never invoke a shell."""
    root = root.resolve()
    try:
        listing = subprocess.run(["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if listing.returncode == 0:
            names = set(os.fsdecode(p) for p in listing.stdout.split(b"\0") if p)
            head = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "HEAD"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            head_sha = head.stdout.decode("ascii").strip() if head.returncode == 0 else None
        else:
            if (root / ".git").exists():
                raise collab.CollaborationError("Git could not enumerate this worktree; refusing an incomplete fingerprint")
            names = set()
            for directory, dirs, files in os.walk(root, followlinks=False):
                dirs[:] = [d for d in dirs if d not in {".git", ".ai-collab", "__pycache__"}]
                for name in list(dirs):
                    if (Path(directory) / name).is_symlink():
                        names.add((Path(directory) / name).relative_to(root).as_posix())
                        dirs.remove(name)
                for name in files:
                    names.add((Path(directory) / name).relative_to(root).as_posix())
            head_sha = None
    except OSError as exc:
        raise collab.CollaborationError(f"cannot inspect code version: {exc}") from exc
    entries = []
    for name in sorted(names):
        if name.split("/", 1)[0] in {".git", ".ai-collab"}:
            continue
        path = root / name
        try:
            mode = stat.S_IMODE(path.lstat().st_mode) & 0o111 if path.exists() or path.is_symlink() else None
        except OSError as exc:
            raise collab.CollaborationError(f"cannot inspect {path}: {exc}") from exc
        content = code_version(path) if not path.is_symlink() and path.is_dir() and (path / ".git").exists() else _file_digest(path)
        entries.append([name, mode, content])
    return {"head_sha": head_sha, "worktree_fingerprint": _digest(entries)}


def capture_context(root: Path, thread_id: str) -> Dict[str, Any]:
    thread = _path(root, thread_id).parent
    collab.load_manifest(root, thread_id)
    messages = []
    for directory in ("turns", "signals", "human-events"):
        for path in sorted((thread / directory).glob("*")):
            if path.is_file() and not path.name.startswith("."):
                messages.append([directory + "/" + path.name, _file_digest(path)])
    identities = collab.resolve_repo_path(root, ".ai-collab/identities.json", label="identity registry")
    if identities.exists():
        messages.append(["identities.json", _file_digest(identities)])
    return {"code_version": code_version(root), "messages_fingerprint": _digest(messages)}


def _version(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"head_sha", "worktree_fingerprint"}:
        raise collab.CollaborationError("code_version requires head_sha and worktree_fingerprint")
    if value["head_sha"] is not None and (not isinstance(value["head_sha"], str) or not re.fullmatch(r"[0-9a-f]{40,64}", value["head_sha"])):
        raise collab.CollaborationError("head_sha must be a commit hash or null")
    if not isinstance(value["worktree_fingerprint"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["worktree_fingerprint"]):
        raise collab.CollaborationError("worktree_fingerprint must be a SHA-256 hash")
    return value


def load_task(root: Path, thread_id: str) -> Optional[Dict[str, Any]]:
    path = _path(root, thread_id)
    if not path.exists():
        return None
    task = collab.read_json(path, "coding task")
    collab.require_fields(task, ["schema_version", "profile", "thread_id", "revision", "phase", "owner", "implementation_owners", "criteria", "evidence", "reviews", "checkpoint", "operations"], "coding task")
    if task["schema_version"] != SCHEMA_VERSION or task["profile"] != "coding" or task["thread_id"] != thread_id:
        raise collab.CollaborationError("unsupported coding task schema or mismatched identity")
    if type(task["revision"]) is not int or task["revision"] < 1:
        raise collab.CollaborationError("coding task revision must be a positive integer")
    if not isinstance(task["phase"], str) or task["phase"] not in PHASES | {"accepted", "cancelled"}:
        raise collab.CollaborationError("invalid coding task phase")
    manifest = collab.load_manifest(root, thread_id)
    if task["owner"] not in manifest["participants"]:
        raise collab.CollaborationError("coding task owner must be a participant")
    owners = _strings(task["implementation_owners"], "implementation_owners", nonempty=True)
    if task["owner"] not in owners or not set(owners).issubset(set(manifest["participants"])):
        raise collab.CollaborationError("invalid implementation ownership history")
    _validate_criteria(task["criteria"])
    if not all(isinstance(task[k], list) for k in ("evidence", "reviews")) or not isinstance(task["operations"], dict) or not isinstance(task["checkpoint"], dict):
        raise collab.CollaborationError("invalid coding task collections")
    _validate_records(task, manifest)
    return task


def _validate_records(task: dict, manifest: dict) -> None:
    """Malformed sidecars fail closed; absent sidecars alone mean legacy mode."""
    checkpoint = task["checkpoint"]
    collab.require_fields(checkpoint, ["phase", "owner", "artifact_refs", "review_issue_ids", "next_action", "waiting_on", "context"], "checkpoint")
    if checkpoint["phase"] != task["phase"] or checkpoint["owner"] != task["owner"]:
        raise collab.CollaborationError("checkpoint and task phase/owner disagree")
    for field in ("artifact_refs", "review_issue_ids"):
        _strings(checkpoint[field], field)
    _text(checkpoint["next_action"], "next_action")
    observed = checkpoint["context"]
    if not isinstance(observed, dict) or not isinstance(observed.get("messages_fingerprint"), str):
        raise collab.CollaborationError("invalid checkpoint context")
    _version(observed.get("code_version"))
    waiting = checkpoint["waiting_on"]
    if waiting is not None and (not isinstance(waiting, dict) or waiting.get("kind") not in ("peer", "human") or not isinstance(waiting.get("identity"), str) or not waiting["identity"].strip()):
        raise collab.CollaborationError("invalid checkpoint waiting_on")
    known_criteria = {c["id"] for c in task["criteria"]}
    seen = set()
    for item in task["evidence"]:
        if not isinstance(item, dict):
            raise collab.CollaborationError("evidence must be an object")
        collab.require_fields(item, ["id", "author", "criterion_ids", "code_version", "command", "cwd", "exit_code", "log", "log_sha256", "blocked_items"], "evidence")
        identifier = collab.validate_id(_text(item["id"], "evidence id"), "evidence id")
        if identifier in seen or item["author"] not in manifest["participants"]:
            raise collab.CollaborationError("duplicate evidence id or unknown evidence author")
        seen.add(identifier)
        if not set(_strings(item["criterion_ids"], "criterion_ids", nonempty=True)).issubset(known_criteria):
            raise collab.CollaborationError("unknown evidence criterion")
        _version(item["code_version"])
        command = item["command"]
        if isinstance(command, list):
            if not command or not all(isinstance(x, str) and x.strip() for x in command):
                raise collab.CollaborationError("invalid evidence command")
        else:
            _text(command, "command")
        for field in ("cwd", "log", "log_sha256"):
            _text(item[field], field)
        if item["exit_code"] is not None and type(item["exit_code"]) is not int:
            raise collab.CollaborationError("invalid evidence exit_code")
        _strings(item["blocked_items"], "blocked_items")
        if item["exit_code"] is None and not item["blocked_items"]:
            raise collab.CollaborationError("null exit_code requires blocked_items")
    review_ids = set()
    for item in task["reviews"]:
        if not isinstance(item, dict):
            raise collab.CollaborationError("review must be an object")
        collab.require_fields(item, ["id", "author", "review_kind", "verdict", "code_version", "evidence_ids", "findings", "summary"], "review")
        identifier = collab.validate_id(_text(item["id"], "review id"), "review id")
        if identifier in review_ids or item["author"] not in manifest["participants"]:
            raise collab.CollaborationError("duplicate review id or unknown review author")
        review_ids.add(identifier)
        _choice(item["review_kind"], {"spec", "quality"}, "review_kind")
        _choice(item["verdict"], {"pass", "changes-requested", "blocked"}, "verdict")
        _version(item["code_version"])
        if not set(_strings(item["evidence_ids"], "evidence_ids")).issubset(seen):
            raise collab.CollaborationError("review references unknown evidence")
        _text(item["summary"], "summary")
        if not isinstance(item["findings"], list):
            raise collab.CollaborationError("review findings must be a list")
        finding_ids = set()
        for finding in item["findings"]:
            if not isinstance(finding, dict):
                raise collab.CollaborationError("finding must be an object")
            identifier = _text(finding.get("id"), "finding id")
            if identifier in finding_ids or type(finding.get("blocking")) is not bool:
                raise collab.CollaborationError("duplicate or invalid finding")
            finding_ids.add(identifier)
            _choice(finding.get("status"), {"open", "resolved"}, "finding status")
            _text(finding.get("text"), "finding text")
    for identifier, op in task["operations"].items():
        collab.validate_id(identifier, "operation_id")
        if not isinstance(op, dict) or not isinstance(op.get("request_hash"), str) or type(op.get("revision")) is not int:
            raise collab.CollaborationError("invalid persisted operation receipt")


def _validate_criteria(criteria: Any) -> None:
    if not isinstance(criteria, list) or not criteria:
        raise collab.CollaborationError("criteria must be a non-empty list")
    ids = []
    for item in criteria:
        if not isinstance(item, dict):
            raise collab.CollaborationError("criterion must be an object")
        collab.validate_id(_text(item.get("id"), "criterion id"), "criterion id")
        _text(item.get("text"), "criterion text")
        ids.append(item["id"])
    if len(ids) != len(set(ids)):
        raise collab.CollaborationError("criterion ids must be unique")


@contextmanager
def _locked(root: Path, thread_id: str):
    collab.validate_id(thread_id, "thread_id")
    path = collab.resolve_repo_path(root, f".ai-collab/threads/{thread_id}/.task.lock", label="task lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _save(root: Path, thread_id: str, task: Dict[str, Any]) -> None:
    path = _path(root, thread_id)
    collab.atomic_write_json(path, task)
    if os.name != "nt":
        descriptor = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def initialize_task(root: Path, thread_id: str, owner: str, criteria: list) -> Dict[str, Any]:
    _path(root, thread_id)
    manifest = collab.load_manifest(root, thread_id)
    if owner not in manifest["participants"]:
        raise collab.CollaborationError("owner must be a thread participant")
    _validate_criteria(criteria)
    with _locked(root, thread_id):
        if load_task(root, thread_id) is not None:
            raise collab.CollaborationError("coding task already exists")
        context = capture_context(root, thread_id)
        task = {"schema_version": 1, "profile": "coding", "thread_id": thread_id, "revision": 1,
                "phase": "planned", "owner": owner, "implementation_owners": [owner], "criteria": copy.deepcopy(criteria), "evidence": [], "reviews": [],
                "checkpoint": {"phase": "planned", "owner": owner, "artifact_refs": [], "review_issue_ids": [],
                               "next_action": "Read the ticket, latest turns and current code before implementation.",
                               "waiting_on": None, "context": context, "updated_at": collab.now_iso()},
                "operations": {}, "created_at": collab.now_iso(), "updated_at": collab.now_iso()}
        _save(root, thread_id, task)
        return task


def _same_identity(root: Path, first: str, second: str) -> bool:
    import task_discovery
    bindings = task_discovery.load_identities(root)
    return task_discovery.identity_for(first, bindings) == task_discovery.identity_for(second, bindings)


def acceptance(task: Dict[str, Any], root: Path, current: Dict[str, Any]) -> Dict[str, Any]:
    blockers = []
    def block(code: str, message: str, **fields: Any) -> None:
        blockers.append({"code": code, "message": message, **fields})
    selected = {}
    for evidence in task["evidence"]:
        # Only a rerun of the SAME check can supersede its prior result. An
        # unrelated passing unit test must never hide a blocked integration test.
        check_key = _digest({"command": evidence["command"], "cwd": evidence["cwd"]})
        for criterion in evidence["criterion_ids"]:
            selected[(criterion, check_key)] = evidence
    required_ids = set()
    for criterion in task["criteria"]:
        checks = [item for (criterion_id, _), item in selected.items() if criterion_id == criterion["id"]]
        if not checks:
            block("missing-evidence", "No verification evidence for criterion", criterion_id=criterion["id"])
        for item in checks:
            required_ids.add(item["id"])
            fields = {"criterion_id": criterion["id"], "evidence_id": item["id"]}
            if item["code_version"] != current["code_version"]:
                block("stale-evidence", "Verification evidence describes different code", **fields)
            if type(item["exit_code"]) is not int or item["exit_code"] != 0:
                block("failed-evidence", "Verification did not exit successfully", **fields)
            if item["blocked_items"]:
                block("blocked-evidence", "Verification has blocked items", **fields)
            try:
                log = collab.resolve_repo_path(root, item["log"], must_exist=True, label="evidence log")
                if not log.is_file() or _file_digest(log) != item["log_sha256"]:
                    block("changed-log", "Verification log is missing or changed", **fields)
            except collab.CollaborationError:
                block("changed-log", "Verification log is missing or changed", **fields)
    for kind in ("spec", "quality"):
        reviews = [r for r in task["reviews"] if isinstance(r, dict) and r.get("review_kind") == kind]
        if not reviews:
            block("missing-review", f"Missing {kind} review", review_kind=kind)
            continue
        review = reviews[-1]
        if review.get("code_version") != current["code_version"]:
            block("stale-review", f"{kind} review describes different code", review_id=review.get("id"))
        if review.get("verdict") != "pass":
            block("review-not-passed", f"{kind} review has not passed", review_id=review.get("id"))
        if any(_same_identity(root, review.get("author"), owner) for owner in task["implementation_owners"]):
            block("self-review", f"{kind} review must be independent of the task owner")
        if not required_ids.issubset(set(review.get("evidence_ids", []))):
            block("unreviewed-evidence", f"{kind} review does not cover current evidence")
    # A later review cannot silently drop an earlier blocking finding. A new
    # finding entry with the same id must explicitly resolve it.
    findings = {}
    for review in task["reviews"]:
        for finding in review.get("findings", []):
            key = (review.get("review_kind"), finding["id"])
            previous = findings.get(key, {})
            current_finding = dict(finding)
            if previous.get("blocking") and previous.get("status") != "resolved":
                current_finding["blocking"] = True
            findings[key] = current_finding
    for (kind, identifier), finding in findings.items():
        if finding.get("blocking") and finding.get("status") != "resolved":
            block("blocking-finding", finding.get("text", "Unresolved finding"), finding_id=identifier, review_kind=kind)
    if task["checkpoint"].get("waiting_on"):
        block("waiting", "Task is waiting on a peer or human")
    if task["phase"] == "blocked":
        block("blocked-task", "Task checkpoint is blocked")
    return {"ready": not blockers, "blockers": blockers}


def task_view(root: Path, thread_id: str) -> Optional[Dict[str, Any]]:
    task = load_task(root, thread_id)
    if task is None:
        return None
    current = capture_context(root, thread_id)
    result = copy.deepcopy(task)
    result.pop("operations", None)
    result["code_version"] = current["code_version"]
    result["acceptance"] = acceptance(task, root, current)
    reasons = []
    observed = task["checkpoint"].get("context", {})
    if observed.get("code_version") != current["code_version"]:
        reasons.append("code-changed")
    if observed.get("messages_fingerprint") != current["messages_fingerprint"]:
        reasons.append("messages-changed")
    result["recovery"] = {"needs_recheck": bool(reasons), "reasons": reasons, "context": current}
    result["stored_phase"] = task["phase"]
    if task["phase"] == "accepted" and not result["acceptance"]["ready"]:
        result["phase"] = "reviewing"
    return result


def _record_evidence(root: Path, task: dict, agent: str, op: dict, current: dict) -> None:
    identifier = collab.validate_id(_text(op.get("id"), "evidence id"), "evidence id")
    if any(e["id"] == identifier for e in task["evidence"]):
        raise collab.CollaborationError("evidence id already exists; append a new evidence record")
    criteria = _strings(op.get("criterion_ids"), "criterion_ids", nonempty=True)
    if not set(criteria).issubset({c["id"] for c in task["criteria"]}):
        raise collab.CollaborationError("evidence references an unknown criterion")
    if _version(op.get("code_version")) != current["code_version"]:
        raise collab.CollaborationError("evidence code version is stale")
    command = op.get("command")
    if isinstance(command, list):
        if not command or not all(isinstance(s, str) and s.strip() for s in command):
            raise collab.CollaborationError("command must contain non-empty strings")
    else:
        _text(command, "command")
    cwd = collab.resolve_repo_path(root, _text(op.get("cwd"), "cwd"), must_exist=True, label="cwd")
    if not cwd.is_dir():
        raise collab.CollaborationError("cwd must be a repository directory")
    if op.get("exit_code") is not None and type(op.get("exit_code")) is not int:
        raise collab.CollaborationError("exit_code must be an integer or null")
    if "exit_code" not in op:
        raise collab.CollaborationError("exit_code is required (null when blocked)")
    blocked = _strings(op.get("blocked_items"), "blocked_items")
    if op["exit_code"] is None and not blocked:
        raise collab.CollaborationError("a missing exit code requires blocked_items")
    log = collab.resolve_repo_path(root, _text(op.get("log"), "log"), must_exist=True, label="log")
    if not log.is_file():
        raise collab.CollaborationError("log must be a readable file")
    item = {k: copy.deepcopy(op[k]) for k in ("id", "criterion_ids", "code_version", "command", "cwd", "exit_code", "log", "blocked_items")}
    item.update(author=agent, created_at=collab.now_iso(), log_sha256=_file_digest(log))
    task["evidence"].append(item)


def _record_review(root: Path, task: dict, agent: str, op: dict, current: dict) -> None:
    collab.validate_id(_text(op.get("id"), "review id"), "review id")
    if any(r["id"] == op["id"] for r in task["reviews"]):
        raise collab.CollaborationError("review id already exists; append a new review")
    if any(_same_identity(root, agent, owner) for owner in task["implementation_owners"]):
        raise collab.CollaborationError("review author must differ from every implementation owner stable identity")
    _choice(op.get("review_kind"), {"spec", "quality"}, "review_kind")
    _choice(op.get("verdict"), {"pass", "changes-requested", "blocked"}, "verdict")
    if _version(op.get("code_version")) != current["code_version"]:
        raise collab.CollaborationError("review code version is stale")
    _text(op.get("summary"), "summary")
    refs = _strings(op.get("evidence_ids"), "evidence_ids")
    if not set(refs).issubset({e["id"] for e in task["evidence"]}):
        raise collab.CollaborationError("review references unknown evidence")
    findings = op.get("findings")
    if not isinstance(findings, list):
        raise collab.CollaborationError("findings must be a list")
    seen = set()
    for finding in findings:
        if not isinstance(finding, dict):
            raise collab.CollaborationError("finding must be an object")
        identifier = _text(finding.get("id"), "finding id")
        if identifier in seen:
            raise collab.CollaborationError("duplicate finding id")
        seen.add(identifier)
        if type(finding.get("blocking")) is not bool:
            raise collab.CollaborationError("finding blocking must be boolean")
        _choice(finding.get("status"), {"open", "resolved"}, "finding status")
        _text(finding.get("text"), "finding text")
    item = {k: copy.deepcopy(op[k]) for k in ("id", "review_kind", "verdict", "code_version", "evidence_ids", "findings", "summary")}
    item.update(author=agent, created_at=collab.now_iso())
    task["reviews"].append(item)


def apply_operation(root: Path, thread_id: str, agent: str, operation: dict,
                    expected_revision: int, operation_id: str) -> Dict[str, Any]:
    _path(root, thread_id)
    manifest = collab.load_manifest(root, thread_id)
    if agent not in manifest["participants"]:
        raise collab.CollaborationError("agent must be a thread participant")
    collab.validate_id(_text(operation_id, "operation_id"), "operation_id")
    if not isinstance(operation, dict):
        raise collab.CollaborationError("operation must be an object")
    request_hash = _digest({"agent": agent, "operation": operation, "expected_revision": expected_revision})
    with _locked(root, thread_id):
        task = load_task(root, thread_id)
        if task is None:
            raise collab.CollaborationError("thread has no coding task profile; use task-init")
        previous = task["operations"].get(operation_id)
        if previous:
            if previous["request_hash"] != request_hash:
                raise collab.CollaborationError("operation_id reused with different content")
            return task
        if type(expected_revision) is not int or expected_revision != task["revision"]:
            raise collab.CollaborationError("stale task revision; read task-show and re-evaluate")
        current = capture_context(root, thread_id)
        if operation.get("context") != current:
            raise collab.CollaborationError("stale context; re-read current code and messages using task-context")
        kind = _text(operation.get("kind"), "operation kind")
        if task["phase"] == "cancelled":
            raise collab.CollaborationError("cancelled task cannot be modified; create a new thread")
        if kind == "checkpoint":
            if agent not in {task["owner"], manifest.get("coordinator")}:
                raise collab.CollaborationError("only the owner or designated coordinator may update the checkpoint")
            _choice(operation.get("phase"), PHASES, "checkpoint phase")
            if operation.get("owner") not in manifest["participants"]:
                raise collab.CollaborationError("checkpoint owner must be a participant")
            for field in ("artifact_refs", "review_issue_ids"):
                _strings(operation.get(field), field)
            for artifact in operation["artifact_refs"]:
                collab.resolve_repo_path(root, artifact, must_exist=True, label="checkpoint artifact")
            _text(operation.get("next_action"), "next_action")
            waiting = operation.get("waiting_on")
            if waiting is not None:
                if not isinstance(waiting, dict) or waiting.get("kind") not in ("peer", "human"):
                    raise collab.CollaborationError("waiting_on must be null or an object with kind peer/human")
                _text(waiting.get("identity"), "waiting_on identity")
            task["checkpoint"] = {k: copy.deepcopy(operation.get(k)) for k in ("phase", "owner", "artifact_refs", "review_issue_ids", "next_action", "waiting_on")}
            task["owner"], task["phase"] = operation["owner"], operation["phase"]
            if operation["owner"] not in task["implementation_owners"]:
                task["implementation_owners"].append(operation["owner"])
        elif kind == "evidence":
            _record_evidence(root, task, agent, operation, current)
            if task["phase"] != "blocked":
                task["phase"] = "verifying"
        elif kind == "review":
            _record_review(root, task, agent, operation, current)
            if task["phase"] != "blocked":
                task["phase"] = "reviewing"
        elif kind in {"accept", "cancel"}:
            coordinator = manifest.get("coordinator")
            if coordinator and agent != coordinator:
                raise collab.CollaborationError("only the designated coordinator may accept/cancel")
            if kind == "cancel" and not coordinator and agent != task["owner"]:
                raise collab.CollaborationError("only the owner may cancel when no coordinator is designated")
            if kind == "accept":
                gate = acceptance(task, root, current)
                if not gate["ready"]:
                    raise collab.CollaborationError("cannot accept: " + "; ".join(b["message"] for b in gate["blockers"]))
                task["accepted_at"] = collab.now_iso()
                task["accepted_by"] = agent
                task["accepted_context"] = current
            else:
                _text(operation.get("reason"), "cancellation reason")
                task["cancellation_reason"] = operation["reason"]
            task["phase"] = "accepted" if kind == "accept" else "cancelled"
        else:
            raise collab.CollaborationError("unknown task operation")
        # Detect code/messages changed while validating a record, before publication.
        if capture_context(root, thread_id) != current:
            raise collab.CollaborationError("context changed during task update; retry after re-reading")
        task["checkpoint"].update(phase=task["phase"], owner=task["owner"], context=current, updated_at=collab.now_iso())
        task["revision"] += 1
        task["updated_at"] = collab.now_iso()
        task["operations"][operation_id] = {"request_hash": request_hash, "revision": task["revision"]}
        _save(root, thread_id, task)
        return task


def require_publishable(root: Path, thread_id: str, status: str) -> None:
    """Gate new acceptance publications only. Never gate reading/ACKing old mail."""
    if status not in {"AGREED", "CLOSED"}:
        return
    view = task_view(root, thread_id)
    if view is None:
        return
    if status == "CLOSED" and view["phase"] == "cancelled":
        return
    if view["phase"] != "accepted" or not view["acceptance"]["ready"]:
        raise collab.CollaborationError("coding task is not accepted; inspect task-show blockers before AGREED/CLOSED")


def cmd_task_init(args):
    criteria = []
    for item in args.criterion:
        identifier, separator, text = item.partition("=")
        if not separator:
            raise collab.CollaborationError("--criterion must be id=text")
        criteria.append({"id": identifier, "text": text})
    collab.emit(initialize_task(collab.repository_root(args.root), args.thread_id, args.owner, criteria))


def cmd_task_context(args):
    collab.emit(capture_context(collab.repository_root(args.root), args.thread_id))


def cmd_task_show(args):
    view = task_view(collab.repository_root(args.root), args.thread_id)
    collab.emit(view if view is not None else {"profile": "discussion", "thread_id": args.thread_id})


def cmd_task_update(args):
    root = collab.repository_root(args.root)
    operation_path = collab.resolve_repo_path(root, args.operation_file, must_exist=True, label="operation file")
    operation = collab.read_json(operation_path, "task operation")
    collab.emit(apply_operation(root, args.thread_id, args.agent, operation, args.expected_revision, args.operation_id))


def add_parsers(subparsers):
    for name, handler in (("task-init", cmd_task_init), ("task-context", cmd_task_context), ("task-show", cmd_task_show), ("task-update", cmd_task_update)):
        parser = subparsers.add_parser(name, help={"task-init": "opt a thread into coding acceptance", "task-context": "read latest code/message context", "task-show": "show acceptance blockers and recovery", "task-update": "atomically record checkpoint/evidence/review/acceptance"}[name])
        parser.add_argument("--root", default=".")
        parser.add_argument("--thread-id", required=True)
        parser.set_defaults(handler=handler)
        if name == "task-init":
            parser.add_argument("--owner", required=True)
            parser.add_argument("--criterion", action="append", required=True)
        if name == "task-update":
            parser.add_argument("--agent", required=True)
            parser.add_argument("--expected-revision", type=int, required=True)
            parser.add_argument("--operation-id", required=True)
            parser.add_argument("--operation-file", required=True)
