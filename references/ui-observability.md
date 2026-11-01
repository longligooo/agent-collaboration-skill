# Read-Only UI: Live Execution And Human-Input Provenance

The approved group-history UI reads the same coding-task model as the CLI.
Thread lists and the visible conversation/task view refresh automatically while
the page is visible. Unchanged messages retain their DOM nodes, reading position,
expanded disclosures and search focus; stable project/thread/message keys avoid
duplicates and cross-project collisions. Requests are bounded, stale responses
are ignored, and failed refreshes retain the last loaded records with a warning.
The read APIs currently return full snapshots; incremental reconciliation happens
in the browser, not by importing or intercepting any external conversation.
A recorded task phase or pending inbox is **not** a running process. The UI has no message-sending or command-execution widget.

## Latest Activity And Duration

Threads are sorted globally, not concatenated in project order. The separate
`last_activity_at` is the latest valid thread creation, agent-turn, human-input,
or human-recording timestamp. Recording a supplied older original is therefore
new activity without changing the source message's chronological position.
Invalid timestamps sort last; equal instants use project ID then thread ID for
stable ordering. `updated_at` retains its protocol-last-round meaning, including
its existing use as the end of a terminal thread's duration.

## Observed Runner Lifecycle

The existing, explicitly configured `collab.py watch` command and bundled
`claude-wake.sh` / `cursor-wake.sh` scripts observe the child process they already
launch. This adds no new runner, scheduler, command source,
permission, or credential setup. Starting the web UI does not start a watcher.

After a real child starts, its observer writes an atomic observation to:

```text
.ai-collab/runtime/runs/<run-id>.json
```

Runtime files are outside `threads/`, so the existing `watch --git-sync` path
will not publish local PIDs or activity telemetry. A record includes:

- run, thread, message, local participant, and captured stable identity IDs;
- observer `watcher` or `wake-adapter`, hashed local machine/boot identity, and runner/child PIDs;
- available process-start tokens, start/heartbeat/expiry timestamps;
- `running`, `completed`, `failed`, `timed-out`, or `cancelled` status;
- terminal exit/error information and the existing watcher repository-relative log path;
  adapter observations have `log: null` and create no raw-output log. An invalid
  adapter notification has `message_id: null`, never a manufactured message ID.

The fixed-tool adapter helper preserves existing command arguments, resume
behavior, standard input/output and polling. Telemetry stores no prompt, session
ID, environment or raw CLI output; errors contain an exception class, not argv.
It observes only calls made through these bundled scripts or the watcher.
Manually started external sessions remain unobserved; no universal interception
or automatic external-chat import is provided.

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

Write a JSON input under `.ai-collab/local/`, then explicitly record it:

```bash
python scripts/collab.py human-record --root . --thread-id config-redesign \
  --event-file .ai-collab/local/human-input.json
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

New events are immutable, **local-only by default** files under
`.ai-collab/local/human-events/<thread-id>/<event-id>.json`, outside the thread
publication root. A local `.gitignore` also protects ordinary `git add` of new
captures. Reads merge this store with legacy
`.ai-collab/threads/<id>/human-events/` records without moving or deleting them.
Identical retained copies display once; conflicts are reported, not overwritten.
Atomic no-clobber publication and process locking prevent partial records and
concurrent duplicates. Identical event/source retries are idempotent across both
stores; changed content or recipients for an existing ID/source message are
rejected. Timestamps are normalized to UTC, and sender/recipient stable identities
at capture are retained (later alias remapping cannot rewrite historical
mentions). Both stores change the task's message fingerprint, so recovery must
re-read new human input.

### Git-Sync Privacy Boundary

`watch --git-sync` explicitly excludes legacy `human-events/` and its lock from
both staging and path-scoped commits. This also excludes modifications to files
that were already tracked or staged; existing staged human/business changes and
working-copy bytes are preserved, never reset, untracked, or deleted.

Before publishing, git-sync checks **all outgoing history**, not only the final
tree, for known private paths (`.ai-collab/local/`, legacy thread human events and
locks, and the old `.ai-collab/human-input.json` example source). A private event
committed and then deleted still blocks publication. The actual single push URL
and branch are inspected, including `pushurl`/`pushInsteadOf`, rather than trusting
a fetch remote or stale tracking ref. Multiple destinations, unstable URL
rewriting, unknown remote commits, or unavailable upstream checks fail closed.
The checked commit and destination are pinned; automatic tags are not published.
An alternate `pushRemote`/`remote.pushDefault`, explicit push refspecs, mirror or
matching policies, or a mismatched simple/current upstream fails closed rather
than silently publishing to a different branch or remote. Git replacement refs
do not hide private commits from the history check. Git error output omits remote URLs and hook
output so credentials or raw input are not copied into watcher messages.

This is not a history scrubber or a universal content filter. **Previously
pushed human data remains in the remote and its history.** An outgoing private
commit requires a separate human-reviewed recovery decision; the watcher does
not rewrite history, delete records, or remove files from the index. Explicit
manual `git add -f` / `git commit` / `git push` can bypass these application
safeguards. Original text copied into ordinary turns or arbitrary source files
cannot be recognized automatically; keep supplied source files under
`.ai-collab/local/` and do not paste private originals into shareable turns.
No external conversation is automatically imported or intercepted.

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
