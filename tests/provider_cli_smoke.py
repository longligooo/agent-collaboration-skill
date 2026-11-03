"""Opt-in installed CLI discovery using an empty credential/config home.

Example: python tests/provider_cli_smoke.py --path /path/to/trusted/cli/bin
Runs only trusted installed provider executables: help, auth status, and Codex
app-server discovery. Never starts a model thread/turn, authenticates, or installs.
Missing authentication is expected. Do not use untrusted executable directories.
This cannot prove successful account execution or native Windows compatibility
when run on another platform. No tokens/config values are printed.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from auto_providers import ProviderRegistry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path', required=True, help='PATH containing trusted CLI executables and needed OS programs')
    args = parser.parse_args()
    original = dict(os.environ)
    with tempfile.TemporaryDirectory(prefix='collab-empty-cli-home-') as root:
        # Never reuse the host account, API credentials or runtime injection.
        env = {key: value for key, value in original.items()
               if key in ('SYSTEMROOT', 'SystemRoot', 'WINDIR', 'TEMP', 'TMP')}
        env.update(HOME=root, USERPROFILE=root, APPDATA=root, LOCALAPPDATA=root,
                   CODEX_HOME=root, CLAUDE_CONFIG_DIR=root, PATH=args.path,
                   NO_COLOR='1', CI='1')
        try:
            os.environ.clear()
            os.environ.update(env)
            registry = ProviderRegistry(path=args.path, timeout=15)
            print(json.dumps(registry.discover(refresh=True), indent=2))
        finally:
            os.environ.clear()
            os.environ.update(original)


if __name__ == '__main__':
    main()
