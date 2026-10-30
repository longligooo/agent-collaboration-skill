# Read-Only UI: Live Execution And Human-Input Provenance

The approved group-history UI reads the same coding-task model as the CLI.
Conversation and task details refresh manually. Only the lightweight activity
endpoint is polled; a recorded task phase or pending inbox is **not** a running
process. The UI has no message-sending or command-execution widget.

## Observed Runner Lifecycle

The existing, explicitly configured `collab.py watch` command observes the child
process it already launches. This adds no new runner, scheduler, command source,
permission, or credential setup. Starting the web UI does not start a watcher.

After a real child starts, the watcher writes an atomic observation to:

```text
.ai-collab/runtime/runs/<run-id>.json
```

Runtime files are outside `threads/`, so the existing `watch --git-sync` path
will not publish local PIDs or activity telemetry. A record includes:

- run, thread, message, local participant, and captured stable identity IDs;
- observer `watcher`, hashed local machine/boot identity, and runner/child PIDs;
- available process-start tokens, start/heartbeat/expiry timestamps;
- `running`, `completed`, `failed`, `timed-out`, or `cancelled` status;
- terminal exit/error information and the existing repository-relative log path.

The normal heartbeat is every second with a five-second lease. The reader marks
an observation active only when its status is running, its lease is fresh, its
machine matches, and both processes are observable as alive. Linux also checks
process-start tokens and excludes zombies. Missing permission, foreign-machine
records, expired heartbeats, PID mismatch, process exit, or invalid timestamps
are inactive/unknown—not guessed activity. Other supported systems use their
available process liveness check plus the short heartbeat lease.

Completion/error records expire immediately. Telemetry persistence failure does not interrupt the authorized command; absent/stale telemetry fails closed only for the display. Timeout kills and waits for the
observed child, matching the watcher's existing execution boundary. Crashed
watchers cannot keep an avatar animated through a stale record. This observes
the direct child process, not independent work it may deliberately detach.

The frontend additionally checks expiry locally, clears active state on polling
failure, and animates every occurrence of the same stable collaborator identity.
Human avatars never animate. Reduced-motion preferences replace animation with
static activity indication. These observations are local operational telemetry,
not authenticated proof: someone with write access to the workspace can edit
files, so the filesystem remains the trust boundary.

## Explicit Human Input, Not Agent Paraphrase

Ordinary protocol turns have no verified original-human provenance. They remain
ordinary collaborator messages even if an agent quotes or summarizes a user.
A coordinator's message is not automatically a human message.

To display original human input, explicitly record user-supplied text with
`human-record`. Do not scrape external conversations. The input can be:

- `manual`: text explicitly supplied for manual entry; the UI labels it as a
  manual human-input entry, not verified external capture;
- `authorized-import`: an authorized supplied original record with its source
  conversation ID, source message ID, and source author. The UI preserves the
  supplied provenance but does not claim independent authentication.

Write a JSON input under `.ai-collab/`, then explicitly record it:

```bash
python scripts/collab.py human-record --root . --thread-id config-redesign \
  --event-file .ai-collab/human-input.json
```

Example (illustrative source IDs, not a real imported conversation):

```json
{
  "event_id": "human-request-1",
  "actor_type": "human",
  "author": "project-owner",
  "body": "Please ask @backend to verify the migration before acceptance.",
  "created_at": "2026-10-30T09:00:00Z",
  "recipients": ["backend"],
  "provenance": {
    "kind": "authorized-import",
    "source_conversation_id": "supplied-conversation-id",
    "source_message_id": "supplied-original-message-id",
    "source_author": "project-owner",
    "supplied_authorized": true,
    "original_text": true
  }
}
```

Manual records use `kind: "manual"`; source identifiers may be omitted. In both
cases the explicit authorization/original-text declarations are mandatory;
unknown fields or paraphrase assertions are not upgraded into human originals.
These declarations are not a substitute for the user's actual authorization.

Recipients must be existing local thread participants and come from the supplied
source conversation/explicit instruction. Do not mention every collaborator by
inference. Human authors may be external stable slugs not present in old immutable
manifests; recording a human event grants no publishing, ACK, task-update, or
command authority to that author and does not modify the manifest.

Events are immutable files under `threads/<id>/human-events/<event-id>.json`.
Atomic no-clobber publication and process locking prevent partial records and
concurrent duplicates. Identical event/source retries are idempotent; changed
content or recipients for an existing ID/source message are rejected. Timestamps
are normalized to UTC, and sender/recipient stable identities at capture are retained (later alias remapping cannot rewrite historical mentions). A human
event changes the task's message fingerprint, so recovery must re-read it.

## Shared Read APIs

All project roots come from the existing local registry. URL parameters cannot
add arbitrary filesystem roots. Existing `/api/snapshot` and `/api/tasks` remain
available; old records without task/runtime/human-event data remain readable.

- `GET /api/thread?project_id=<id>&thread_id=<id>` returns the project and an
  enriched thread with `participant_details`, `coordinator_identity`,
  `coding_task`, `history`, and `history_errors`.
- `GET /api/activity` returns current observations and per-stable-identity active
  summaries. It never launches, continues, or acknowledges work.

History preserves source text as data. The frontend escapes it; it must not
execute markup or follow source IDs as commands. Human messages carry explicit
provenance/attribution; old turns carry none. Old turn recipients are reported
only when a current valid signal still refers to that turn (`current-signal`).
Older overwritten recipient notifications are `unknown`, never invented.
