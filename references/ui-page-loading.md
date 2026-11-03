# Page-scoped UI reads

The read-only WebUI uses one `/api/page` request for its current route. It no
longer calls the progressive `/api/snapshot` cursor protocol or polls background
snapshot jobs. The legacy snapshot API remains available to existing callers.

## When content is fetched

- Initial entry, browser reload, route changes, explicit Refresh, thread search
  submission and pagination fetch the current route.
- Thread lists (global, per project and per person) contain **20 rows per page**.
- Opening a selected thread reads that thread's full conversation and acceptance
  details, plus its paginated sidebar. Other threads do not get rich projections.
- Expanding a message, changing its local display filter, idle time, and returning
  to a visible browser tab do not trigger a content reload.
- Runtime activity still has a separate project-scoped heartbeat. It does not
  fetch conversation content or evaluate code acceptance.

## Cache-first display

Successful page responses are saved in localStorage, keyed by UI origin/path,
registry scope, schema, route, selected project/thread/identity, search and page.
On entry, a valid cached page is shown immediately. A silent request replaces
that entire page only after success. Deleted records therefore disappear rather
than being merged back into an older page. Errors preserve cached data with an
explicit stale-data message. Old in-flight responses cannot replace a newer
route or page. Changed registry scope invalidates that UI namespace's cache.

The cache is best effort: disabled storage, invalid JSON or quota failures do not
prevent server reads. At most 12 page entries are retained. Entries exceeding
1.5 million serialized characters remain memory-only. No automatic-service
credentials or authorization tokens are stored in this cache. Conversation and
task data explicitly viewed in the read UI **are** stored locally; clearing the
site's localStorage removes persistent copies. This is not server-side storage.

## Backend cost and limits

Projects and automatic-task entry pages read only registered project shells and
thread directory counts. Lists use a lightweight, bounded in-memory metadata
index for exact global totals, search and recorded-activity ordering. A cold
service must read manifests and bounded turn headers across the requested scope;
this is not a claim that only 20 files are read. Warm requests check metadata and
reparse only changed index records. Project routes never index unrelated roots.
Human-event identity/timestamp indexing validates stored event JSON before using
its provenance; bodies are discarded from the index and list payloads.

List requests never scan source files, evaluate task acceptance, or build full
conversation histories. Selected list rows alone inspect their inboxes for
notification badges. Overview aggregates inbox counts because that page displays
them, returning at most 20 attention entries and 12 recent events. Collaborator
pages do not pretend uncomputed notification totals are zero. The tasks page
still computes the selected identity's real task/acceptance projection; it is
not triggered by navigation to unrelated pages.

Two actual page reads may execute concurrently; identical in-flight page queries
share work. Each has a 30-second cooperative budget. There is no cursor, async
job polling or completed-result replay. A blocked OS read cannot be forcibly
terminated: it retains capacity until returning, and excess independent reads
receive an explicit error instead of creating unlimited scanning workers.

## Validation

Run `python -m unittest discover -s tests -v`, the existing Node UI tests, and
`python tests/page_scale_smoke.py`. The last command creates only temporary
synthetic data: 12 Git projects, 1,200 threads, 12 tasks and 1,800 source files.
It reports cold/warm timings, returned rows, manifest reads and rich projections;
checks cross-page search, one changed record, deletion and last-page clamping.
`tests/test_ui_pages.py` also tests coalescing, saturation, timeout recovery and
an actual HTTP client disconnect. The optional browser fixture is
`node tests/ui_refresh_browser.cjs`; it requires an available Chromium runtime.

After updating code, stop and restart the existing UI process. Refreshing the
browser alone does not reload Python modules. See [version checks](ui-read-recovery.md).
