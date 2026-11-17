"""Decide how to listen and evaluate one scan; does not start any client."""
from datetime import datetime, timedelta, timezone

import collab
import session_feedback


def plan(root, thread, agent, client, peers, *, self_interval=5, relay_interval=2, idle_minutes=60):
    manifest = collab.load_manifest(root, thread)
    roster = set(manifest["participants"])
    if agent not in roster or agent in peers or not set(peers) <= roster:
        raise collab.CollaborationError("listener and distinct peers must be members of this thread")
    if not all(x > 0 for x in (self_interval, relay_interval, idle_minutes)):
        raise collab.CollaborationError("listener intervals and idle timeout must be positive")
    client = collab.validate_id(client, "client")
    peers = list(dict.fromkeys(peers))
    rows = session_feedback.targets(root, thread)["targets"]
    observed = {t["participant"]: t.get("client") for t in rows}
    bound = sum(t["participant"] == agent and t["vendor"] == "codex" for t in rows) == 1
    direct = [p for p in peers if client == "codex" and observed.get(p) == "codex"]
    monitored = [p for p in peers if p not in direct]
    mode = ("shared_relay" if client == "codex" else "self") if monitored else ("direct" if direct else "none")
    return {"thread_id": thread, "participant": agent, "mode": mode, "direct_peers": direct,
            "poll_peers": monitored, "unknown_peers": [p for p in peers if not observed.get(p)],
            "interval_minutes": (relay_interval if mode == "shared_relay" else self_interval if mode == "self" else None),
            "idle_minutes": idle_minutes, "relay_model": "luna" if mode == "shared_relay" else None,
            "unique_codex_binding": bound if client == "codex" else None}


def _time(raw):
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.utcoffset() is None:
            raise ValueError()
        return dt
    except (AttributeError, ValueError):
        raise collab.CollaborationError("listener timestamps must contain a time zone") from None


def step(root, config, started_at, last_new_at, seen_message_id=None, *, completed=False, now=None):
    thread, agent = config["thread_id"], config["participant"]
    now = now or datetime.now(timezone.utc)
    started, last = _time(started_at), _time(last_new_at or started_at)
    if not started <= last <= now:
        raise collab.CollaborationError("listener timestamps must be in order and not in the future")
    result = {"state": "continue", "reason": None, "new_message": None,
              "last_new_at": last.isoformat(), "seen_message_id": seen_message_id}
    if completed or (root / ".ai-collab/STOP").is_file() or (collab.thread_directory(root, thread) / "STOP").is_file():
        return {**result, "state": "stop", "reason": "closed_or_stopped"}
    if config["mode"] in {"none", "direct"}:
        return {**result, "state": "stop", "reason": "no_poll_peers"}
    for row in collab.inspect_inbox(root, agent, thread)["results"]:
        if row["decision"] not in {"NEW", "TERMINAL"}:
            continue
        msg = row["message"]
        if msg["from"] not in config["poll_peers"] or msg["message_id"] == seen_message_id:
            continue
        # Do not count old pending mail as new traffic after starting.
        created = _time(msg["created_at"])
        relevant = started <= created <= now
        result.update(new_message={k: msg[k] for k in ("thread_id", "round", "from", "to", "message_id", "status")},
                      seen_message_id=msg["message_id"])
        if relevant:
            result["last_new_at"] = now.isoformat()
            last = now
        if msg["status"] in {"CLOSED", "CANCELLED"}:
            return {**result, "state": "stop_after_delivery", "reason": "closed"}
    if now - last >= timedelta(minutes=config["idle_minutes"]):
        return {**result, "state": "stop_after_delivery" if result["new_message"] else "stop",
                "reason": "idle_timeout"}
    return result


def command(args):
    root = collab.repository_root(args.root)
    config = plan(root, args.thread_id, args.agent, args.client, args.peer,
                  self_interval=args.interval, relay_interval=args.relay_interval, idle_minutes=args.idle)
    if args.started_at:
        config["cycle"] = step(root, config, args.started_at, args.last_new_at, args.seen_message_id,
                               completed=args.completed)
    collab.emit(config)


def add_parser(subparsers):
    p = subparsers.add_parser("listener-plan", help="plan or evaluate a scoped listener without launching it")
    p.add_argument("--root", default=".")
    p.add_argument("--thread-id", required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--client", required=True)
    p.add_argument("--peer", action="append", default=[])
    p.add_argument("--interval", type=int, default=5, help="self interval in minutes")
    p.add_argument("--relay-interval", type=int, default=2, help="relay interval in minutes")
    p.add_argument("--idle", type=int, default=60, help="idle stop in minutes")
    p.add_argument("--started-at", help="ISO timestamp; include to evaluate one scan")
    p.add_argument("--last-new-at", help="previous cycle's last_new_at")
    p.add_argument("--seen-message-id", help="previous cycle's seen_message_id")
    p.add_argument("--completed", action="store_true", help="cycle owner confirmed collaboration complete")
    p.set_defaults(handler=command)
