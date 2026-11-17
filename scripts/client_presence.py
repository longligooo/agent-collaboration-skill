"""Advisory, local-only client identity reported by a participant's poll."""
from datetime import datetime, timedelta, timezone

import collab
import manual_ingress

FRESHNESS = timedelta(minutes=15)


def report(root, participant, client, thread_id=None):
    client = collab.validate_id(client, "client")
    for tid in collab.list_thread_ids(root, thread_id):
        try:
            m = collab.load_manifest(root, tid)
        except collab.CollaborationError:
            continue
        if participant in m["participants"]:
            break
    else:
        raise collab.CollaborationError("client reporting requires membership in this project")
    with manual_ingress.database(root, create=True) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS participant_clients (
            participant TEXT PRIMARY KEY, client TEXT NOT NULL, observed_at TEXT NOT NULL)""")
        now = datetime.now(timezone.utc).isoformat()
        db.execute("""INSERT INTO participant_clients VALUES (?, ?, ?)
            ON CONFLICT(participant) DO UPDATE SET client=excluded.client, observed_at=excluded.observed_at""",
            (participant, client, now))


def fresh_clients(db, participants, *, now=None):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='participant_clients'").fetchone():
        return {}
    now = now or datetime.now(timezone.utc)
    result = {}
    for participant in participants:
        row = db.execute("SELECT client, observed_at FROM participant_clients WHERE participant=?",
                         (participant,)).fetchone()
        if not row:
            continue
        client, seen = row
        observed = datetime.fromisoformat(seen)
        expiry = observed + FRESHNESS
        if observed <= now < expiry:
            result[participant] = {"client": client, "client_seen_at": seen,
                                   "client_expires_at": expiry.isoformat()}
    return result
