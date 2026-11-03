# Local UI reads and recovery

The browser opts into `async=1` on progressive snapshot requests. The server returns
HTTP 202 with a job token when a read needs longer than a short response window.
Polling that token reads the same result; aborting a fetch, navigating, reopening
the page, or clicking refresh does not create another identical running scan.
Only running work is shared. A fresh refresh never adopts an abandoned completed
initial result. Completed job tokens remain replayable for up to 120 seconds.

Each server permits at most two background reads, with a 45-second cooperative
budget per logical batch (initial metadata and final validation included). Git
waits within those reads are limited to ten seconds or the remaining budget,
whichever is shorter. Task projection source hashing uses the same thread-local
budget. The browser retains its original 60-second batch timeout across polling.
Job results have count/byte/TTL limits; this is not an unbounded work queue.

A cooperative budget cannot interrupt an operating-system call stuck in a disk or
network filesystem driver. A timed-out worker still occupies its slot until that
call returns. The UI reports timeout/busy instead of starting unlimited replacement
threads. Check unavailable project directories, slow Git/filesystems, or restart
the specific UI process after confirming its PID. Do not repeatedly refresh or
raise worker counts as a workaround. Existing completed records are preserved on
failure; a first load with no completed records cannot display a prior snapshot.
The synchronous legacy HTTP endpoints remain compatible and do not use the new
background job transport; this is not a global cancellation facility for all CLI
or detail operations.

## Confirm the running version

`GET /api/health` now returns `build.commit`, `build.source_root`, and a startup
`build.source_fingerprint`, in addition to PID and port. The fingerprint includes
Python and UI sources so it also detects dirty files and installed copies without
Git metadata. It describes the sources present when that server started, not an
assumption that a later `git pull` restarted Python.

`python scripts/collab.py ui --root .` compares that fingerprint before reusing a
running UI. If the old process lacks version information or differs, the command
reports its PID and asks you to stop that UI process and rerun. It never kills a
process automatically. Updating a checkout or refreshing the browser is not a
server restart. The auto-service also exposes startup build information.

## Reproduce the scale check

```sh
python -m unittest discover -s tests -p 'test_ui_job_recovery.py' -v
python -m unittest discover -s tests -p 'test_ui_snapshot_jobs.py' -v
python tests/snapshot_scale_smoke.py
```

The scale smoke test creates 12 temporary Git projects, 1,200 threads, 12 coding
tasks and 1,800 source files. It uses the real HTTP async transport, verifies the
complete summary, then verifies zero unchanged thread scans and exactly one changed
thread scan. It prints HTTP count, pending responses, elapsed time and maximum
HTTP response duration. It does not invoke models or modify real registered projects.
