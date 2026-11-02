#!/usr/bin/env python3
"""Opt-in local manual-session ingress. No installation, model calls or history IO.

Hook events are NOT process observations and never automatically become human
messages. The separate human command delegates explicit records to human_events.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

import collab
import human_events
import manual_hook_adapters as adapters
import task_discovery

SCHEMA = 1
MAX_INPUT = 1024 * 1024
LEASE_SECONDS = 15
BINDING_FIELDS = {"schema_version", "binding_id", "revision", "project_root", "thread_id", "participant",
                  "identity", "vendor", "session_id", "tool_version", "schema_profile", "version_validation",
                  "consent_activity", "consent_human", "created_at", "revoked_at"}
STATE_FIELDS = {"evidence_kind", "status", "observed_at", "turn_id", "expires_at", "last_event", "sequence"}
SELF_FIELDS = {"evidence_kind", "status", "observed_at", "expires_at", "last_event"}


def validate_binding(b, root):
    if not isinstance(b, dict) or set(b) != BINDING_FIELDS:
        raise ValueError("invalid binding fields")
    if b["schema_version"] != SCHEMA or b["project_root"] != str(root):
        raise ValueError("binding schema or project mismatch")
    if b["vendor"] not in adapters.PROFILES or b["schema_profile"] != adapters.PROFILES[b["vendor"]]:
        raise ValueError("unsupported binding schema profile")
    if b["consent_activity"] is not True or type(b["consent_human"]) is not bool:
        raise ValueError("invalid binding consent")
    for key in ("binding_id", "thread_id", "participant", "identity"):
        collab.validate_id(b[key], key)
    for key in ("session_id", "tool_version"):
        adapters.string(b[key], key)
    timestamp(b["created_at"])
    if b["revoked_at"] is not None:
        timestamp(b["revoked_at"])
    if type(b["revision"]) is not int or b["revision"] < 1:
        raise ValueError("invalid binding revision")


def validate_state(state, self_report=False):
    fields = SELF_FIELDS if self_report else STATE_FIELDS
    if not isinstance(state, dict) or set(state) != fields:
        raise ValueError("invalid observation fields")
    expected = "agent-self-reported" if self_report else "hook-event-observed"
    statuses = ("working", "idle", "waiting") if self_report else ("ended", "recent-event")
    if state["evidence_kind"] != expected or state["status"] not in statuses:
        raise ValueError("invalid observation evidence")
    timestamp(state["observed_at"]); timestamp(state["expires_at"])
    if not self_report and (type(state["sequence"]) is not int or state["sequence"] < 1):
        raise ValueError("invalid observation sequence")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def root_path(root):
    return collab.repository_root(str(root))


def storage_path(root):
    relative = Path(".ai-collab/local/manual-ingress")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("manual ingress storage must not contain symlinks")
    for name in ("store.sqlite3", "store.sqlite3-journal", "store.sqlite3-wal", "store.sqlite3-shm", ".gitignore"):
        if (current / name).is_symlink():
            raise ValueError("manual ingress storage must not contain symlinks")
    return current / "store.sqlite3"


@contextmanager
def database(root, create=False):
    path = storage_path(root)
    if not path.exists() and not create:
        raise ValueError("manual ingress is not configured")
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        ignore = path.parent / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n", encoding="utf-8")
    con = sqlite3.connect(str(path), timeout=10)
    try:
        con.execute("BEGIN IMMEDIATE")
        if create:
            con.execute("CREATE TABLE IF NOT EXISTS bindings (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            con.execute("CREATE TABLE IF NOT EXISTS events (binding_id TEXT NOT NULL, delivery_id TEXT NOT NULL, sequence INTEGER NOT NULL, data TEXT NOT NULL, PRIMARY KEY(binding_id,delivery_id), UNIQUE(binding_id,sequence))")
            con.execute("CREATE TABLE IF NOT EXISTS states (binding_id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            con.execute("CREATE TABLE IF NOT EXISTS selfreports (binding_id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def binding_from(con, binding_id, root, require_active=True):
    collab.validate_id(binding_id, "binding_id")
    row = con.execute("SELECT data FROM bindings WHERE id=?", (binding_id,)).fetchone()
    if row is None:
        raise ValueError("unknown binding")
    b = json.loads(row[0])
    validate_binding(b, root)
    if require_active and b["revoked_at"] is not None:
        raise ValueError("binding revoked")
    if require_active:
        manifest = collab.load_manifest(root, b["thread_id"])
        if b["participant"] not in manifest["participants"]:
            raise ValueError("bound participant is no longer in thread")
        identity = task_discovery.identity_for(b["participant"], task_discovery.load_identities(root))
        if identity != b["identity"]:
            raise ValueError("participant identity changed; explicit rebind required")
    return b


def bind(root, binding_id, thread_id, participant, vendor, session_id, tool_version,
         *, consent_activity=False, consent_human=False, accept_unverified_version=False):
    root = root_path(root)
    collab.validate_id(binding_id, "binding_id")
    manifest = collab.load_manifest(root, thread_id)
    if participant not in manifest["participants"]:
        raise ValueError("participant must be an existing thread participant")
    if vendor not in adapters.PROFILES:
        raise ValueError("unsupported vendor")
    if consent_activity is not True:
        raise ValueError("explicit activity consent required")
    if accept_unverified_version is not True:
        raise ValueError("installed version is not live-verified; explicit acknowledgement required")
    adapters.string(session_id, "session_id")
    adapters.string(tool_version, "tool_version")
    b = {"schema_version": SCHEMA, "binding_id": binding_id, "revision": 1,
         "project_root": str(root), "thread_id": thread_id, "participant": participant,
         "identity": task_discovery.identity_for(participant, task_discovery.load_identities(root)),
         "vendor": vendor, "session_id": session_id, "tool_version": tool_version,
         "schema_profile": adapters.PROFILES[vendor], "version_validation": "operator-pinned-fixture-only",
         "consent_activity": True, "consent_human": consent_human is True,
         "created_at": now_iso(), "revoked_at": None}
    with database(root, create=True) as con:
        # Do not silently fan one session into several projects/threads locally.
        for row in con.execute("SELECT data FROM bindings"):
            existing = json.loads(row[0])
            if existing["revoked_at"] is None and (existing["vendor"], existing["session_id"]) == (vendor, session_id):
                raise ValueError("session already bound; revoke explicitly before rebinding")
        con.execute("INSERT INTO bindings VALUES (?,?)", (binding_id, json.dumps(b)))
    return public_binding(b)


def public_binding(b):
    return {k: b[k] for k in sorted(BINDING_FIELDS - {"session_id"})}


def revoke(root, binding_id):
    root = root_path(root)
    with database(root) as con:
        b = binding_from(con, binding_id, root, require_active=False)
        if b["revoked_at"] is None:
            b["revoked_at"] = now_iso()
            b["revision"] += 1
            con.execute("UPDATE bindings SET data=? WHERE id=?", (json.dumps(b), binding_id))
        return public_binding(b)


def record_hook(root, binding_id, payload, tool_version, *, delivery_id=None, observed_at=None, sequence=None):
    """Consume callback once. Optional receipt metadata is for trusted collectors/tests.

    observed_at is collector observation time, NEVER a provider prompt timestamp.
    Local callbacks without metadata are ordered at receipt, not at vendor origin.
    """
    root = root_path(root)
    observed_at = observed_at or now_iso()
    timestamp(observed_at)
    delivery_id = delivery_id or str(uuid.uuid4())
    adapters.string(delivery_id, "delivery_id")
    with database(root) as con:
        b = binding_from(con, binding_id, root)
        if tool_version != b["tool_version"]:
            raise ValueError("tool version changed; explicitly rebind after review")
        event = adapters.normalize_hook(b["vendor"], payload)
        if event["session_id"] != b["session_id"]:
            raise ValueError("session does not match explicit binding")
        if event["reported_version"] is not None and event["reported_version"] != b["tool_version"]:
            raise ValueError("reported tool version does not match binding")
        if str(root) not in [str(Path(item).expanduser().resolve()) for item in event["roots"]]:
            raise ValueError("hook workspace does not match bound project")
        # Session IDs and all prompt/output data stay out of the event log and UI.
        event = {k: v for k, v in event.items() if k not in ("session_id", "roots")}
        prior = con.execute("SELECT data FROM events WHERE binding_id=? AND delivery_id=?", (binding_id, delivery_id)).fetchone()
        if prior:
            stored = json.loads(prior[0])
            if stored["event"] != event:
                raise ValueError("delivery ID reused with different metadata")
            return {"result": "DUPLICATE", "observation": stored}
        last = con.execute("SELECT MAX(sequence) FROM events WHERE binding_id=?", (binding_id,)).fetchone()[0] or 0
        if sequence is None:
            sequence = last + 1
        if type(sequence) is not int or sequence <= last:
            raise ValueError("stale or invalid collector sequence")
        if timestamp(observed_at) < timestamp(b["created_at"]) or timestamp(observed_at) > datetime.now(timezone.utc) + timedelta(seconds=1):
            raise ValueError("observation outside binding lifetime or in future")
        row = con.execute("SELECT data FROM states WHERE binding_id=?", (binding_id,)).fetchone()
        state = json.loads(row[0]) if row else None
        if state:
            validate_state(state)
        if state and timestamp(observed_at) < timestamp(state["observed_at"]):
            raise ValueError("out-of-order observation")
        if state and state.get("last_event") in ("SessionEnd", "sessionEnd"):
            raise ValueError("session ended; explicitly rebind before accepting new events")
        if (state and event["turn_id"] is not None and state["turn_id"] != event["turn_id"]
                and event["kind"] != "prompt"):
            raise ValueError("callback for another turn cannot change current turn")
        if state and event["turn_id"] is not None and event["turn_id"] != state["turn_id"]:
            # A delayed old prompt must not replace a newer turn. Turn IDs are
            # opaque: never order lexically or fabricate timestamps from them.
            for historical in con.execute("SELECT data FROM events WHERE binding_id=?", (binding_id,)):
                if json.loads(historical[0])["event"]["turn_id"] == event["turn_id"]:
                    raise ValueError("previously observed turn cannot reopen after another turn")
        terminal = event["kind"] in ("stop", "interrupt", "error", "session-end")
        # A tool callback cannot reopen a terminal turn. Claude lacks turn IDs:
        # only a new prompt begins a new receipt epoch after its terminal event.
        closed = bool(state and state["status"] == "ended" and
                      (event["turn_id"] == state["turn_id"]) and
                      (event["turn_id"] is not None or event["kind"] != "prompt"))
        status = "ended" if terminal or closed else "recent-event"
        observation = {"schema_version": SCHEMA, "binding_id": binding_id,
                       "binding_revision": b["revision"], "delivery_id": delivery_id,
                       "sequence": sequence, "observed_at": observed_at, "event": event}
        new_state = {"evidence_kind": "hook-event-observed", "status": status,
                     "observed_at": observed_at, "turn_id": event["turn_id"],
                     "expires_at": (timestamp(observed_at) + timedelta(seconds=LEASE_SECONDS)).isoformat(),
                     "last_event": event["event"], "sequence": sequence}
        con.execute("INSERT INTO events VALUES (?,?,?,?)", (binding_id, delivery_id, sequence, json.dumps(observation)))
        con.execute("INSERT OR REPLACE INTO states VALUES (?,?)", (binding_id, json.dumps(new_state)))
    return {"result": "RECORDED", "observation": observation}


def self_report(root, binding_id, status):
    """Explicit agent assertion, kept separate from vendor event observations."""
    if status not in ("working", "idle", "waiting"):
        raise ValueError("unsupported self-report status")
    root = root_path(root)
    with database(root) as con:
        b = binding_from(con, binding_id, root)
        now = now_iso()
        state = {"evidence_kind": "agent-self-reported", "status": status,
                 "observed_at": now,
                 "expires_at": (timestamp(now) + timedelta(seconds=LEASE_SECONDS)).isoformat(),
                 "last_event": "explicit-self-report"}
        con.execute("INSERT OR REPLACE INTO selfreports VALUES (?,?)", (binding_id, json.dumps(state)))
    return state


def record_human(root, binding_id, payload):
    """Explicit caller-supplied original only, never a hook prompt candidate.

    Authorization booleans record a declaration, not authenticated proof. Caller
    must have actual human permission. Existing human-record validation is final.
    """
    root = root_path(root)
    with database(root) as con:
        b = binding_from(con, binding_id, root)
        if b["consent_human"] is not True:
            raise ValueError("human-original recording is not authorized for binding")
        if not isinstance(payload, dict) or payload.get("recipients") != [b["participant"]]:
            raise ValueError("manual ingress human input must target exactly the bound participant")
        created = timestamp(payload.get("created_at"))
        if created < timestamp(b["created_at"]) or created > datetime.now(timezone.utc) + timedelta(seconds=1):
            raise ValueError("human input outside consent lifetime or in future")
        provenance = payload.get("provenance", {})
        if isinstance(provenance, dict) and provenance.get("kind") == "authorized-import":
            if provenance.get("source_conversation_id") != b["session_id"]:
                raise ValueError("human original source conversation does not match binding")
        return human_events.record_human_event(root, b["thread_id"], payload)



def record_human_text(root, binding_id, text, author, *, confirm_original=False,
                      confirm_authorized=False, event_id=None):
    """Record an explicitly supplied current manual entry, without JSON assembly.

    The timestamp is the manual entry's creation time, not an asserted external
    source-message timestamp. The generated ID identifies this local entry only.
    """
    if confirm_original is not True or confirm_authorized is not True:
        raise ValueError("explicit original-text and authorized-supply confirmation required")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_INPUT:
        raise ValueError("invalid human text")
    root = root_path(root)
    with database(root) as con:
        b = binding_from(con, binding_id, root)
        participant = b["participant"]
    payload = {"event_id": event_id or "manual-" + uuid.uuid4().hex, "actor_type": "human",
               "author": author, "body": text, "created_at": now_iso(), "recipients": [participant],
               "provenance": {"kind": "manual", "original_text": True, "supplied_authorized": True}}
    # record_human rechecks consent/revocation under its transaction.
    return record_human(root, binding_id, payload)


def snapshot(roots, *, now=None):
    now = now or datetime.now(timezone.utc)
    observations, errors = [], []
    for raw in dict.fromkeys(str(Path(r).resolve()) for r in roots):
        root = Path(raw)
        offset = len(observations)
        try:
            path = storage_path(root)
            if not path.exists():
                continue
            # Read-only access cannot create journals, locks or an empty store.
            con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
            try:
                for row in con.execute("SELECT b.id,b.data,s.data FROM bindings b LEFT JOIN states s ON b.id=s.binding_id"):
                    b = json.loads(row[1])
                    validate_binding(b, root)
                    state = json.loads(row[2]) if row[2] else None
                    if state:
                        validate_state(state)
                    status = "unknown"
                    if b["revoked_at"] is not None:
                        status = "revoked"
                    elif state:
                        if timestamp(state["observed_at"]) > now:
                            status = "unknown"
                        elif state["status"] == "ended":
                            status = "ended"
                        elif (timestamp(state["expires_at"]) > now and
                              now - timestamp(state["observed_at"]) < timedelta(seconds=LEASE_SECONDS)):
                            status = "recent-event"
                    observations.append({**public_binding(b), **(state or {}), "status": status,
                                         "active": False, "evidence_active": status == "recent-event",
                                         "evidence_kind": "hook-event-observed", "project_name": root.name,
                                         "project_id": hashlib.sha256(raw.encode()).hexdigest()[:12],
                                         "label": "Recent hook event; execution not independently observed"})
                    sr = con.execute("SELECT data FROM selfreports WHERE binding_id=?", (row[0],)).fetchone()
                    if sr:
                        report = json.loads(sr[0])
                        validate_state(report, self_report=True)
                        reported_status = report["status"]
                        if b["revoked_at"] is not None:
                            reported_status = "revoked"
                        elif (timestamp(report["expires_at"]) <= now or timestamp(report["observed_at"]) > now or
                              now - timestamp(report["observed_at"]) >= timedelta(seconds=LEASE_SECONDS)):
                            reported_status = "unknown"
                        observations.append({**public_binding(b), **report, "status": reported_status,
                                             "active": False, "evidence_active": reported_status == "working",
                                             "project_name": root.name,
                                             "project_id": hashlib.sha256(raw.encode()).hexdigest()[:12],
                                             "label": "Agent self-report; not a process observation"})
            finally:
                con.close()
        except (collab.CollaborationError, ValueError, KeyError, TypeError, OSError, sqlite3.Error):
            del observations[offset:]
            errors.append({"project_root": raw, "error": "manual ingress store unavailable or invalid"})
    return {"schema_version": SCHEMA, "observations": observations, "errors": errors,
            "generated_at": now.isoformat()}


def read_stdin():
    data = sys.stdin.read(MAX_INPUT + 1)
    if len(data) > MAX_INPUT:
        raise ValueError("hook payload too large")
    return json.loads(data)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    subs = parser.add_subparsers(dest="command", required=True)
    p = subs.add_parser("bind")
    for name in ("binding-id", "thread-id", "participant", "vendor", "session-id", "tool-version"):
        p.add_argument("--" + name, required=True)
    for name in ("consent-activity", "consent-human", "accept-unverified-version"):
        p.add_argument("--" + name, action="store_true")
    p = subs.add_parser("revoke"); p.add_argument("--binding-id", required=True)
    p = subs.add_parser("hook")
    p.add_argument("--binding-id", required=True); p.add_argument("--tool-version", required=True)
    p.add_argument("--delivery-id")
    p = subs.add_parser("human"); p.add_argument("--binding-id", required=True)
    p = subs.add_parser("human-text"); p.add_argument("--binding-id", required=True)
    p.add_argument("--author", required=True); p.add_argument("--event-id")
    p.add_argument("--confirm-original", action="store_true")
    p.add_argument("--confirm-authorized", action="store_true")
    p = subs.add_parser("self-report"); p.add_argument("--binding-id", required=True)
    p.add_argument("--status", required=True, choices=("working", "idle", "waiting"))
    subs.add_parser("snapshot")
    args = vars(parser.parse_args(argv)); command = args.pop("command"); root = args.pop("root")
    try:
        if command == "bind":
            result = bind(root, **args)
        elif command == "revoke":
            result = revoke(root, **args)
        elif command == "hook":
            payload = read_stdin()
            result = record_hook(root, payload=payload, **args)
            # Valid neutral hook output: never prompt injection or continuation.
            print(json.dumps({"continue": True} if payload.get("hook_event_name") == "beforeSubmitPrompt" else {}))
            return 0
        elif command == "self-report":
            result = self_report(root, **args)
        elif command == "human-text":
            result = record_human_text(root, text=sys.stdin.read(MAX_INPUT + 1), **args)
        elif command == "human":
            result = record_human(root, payload=read_stdin(), **args)
        else:
            result = snapshot([root])
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (collab.CollaborationError, ValueError, OSError, sqlite3.Error, TypeError):
        # Do not echo prompts, paths, session IDs or hook arguments in errors.
        print("manual ingress rejected or unavailable; inspect binding/schema/consent", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
