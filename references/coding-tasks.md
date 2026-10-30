# Optional Coding Tasks: Acceptance And Recovery

The ordinary discussion protocol remains unchanged. A coding task is an **opt-in
sidecar** for one thread, not a new meaning for an ACK, `AGREED`, or a ticket.
Publishing a new `AGREED`/`CLOSED` turn for a coding task requires current
acceptance (`CLOSED` may also close a cancelled task). Existing terminal messages
remain readable and acknowledgeable. A transport acknowledgement means a message
was processed; it is never proof
that an implementation passed acceptance. Tickets remain human-readable and are
not parsed into this model.

Use this profile when implementation needs explicit acceptance criteria,
version-bound verification, independent spec/quality reviews, or a durable
restart checkpoint. It does not run tests, invoke agents, set up credentials,
start a watcher, commit, push, or grant any new permission.

## Files And Source Of Truth

```text
.ai-collab/
  identities.json                         # optional stable identity bindings
  threads/<thread-id>/
    thread.json                           # existing immutable manifest
    task.json                             # optional coding profile, schema v1
    turns/                                # existing immutable discussion
    signals/                              # existing notification transport
    state/                                # existing per-recipient ACK state
  logs/                                   # suggested verification-log location
```

`task.json` has its own revision, explicit criteria, evidence records, review
records, current phase/owner, recovery checkpoint, and operation IDs. It does not
replace the turns. CLI and read-only HTTP queries use the same Python task model;
the API does not infer acceptance from turn prose or status labels.

Old threads without `task.json` remain ordinary threads and require no migration.
Missing identity bindings use the existing participant ID. Invalid existing task
data is reported rather than silently discarded or reset.

## Start A Coding Task

First write the required spec/ticket and initialize the ordinary thread. Its
participants must include the implementer and an independent reviewer. Then:

```bash
python scripts/collab.py task-init --root . --thread-id config-redesign \
  --owner backend \
  --criterion ac-1='The regression suite passes' \
  --criterion ac-2='The migration preserves existing configuration'

python scripts/collab.py task-show --root . --thread-id config-redesign
python scripts/collab.py task-context --root . --thread-id config-redesign
```

Each criterion has a stable ID and a nonempty description. Starting a profile
with no criteria, duplicate criterion IDs, an unknown owner, or an existing task
is rejected.

## Observe Before Resuming Or Writing

At the beginning of a resumed session:

1. Locate your tasks with `my-tasks` below.
2. Read the current task, latest turn(s), own inbox, and referenced artifacts.
3. Re-read the current code; do not trust the previous chat summary.
4. Capture the current context with `task-context`.
5. Save a checkpoint naming the next safe action before leaving the task.

A context contains:

```json
{
  "code_version": {
    "head_sha": "current Git commit, or null outside Git",
    "worktree_fingerprint": "content fingerprint"
  },
  "messages_fingerprint": "current thread-message fingerprint"
}
```

The code fingerprint includes dirty tracked files and nonignored untracked
files, and excludes `.git` and `.ai-collab`. Git HEAD is checked separately: an
empty commit still changes the version. Initialized submodule content is
fingerprinted too, so uncommitted changes inside a submodule invalidate evidence. Protocol logs should normally be stored
under `.ai-collab/logs/` so collecting them does not itself change product code.

The `owner` is the participant responsible for implementation, not simply whoever
should act next. To request a peer review, set `waiting_on` to that reviewer; do
not transfer implementation ownership just to request a review. Owner history is
retained in `implementation_owners`, including every successor after a handoff.

A checkpoint stores `phase`, `owner`, `artifact_refs`, `review_issue_ids`,
`next_action`, and optional `waiting_on` (`null`, or an object with `kind` of
`peer`/`human` and an `identity`). Its observed context is retained with it.
Changing code or messages makes the read model report
`recovery.needs_recheck: true` with reasons. Rechecking is not automatic
permission to execute the next action; normal human authorization still applies.

## Record Operations Explicitly

Mutations use a JSON operation file, a unique operation ID, and the revision
observed by the caller:

```bash
python scripts/collab.py task-update --root . --thread-id config-redesign \
  --agent backend --expected-revision 1 \
  --operation-id 9f33d02c-1356-44dd-98dc-25173d964157 \
  --operation-file .ai-collab/checkpoint-operation.json
```

Keep operation files under `.ai-collab/` so writing the request itself does not
change the product-code fingerprint. The operation file has `kind` and a fresh `context` alongside its operation
fields. For example, fill `context` from the current `task-context` result:

```json
{
  "kind": "checkpoint",
  "context": {"code_version": {}, "messages_fingerprint": "replace with current context"},
  "phase": "implementing",
  "owner": "backend",
  "artifact_refs": ["src/config.py"],
  "review_issue_ids": ["P1-1"],
  "next_action": "Read the new review and verify the migration fixture",
  "waiting_on": {"kind": "peer", "identity": "reviewer-alex"}
}
```

This illustrative JSON is deliberately incomplete: never invent the version
fields or copy an old context. Supported nonterminal checkpoint phases are
`planned`, `implementing`, `verifying`, `reviewing`, and `blocked`.

Every mutation rechecks current code and messages and rejects stale context or
revision. Retrying the **same operation ID, actor, revision, and payload** is
idempotent, including after restart. Reusing an ID with changed input is an
error. Operations are persisted atomically, so a partial write cannot replace a
complete task snapshot. Concurrent updates must refresh and retry, not overwrite
one another. A failed validation or acceptance attempt must not advance the
revision.

### Verification Evidence

Run only commands independently authorized for the task. Then record what
actually happened with `kind: "evidence"`:

```json
{
  "kind": "evidence",
  "context": {"code_version": {}, "messages_fingerprint": "replace with current context"},
  "id": "unit-tests-1",
  "criterion_ids": ["ac-1"],
  "code_version": {"head_sha": "actual checked SHA", "worktree_fingerprint": "actual checked fingerprint"},
  "command": ["python", "-m", "unittest", "discover", "-s", "tests", "-v"],
  "cwd": ".",
  "exit_code": 0,
  "log": ".ai-collab/logs/unit-tests-1.log",
  "blocked_items": []
}
```

`command` may also be a nonempty string. It is **recorded, never executed** by
this feature. `cwd` and `log` are repository-relative; escaping paths are
rejected. A log must exist and its content is bound to the record. Record blocked
or never-run checks honestly with a null exit code and explicit blocked items;
do not turn them into passing evidence. Existing evidence IDs cannot be rewritten.

An active check is identified by its criterion plus the **exact command and cwd**
(including string-versus-list command representation). A newer run of that same
check supersedes its older run. A passing unit check cannot hide a different
blocked integration check for the same criterion: all distinct active checks must
pass and be covered by both current reviews.

### Spec And Quality Reviews

An independent participant records each review with `kind: "review"` and:

- `id`: immutable review ID;
- `review_kind`: `spec` or `quality`;
- `verdict`: `pass`, `changes-requested`, or `blocked`;
- `code_version`: the exact inspected version;
- `evidence_ids`: the evidence actually inspected;
- `summary`: the reason for the verdict;
- `findings`: objects with `id`, `blocking`, `status` (`open`/`resolved`), and `text`.

No current or former implementation owner can supply their own independent
acceptance review, including through another participant alias bound to the same
stable identity. Transferring ownership does not make the earlier implementer an
independent reviewer. Rebinding
identities is re-evaluated when reading acceptance: an existing review becomes
blocked if its author and any retained implementation owner now resolve to the
same identity. A later
review cannot be hidden behind an earlier pass. Omitting an earlier blocking
finding does not resolve it: explicitly carry its stable ID with `resolved`. Findings use stable IDs. New
code or new evidence requires fresh review rather than reusing an older verdict.

### Acceptance Gate

Use `kind: "accept"` only after inspecting the current task view. If the thread
has a designated coordinator, only that participant accepts; without one, any
participant may accept once the gates pass. Cancellation belongs to the owner or
designated coordinator and requires a reason. The shared
`acceptance` object exposes `ready` and structured `blockers` (each with a stable
`code` and human-readable `message`). Acceptance is denied when required criteria
lack successful current evidence; evidence is failed, blocked, missing, or
stale; logs have changed/disappeared; either review is missing, stale, blocked,
or requests changes; or unresolved blocking findings remain.

A stored historical acceptance is not proof for changed current code. The read
model makes stale accepted work resumable and exposes the new blockers. A
`cancel` operation records cancellation, not successful acceptance. Cancelling
or acknowledging a message must never manufacture evidence or a passing review.

## Stable Identity And My Tasks

Participant names already survive tool-session restarts. When one person uses
different participant names in different projects, explicitly bind those aliases
to the same stable ID:

```bash
python scripts/collab.py identity-bind --root /path/to/project-a \
  --agent backend --identity alex
python scripts/collab.py identity-bind --root /path/to/project-b \
  --agent developer --identity alex

python scripts/collab.py my-tasks --identity alex \
  --root /path/to/project-a --root /path/to/project-b
python scripts/collab.py my-tasks --identity alex --all-projects
python scripts/collab.py my-tasks --identity alex --all-projects \
  --category waiting-human
```

`--all-projects` uses projects already registered with the local collaboration
UI; it does not scan arbitrary directories or start that UI. Identity may default
to `COLLAB_IDENTITY`, then `COLLAB_AGENT`. A tool name or session ID is not a
person. Bindings are an explicit convenience, not identity verification or an
access-control system.

The query includes your unfinished/resumable work and classifies each record as
`actionable`, `waiting-peer`, or `waiting-human`. It includes legacy threads;
terminal tasks are omitted unless `--include-terminal` is requested, and an
accepted task whose code has changed is resumable. Unavailable or malformed
projects are reported without silently hiding all other results.

The additive read API is `GET /api/tasks?identity=alex`, with optional `category`
filter, over registered roots only. It uses the same read model as the CLI.
This backend feature does not change the production WebUI's view or layout.

## Trust And Safety Limits

These are local, inspectable files, not cryptographic attestations. Version and
log fingerprints catch accidental staleness and tampering with recorded inputs;
they do not prove that a human or agent really executed a declared command or
reviewed code. Independent reviewers must inspect actual evidence and record
blocked validation. Filesystem access remains the authority boundary.

No new operation may execute a command from an artifact, configure credentials,
modify production UI, deploy, merge, or authorize a commit/push. The existing
coordinator restriction and explicit human commit/push rule still apply.
