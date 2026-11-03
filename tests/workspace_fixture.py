"""Hermetic ancestry for disposable snapshot tests, never production behavior.

Some execution hosts install protected empty .git scaffolding at /tmp or other
workspace ancestors. Production correctly fails closed on those markers. These
tests simulate an ordinary non-repository parent while preserving all real
markers, ignores and filesystem operations inside their disposable fixture.
"""
import os
from pathlib import Path
from unittest.mock import patch


def isolate_ancestor_markers(base):
    base = Path(base).absolute()
    outside_markers = {ancestor / '.git' for ancestor in base.parents}
    original = os.path.lexists

    def exists(path):
        if Path(path).absolute() in outside_markers:
            return False
        return original(path)

    return patch.object(os.path, 'lexists', side_effect=exists)
