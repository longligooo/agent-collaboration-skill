# Provider execution boundary (MVP)

This package never runs a model in `discover`; GET-style `discover()` is cache-only.
Explicit refresh performs bounded noninteractive probes from an empty temporary
cwd, using a fixed argv and a filtered environment. Existing CLI account stores
remain the provider's responsibility; this package does not open their auth/keychain
stores. It reads bounded trusted user config to import allowlisted auth/routing
settings into a private subprocess environment.
Account responses are reduced to status, not identity, email, keys or raw stderr.
Discovery may contact each provider's catalogue/auth services; it is not an offline
check. It does not request login, import, onboarding, installs, or explicit token refresh.
Providers may still perform normal authenticated catalogue calls or internal
credential maintenance; no stronger no-refresh guarantee is claimed.

## Trust assumptions, not an OS security claim

Installed CLI executables and administrator-managed policies are trusted. The
service cannot make a hostile executable or mandatory startup hook safe. Environment
runtime injection flags are not inherited; explicitly named credential variables
and existing configuration locations are passed to the CLI, never exposed in a
public response. `LaunchSpec.stdin` and `.env` are excluded from its repr.

### Claude

A fresh authenticated discovery must confirm required flags and a conservative
supported version window, 2.1.301 <= version < 2.2.0. The executable identity/stat is
rechecked before constructing a launch. A changed binary requires rediscovery.

- Planning/coordinator: `--tools ""` and `--disallowedTools "*"` remove all model
  tools. This is not `--allowedTools []` (which only controls auto-approval).
- All phases: safe/restricted mode with fixed `disableAllHooks`, disabled slash commands, empty strict MCP
  configuration, no session persistence, finite turns, JSON output, and permission
  prompts set to `none`. No bypass or dangerous permission flags are accepted.
- Workers use finite file tools; on supported Linux/macOS hosts with the documented
  sandbox flags/dependencies they also expose Bash, never an unconditional Bash
  allow rule. Fixed inline `--settings` requires sandbox startup, disables retries
  outside it, constrains network and protected filesystem paths, and enables
  credential environment scrubbing. Native Windows lacks this provider sandbox:
  worker remains file-only with `claude_sandbox_requires_wsl2`. Missing Linux
  dependencies likewise stay file-only; nothing is installed automatically.
- Isolated managed checkout cwd is required by production runtime. Original project and
  service-state reads/writes are restricted; `.git`, `.claude`, `.codex`, protocol
  state and automatic metadata writes are protected. Tests may run only within
  the provider sandbox; successful process exit still is not acceptance.
- Admin-managed settings still apply, including trusted hooks and potentially
  additional working directories. This is a model-tool restriction, not a promise
  of arbitrary host isolation or immunity from administrator policy. A deployment
  requiring a stronger boundary must supply that boundary rather than remove flags.
- Claude 2.1.317 was tested through an actual OpenRouter free-model synthetic
  planning, two-worker file write/read and coordinator summary cycle in a managed
  checkout. All four jobs succeeded; the runtime correctly stopped awaiting
  verification and did not apply changes to the original project. The model
  falsely claimed a trailing newline, illustrating why job success is not
  acceptance. Native Windows execution and sandboxed Bash remain untested.

Claude's SDK `supportedModels()`/initialization result is the documented catalogue
surface, not an invented `claude models`. The stdlib-only adapter currently reports
`catalog_status=unknown`; configured local model IDs are labeled unverified, or
`catalog_reason=sdk_discovery_not_available` when none are configured. Users can
choose provider default or a syntactically valid model, but availability is not
promised. No SDK is installed or queried through a model call.

### Codex

Discovery verifies effective config, feature switches and the named permission
profile before account/read (no token refresh) and bounded model/list. It never
starts a model turn. Execution repeats verification, starts the expected profile,
and uses a duplex broker that rejects unexpected approvals/tool requests. Planning
has no shell/delegation/MCP; workers get the named isolated-checkout sandbox with
network disabled and control paths denied. Only an acknowledged turn receives
messages, and only a matching successful terminal event supplies final output.
The broker flushes denial responses before stopping the owned process tree.
Models shown by discovery are not guaranteed account execution entitlements.

### agy

This is a separate adapter, not a Gemini executable alias. `agy models` has no
stable documented machine-readable format, so text output is not guessed into a
model list. Auth remains unknown; no onboarding or import is attempted. Headless
support is help-observed. Execution is deferred from v1 by the user; only Codex CLI and Claude Code are in v1.

## Sources checked 2026-11-02

- https://code.claude.com/docs/en/cli-reference (tools, disallowedTools wildcard,
  safe/restricted, strict MCP, permission-prompts, output format)
- https://code.claude.com/docs/en/permissions (working directories, resolved symlink
  checks, acceptEdits does not auto-approve outside-working-directory writes)
- https://code.claude.com/docs/en/agent-sdk/typescript (supportedModels)
- https://learn.chatgpt.com/docs/app-server (account/read and model/list)
- https://learn.chatgpt.com/docs/config-file/config-reference (shell feature and
  managed settings do not establish a universal empty tool surface)
- https://antigravity.google/docs/cli/headless/ (headless and JSON result)
- https://antigravity.google/docs/cli/gcli-migration/ (separate migrated CLI)

Process control uses POSIX owned process groups or native Windows pre-associated
Job Objects, never a `.cmd`/shell fallback. Windows native ABI execution remains
untested here; fixture/mocked tests are not native-host proof. Runtime/probe pipe
I/O is threaded and bounded. Windows immutable files are content-flushed before
atomic publication; directory-entry power-loss durability is not promised.

Review hardening: worker deny rules cover exact `.git` (worktree pointer file),
exact `.ai-collab`, descendants, and server-owned `.scratch/automatic-*` ticket/spec
paths. Runtime-generated acceptance documents must use that protected prefix.
Credential environment entries are provider-scoped: Anthropic entries are not
forwarded to Codex/agy, nor OpenAI/Google entries to unrelated providers.

## Sandbox/admin boundary

Claude restricted mode excludes user/project settings, not administrator policy.
Managed policy may override session booleans or add policy-approved directories,
hooks and proxy settings. This implementation trusts local administrators and
installed executables; it neither edits nor bypasses their policy. It does not
invent an effective-config CLI command. Sandbox startup failure is fail-closed;
unsupported native Windows remains explicitly file-only, not full test autonomy.

Additional official sources: https://code.claude.com/docs/en/sandboxing,
https://code.claude.com/docs/en/settings-reference,
https://code.claude.com/docs/en/env-vars,
https://learn.chatgpt.com/docs/permissions.

Codex native Windows npm discovery resolves only the documented global-prefix or
local `node_modules/.bin/codex.cmd` layouts to the adjacent verified package's
`vendor/<target>/bin/codex.exe`. It never runs/parses the shim, calls cmd.exe,
installs a package, or guesses arbitrary script paths. Package/version/platform
metadata, containment and PE architecture are checked; wrapper and native payload
identity/stat are revalidated before launch. Metadata is not code signing: the
installed binary remains trusted. Unknown layouts fail with a specific resolver
reason. Windows fixture paths/PE headers are synthetic, not native execution proof.


Custom Codex provider state is `configured`, not `authenticated`, when the
protocol explicitly says no OpenAI auth is required and a trusted credential
source, endpoint and model are configured. The exact effective provider table
and model are privately bound across discovery and execution. Only that configured
model is eligible; bundled catalog models are not gateway entitlements. Provider
fallback is explicitly false and thread-returned model/provider must match before
a turn is sent. No remote credential-validity claim is made. Optional analytics
and OTEL exporters are disabled and verified. Real Codex execution remains
blocked by the test host's nested sandbox; no sandbox controls were weakened.
