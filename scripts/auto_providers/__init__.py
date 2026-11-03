"""Conservative automatic-task provider discovery and launch contracts.

No query is sent during discovery. A catalogue is not an execution entitlement.
Claude supports a finite model tool surface on confirmed modern versions. Local
executables/admin policy remain trusted; this is not an OS sandbox for startup
hooks. Codex uses a verified app-server profile; unsupported capabilities fail closed.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import auto_process

PROVIDERS = ("codex", "claude", "agy")
from auto_model_id import MODEL_ID


class ProviderError(ValueError):
    """Public, sanitized error; never includes subprocess output or credentials."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class LaunchSpec:
    argv: tuple[str, ...]
    stdin: bytes = field(repr=False)
    cwd: Path
    env: dict[str, str] = field(repr=False)
    output_format: str = "json"
    timeout: float = 300.0
    transport: dict | None = field(default=None, repr=False)


def _stop(process):
    try:
        process.terminate_tree(timeout=2)
    except auto_process.ProcessError:
        raise ProviderError("process_unknown") from None


class _Process:
    """Bounded stdio; optional private stderr parsing, never exposed to clients."""
    def __init__(self, argv, *, cwd, env, timeout, max_output, merge_stderr=False):
        self.deadline = time.monotonic() + timeout
        self.limit = max_output
        self.queue = queue.Queue(maxsize=64)
        self.stop = threading.Event()
        self.process = auto_process.spawn(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT if merge_stderr else subprocess.DEVNULL)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.writes = queue.Queue(1)
        self.writer = threading.Thread(target=self._write, daemon=True)
        self.writer.start()

    def _put(self, value):
        while not self.stop.is_set():
            try:
                self.queue.put(value, timeout=.05)
                return
            except queue.Full:
                pass

    def _read(self):
        total = 0
        try:
            while not self.stop.is_set():
                line = self.process.stdout.readline(self.limit + 1)
                if not line:
                    self._put(None)
                    return
                total += len(line)
                if total > self.limit:
                    self._put(ProviderError("output_limit"))
                    return
                self._put(line)
        except (OSError, ValueError):
            self._put(ProviderError("probe_io_error"))

    def next(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderError("probe_timeout")
        try:
            value = self.queue.get(timeout=remaining)
        except queue.Empty:
            raise ProviderError("probe_timeout") from None
        if isinstance(value, Exception):
            raise value
        return value

    def _write(self):
        while not self.stop.is_set():
            try:
                data, response = self.writes.get(timeout=.05)
            except queue.Empty:
                continue
            try:
                view = memoryview(data)
                while view and not self.stop.is_set():
                    size = os.write(self.process.stdin.fileno(), view[:65536])
                    if size <= 0:
                        raise BrokenPipeError()
                    view = view[size:]
                self.process.stdin.flush()
                response.put(None)
            except (OSError, ValueError):
                response.put(ProviderError("probe_io_error"))

    def send(self, value):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderError("probe_timeout")
        response = queue.Queue(1)
        try:
            self.writes.put((json.dumps(value).encode() + b"\n", response), timeout=remaining)
            result = response.get(timeout=max(.001, self.deadline - time.monotonic()))
        except (queue.Empty, queue.Full):
            raise ProviderError("probe_timeout") from None
        if isinstance(result, Exception):
            raise result

    def close(self):
        self.stop.set()
        try:
            _stop(self.process)
        finally:
            try:
                self.reader.join(timeout=2)
                self.writer.join(timeout=2)
            finally:
                self.process.close()


def _models(items, *, key="id", label="displayName"):
    if not isinstance(items, list) or len(items) > 1000:
        raise ProviderError("invalid_catalog")
    result = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise ProviderError("invalid_catalog")
        value = item.get(key)
        name = item.get(label, value)
        if not isinstance(value, str) or not MODEL_ID.fullmatch(value):
            raise ProviderError("invalid_catalog")
        if not isinstance(name, str) or len(name) > 1200 or any(ord(c) < 32 for c in name):
            raise ProviderError("invalid_catalog")
        if value not in seen:
            result.append({"id": value, "label": name})
            seen.add(value)
    return result


def parse_result(provider_id: str, stdout: bytes | str) -> str:
    """Extract plain result, not arbitrary events; the runtime validates plan JSON."""
    if provider_id not in PROVIDERS:
        raise ProviderError("unknown_provider")
    if not isinstance(stdout, (bytes, str)) or len(stdout) > 1024 * 1024:
        raise ProviderError("invalid_result")
    try:
        value = json.loads(stdout)
    except (ValueError, UnicodeError):
        raise ProviderError("invalid_result") from None
    if not isinstance(value, dict) or value.get("is_error") is True or value.get("error"):
        raise ProviderError("provider_failed")
    # Codex exec emits NDJSON, not a JSON result envelope; unsupported until a
    # supervised app-server execution adapter establishes terminal event evidence.
    if provider_id == "agy" and value.get("status") != "SUCCESS":
        raise ProviderError("provider_failed")
    key = {"claude": "result", "agy": "response"}.get(provider_id)
    if key is None:
        raise ProviderError("unsupported_result_format")
    text = value.get(key)
    if not isinstance(text, str):
        raise ProviderError("invalid_result")
    return text


class ProviderRegistry:
    def __init__(self, path=None, *, timeout=5.0, max_output=262144, ttl=300.0):
        if not 0 < timeout <= 60 or not 1024 <= max_output <= 4 * 1024 * 1024 or not 0 < ttl <= 3600:
            raise ValueError("invalid discovery bounds")
        self.path = os.environ.get("PATH", "") if path is None else str(path)
        self.timeout, self.max_output, self.ttl = timeout, max_output, ttl
        self._cache = None
        self._executables = {}
        self._sources = {}
        self._env_signatures = {}
        self._codex_provider_binding = None
        self._checked = 0.0
        self._lock = threading.Lock()

    def _env(self, provider=None):
        from .environment import cli_environment
        return cli_environment(provider, self.path)

    def _env_signature(self, provider, env=None):
        # Private binding only; never emit credentials or derived values in records.
        from .environment import config_binding
        env = self._env(provider) if env is None else env
        return hashlib.sha256(json.dumps(env, sort_keys=True).encode() + config_binding(provider)).digest()

    def _command(self, argv, cwd, provider, *, include_stderr=False):
        proc = _Process(argv, cwd=cwd, env=self._env(provider), timeout=self.timeout, max_output=self.max_output, merge_stderr=include_stderr)
        try:
            proc.process.stdin.close()
            output = []
            while True:
                line = proc.next()
                if line is None:
                    break
                output.append(line)
            remaining = max(.001, proc.deadline-time.monotonic())
            try:
                code = proc.process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                raise ProviderError("probe_timeout") from None
            return code, b"".join(output).decode("utf-8")
        finally:
            proc.close()

    def _codex_probe(self, executable, cwd, session, env, disabled_mcp=()):
        from .codex import fixed_startup_argv
        proc = _Process(fixed_startup_argv(executable, session.phase, cwd, disabled_mcp=disabled_mcp),
                        cwd=cwd, env=env, timeout=self.timeout, max_output=self.max_output)
        try:
            for outgoing in session.start():
                proc.send(outgoing)
            while not session.done:
                line = proc.next()
                if line is None:
                    session.eof()
                    break
                try:
                    value = json.loads(line)
                except (ValueError, UnicodeError):
                    raise ProviderError("invalid_probe_response") from None
                for outgoing in session.receive(value):
                    proc.send(outgoing)
            if session.error_code:
                raise ProviderError(session.error_code)
            return session
        finally:
            proc.close()

    def _codex_mcp_inventory(self, executable, cwd, env, phase="planning"):
        from .codex import CodexSession
        # This session can only initialize + read config; no account/model/thread
        # requests are made. Configured MCP is started by Codex thread creation,
        # not this inventory phase (covered by real CLI sentinel smoke).
        session = CodexSession(phase, "", cwd, discovery_only=True, inventory_only=True)
        return self._codex_probe(executable, cwd, session, env).mcp_names

    def _codex(self, executable, cwd, record):
        from .codex import CodexSession
        env = self._env("codex")
        names = self._codex_mcp_inventory(executable, cwd, env)
        session = CodexSession("planning", "", cwd, discovery_only=True,
            credential_env_names=[k for k, v in env.items() if v], disabled_mcp=names)
        self._codex_probe(executable, cwd, session, env, names)
        record.update(session.capabilities)
        record["auth_status"] = session.auth_status
        record["headless_status"] = "available"
        record["models"] = session.models
        record["mcp_isolation"] = "disabled_per_process"
        record["mcp_disabled_count"] = len(names)
        self._codex_provider_binding = session.provider_binding
        record["catalog_status"] = "unknown" if session.provider_binding else "fresh"
        record["catalog_source"] = "codex.local_config" if session.provider_binding else "codex.app-server.model/list"
        if session.provider_binding:
            record["catalog_reason"] = "local_config_not_provider_verified"
        record["unavailable_reason"] = None if session.auth_status in ("authenticated", "configured") else "auth_unconfirmed"

    def _claude(self, executable, cwd, record):
        code, output = self._command((executable, "auth", "status"), cwd, "claude")
        try:
            auth = json.loads(output)
        except ValueError:
            raise ProviderError("invalid_auth_response") from None
        if not isinstance(auth, dict):
            raise ProviderError("invalid_auth_response")
        if code == 0 and auth.get("loggedIn") is True:
            record["auth_status"] = "authenticated"
        elif code == 1 and auth.get("loggedIn") is False:
            record["auth_status"] = "unauthenticated"
        code, help_text = self._command((executable, "--help"), cwd, "claude")
        if code == 0 and "--print" in help_text and "--output-format" in help_text:
            record["headless_status"] = "available"
        required = {"--safe-mode", "--settings", "--tools", "--disallowedTools", "--strict-mcp-config", "--mcp-config",
                    "--disable-slash-commands", "--permission-prompts", "--permission-mode", "--no-session-persistence",
                    "--restricted", "--output-format", "--permission-prompts"}
        if code == 0 and all(flag in help_text for flag in required):
            version_code, version_text = self._command((executable, "--version"), cwd, "claude")
            match = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:\s|$)", version_text.strip())
            if version_code == 0 and match and (2, 1, 301) <= tuple(map(int, match.groups())) < (2, 2, 0):
                if "--max-turns" not in help_text:
                    from .claude import verify_hidden_max_turns
                    verify_hidden_max_turns(lambda args: self._command(
                        (executable, *args), cwd, "claude", include_stderr=True))
                auth_code, auth_output = self._command(
                    (executable, "--safe-mode", "--restricted", "auth", "status"), cwd, "claude")
                from .claude import parse_auth_status
                record["auth_status"] = parse_auth_status(auth_code, auth_output)
                record["planning_isolation"] = "model_tools_denied"
                from .claude import sandbox_capability
                reason = sandbox_capability(self.path, help_text)
                record["execution_status"] = "file_tools_only" if reason else "sandbox_required"
                record["execution_reason"] = reason
                record["sandbox_startup_tested"] = False
                record["managed_policy_trusted"] = True
                record["unavailable_reason"] = None if record["auth_status"] == "authenticated" else "auth_unconfirmed"
        from .claude import configured_models
        record["models"] = configured_models(self._env("claude"))
        record["catalog_source"] = "claude.local_config" if record["models"] else "claude.agent-sdk.supportedModels"
        record["catalog_reason"] = "local_config_not_provider_verified" if record["models"] else "sdk_discovery_not_available"

    def _agy(self, executable, cwd, record):
        code, help_text = self._command((executable, "--help"), cwd, "agy")
        if code == 0 and "--output-format" in help_text and ("--prompt" in help_text or "-p" in help_text):
            record["headless_status"] = "available"
        # Do not invent --json, auth status, or parse arbitrary tabular text as
        # model IDs. The documented command is agy models; format is not stable.
        code, output = self._command((executable, "models"), cwd, "agy")
        record["catalog_source"] = "agy.models"
        record["catalog_reason"] = "unstructured_catalog" if code == 0 else "catalog_command_failed"
        # JSON is not documented; even JSON-looking output is not an agreed schema.

    def discover(self, refresh=False):
        """Cache-only unless refresh=True. No implicit process launch on GET."""
        with self._lock:
            if refresh:
                records = []
                with tempfile.TemporaryDirectory(prefix="collab-provider-probe-") as cwd:
                    for provider in PROVIDERS:
                        executable = shutil.which(provider, path=self.path)
                        record = {"id": provider, "installed": executable is not None,
                            "auth_status": "unknown", "headless_status": "unknown",
                            "planning_isolation": "unverified", "execution_status": "blocked",
                            "models": [], "catalog_source": None, "catalog_checked_at": None,
                            "catalog_status": "unknown", "unavailable_reason": "isolation_unverified"}
                        if executable is None:
                            record["unavailable_reason"] = "not_installed"
                            record["headless_status"] = "unavailable"
                        else:
                            record["catalog_checked_at"] = datetime.now(timezone.utc).isoformat()
                            try:
                                source = Path(executable).resolve()
                                self._sources[provider] = (str(source), source.stat())
                                resolved = self._resolve_executable(provider, source)
                                self._executables[provider] = (str(resolved), resolved.stat())
                                before = self._env_signature(provider)
                                getattr(self, "_" + provider)(str(resolved), cwd, record)
                                if before != self._env_signature(provider):
                                    raise ProviderError("provider_configuration_changed")
                                self._env_signatures[provider] = before
                            except (OSError, UnicodeError, ProviderError) as exc:
                                record["probe_error"] = getattr(exc, "code", "probe_io_error")
                                record["unavailable_reason"] = record["probe_error"]
                                record["planning_isolation"] = "unverified"
                                record["execution_status"] = "blocked"
                                if record["catalog_status"] == "fresh":
                                    record["catalog_status"] = "stale"
                        records.append(record)
                self._cache, self._checked = records, time.monotonic()
            if self._cache is None:
                return [{"id": p, "installed": shutil.which(p, path=self.path) is not None,
                    "auth_status": "unknown", "headless_status": "unknown", "planning_isolation": "unverified",
                    "execution_status": "blocked", "models": [], "catalog_source": None,
                    "catalog_checked_at": None, "catalog_status": "unknown",
                    "unavailable_reason": "discovery_required"} for p in PROVIDERS]
            result = copy.deepcopy(self._cache)
            if time.monotonic()-self._checked > self.ttl:
                for record in result:
                    record["capabilities_status"] = "stale"
                    if record["installed"]:
                        record["unavailable_reason"] = "discovery_stale"
                    if record["catalog_status"] == "fresh":
                        record["catalog_status"] = "stale"
            return result

    @staticmethod
    def _resolve_executable(provider, candidate):
        if provider == "codex" and sys.platform == "win32":
            from .codex import resolve_executable
            return resolve_executable(candidate)
        if provider == "claude" and sys.platform == "win32":
            from .claude import resolve_executable
            return resolve_executable(candidate)
        return Path(candidate).resolve()

    def build_launch(self, provider_id, model_id=None, *, phase="planning", prompt, cwd, protected_roots=()):
        if provider_id not in PROVIDERS:
            raise ProviderError("unknown_provider")
        if phase not in {"planning", "worker", "coordinator"}:
            raise ProviderError("invalid_phase")
        if model_id is not None and (not isinstance(model_id, str) or not MODEL_ID.fullmatch(model_id)):
            raise ProviderError("invalid_model")
        if not isinstance(prompt, str) or not prompt or len(prompt.encode()) > 1024 * 1024:
            raise ProviderError("invalid_prompt")
        records = self.discover()
        record = next(r for r in records if r["id"] == provider_id)
        supported = {"claude": "model_tools_denied", "codex": "provider_read_only_profile"}
        if record["planning_isolation"] != supported.get(provider_id):
            raise ProviderError("isolation_unverified")
        if time.monotonic() - self._checked > self.ttl:
            raise ProviderError("discovery_stale")
        if (record["auth_status"] != "authenticated"
                and not (provider_id == "codex" and record["auth_status"] == "configured"
                         and self._codex_provider_binding)):
            raise ProviderError("auth_unconfirmed")
        launch_env = self._env(provider_id)
        if (provider_id in self._env_signatures
                and self._env_signature(provider_id, launch_env) != self._env_signatures[provider_id]):
            raise ProviderError("provider_configuration_changed")
        executable, checked_stat = self._executables[provider_id]
        try:
            actual = Path(executable).stat()
            current = shutil.which(provider_id, path=self.path)
            if current is None:
                raise ProviderError("executable_changed")
            source = Path(current).resolve()
            checked_source, source_stat = self._sources[provider_id]
            source_now = source.stat()
            if (str(source) != checked_source or str(self._resolve_executable(provider_id, source)) != executable
                    or any(getattr(actual, key) != getattr(checked_stat, key)
                           or getattr(source_now, key) != getattr(source_stat, key)
                           for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns"))):
                raise ProviderError("executable_changed")
            directory = Path(cwd).resolve(strict=True)
        except OSError:
            raise ProviderError("executable_or_workspace_missing") from None
        if not directory.is_dir():
            raise ProviderError("invalid_workspace")
        if provider_id == "codex":
            from .codex import fixed_startup_argv
            names = self._codex_mcp_inventory(executable, directory, launch_env, phase)
            if self._env_signature(provider_id, launch_env) != self._env_signatures.get(provider_id):
                raise ProviderError("provider_configuration_changed")
            return LaunchSpec(fixed_startup_argv(executable, phase, directory, disabled_mcp=names), prompt.encode("utf-8"),
                              directory, launch_env, output_format="codex-app-server",
                              transport={"phase": phase, "model_id": model_id,
                                         "provider_binding": self._codex_provider_binding, "disabled_mcp": names})
        # The trusted executable and managed policy are not sandboxed here.
        # These flags remove model-accessible execution/delegation/MCP tools.
        sandboxed = phase == "worker" and record.get("execution_status") == "sandbox_required"
        tools = ("Read,Edit,Write,Glob,Grep,Bash" if sandboxed else "Read,Edit,Write,Glob,Grep") if phase == "worker" else ""
        denied = ("Bash", "Agent", "Task", "mcp__*", "Edit(./.ai-collab/**)",
                  "Edit(./.git/**)", "Edit(./.git)", "Edit(./.ai-collab)",
                  "Edit(./.scratch/automatic-*)", "Edit(./.scratch/automatic-*/**)",
                  "Read(./.ai-collab/local/**)", "Edit(./.claude)", "Edit(./.claude/**)",
                  "Edit(./.codex)", "Edit(./.codex/**)") if phase == "worker" else ("*",)
        if sandboxed:
            denied = tuple(rule for rule in denied if rule != "Bash")
        argv = (executable, "--safe-mode", "--restricted", "--tools", tools,
                "--disallowedTools", *denied, "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--disable-slash-commands", "--permission-prompts", "none", "--permission-mode", "acceptEdits" if phase == "worker" else "default",
                "--no-session-persistence", "--max-turns", "8" if phase == "worker" else "1",
                "--output-format", "json", "-p")
        env = launch_env
        if sandboxed:
            from .claude import worker_settings
            settings = worker_settings(directory, protected_roots)
            argv += ("--settings", json.dumps(settings, separators=(",", ":")))
            env["CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"] = "1"
        else:
            argv += ("--settings", '{"disableAllHooks":true}')
        if model_id is not None:
            argv += ("--model", model_id)
        return LaunchSpec(argv, prompt.encode("utf-8"), directory, env)
