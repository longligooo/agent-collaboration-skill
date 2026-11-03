"""Reuse trusted CLI user authentication/routing, never arbitrary runtime options.

Only the service account's user config is read, not project-controlled settings.
Values remain private subprocess environment and never enter discovery records.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit

from . import MODEL_ID, ProviderError

CLAUDE_KEYS = (
    'CLAUDE_CONFIG_DIR', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN',
    'CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_BASE_URL', 'ANTHROPIC_MODEL',
    'ANTHROPIC_DEFAULT_OPUS_MODEL', 'ANTHROPIC_DEFAULT_SONNET_MODEL',
    'ANTHROPIC_DEFAULT_HAIKU_MODEL', 'ANTHROPIC_DEFAULT_FABLE_MODEL',
    'CLAUDE_CODE_SUBAGENT_MODEL',
)
CODEX_KEYS = ('CODEX_HOME', 'OPENAI_API_KEY', 'OPENAI_BASE_URL')
NETWORK_KEYS = (
    'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
    'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy',
    'SSL_CERT_FILE', 'SSL_CERT_DIR', 'REQUESTS_CA_BUNDLE', 'NODE_EXTRA_CA_CERTS',
)
BASE_KEYS = ('HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'SYSTEMROOT',
             'WINDIR', 'TEMP', 'TMP', 'TMPDIR')


def _read(path, parser):
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise ProviderError('provider_user_config_invalid')
        with path.open('rb') as stream:
            data = stream.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ProviderError('provider_user_config_invalid')
        value = parser(data.decode('utf-8'))
        if not isinstance(value, dict):
            raise ProviderError('provider_user_config_invalid')
        return value
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeError):
        raise ProviderError('provider_user_config_invalid') from None


def _url(value, *, proxy=False):
    # Credentials in URLs can leak through proxy errors, CLI logs or redirects.
    try:
        if any(ord(c) < 33 for c in value) or '\\' in value:
            raise ValueError()
        url = urlsplit(value)
        if (url.scheme not in (('http', 'https', 'socks5', 'socks5h') if proxy else ('http', 'https'))
                or not url.hostname or url.username is not None or url.password is not None
                or url.query or url.fragment):
            raise ValueError()
        port = url.port
        if not proxy and url.scheme == 'http':
            try:
                local = ipaddress.ip_address(url.hostname).is_loopback
            except ValueError:
                local = url.hostname.lower() == 'localhost'
            if not local:
                raise ValueError()
    except ValueError:
        raise ProviderError('provider_endpoint_invalid') from None


def _codex_key_names(source, home):
    # Parse only the selected provider from the trusted user config. Never run
    # auth.command ourselves or inherit variables named by untrusted projects.
    try:
        import tomllib
    except ImportError:
        return ()  # Python <3.11 retains normal CLI file/keychain auth.
    config = _read(Path(source.get('CODEX_HOME', str(home / '.codex'))) / 'config.toml', tomllib.loads)
    provider = config.get('model_provider', 'openai')
    profile = config.get('profile')
    if isinstance(profile, str):
        profiles = config.get('profiles', {})
        selected = profiles.get(profile, {}) if isinstance(profiles, dict) else {}
        if isinstance(selected, dict):
            provider = selected.get('model_provider', provider)
    providers = config.get('model_providers', {})
    selected = providers.get(provider, {}) if isinstance(provider, str) and isinstance(providers, dict) else {}
    if not isinstance(selected, dict):
        return ()
    if isinstance(selected.get('base_url'), str):
        _url(selected['base_url'])
    name = selected.get('env_key')
    if name is None:
        return ()
    # Only credential-shaped names, not arbitrary configuration/injection names.
    if not isinstance(name, str) or not re.fullmatch(r'(?:[A-Z][A-Z0-9_]{0,100}_)?(?:API_KEY|AUTH_TOKEN|ACCESS_TOKEN|TOKEN)', name):
        raise ProviderError('provider_auth_env_unsupported')
    return (name,)


def config_binding(provider, source=None):
    """Private digest input for trusted user config changes between probes."""
    import hashlib
    source = os.environ if source is None else source
    home = Path(source.get('HOME') or source.get('USERPROFILE') or str(Path.home()))
    if provider == 'claude':
        path = Path(source.get('CLAUDE_CONFIG_DIR', str(home / '.claude'))) / 'settings.json'
    elif provider == 'codex':
        path = Path(source.get('CODEX_HOME', str(home / '.codex'))) / 'config.toml'
    else:
        return b''
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise ProviderError('provider_user_config_invalid')
        with path.open('rb') as stream:
            data = stream.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ProviderError('provider_user_config_invalid')
        return hashlib.sha256(data).digest()
    except FileNotFoundError:
        return b''
    except OSError:
        raise ProviderError('provider_user_config_invalid') from None


def cli_environment(provider, path, source=None):
    source = os.environ if source is None else source
    home = Path(source.get('HOME') or source.get('USERPROFILE') or str(Path.home()))
    defaults = {}
    if provider == 'claude':
        config = _read(Path(source.get('CLAUDE_CONFIG_DIR', str(home / '.claude'))) / 'settings.json', json.loads)
        user_env = config.get('env', {})
        if isinstance(user_env, dict):
            defaults = {k: v for k, v in user_env.items() if k in CLAUDE_KEYS and k != 'CLAUDE_CONFIG_DIR' and isinstance(v, str)}
        if isinstance(config.get('model'), str):
            defaults.setdefault('ANTHROPIC_MODEL', config['model'])
    provider_keys = {'claude': CLAUDE_KEYS, 'codex': CODEX_KEYS, 'agy': ('GEMINI_API_KEY', 'GOOGLE_API_KEY')}.get(provider, ())
    if provider == 'codex':
        provider_keys += _codex_key_names(source, home)
    keys = BASE_KEYS + (NETWORK_KEYS if provider else ()) + provider_keys
    env = {k: source[k] if k in source else defaults[k] for k in keys if k in source or k in defaults}
    for key, value in env.items():
        if not isinstance(value, str) or '\x00' in value:
            raise ProviderError('provider_environment_invalid')
        if value and (key.endswith('_BASE_URL') or key.lower() in ('http_proxy', 'https_proxy', 'all_proxy')):
            _url(value, proxy=key.lower().endswith('_proxy'))
        if value and (key == 'ANTHROPIC_MODEL' or key.endswith('_MODEL')) and not MODEL_ID.fullmatch(value):
            raise ProviderError('invalid_model')
    if provider == 'claude':
        # A gateway credential must not be included in unrelated vendor traffic.
        # These documented switches disable optional telemetry/feature/update calls.
        env.update(CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',
                   DISABLE_TELEMETRY='1', DISABLE_ERROR_REPORTING='1',
                   DISABLE_AUTOUPDATER='1')
    return {**env, 'PATH': path, 'NO_COLOR': '1', 'CI': '1'}
