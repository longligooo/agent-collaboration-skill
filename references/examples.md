# Collaboration Examples

## Design Discussion

1. `lead`, the coordinator, publishes a `PROPOSAL` requesting `CHALLENGE` from `frontend`. `lead` does not edit product code.
2. `frontend` inspects the current architecture and publishes a `CHALLENGE` requesting `REVISION`.
3. `lead` publishes a `REVISION` requesting `REVIEW` from `backend`.
4. `backend` verifies the revised constraints and publishes a `DECISION` with `AGREED`. None of them commit unless the human explicitly asked for a commit.

## Implementation And Review

1. An implementer publishes an `IMPLEMENTATION` turn with the changed files and tests, requesting `REVIEW`. The turn does not include a commit unless the human explicitly requested one.
2. A reviewer publishes a `REVIEW` with stable `P0`, `P1`, and `P2` finding IDs.
3. The implementer publishes another `IMPLEMENTATION`, retaining every prior finding ID and its claimed disposition.
4. The reviewer re-runs targeted evidence and publishes `VERIFICATION` or another `REVIEW`.
5. The coordinator publishes `CLOSURE` only after blocking findings are closed or explicitly accepted by a human.

## Independent Verification

1. A proposer publishes a benchmark claim and requests `VERIFY`.
2. The verifier reconstructs the environment from repository artifacts, not the proposal's conclusion.
3. The verifier publishes commands, raw results, limitations, and a `READY` or `NEEDS_CHANGES` status.

## Human Redirect

1. Agents are discussing a database migration.
2. A human removes the migration from scope and asks for an API-only plan.
3. The active agent stops the old action, publishes a `REVISION` that records the human override, and requests a new `CHALLENGE` against the reduced scope.

## Duplicate Signal

1. A scheduled scanner receives the same `message_id` twice.
2. `collab.py inspect` returns `DUPLICATE` on the second scan.
3. The agent exits without creating a turn, modifying code, or rewriting state.
