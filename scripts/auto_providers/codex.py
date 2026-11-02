"""Bounded Codex app-server protocol, with no process or credential ownership.

The supervisor owns transport, deadlines, cancellation and human authorization.
This module never runs a CLI, writes configuration, refreshes auth, or accepts an
escalation. A worker may execute repository code *inside* a provider-enforced
permission profile; that code is not claimed intrinsically safe. The executable,
provider sandbox implementation and administrator policy are trusted. Native
Windows/provider sandbox setup is not performed here.

Protocol/config references (checked 2026-11-02):
https://learn.chatgpt.com/docs/app-server
https://learn.chatgpt.com/docs/config-file/config-reference
openai/codex app-server-protocol/schema/typescript/v2/{ActivePermissionProfile,
PermissionProfileSummary,ExperimentalFeature,ConfigReadParams}.ts

Named permission profiles are beta. Older/unknown schemas fail closed rather
than silently falling back to workspace-write or a planning prompt. Real account
integration has NOT been tested; all tests use synthetic server responses.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import MODEL_ID, ProviderError

DISABLED_FEATURES = (
    "hooks", "multi_agent", "apps", "plugins", "remote_plugin",
    "workspace_dependencies", "skill_mcp_dependency_install", "shell_snapshot",
    "goals", "memories", "browser_use", "computer_use", "code_mode",
)
MAX_PAGES = 50
MAX_ITEMS = 1000
MAX_OUTPUT = 1024 * 1024



def resolve_executable(candidate, platform=None, arch=None):
    """Resolve common npm Windows shims to a validated adjacent native payload.

    No wrapper is read, parsed or executed. Supports npm's global prefix and
    local node_modules/.bin layouts, with nested or hoisted optional dependency.
    Other package-manager layouts fail closed. Package metadata is structural
    evidence, NOT signature/authenticity verification; the installed executable
    remains trusted and the caller must revalidate wrapper AND payload identity.

    Sources: openai/codex codex-cli/bin/codex.js (main and rust-v0.164.0),
    codex-cli/scripts/build_npm_package.py; npm CLI v11 folders documentation.
    """
    import platform as host_platform
    import stat
    import sys

    target_platform = sys.platform if platform is None else platform
    path = Path(candidate).absolute()
    try:
        if not path.is_file():
            raise ProviderError("codex_executable_missing")
        if target_platform not in ("win32", "nt", "windows"):
            return path.resolve(strict=True)
        if path.suffix.lower() in (".exe", ".com"):
            return path.resolve(strict=True)
        if path.name.lower() != "codex.cmd" or path.is_symlink():
            raise ProviderError("codex_windows_shim_unsupported")
        machine = (host_platform.machine() if arch is None else arch).lower()
        architecture = {"amd64": "x64", "x86_64": "x64", "x64": "x64",
                        "arm64": "arm64", "aarch64": "arm64"}.get(machine)
        if architecture is None:
            raise ProviderError("codex_windows_arch_unsupported")
        triple = {"x64": "x86_64-pc-windows-msvc", "arm64": "aarch64-pc-windows-msvc"}[architecture]
        # Deliberately bounded, documented npm layouts; never scan arbitrary PATH
        # directories or follow destinations written in the .cmd file.
        if path.parent.name.lower() == ".bin" and path.parent.parent.name.lower() == "node_modules":
            modules = path.parent.parent
        else:
            modules = path.parent / "node_modules"
        modules = modules.resolve(strict=True)

        def contained(p):
            resolved = p.resolve(strict=True)
            try:
                resolved.relative_to(modules)
            except ValueError:
                raise ProviderError("codex_npm_path_escape") from None
            return resolved

        def manifest(package):
            source = contained(package / "package.json")
            if not stat.S_ISREG(source.stat().st_mode):
                raise ProviderError("codex_npm_metadata_invalid")
            with source.open("rb") as stream:
                data = stream.read(65537)
            if len(data) > 65536:
                raise ProviderError("codex_npm_metadata_invalid")
            value = json.loads(data)
            if not isinstance(value, dict) or value.get("name") != "@openai/codex":
                raise ProviderError("codex_npm_metadata_invalid")
            return value

        base = modules / "@openai" / "codex"
        main = manifest(base)
        version = main.get("version")
        if (not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?", version)
                or main.get("bin") != {"codex": "bin/codex.js"}):
            raise ProviderError("codex_npm_metadata_invalid")
        # Confirm the advertised package entry exists without executing/reading JS.
        if not contained(base / "bin" / "codex.js").is_file():
            raise ProviderError("codex_npm_metadata_invalid")
        tag = "win32-" + architecture
        alias = "@openai/codex-" + tag
        deps = main.get("optionalDependencies", {})
        if not isinstance(deps, dict):
            raise ProviderError("codex_npm_metadata_invalid")
        expected_version = version + "-" + tag
        if alias in deps and deps[alias] != "npm:@openai/codex@" + expected_version:
            raise ProviderError("codex_npm_dependency_mismatch")
        package = None
        for location in (base / "node_modules" / "@openai" / ("codex-" + tag),
                         modules / "@openai" / ("codex-" + tag)):
            if location.exists() or location.is_symlink():
                if alias not in deps:
                    raise ProviderError("codex_npm_dependency_mismatch")
                metadata = manifest(location)
                if (metadata.get("version") != expected_version or metadata.get("os") != ["win32"]
                        or metadata.get("cpu") != [architecture]):
                    raise ProviderError("codex_npm_platform_mismatch")
                package = location
                break
        # Matches the official wrapper's fallback when optional package resolution
        # fails; no fallback from an existing but invalid platform package.
        payload = contained((base if package is None else package) / "vendor" / triple / "bin" / "codex.exe")
        if not stat.S_ISREG(payload.stat().st_mode):
            raise ProviderError("codex_native_payload_invalid")
        # PE header machine check avoids treating a renamed script or wrong-arch
        # payload as a native Windows executable. No bytes are ever executed here.
        with payload.open("rb") as stream:
            header = stream.read(64)
            if len(header) != 64 or header[:2] != b"MZ":
                raise ProviderError("codex_native_payload_invalid")
            offset = int.from_bytes(header[60:64], "little")
            if not 64 <= offset <= 16 * 1024 * 1024:
                raise ProviderError("codex_native_payload_invalid")
            stream.seek(offset)
            signature = stream.read(6)
        expected_machine = {"x64": 0x8664, "arm64": 0xaa64}[architecture]
        if signature[:4] != b"PE\x00\x00" or int.from_bytes(signature[4:], "little") != expected_machine:
            raise ProviderError("codex_native_payload_invalid")
        return payload
    except ProviderError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError):
        raise ProviderError("codex_npm_layout_unavailable") from None


def _workspace(cwd):
    path = Path(cwd).resolve(strict=True)
    if not path.is_dir() or path == Path(path.anchor):
        raise ProviderError("invalid_workspace")
    return str(path)


def profile_name(phase, cwd):
    if phase not in {"planning", "coordinator", "worker"}:
        raise ProviderError("invalid_phase")
    role = "worker" if phase == "worker" else "planner"
    return "collab_" + role + "_" + hashlib.sha256(str(cwd).encode()).hexdigest()[:12]


def permission_profile(phase, cwd):
    """No inherited profile; only platform minimum and the registered checkout.

    Protect scheduler/Git/private state even though product edits are permitted.
    Denying a parent path includes its descendants under the provider contract.
    Protect the whole .scratch tree (not a race-prone glob of existing files).
    """
    root = _workspace(cwd)
    return {"filesystem": {":root": "deny", ":minimal": "read",
        "~/.ssh": "deny", "~/.aws": "deny", "~/.codex": "deny",
        "~/.claude": "deny", "~/.gemini": "deny", root: {
        ".": "write" if phase == "worker" else "read",
        ".ai-collab": "deny", ".git": "deny", ".codex": "deny", ".aws": "deny",
        ".agents": "read", "AGENTS.md": "read", "AGENTS.override.md": "read",
        ".scratch": "deny",
    }}, "network": {"enabled": False}}


def config_overrides(phase, cwd):
    root = _workspace(cwd)
    name = profile_name(phase, root)
    values = {"features." + key: False for key in DISABLED_FEATURES}
    values.update({
        "features.shell_tool": phase == "worker",
        # Required by Codex when a named permissions table is supplied.
        "default_permissions": name,
        "web_search": "disabled",
        "allow_login_shell": False,
        "approval_policy": "on-request",
        "approvals_reviewer": "user",
        "shell_environment_policy.inherit": "core",
        "shell_environment_policy.ignore_default_excludes": False,
        "shell_environment_policy.include_only": ["PATH", "SystemRoot", "WINDIR", "TEMP", "TMP"],
        "shell_environment_policy.set": {},
        "permissions." + name: permission_profile(phase, root),
    })
    return values


def _toml(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ",".join(_toml(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k) + "=" + _toml(v) for k, v in value.items()) + "}"
    raise ProviderError("invalid_override")


def fixed_startup_argv(executable, phase, cwd):
    """Only fixed argv, never a shell command; no prompt/credential in argv.

    Existing MCP configuration is NOT erased by an empty-table merge. The engine
    explicitly rejects enabled MCP configurations before thread creation. Startup
    hooks are disabled before the process initializes. CLI/admin startup itself
    is trusted, not isolated by these model-tool policy controls.
    """
    if not isinstance(executable, str) or not executable or "\x00" in executable:
        raise ProviderError("invalid_executable")
    argv = [executable, "app-server"]
    for key, value in config_overrides(phase, cwd).items():
        argv.extend(("-c", key + "=" + _toml(value)))
    return tuple(argv)


def _get(value, dotted):
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            raise ProviderError("codex_effective_config_unverified")
        value = value[part]
    return value


def verify_effective_config(config, phase, cwd):
    if not isinstance(config, dict):
        raise ProviderError("codex_effective_config_unverified")
    for key, expected in config_overrides(phase, cwd).items():
        if _get(config, key) != expected:
            raise ProviderError("codex_effective_config_unverified")
    # An override of include_only must not leave alternative filters or injected
    # environment values from another config layer active.
    environment = config["shell_environment_policy"]
    if environment.get("filters") or environment.get("set"):
        raise ProviderError("codex_environment_unverified")
    servers = config.get("mcp_servers", {})
    if not isinstance(servers, dict) or any(
        not isinstance(entry, dict) or entry.get("enabled") is not False
        for entry in servers.values()
    ):
        raise ProviderError("codex_mcp_enabled")
    # Managed hooks can be independently enforced. Refuse even configured hooks
    # instead of guessing whether disabling the feature suppresses every source.
    if config.get("hooks") or config.get("notify"):
        raise ProviderError("codex_hooks_configured")
    if config.get("environments") or config.get("remote_execution"):
        raise ProviderError("codex_remote_environment_unverified")
    # Native browser/computer surfaces are outside command sandbox boundaries.
    computer = config.get("computer_use", {})
    browser = config.get("browser_use", {})
    if computer or browser:
        raise ProviderError("codex_external_tools_unverified")


class CodexSession:
    """Incremental, one-turn protocol engine; receive() accepts decoded objects.

    start/receive return outbound JSON objects. Flush returned objects even when
    done becomes true, then stop the owned process tree. EOF without done is a
    failure; call eof(). No model prompt is sent before verified config, feature
    flags, profile availability and thread policy. delivered means turn/start was
    acknowledged, not that the model understood it. discovery_only never starts
    a thread or turn, and does not assert execution was account-tested.
    """
    def __init__(self, phase, prompt, cwd, model_id=None, *, discovery_only=False):
        self.cwd = _workspace(cwd)
        self.profile = profile_name(phase, self.cwd)
        if not isinstance(prompt, str) or (not prompt and not discovery_only) or len(prompt.encode()) > MAX_OUTPUT:
            raise ProviderError("invalid_prompt")
        if model_id is not None and (not isinstance(model_id, str) or not MODEL_ID.fullmatch(model_id)):
            raise ProviderError("invalid_model")
        self.phase, self.prompt, self.model_id = phase, prompt, model_id
        self.discovery_only = bool(discovery_only)
        self.done, self.delivered, self.output, self.error_code = False, False, "", None
        self.thread_id, self.turn_id = None, None
        self.auth_status = "unknown"
        self.models = []
        self.capabilities = {"planning_isolation": "unverified", "execution_status": "blocked",
                             "actual_account_execution_tested": False}
        self._counter = 0
        self._pending = None
        self._started = False
        self._features = {}
        self._profiles = []
        self._cursors = set()
        self._pages = 0
        self._messages = {}
        self._size = 0
        self._notifications = 0

    def _request(self, method, params):
        self._counter += 1
        self._pending = (self._counter, method)
        return {"id": self._counter, "method": method, "params": params}

    def start(self):
        if self._started:
            raise ProviderError("codex_session_already_started")
        self._started = True
        return [self._request("initialize", {"clientInfo": {
            "name": "agent_collaboration", "version": "1"},
            "capabilities": {"experimentalApi": True}})]

    def _fail(self, code):
        self.error_code, self.done, self.output = code, True, ""
        return []

    def eof(self):
        if not self.done:
            self._fail("codex_incomplete_turn")

    def _paginate(self, method, result, params):
        self._pages += 1
        cursor = result.get("nextCursor")
        if cursor is None:
            self._pages, self._cursors = 0, set()
            return None
        if (not isinstance(cursor, str) or not cursor or len(cursor) > 4096
                or cursor in self._cursors or self._pages >= MAX_PAGES):
            raise ProviderError("invalid_catalog_cursor")
        self._cursors.add(cursor)
        return [self._request(method, {**params, "cursor": cursor})]

    def _page(self, result):
        page = result.get("data")
        if not isinstance(page, list) or len(page) > MAX_ITEMS or any(not isinstance(v, dict) for v in page):
            raise ProviderError("invalid_catalog")
        return page

    def receive(self, obj):
        if self.done:
            return []
        try:
            if not self._started or not isinstance(obj, dict):
                raise ProviderError("invalid_probe_response")
            if "method" in obj:
                if not isinstance(obj["method"], str):
                    raise ProviderError("codex_invalid_response")
                if "id" in obj:
                    return self._server_request(obj)
                return self._notification(obj)
            if (self._pending is None or type(obj.get("id")) is not int
                    or obj["id"] != self._pending[0]):
                raise ProviderError("codex_unexpected_response")
            method = self._pending[1]
            self._pending = None
            result = obj.get("result")
            if "error" in obj or not isinstance(result, dict):
                raise ProviderError("codex_protocol_unsupported" if not self.delivered else "codex_rpc_failed")
            return self._response(method, result)
        except ProviderError as exc:
            return self._fail(exc.code)
        except (KeyError, TypeError, ValueError):
            return self._fail("codex_invalid_response")

    def _response(self, method, result):
        if method == "initialize":
            return [{"method": "initialized", "params": {}}, self._request("config/read", {
                "includeLayers": False, "cwd": self.cwd})]
        if method == "config/read":
            verify_effective_config(result.get("config"), self.phase, self.cwd)
            return [self._request("experimentalFeature/list", {"limit": 100})]
        if method == "experimentalFeature/list":
            for item in self._page(result):
                name, enabled = item.get("name"), item.get("enabled")
                if not isinstance(name, str) or type(enabled) is not bool or name in self._features:
                    raise ProviderError("codex_feature_schema_unsupported")
                self._features[name] = enabled
            next_page = self._paginate(method, result, {"limit": 100})
            if next_page is not None:
                return next_page
            expected = {key: False for key in DISABLED_FEATURES}
            expected["shell_tool"] = self.phase == "worker"
            if any(self._features.get(key) is not enabled for key, enabled in expected.items()):
                raise ProviderError("codex_feature_schema_unsupported")
            # Unsupported auxiliary execution surfaces must not be enabled.
            if any(self._features.get(key) is True for key in ("browser_use", "computer_use", "js_repl", "python", "code_mode")):
                raise ProviderError("codex_external_tools_unverified")
            return [self._request("permissionProfile/list", {"cwd": self.cwd, "limit": 100})]
        if method == "permissionProfile/list":
            self._profiles.extend(self._page(result))
            if len(self._profiles) > MAX_ITEMS:
                raise ProviderError("catalog_page_limit")
            next_page = self._paginate(method, result, {"cwd": self.cwd, "limit": 100})
            if next_page is not None:
                return next_page
            matches = [p for p in self._profiles if p.get("id") == self.profile]
            if len(matches) != 1 or matches[0].get("allowed") is not True:
                raise ProviderError("codex_permission_profile_unavailable")
            self.capabilities.update({"planning_isolation": "provider_read_only_profile" if self.phase != "worker" else "not_applicable",
                                      "execution_status": "sandboxed_workspace" if self.phase == "worker" else "planning_only",
                                      "capabilities_status": "protocol_verified"})
            return [self._request("account/read", {"refreshToken": False})]
        if method == "account/read":
            account = result.get("account")
            if isinstance(account, dict):
                self.auth_status = "authenticated"
            elif result.get("requiresOpenaiAuth") is True:
                self.auth_status = "unauthenticated"
            return [self._request("model/list", {"limit": 100, "includeHidden": False})]
        if method == "model/list":
            for item in self._page(result):
                mid, label = item.get("id"), item.get("displayName", item.get("id"))
                if (not isinstance(mid, str) or not MODEL_ID.fullmatch(mid) or not isinstance(label, str)
                        or len(label) > 200 or any(ord(c) < 32 for c in label)):
                    raise ProviderError("invalid_catalog")
                if any(m["id"] == mid for m in self.models):
                    raise ProviderError("invalid_catalog")
                self.models.append({"id": mid, "label": label})
            if len(self.models) > MAX_ITEMS:
                raise ProviderError("catalog_page_limit")
            next_page = self._paginate(method, result, {"limit": 100, "includeHidden": False})
            if next_page is not None:
                return next_page
            if self.discovery_only:
                self.done = True
                return []
            if self.auth_status != "authenticated":
                raise ProviderError("auth_unconfirmed")
            if self.model_id and not any(m["id"] == self.model_id for m in self.models):
                raise ProviderError("model_not_in_catalog")
            params = {"cwd": self.cwd, "permissions": self.profile, "approvalPolicy": "on-request",
                      "approvalsReviewer": "user", "ephemeral": True,
                      "config": config_overrides(self.phase, self.cwd)}
            if self.model_id:
                params["model"] = self.model_id
            return [self._request("thread/start", params)]
        if method == "thread/start":
            active = result.get("activePermissionProfile")
            if (not isinstance(active, dict) or active.get("id") != self.profile or active.get("extends") is not None
                    or result.get("cwd") != self.cwd or result.get("approvalPolicy") != "on-request"
                    or result.get("approvalsReviewer") != "user"):
                raise ProviderError("codex_thread_policy_unverified")
            thread = result.get("thread")
            if not isinstance(thread, dict):
                raise ProviderError("codex_invalid_thread")
            self.thread_id = thread.get("id")
            if not isinstance(self.thread_id, str) or not self.thread_id or len(self.thread_id) > 256:
                raise ProviderError("codex_invalid_thread")
            return [self._request("turn/start", {"threadId": self.thread_id,
                "input": [{"type": "text", "text": self.prompt}]})]
        if method == "turn/start":
            turn = result.get("turn")
            if not isinstance(turn, dict):
                raise ProviderError("codex_invalid_turn")
            tid = turn.get("id")
            if not isinstance(tid, str) or not tid or len(tid) > 256 or (self.turn_id and tid != self.turn_id):
                raise ProviderError("codex_invalid_turn")
            self.turn_id, self.delivered = tid, True
            return []
        raise ProviderError("codex_unexpected_response")

    def _scope(self, params, *, require_turn=True):
        if not isinstance(params, dict) or params.get("threadId") != self.thread_id or self.thread_id is None:
            raise ProviderError("codex_event_scope_mismatch")
        if require_turn and (self.turn_id is None or params.get("turnId") != self.turn_id):
            raise ProviderError("codex_event_scope_mismatch")

    def _server_request(self, obj):
        request_id = obj["id"]
        if type(request_id) not in (int, str):
            raise ProviderError("invalid_probe_response")
        method = obj.get("method")
        # Never echo arbitrary arguments/commands/URLs/private provider content.
        if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            result = {"decision": "decline"}
        elif method == "item/permissions/requestApproval":
            result = {"permissions": {}, "scope": "turn"}
        elif method == "mcpServer/elicitation/request":
            result = {"action": "decline", "content": None}
        else:
            self._fail("codex_unknown_server_request")
            return [{"id": request_id, "error": {"code": -32601, "message": "Unsupported server request"}}]
        self._fail("approval_needed" if self.phase == "worker" else "codex_tool_denied")
        return [{"id": request_id, "result": result}]

    def _notification(self, obj):
        self._notifications += 1
        if self._notifications > 10000:
            raise ProviderError("output_limit")
        method, params = obj.get("method"), obj.get("params", {})
        if not isinstance(params, dict):
            raise ProviderError("codex_invalid_response")
        if method in {"warning", "configWarning"}:
            # Unknown config flags/managed overrides must never become warnings
            # that silently weaken isolation.
            raise ProviderError("codex_configuration_warning")
        if method == "error":
            raise ProviderError("provider_failed")
        if method == "turn/started":
            self._scope(params, require_turn=False)
            turn = params.get("turn")
            if not isinstance(turn, dict):
                raise ProviderError("codex_invalid_turn")
            tid = turn.get("id")
            if not isinstance(tid, str) or not tid or (self.turn_id and self.turn_id != tid):
                raise ProviderError("codex_invalid_turn")
            self.turn_id = tid
        elif method == "item/completed":
            self._scope(params)
            item = params.get("item")
            if not isinstance(item, dict):
                raise ProviderError("codex_invalid_item")
            if item.get("type") == "agentMessage":
                key, text = item.get("id"), item.get("text")
                if not isinstance(key, str) or not key or not isinstance(text, str) or key in self._messages:
                    raise ProviderError("codex_invalid_item")
                self._size += len(text.encode())
                if self._size > MAX_OUTPUT or len(self._messages) >= MAX_ITEMS:
                    raise ProviderError("output_limit")
                if item.get("phase") in (None, "final_answer"):
                    self._messages[key] = text
        elif method == "turn/completed":
            self._scope(params, require_turn=False)
            turn = params.get("turn")
            if not isinstance(turn, dict):
                raise ProviderError("codex_invalid_turn")
            if not self.delivered or turn.get("id") != self.turn_id:
                raise ProviderError("codex_invalid_turn")
            if turn.get("status") != "completed" or turn.get("error"):
                raise ProviderError("provider_failed")
            if not self._messages:
                raise ProviderError("codex_missing_final_output")
            self.output = next(reversed(self._messages.values()))
            self.done = True
        return []
