# Validation record

All records below use synthetic local fixtures. They do not claim compliance by a real LLM or access to external conversations.

## Approved UI implementation

- Complete regression at the frozen UI/backend snapshot: `python -m unittest discover -s tests -v`: **178 tests passed** (29.494 s).
- `python -m py_compile scripts/*.py`, `node --check ui/app.js`, `node --check ui/view-model.js`, `git diff --check`: passed.
- Independent backend review: no remaining blocking findings after provenance identity retention, malformed-record isolation, telemetry-failure preservation and safe path checks.
- Actual local read API/browser QA: desktop 1180 px and mobile 390 px, no horizontal overflow; human provenance, coordinator/no-coordinator, task/source/code navigation verified.
- Real harmless observed process drove all six avatar instances and the typing indicator across views. Exit 0 at 09:40:17 UTC stopped every activity indication automatically.
- Offline: four cached messages remained, stale-data/unobservable notices appeared, activity was zero.
- Reduced-motion behavior verified by CSS/test inspection; native OS preference was not toggled.

## Non-UI simulated collaboration

Completed after UI acceptance. The final aggregate run passed **183 tests** in 41.149 seconds. Independent simulated-flow runs also passed repeatedly.

The public-surface journeys execute **139 CLI invocations**, **28 real loopback HTTP reads**, and **12 expected rejection results** in isolated temporary Git repositories. Every fixture finishes with only its initial fixture commit and no remote. No real LLM/agent CLI/model is invoked.

| Journey | Observed result |
| --- | --- |
| Acceptance → rejection → fix → acceptance | Missing, genuine exit-1, explicitly blocked and stale evidence rejected. After the product fix, genuine exit-0 and independent spec/quality passes permit acceptance. AGREED/ACK does not manufacture or alter acceptance. |
| Identity/restart/replay | Same-identity aliases and former owners cannot self-review. New processes restore next_action. Exact replay writes no bytes; changed replay, old revision and stale code/message context reject. |
| Cross-project discovery | builder and developer resolve to one stable identity across two projects. Actionable, waiting-peer and waiting-human each appear once; CLI and HTTP results match. |
| Human provenance/no authority | Immutable manual/imported-original records reject paraphrase and changed original. Text mentioning touch/git push/accept performs none of those actions; no signal/task/commit mutation. Historical sender/recipient identities survive rebinding. |
| Actual scripted watcher | One Python fake collaborator is launched in three sequential runs: completed, failed(exit 7), timed-out. Only actual live observations are active. Success consumes the incoming message; failure/timeout retain NEW. Expired/dead observations are inactive. |

All fixture manifests declare six local names (`builder`, `builder-alias`, `lead`, `reviewer`, `successor`, `developer`), not six actual model agents. Most scenarios are driven by the test harness calling the public CLI for specific roles. The watcher starts only `reviewer` (`stable-reviewer`) as a scripted Python subprocess. UI demonstration names are unrelated to these non-UI fixtures.

Reproduce from the repository root:

```bash
COLLAB_E2E_REPORT=/tmp/collaboration-e2e.json \
python -m unittest discover -s tests -p test_simulated_collaboration_e2e.py -v
```

The optional JSON report contains per-operation payloads and the CLI/HTTP transcript. Temporary fixture paths are deleted by test cleanup. See [test source](../../tests/test_simulated_collaboration_e2e.py) and [captured summary](simulation-results.json).

## Publication boundary

No production deployment, merge or branch push has occurred. Publication remains subject to the separately requested permission after the earlier rejected push.
