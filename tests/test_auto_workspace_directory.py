"""Non-Git document projects use the same immutable snapshot and export contract."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from workspace_fixture import isolate_ancestor_markers
import auto_workspace as m
from auto_model import AutoError

class DirectoryWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name); self.root = self.base / 'documents'; self.root.mkdir()
        ancestry = isolate_ancestor_markers(self.base)
        ancestry.start(); self.addCleanup(ancestry.stop)
        self.manager = m.WorkspaceManager(self.base / 'managed')

    def test_no_git_full_capture_binary_deletion_export_and_later_original_edit(self):
        (self.root / 'plan.md').write_bytes(b'original\n')
        (self.root / 'gone.txt').write_bytes(b'gone')
        (self.root / 'figure.bin').write_bytes(b'\0\xff')
        with patch.object(m, '_git', side_effect=AssertionError('Git must not be called')):
            cap = m.capabilities(self.root); self.assertTrue(cap['supported']); self.assertEqual('directory', cap['source_kind'])
            w = self.manager.ensure(self.root, 'project', 'task')
            self.assertIsNone(w['base_commit']); self.assertFalse((self.root / '.git').exists())
            checkout = Path(w['path'])
            (checkout / 'plan.md').write_bytes(b'AI edit\n'); (checkout / 'gone.txt').unlink()
            (checkout / 'figure.bin').write_bytes(b'\0new'); (checkout / 'new.txt').write_bytes(b'new')
            (self.root / 'plan.md').write_bytes(b'user later edit\n')
            out = self.manager.diff(w); self.assertEqual(4, out['change_count']); self.assertFalse(out['automatic'])
            manifest = json.loads(Path(out['retained_diff']).read_text())
            self.assertEqual('directory', manifest['source_kind']); self.assertIsNone(manifest['base_commit'])
            self.assertEqual(b'\0new', (Path(out['artifact_directory']) / 'after/figure.bin').read_bytes())
            self.assertEqual(b'user later edit\n', (self.root / 'plan.md').read_bytes())
            self.assertEqual(b'gone', (self.root / 'gone.txt').read_bytes())
            self.assertEqual(w['snapshot_id'], self.manager.ensure(self.root, 'project', 'task')['snapshot_id'])

    def test_exclusions_and_metadata(self):
        (self.root / 'ok.md').write_text('ok')
        (self.root / '.env').write_text('secret')
        (self.root / 'token.key').write_text('secret')
        (self.root / 'broker.json').write_text('metadata')
        for name in ('.claude', 'node_modules', '.venv'):
            (self.root / name).mkdir(); (self.root / name / 'secret').write_text('secret')
        w = self.manager.ensure(self.root, 'project', 'task', metadata_paths=('broker.json',))
        self.assertEqual(['ok.md'], sorted(p.name for p in Path(w['path']).iterdir()))

    def test_symlink_and_fifo_rejected(self):
        target = self.base / 'outside'; target.write_text('secret')
        try: (self.root / 'alias').symlink_to(target)
        except OSError: self.skipTest('symlink unsupported')
        self.assertEqual('unsafe_path', m.capabilities(self.root)['reason'])
        (self.root / 'alias').unlink()
        if hasattr(os, 'mkfifo'):
            os.mkfifo(self.root / 'pipe'); self.assertEqual('unsupported_file_type', m.capabilities(self.root)['reason'])

    def test_limits_and_empty_folder(self):
        self.assertTrue(m.capabilities(self.root)['supported'])
        (self.root / 'a').write_text('a'); (self.root / 'b').write_text('b')
        with patch.object(m, 'MAX_FILES', 1): self.assertEqual('snapshot_limit', m.capabilities(self.root)['reason'])

    def test_concurrent_change_aborts_snapshot(self):
        (self.root / 'a').write_text('before')
        original = m._read
        def changed(root, name, *args, **kw):
            result = original(root, name, *args, **kw)
            if root == self.root: (self.root / 'a').write_text('after')
            return result
        with patch.object(m, '_read', side_effect=changed):
            with self.assertRaises(AutoError) as cm: self.manager.ensure(self.root, 'project', 'task')
        self.assertEqual('project_changed', cm.exception.code)
        self.assertFalse((self.base / 'managed/project/task').exists())

    def test_nested_repo_and_ancestor_markers_do_not_become_plain(self):
        nested = self.root / 'nested'; nested.mkdir(); (nested / '.git').mkdir()
        self.assertEqual('unsupported_nested_repository', m.capabilities(self.root)['reason'])
        (nested / '.git').rmdir(); (self.base / '.git').mkdir(); (self.base / '.git/HEAD').write_text('ref: refs/heads/main')
        with patch.object(m, '_git', return_value=(1, b'')):
            self.assertEqual('not_git', m.capabilities(self.root)['reason'])

    def test_incomplete_ancestor_repository_cannot_bypass_ignore_rules(self):
        (self.base / '.git').mkdir()  # Deliberately missing HEAD.
        (self.base / '.gitignore').write_text('documents/private.txt\n')
        (self.root / 'private.txt').write_text('synthetic private content')
        with patch.object(m, '_git', return_value=(1, b'')):
            capability = m.capabilities(self.root)
            self.assertFalse(capability['supported'])
            self.assertEqual('not_git', capability['reason'])
            with self.assertRaises(AutoError) as error:
                self.manager.ensure(self.root, 'project', 'task')
            self.assertEqual('not_git', error.exception.code)
        self.assertFalse((self.base / 'managed/project/task').exists())

    def test_ancestor_git_alias_without_head_is_rejected(self):
        outside = self.base / 'outside'; outside.mkdir()
        try: (self.base / '.git').symlink_to(outside, target_is_directory=True)
        except OSError: self.skipTest('symlink unsupported')
        with patch.object(m, '_git', side_effect=AssertionError('alias must be rejected first')):
            self.assertEqual('unsafe_path', m.capabilities(self.root)['reason'])

    def test_tampered_source_kind_rejected(self):
        w = self.manager.ensure(self.root, 'project', 'task')
        p = Path(w['path']).parent / 'workspace.json'; p.chmod(0o600)
        v = json.loads(p.read_text()); v['source_kind'] = 'unknown'; p.write_text(json.dumps(v))
        with self.assertRaises(AutoError): self.manager.describe(self.root, 'project', 'task')

    def test_ignore_rules_fail_closed(self):
        (self.root / '.gitignore').write_text('private.txt\n')
        (self.root / 'private.txt').write_text('secret')
        self.assertEqual('plain_ignore_rules_unsupported', m.capabilities(self.root)['reason'])

    def test_git_marker_alias_rejected(self):
        outside = self.base / 'elsewhere'; outside.mkdir()
        try: (self.root / '.git').symlink_to(outside, target_is_directory=True)
        except OSError: self.skipTest('symlink unsupported')
        self.assertEqual('unsafe_path', m.capabilities(self.root)['reason'])

    def test_candidate_limit_counts_omitted_directories(self):
        for i in range(12): (self.root / ('.env-' + str(i))).mkdir()
        class Many:
            def __enter__(self):
                with os.scandir(self.root) as entries: self.items = list(entries)
                return iter(self.items * 3000)
            def __exit__(self, *args): pass
        many = Many(); many.root = self.root
        real = os.scandir
        def enter():
            with real(self.root) as entries: many.items = list(entries)
            return iter(many.items * 3000)
        many.__class__.__enter__ = lambda _: enter()
        with patch.object(m.os, 'scandir', return_value=many):
            self.assertEqual('snapshot_limit', m.capabilities(self.root)['reason'])
