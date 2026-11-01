# Validation — observability gaps

## Scope and publication (2026-10-30 validation snapshot)

Base: `longligooo/agent-collaboration-skill` main
`5a36cc5712eee0033fc0263ea7edf2e5fc39eda9` (checked before implementation and again after review).
Local branch: `fix/observability-gaps`. No commit, GitHub push, PR, merge, or deployment
was performed. Publication requires a new decision. Tests create commits/pushes
only in disposable local fixture repositories and bare remotes.

## Implemented behavior

- Global newest-activity ordering across projects, including empty/new threads,
  every valid turn time and explicit human occurrence/recording time. Separate
  `last_activity_at` leaves terminal `updated_at` duration semantics unchanged.
- Visible-page periodic reads and keyed browser reconciliation. Full read API
  snapshots remain the transport; unchanged messages keep nodes and expanded
  state, desktop/mobile scroll anchors and search focus. Late responses cannot
  replace newer state, repeated records do not duplicate, and offline retains
  the last loaded content with a warning.
- Both bundled wake scripts use actual subprocess observations. Existing vendor
  argv, resume, stdio and polling semantics remain. Telemetry has no prompt,
  session ID, environment, raw output, or raw exception argv.
- New original human inputs are local-only; legacy reads and recovery fingerprints
  remain compatible. Git-sync excludes private staging/commits, preserves staged
  human/business bytes, and fails closed on outgoing private ancestors or an
  unverifiable/ambiguous push destination. No migration deletes data or rewrites
  history. Already published content stays published; manual Git or private text
  copied into arbitrary shared files is outside this path-based safeguard.

## Tests and review

**Frozen final aggregate:** `python -m unittest discover -s tests -v` — **229 tests passed** in 47.457 seconds (original 183 plus 46 new regressions).

`python -m py_compile scripts/*.py`, `node --check ui/app.js`,
`node --check ui/view-model.js`, `node --check tests/ui_refresh_browser.cjs`,
`sh -n` on both wake scripts, and `git diff --check` passed.
Full local log: `/tmp/observability-regression-frozen.log`.
Independent review passed 61 focused tests, then separately rechecked all 13
refresh tests after the final timestamp-display change.

New suites cover:

- `test_ui_activity_sort.py`: real 116+1 project snapshot with colliding thread IDs,
  newest human-only activity, last-round duration preservation, equal/invalid
  timestamps and malformed timestamp history without crashing.
- `test_ui_refresh.py`: production app.js in a DOM harness; stable nodes,
  expansion, focus/selection, desktop and mobile anchors, bottom-follow,
  unchanged no-op polling, duplicate/late responses, cross-project collisions,
  offline, visibility/BFCache and interrupted manual refresh.
- `test_wake_adapters.py`: real harmless fake executable processes launched
  through both shell scripts; success, failure, missing binary, crash, expiry,
  unavailable telemetry storage, no fabricated busy state or leaked secrets.
- `test_human_privacy.py`: disposable Git/bare remotes; untracked, staged and
  tracked legacy/local records; unchanged index/business state; already-public
  records versus unpushed private ancestors (including later deletion); stale
  refs, distinct/multiple/rewritten destinations, alternate push policies,
  replacement refs, extra refspecs and tags.

Independent review found and verified fixes for push-URL privacy bypass,
mobile document scroll preservation and an invalid-timestamp history crash.
No remaining blocking findings were reported.

## Verification limits

No real vendor CLI/model, real external chat, desktop profile, private session
directory, or credentials were accessed. Fake CLI processes are explicitly
synthetic and do not establish real model compliance.

Actual Chromium rendering was **not executed**. A normal launch failed with
sandbox `socket() EPERM`; one scoped escalation failed environment setup with
`bwrap: Can't create file at /home/agent/.codex/sessions: Read-only file system`.
No forbidden read, browser launch, workaround or further retry occurred. The
optional `node tests/ui_refresh_browser.cjs` fixture is syntax-checked and retained
for a supported environment; it is not counted as a passing browser test.
Reduced-motion coverage is CSS/DOM regression coverage, not an OS preference
or actual-browser validation in this run.

External manual-session observation/import remains out of scope until the user
identifies their tool and launch workflow. This does not implement universal
chat interception, background chat collection or a new execution surface.

## Publication authorization — 2026-11-01

The user explicitly approved direct publication of this batch to `main` at
05:12 UTC. Main was rechecked at `5a36cc5712eee0033fc0263ea7edf2e5fc39eda9`;
no intervening commits were present. Fresh pre-publication regression on 2026-11-01: **229 tests passed** in
42.923 seconds. Python/JavaScript/shell syntax checks and `git diff --check` also
passed. Publication uses a normal fast-forward with an exact
expected-head guard; no forced ref update, credentials setup or protection bypass.
The browser limitation above remains unchanged; no new browser execution is claimed.
