# Start WebUI and automatic collaboration together

`python scripts/collab.py ui --root /path/to/project` now starts one loopback server on port 23451 with both the normal WebUI and automatic collaboration API/supervisor. It does not create a second server on 23452. Repeating the command registers the project and reuses that same process, only if source version, registry home and service mode match.

The result includes `automatic_enabled`, `token_source` and `token_log`. For a background token-enabled service, read the latest local access token from `token_log` and paste it into the automatic collaboration page. When reusing a foreground service, `token_source` is `terminal` and `token_log` is null: use its original terminal, not an older log. In no-token mode, `token_source` is `disabled` and no token is needed. Never share the token or log. Background logs are owner-readable on POSIX; on Windows they inherit the user's profile directory ACL. Health and CLI JSON never contain the secret. Keep `COLLAB_UI_HOME` in a private user directory.

For a terminal-owned service use `python scripts/collab.py ui --serve --root /path/to/project`. The token appears in that terminal. Ctrl+C or SIGTERM stops the shared service and its supervisor. For a detached service, inspect `/api/health`, verify the PID, and stop only that process. Restart after updating code. Port conflicts or service-mode/version/registry mismatches fail explicitly; the command never kills another service to take its port.

Starting the default UI does **not** resume historical queued jobs. Open a task and click **启用本次运行** to enable that task for the current service lifetime. Merely reading task lists or opening a page does not enable execution. Newly created tasks are enabled by the explicit create command, and worker execution still requires team/plan approval. Task queues are retained, not cleared. After another service restart, enable historical tasks again. A retried activation from an older service session is rejected; refresh and explicitly enable it with a new action.

`--read-only` opts out of automatic collaboration. Switching modes requires stopping the existing service first. The advanced `python scripts/auto_service.py serve --port 23452 --max-workers 2` entry remains available and retains its explicit automatic-service recovery behavior. Do not run two execution services against the same projects; checkout locks protect concurrent execution.

## Optional local token validation

Token validation remains enabled by default. For a trusted personal machine,
start with `python scripts/collab.py ui --root . --no-token` to connect without
manual token entry. Stop and restart before switching modes. See
[local authentication and token retrieval](local-auth.md) for terminal/log
instructions, re-enabling token validation, and local-process trust risks.
