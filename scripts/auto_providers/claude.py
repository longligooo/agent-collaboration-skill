"""Fixed Claude sandbox policy, not user-configurable shell flags.

Official references: code.claude.com/docs/en/{sandboxing,settings-reference,
cli-reference,env-vars}. Trusted local executable/admin policy; no guarantee
against managed policy deliberately overriding this session's restrictions.
Native Windows has no Claude Bash sandbox; never enable Bash there.
"""
from pathlib import Path
import shutil
import sys

from . import ProviderError


def sandbox_capability(path, help_text):
    if '--settings' not in help_text:
        return 'claude_sandbox_settings_unconfirmed'
    if sys.platform == 'win32':
        return 'claude_sandbox_requires_wsl2'
    if sys.platform.startswith('linux'):
        if not all(shutil.which(name, path=path) for name in ('bwrap', 'socat')):
            return 'claude_sandbox_dependencies_missing'
    elif sys.platform != 'darwin':
        return 'claude_sandbox_platform_unsupported'
    return None  # Presence, not a successful sandbox startup probe.


def worker_settings(cwd, protected_roots):
    if not protected_roots:
        raise ProviderError('protected_project_root_required')
    directory = Path(cwd).resolve(strict=True)
    denied = []
    for value in protected_roots:
        root = Path(value).resolve(strict=True)
        if root == directory or directory in root.parents:
            raise ProviderError('invalid_protected_root')
        # A parent can be read-denied with a more-specific managed cwd read allow.
        denied.append(str(root))
    # Avoid ambiguous glob resolution on sandbox/filesystem paths.
    if any(c in str(directory) + ''.join(denied) for c in '*?[]'):
        raise ProviderError('sandbox_path_pattern_unsupported')
    protected = [str(directory / name) for name in ('.git', '.ai-collab', '.claude', '.codex', '.scratch')]
    secrets = ['~/.ssh', '~/.aws', '~/.codex', '~/.claude', '~/.gemini', '~/.git-credentials', '~/.gitconfig']
    return {
        'permissions': {'blockReadsOutsideWorkingDirectories': True},
        'disableAllHooks': True,
        'sandbox': {
            'enabled': True, 'failIfUnavailable': True,
            'autoAllowBashIfSandboxed': True, 'allowUnsandboxedCommands': False,
            'excludedCommands': [], 'enableWeakerNestedSandbox': False,
            'enableWeakerNetworkIsolation': False, 'allowAppleEvents': False,
            'allowNestedUserNamespaces': False,
            'filesystem': {'disabled': False, 'allowWrite': [], 'allowRead': [str(directory)],
                'denyRead': denied + secrets + protected,
                'denyWrite': [p for p in denied if Path(p) not in directory.parents] + protected},
            'network': {'allowedDomains': [], 'deniedDomains': ['*'], 'strictAllowlist': True,
                'allowUnixSockets': [], 'allowAllUnixSockets': False,
                'allowLocalBinding': False, 'allowMachLookup': []},
        },
    }


def verify_hidden_max_turns(command):
    """Read-only parser proof; --help alone accepts unknown options too.

    The known version contract still bounds interpretation. No prompt is sent,
    no session/model is started, and no auth identity fields are retained.
    """
    import json
    sentinel = '--collab-unsupported-probe-flag'
    code, output = command((sentinel, 'auth', 'status'))
    if code == 0 or "unknown option '" + sentinel + "'" not in output:
        raise ProviderError('claude_max_turns_unverified')
    code, output = command(('--max-turns', '1', 'auth', 'status'))
    try:
        value = json.loads(output)
    except (ValueError, TypeError):
        raise ProviderError('claude_max_turns_unverified') from None
    if (not isinstance(value, dict) or type(value.get('loggedIn')) is not bool
            or code != (0 if value['loggedIn'] else 1)):
        raise ProviderError('claude_max_turns_unverified')


def resolve_executable(candidate, platform=None, arch=None):
    """Resolve current official npm native layout without executing cmd/shims.

    The npm registry's @anthropic-ai/claude-code 2.1.317 manifest advertises
    bin/claude.exe. Legacy cli.js wrappers are deliberately not evaluated.
    Package identity and PE validation are structural, not code signatures;
    the installed CLI remains trusted just as direct PATH executables are.
    """
    import json
    import platform as host_platform
    import re
    import stat
    target = sys.platform if platform is None else platform
    path = Path(candidate).absolute()
    try:
        if not path.is_file():
            raise ProviderError('claude_executable_missing')
        if target not in ('win32', 'nt', 'windows'):
            return path.resolve(strict=True)
        if path.suffix.lower() in ('.exe', '.com'):
            return path.resolve(strict=True)
        if path.name.lower() != 'claude.cmd' or path.is_symlink():
            raise ProviderError('claude_native_windows_install_required')
        machine = (host_platform.machine() if arch is None else arch).lower()
        pe_machine = {'amd64':0x8664, 'x86_64':0x8664, 'x64':0x8664,
                      'arm64':0xaa64, 'aarch64':0xaa64}.get(machine)
        if pe_machine is None:
            raise ProviderError('claude_windows_arch_unsupported')
        modules = (path.parent.parent if path.parent.name.lower() == '.bin'
                   and path.parent.parent.name.lower() == 'node_modules'
                   else path.parent / 'node_modules').resolve(strict=True)
        package = (modules / '@anthropic-ai' / 'claude-code').resolve(strict=True)
        package.relative_to(modules)
        metadata_path = (package / 'package.json').resolve(strict=True)
        metadata_path.relative_to(package)
        with metadata_path.open('rb') as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ProviderError('claude_npm_metadata_invalid')
        metadata = json.loads(raw)
        if (not isinstance(metadata, dict) or metadata.get('name') != '@anthropic-ai/claude-code'
                or not isinstance(metadata.get('version'), str)
                or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?', metadata['version'])):
            raise ProviderError('claude_npm_metadata_invalid')
        if metadata.get('bin') != {'claude': 'bin/claude.exe'}:
            raise ProviderError('claude_native_windows_install_required')
        payload = (package / 'bin' / 'claude.exe').resolve(strict=True)
        payload.relative_to(package)
        if not stat.S_ISREG(payload.stat().st_mode):
            raise ProviderError('claude_native_payload_invalid')
        with payload.open('rb') as stream:
            header = stream.read(64)
            if len(header) != 64 or header[:2] != b'MZ':
                raise ProviderError('claude_native_payload_invalid')
            offset = int.from_bytes(header[60:64], 'little')
            if not 64 <= offset <= 16*1024*1024:
                raise ProviderError('claude_native_payload_invalid')
            stream.seek(offset)
            signature = stream.read(6)
        if signature[:4] != b'PE\0\0' or int.from_bytes(signature[4:], 'little') != pe_machine:
            raise ProviderError('claude_native_payload_invalid')
        return payload
    except ProviderError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError):
        raise ProviderError('claude_npm_layout_unavailable') from None


def parse_auth_status(code, output):
    """Use CLI evidence, never infer authentication from an environment key."""
    import json
    try:
        value = json.loads(output)
    except (ValueError, TypeError):
        raise ProviderError('invalid_auth_response') from None
    if not isinstance(value, dict):
        raise ProviderError('invalid_auth_response')
    if code == 0 and value.get('loggedIn') is True:
        return 'authenticated'
    if code == 1 and value.get('loggedIn') is False:
        return 'unauthenticated'
    return 'unknown'


def configured_models(env):
    """Expose configured model IDs, not credentials or a verified catalogue."""
    from . import MODEL_ID
    result = []
    seen = set()
    for name in ('ANTHROPIC_MODEL', 'ANTHROPIC_DEFAULT_OPUS_MODEL',
                 'ANTHROPIC_DEFAULT_SONNET_MODEL', 'ANTHROPIC_DEFAULT_HAIKU_MODEL',
                 'ANTHROPIC_DEFAULT_FABLE_MODEL', 'ANTHROPIC_SMALL_FAST_MODEL'):
        model = env.get(name)
        if isinstance(model, str) and MODEL_ID.fullmatch(model) and model not in seen:
            seen.add(model)
            result.append({'id': model, 'label': model + ' · 本地配置，未验证'})
    return result
