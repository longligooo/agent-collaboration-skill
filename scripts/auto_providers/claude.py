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
