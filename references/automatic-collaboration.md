# Opt-in automatic collaboration (single-machine MVP)

This service is separate from `collab.py watch`. Existing manual CLI/watch and
historical threads retain their behavior. It uses only already registered projects;
there is no HTTP project creation or arbitrary-path execution endpoint.

## Start explicitly

After registering a project with the existing local UI/CLI workflow, run:

```sh
python scripts/auto_service.py serve --port 23452 --max-workers 2
```

Keep that terminal open. Open its loopback URL, enter the **local access token**
printed in that terminal, then use the automatic-task entry inside a project.
The token is a local service credential, not a provider/API key. Do not put it in
URLs, screenshots, chat, or shared logs. A restart issues a new token; task state
remains on disk. Stop with Ctrl-C. Do not start this command as an ordinary skill
turn or read-only inspection: starting the service enables execution of previously
authorized queued automatic tasks.

The legacy UI on port 23451 remains read-only for automatic tasks; it does not
silently start a supervisor. The automatic server refuses `/api/register`.
Register projects through the existing authorized local CLI workflow first.

## Current provider support

Discovery runs only when the local owner requests it. GET requests and importing
modules do not run provider processes or models. Discovery uses the server user's
PATH and configured local CLI authentication, never a browser-supplied executable.

| Provider | Discovery | Automatic execution in this MVP |
| --- | --- | --- |
| Claude Code | Installed/auth/help/version; SDK model catalogue remains unknown when no supported SDK integration exists | Known, capability-confirmed CLI versions only: tool-free coordinator; restricted file tools and, where supported, sandboxed Bash/tests. No delegated agents, MCP, unrestricted fallback or permission bypass. |
| Codex CLI | App-server account/read, paginated model/list, effective feature/profile verification | Experimental permission-profile protocol: read-only coordinator without shell; worker shell/tests only inside verified minimal-read, managed-workspace-write, network-disabled profile. Unknown schemas/policies fail closed. |
| agy CLI | Separate adapter; help and `agy models`; unstructured catalogue stays unknown | Deferred by explicit user choice for v1. No SDK installation or API-key billing substitution. |

Exact version and flag checks, limits, source links, and trust assumptions are in
[`scripts/auto_providers/SECURITY.md`](../scripts/auto_providers/SECURITY.md).
A model catalogue is not a guarantee that every listed model is available to this
account. An unknown catalogue allows the tool's default model, not fabricated
model choices. Capability discovery expires; explicitly refresh before retrying.
There are no installs, onboarding/import flows, authentication changes, or automatic
credential writes. The installed CLI and administrator policy remain trusted;
model-tool restrictions are not a sandbox against a malicious local binary/admin.
Owned process control supports POSIX process groups and Windows 10+ Job Objects.
Native Windows execution tests are explicitly separate from Linux mock tests;
unsupported Job Object policy fails closed without an uncontained fallback.

## Approval and execution

1. Creating a task queues only coordinator planning. It does not approve workers.
2. The plan proposes stable participants, provider/model bindings, task dependencies,
   acceptance criteria, and shared-artifact references. The coordinator does not edit
   product files. Draft edits create a new plan revision/hash.
3. Git projects may contain staged, unstaged and untracked work. The service
   never cleans, stashes, resets or commits your changes. Before creation/approval,
   metadata-only preflight reports unsupported paths or size limits. After approval,
   the first worker captures current tracked and non-ignored untracked file contents
   into an isolated **managed snapshot**, with a separate immutable baseline.
   Ignored untracked dependencies and known credential/control paths are not copied;
   omissions are reported. Embedded secrets in ordinary source cannot be detected
   reliably. Concurrent edits detected during capture stop publication for retry.
4. Approval must match the exact pending task revision, plan revision, and hash.
   Only then may the runtime finalize an immutable thread manifest and queue workers.
   Workers execute outside the original project, without its Git metadata.
   Original HEAD/index/branch/files remain unchanged. Changes are compared against
   the captured snapshot, **not HEAD**, and remain **pending application**. There is
   no automatic commit, merge, push or apply. Later original-project edits do not
   silently update this baseline. Applying results requires checking for conflicts
   with those edits; existing verification/review gates must run on applied code.
5. Within a checkout, execution is serialized. Cross-project concurrency is bounded.
   A legacy watch lock or another automatic runtime blocks execution. Do not bypass
   the lock with `watch --force` or run uncoordinated manual edits in that checkout.
6. Pausing prevents new dispatch and waits for the current boundary. Cancel requests
   stop owned processes; the task is not marked cancelled until termination is known.
7. Process success is **not acceptance**. Where provider sandbox support is unavailable,
   file-only workers cannot run shell tests.
   Native Windows Claude shell sandbox requires WSL2; file-only fallback is labelled
   as such and never falls back to unrestricted Bash. No WSL setup is performed.
   Existing coding-task verification/spec/quality gates remain required; automatic
   execution may finish at `blocked / awaiting_verification`.

Changing a participant's provider keeps their identity and takes effect only at a
safe boundary. Context handoff uses approved shared-artifact references and bounded
local state; it never assumes vendor sessions are interchangeable.

## Review/export and explicit application

The authenticated task view shows a bounded text diff and a change manifest with
add/modify/delete, before/after SHA-256, sizes and executable bits. Binary and large
files are identified even when they have no text patch. Truncation labels mean the
UI is not a complete export. A local, server-generated `artifact_directory` points
to an immutable `snapshot_changes_v1` bundle: `manifest.json` plus `after/` files
for additions/modifications, including binary bytes. Deletions are recorded in the
manifest. Bundles do not share writable files with the execution checkout and stay
stable across later turns. They can be copied locally for review; they are not
uploaded or served through an arbitrary-file API.

There is **no Apply button or apply endpoint in this MVP**. A human or separately
authorized local coding session can use the bundle for explicit application:

1. Pause the task and other editors. Inspect the complete manifest, snapshot ID,
   omissions and test evidence. Missing ignored dependencies or Git metadata may
   prevent tests; such a result is not verified/complete.
2. Before changing any original file, compare its current SHA-256 and executable
   bit with that path's `before` entry. An addition requires the original path to
   remain absent. Reject symlinks/reparse paths. Any mismatch is a conflict: resolve
   it explicitly, never force-overwrite newer user work. `base_commit` is not this
   comparison baseline.
3. Only after explicit application authorization and all conflict checks, copy the
   corresponding `after/` bytes or apply the recorded deletion. Preserve unrelated
   files and the original Git index; do not silently stage, reset, stash or commit.
   Recheck paths immediately before writes. No built-in tool promises an atomic
   multi-file merge against uncooperative concurrent editors.
4. Review the resulting diff and rerun the project's existing verification and
   acceptance gates on the original project. Commit/push still require separate
   authorization.

## Human input and privacy

`@participant` addresses that stable participant, `@all` snapshots the approved
roster, and no mention defaults to the coordinator. Unknown/ambiguous mentions are
rejected. Before approval, only the coordinator is eligible. Every recipient has a
separate queued/delivered/failed record. Delivery means bytes were handed to a
provider's next-turn input—not live injection, model comprehension, or acceptance.

Original human text, local provider output (which may echo human text), and attempt
evidence stay under `.ai-collab/local/automatic/` and existing local human-events
storage. Shared protocol turns and generated tickets contain structural status and
references, not raw prompts/stdout. The authenticated task API provides local-only
outputs for the UI. Do not manually sync or copy local state into shared artifacts.

## Crash and recovery limits

SQLite transactions, idempotency keys, claim tokens and revisions guard dispatch.
An expired/lost lease or a crash during process launch becomes **unknown**, not
permission to run a duplicate. Unknown work quarantines that checkout. A leftover
watch marker is not automatically deleted just because a parent PID is gone:
children might still exist. Recovery requires inspecting and proving the previous
owned process tree is stopped; never use arbitrary HTTP assertions as proof.
Safe failed jobs can be explicitly retried; uncertain execution is not retried.

## Validation boundary

Tests use fake CLIs and temporary registered projects. No real external model calls
were performed for development acceptance. Fixture browser demonstrations must be
labelled simulated; they do not establish real-provider account availability.
