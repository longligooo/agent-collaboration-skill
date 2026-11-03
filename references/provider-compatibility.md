# CLI discovery compatibility

Installed is not the same as ready. Discovery does not run a model, request login or credential refresh,
or start an agent thread. The installed CLI may internally refresh its stored
credentials; the adapter cannot guarantee otherwise. `auth_status: unknown` may mean an
earlier capability check failed; it is not proof that the user is logged out.

## Codex effective configuration

Codex app-server may serialize optional permission-profile fields as `null` even
when the startup override omitted them. The adapter ignores only known null
fields at their exact profile/filesystem/network schema locations. It still
requires the actual access rules to match, including wire types. Unknown fields,
non-null proxy/inheritance settings, extra filesystem grants, missing required
rules and enabled network access fail closed. Do not work around a configuration
error by removing sandbox verification.

Real discovery was tested with Codex `0.166.0-alpha.1` in a fresh empty config home:
configuration, feature and permission-profile checks completed, the model catalog
was returned, and authentication was correctly reported as unconfirmed. This is
not an authenticated model execution test.

## Reproduce without account credentials

`python tests/provider_cli_smoke.py --path PATH_TO_TRUSTED_BIN_DIRECTORIES`

Use the operating system's PATH separator when specifying multiple directories.
The opt-in script creates a temporary empty config/credential home and prints
only sanitized discovery records. The executable directories must be trusted.
Missing login is expected. It makes no model call or login attempt, but tools may
contact vendor services as part of discovery. It cannot substitute for native
Windows tests when run on Linux.

For user-side diagnosis, run `where.exe codex`, `where.exe claude`,
`codex --version`, and `claude --version` in the same PowerShell environment used
to launch the WebUI. Do not send config files, API keys or login tokens.

## Claude native CLI and Windows npm launchers

Claude Code 2.1.317 does not list `--max-turns` in ordinary `--help` output.
Discovery checks all visible isolation flags and the existing supported version
range. When the turn-limit flag is hidden, it performs two read-only parser
checks: a deliberately unknown option before `auth status` must be rejected,
and `--max-turns 1 auth status` must return valid authentication status JSON.
No prompt or model session is started. Help success alone is not capability
proof because the CLI accepts unknown arguments together with `--help`.
Runtime launch retains the explicit turn limit, and missing login remains a
blocking condition. This proves argument acceptance, not model execution.

On native Windows, the current npm native package layout can be resolved from
`claude.cmd` to `node_modules/@anthropic-ai/claude-code/bin/claude.exe`, for either
global prefixes or local `node_modules/.bin`. The adapter validates package
metadata, path containment and the PE machine header. It never executes or
parses the `.cmd` wrapper and never invokes a command shell. The installed
executable remains trusted; these checks are not an authenticity signature.

Legacy JavaScript-only wrappers or other package-manager layouts return
`claude_native_windows_install_required` or `claude_npm_layout_unavailable`.
Use the [official native installer](https://code.claude.com/docs/en/setup), open
a new terminal and verify `where.exe claude` selects the native executable before
restarting WebUI. Installing a native CLI does not automatically log in or prove
its sandbox is working. Native Windows uses file-tool-only workers; sandboxed
shell execution requires a supported environment such as WSL2.

## Reusing Claude authentication and model configuration

`--bare` was unsuitable for ordinary local CLI reuse: Claude 2.1.317 explicitly
ignores OAuth/keychain credentials in that mode. Launches now use `--safe-mode`
with `--restricted`, the existing finite tool list and strict MCP settings.
Safe mode preserves CLI authentication while disabling customizations such as
hooks, skills and plugins. Discovery checks authentication again using those
same safety flags; merely finding a token variable is not treated as proof of
valid credentials or successful model access.

`--restricted` intentionally does not load arbitrary project/user settings.
Only the explicitly allowlisted authentication/provider/model environment
settings are imported; hooks, permission rules, shell startup scripts and
arbitrary environment injection are not inherited. Locally configured model
IDs are shown as local configuration, not as a verified provider catalogue.
A missing model selection leaves the CLI to use its imported local default.
The scope of reuse is the service's user configuration and launch environment;
project-specific settings and shell aliases/functions are not automatically
executed. No separate WebUI provider configuration should be required for the
supported user-level settings, but successful login/model execution still
requires testing with that user's authorized account.

### Supported inherited settings and boundaries

Start WebUI with the same OS account and environment used for the CLI. It keeps
HOME/USERPROFILE, provider config-home paths and native CLI login stores; it does
not copy credentials into WebUI settings. Terminal-only exports must exist in the
process launching WebUI. Shell aliases/functions and shell rc files are not run.

Claude imports only authentication, gateway and model fields from the trusted
user `settings.json` (`CLAUDE_CONFIG_DIR`, otherwise `~/.claude`). Process values
override the imported defaults. `apiKeyHelper`, hooks, plugins, permissions,
project settings and arbitrary `env` entries are not imported. The protected CLI
still trusts administrator-managed policy. Local authentication status means the
CLI recognizes an auth source, not that the gateway has validated its credential.

Codex keeps its own user config and login store. On Python 3.11+, the bridge also
passes a credential environment variable referenced by the selected user
`model_provider` (including the configured default profile), provided its name
is credential-shaped: API_KEY, AUTH_TOKEN, ACCESS_TOKEN or TOKEN, with optional
uppercase prefix. Inactive providers' variables are not copied. Existing trusted
CLI auth helpers are not executed by WebUI itself; helpers needing arbitrary
extra environment, project-specific providers or other credential variable names
are not supported automatically. On older Python versions only the fixed
credential allowlist and CLI-managed file/keychain authentication are retained.

HTTP(S)/SOCKS proxy routing and certificate-authority paths are allowlisted; TLS
verification-disable switches, runtime injection and arbitrary process variables
are not. Gateway URLs must use HTTPS (HTTP is supported on literal loopback or
localhost), and endpoint/proxy URLs containing userinfo, query or fragment are
rejected with a sanitized error. Use dedicated credential variables, not URLs.
These values and credentials never appear in discovery records. A changed
inherited environment after discovery requires a new discovery before launch.

Claude's documented nonessential-traffic, telemetry, error-reporting and automatic
update opt-outs are fixed on for these supervised subprocesses. They avoid
unrelated vendor traffic when using a third-party gateway; they are not a network
firewall. See [Claude environment variables](https://code.claude.com/docs/en/env-vars).


### Verified live scope (2026-11-03)

With Claude 2.1.317 and the explicitly selected OpenRouter free model
`nvidia/nemotron-3-ultra-550b-a55b:free`, the actual supervisor completed planning,
writer, checker and coordinator-summary jobs against synthetic data. The managed
checkout contained `TEST_OK`; the original project was unchanged. The service
stopped at `awaiting_verification`, not accepted/completed. The artifact lacked
the requested trailing newline despite model claims, so semantic acceptance did
not pass. OpenRouter reported zero usage charge for this test. No paid fallback,
account login or persistent credential configuration was used.

Codex 0.166.0 stable and 0.166.0-alpha.1 both passed empty-home discovery.
Authenticated Codex execution remains unverified: the cloud build hit its sandbox
helper setup boundary, and the standalone stable binary attempted a vendor
metadata destination outside the OpenRouter-only test authorization. No sandbox
or egress restriction was disabled. The custom-provider protocol support described below distinguishes configured
credentials from remotely verified credentials; it is not a promise that every
custom provider or helper is supported.

## Custom Codex provider readiness

The app-server explicitly reports `account: null, requiresOpenaiAuth: false` for
providers that do not need an OpenAI account. This is not a requirement to log
into OpenAI, nor evidence that gateway credentials are valid. For a safe explicit
custom endpoint, configured model and credential source, WebUI now reports
`auth_status: configured` (local configuration, remote unverified). A nonempty
allowlisted env-key value or a trusted CLI `auth.command` counts as a configured
source; a helper's existence does not prove its success. Missing/unknown protocol
flags, absent credentials and incomplete provider config remain blocked.

Discovery privately binds the exact effective provider table and default model.
Execution revalidates this binding after reading configuration in the task cwd,
so project configuration cannot silently change the provider, endpoint, helper
or configured default model. The configured model is offered as a suggestion;
a user may enter another exact model ID. Codex may return a bundled OpenAI
catalog unrelated to the gateway, so catalog membership is not an allowlist.
Thread creation sends the exact model/provider with provider fallback disabled
and verifies both returned values before sending any prompt. The existing
sandbox, approval, permission-profile and tool checks remain unchanged.

Official 0.166.0 was tested offline with a synthetic custom env-key provider:
configuration/isolation discovery returned configured, the single local model
was preserved and the real launch contract was built. No gateway or model was
called in that check. Optional analytics and all OTEL exporters are disabled
and included in effective-configuration verification.

A real OpenRouter attempt with telemetry disabled passed that earlier egress
blocker, but thread creation still failed on the cloud's nested Linux sandbox:
`/tmp/codex-daemon-1000` is deliberately masked mode 000 by the outer sandbox;
the inner sandbox requires 0700. No permissions were changed, project-document
checks skipped or security boundary removed. Real Codex model/worker execution
still needs an appropriate non-nested environment and is not claimed here.

Sources: [account semantics](https://developers.openai.com/codex/app-server/),
[model selection](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/models-manager/src/manager.rs#L222-L238),
[experimental fallback default](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/app-server-protocol/src/protocol/v2/thread.rs#L59-L68),
[telemetry opt-out](https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/core/src/otel_init.rs#L64-L75),
[nested sandbox issue](https://github.com/openai/codex/issues/53799).

## Third-party model names

Model names are opaque provider data, not a whitelist. Users may type a model
name directly in the WebUI; the catalog supplies suggestions only. Persistence,
provider environment import, JSON requests, and Claude argv preserve the exact
string, including spaces, Unicode, `@`, brackets, and punctuation. Names must
contain a non-whitespace character, be at most 1024 characters, not begin with
`-`, and contain no C0/C1 control characters or UTF-16 surrogates. These checks
protect transport integrity, not model availability. Arguments are passed without
a shell. A configured or typed name is not evidence that a remote provider accepts
it, and Codex must still confirm the exact selected model before sending a prompt.


## MCP isolation without changing local CLI settings

Configured Codex MCP servers no longer make the whole provider unavailable. A
bounded inventory session initializes the app-server and reads effective config
without account/model/thread requests. The actual provider process receives a
structured `mcp_servers` override marking each discovered entry `enabled=false`.
Root-table encoding preserves dots/quotes as part of a server name. Empty-table
replacement is not assumed to clear inherited configuration.

Discovery and every actual launch repeat effective validation: all MCP entries
must be explicitly disabled before any thread is created. Inventory at the task
cwd covers project/managed entries too; a new enabled entry or ignored override
fails closed. The disabled table is carried into thread-start overrides. User
configuration files are never edited, and normal terminal CLI MCP use is unchanged.

`python tests/provider_mcp_smoke.py --codex /absolute/path/to/trusted/native/codex`
runs an opt-in real CLI sentinel test using a temporary synthetic home. Tested
with official Codex 0.166.0: three configured MCP commands (including dotted and
quoted names) did not run during inventory/protected discovery, and user config
bytes stayed unchanged. Planning and worker launch contracts were built. No
model or thread was started; actual thread-time suppression cannot be established
on this nested-sandbox host, and is not claimed by that test. Unit/protocol tests
also verify disabled entries at thread start, unexpected enabled entries and
unchanged permission checks.
