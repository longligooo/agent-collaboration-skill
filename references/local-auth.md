# Local automatic collaboration authentication

Token validation remains enabled by default. Choose the authentication mode when
starting the service; there is deliberately no unauthenticated HTTP switch that
can change it while it is running.

```sh
# Default: requires local service token
python scripts/collab.py ui --root .
# Trusted local computer: no manual token input
python scripts/collab.py ui --root . --no-token
# Foreground service (prints token in this terminal when enabled)
python scripts/collab.py ui --root . --serve
```

Stop the old UI service before changing modes, then rerun the desired command.
Existing mode mismatch is an error, not a silent downgrade. The advanced
`python scripts/auto_service.py serve --no-token` entry supports the same option.
To re-enable tokens, stop the no-token process and restart without `--no-token`.
`--read-only` still disables the automatic service entirely.

## Finding your token

- Foreground (`--serve`): look in the terminal that started the service for
  `Local access token (paste only into this local UI): ...`.
- Background (ordinary `collab.py ui`): the command prints JSON with `token_log`.
  Open that exact local file in your editor and find the **last** startup's
  `Local access token` line. The file may include earlier starts. Re-running the
  same UI command with the same root/port reports the existing service and its
  token source without starting another process. A foreground service reports
  `token_source: terminal` and `token_log: null`: use the original terminal, not
  an old log. If that terminal output is lost, stop and restart the service. Do not send the file or token in chat.
- This is a local service token, not an OpenAI/Claude API key. The token changes
  each service restart. Paste it into the automatic collaboration connection form.
- Tokens are not returned by browser APIs, URLs, localStorage or cookies. The UI
  retains them only in its current JavaScript page memory. Browser reload/new
  tab requires reconnecting. No-token mode automatically connects after verifying
  the running service explicitly reports that token validation is disabled.

## Trust boundary

`--no-token` trusts other programs and users that can access this machine's
loopback interface. It is inappropriate on shared/untrusted computers, port
forwards, or reverse proxies exposing loopback remotely. It is not a promise of
OS-user isolation. Keep token mode on for those scenarios.

The service still binds only `127.0.0.1`. No-token API calls also verify loopback
bind/peer, exact local Host, same Origin when supplied, and Fetch Metadata.
An explicit `X-Collab-Local: 1` header prevents simple cross-site browser calls;
there are no permissive CORS responses. Forwarded and X-Forwarded-* headers
are rejected in no-token mode; stripped proxy headers or SSH tunnels cannot be
detected reliably and must not be used to expose this mode. Mutation requests still require the
memory-only CSRF value obtained from the session endpoint, JSON body, and
idempotency key. These checks prevent ordinary cross-site requests and DNS
rebinding, but do not authenticate a malicious program already running locally.

Authentication mode does not change execution approval. Starting the unified
WebUI still does not automatically resume historical tasks. “启用本次运行” remains
separate from authentication, and team approval gates are unchanged.
