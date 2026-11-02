"""Local snapshots from disposable Git clones; no commits or model calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import auto_workspace as module
from auto_workspace import WorkspaceManager, capabilities
from auto_model import AutoError

SOURCE = Path(__file__).resolve().parents[1]


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name); self.root = self.base / 'project'
        subprocess.run(['git', '-c', 'core.hooksPath=' + os.devnull, 'clone', '-q', '--no-hardlinks', '--local', str(SOURCE), str(self.root)], check=True)
        self.manager = WorkspaceManager(self.base / 'managed')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args])

    def head_binding(self):
        # A clone of a detached source is legitimately detached too. Preserve
        # the actual symbolic ref where present, otherwise the exact HEAD SHA.
        result = subprocess.run(['git', '-C', str(self.root), 'symbolic-ref', '--quiet', 'HEAD'], capture_output=True)
        if result.returncode == 0:
            return ('branch', result.stdout)
        if result.returncode == 1:
            return ('detached', self.git('rev-parse', 'HEAD'))
        result.check_returncode()

    def test_current_dirty_content_baseline_preserves_index_head_and_untracked(self):
        readme = self.root / 'README.md'
        readme.write_bytes(b'staged version\n'); self.git('add', 'README.md')
        readme.write_bytes(b'CURRENT working content\n')
        (self.root / 'new.bin').write_bytes(b'\x00\xff\x01')
        (self.root / 'gone.txt').write_text('new tracked then deleted'); self.git('add', 'gone.txt'); (self.root / 'gone.txt').unlink()
        head, branch, index = self.git('rev-parse', 'HEAD'), self.head_binding(), (self.root / '.git/index').read_bytes()
        self.assertTrue(capabilities(self.root)['supported'])
        workspace = self.manager.ensure(self.root, 'project', 'auto-task')
        checkout = Path(workspace['path']); baseline = checkout.parent / 'baseline'
        self.assertEqual('managed_snapshot', workspace['kind'])
        self.assertFalse((checkout / '.git').exists())
        self.assertEqual(b'CURRENT working content\n', (checkout / 'README.md').read_bytes())
        self.assertEqual(b'\x00\xff\x01', (checkout / 'new.bin').read_bytes())
        self.assertFalse((checkout / 'gone.txt').exists())
        self.assertNotEqual((checkout / 'README.md').stat().st_ino, (baseline / 'README.md').stat().st_ino)
        (checkout / 'README.md').write_text('Agent change\n'); (checkout / 'new.bin').write_bytes(b'\x00\x02')
        (checkout / 'added.txt').write_text('new output'); (checkout / 'SKILL.md').unlink()
        result = self.manager.diff(workspace)
        self.assertEqual(workspace['snapshot_id'], result['diff_base'])
        self.assertIn('CURRENT working content', result['tracked_patch']); self.assertIn('Agent change', result['tracked_patch'])
        changes = {v['path']: v for v in result['changes']}
        self.assertEqual('modified', changes['new.bin']['kind']); self.assertTrue(changes['new.bin']['binary_or_large'])
        self.assertEqual('deleted', changes['SKILL.md']['kind']); self.assertEqual('added', changes['added.txt']['kind'])
        self.assertFalse(result['automatic']); self.assertEqual('pending', result['status'])
        self.assertEqual(head, self.git('rev-parse', 'HEAD')); self.assertEqual(branch, self.head_binding())
        self.assertEqual(index, (self.root / '.git/index').read_bytes())
        self.assertEqual(b'CURRENT working content\n', readme.read_bytes())
        self.assertEqual(b'\x00\xff\x01', (self.root / 'new.bin').read_bytes())
        readme.write_text('later original edit')
        self.assertEqual(workspace, self.manager.ensure(self.root, 'project', 'auto-task'))
        self.assertEqual(b'CURRENT working content\n', (baseline / 'README.md').read_bytes())

    def test_malformed_descriptor_and_baseline_fail_controlled(self):
        workspace = self.manager.ensure(self.root, 'project', 'malformed')
        folder = Path(workspace['path']).parent
        descriptor = folder / 'workspace.json'; original = descriptor.read_bytes(); descriptor.chmod(0o600)
        for malformed in ([], None, {'kind': 'managed_snapshot'}):
            descriptor.write_text(json.dumps(malformed))
            with self.assertRaises(AutoError) as caught: self.manager.describe(self.root, 'project', 'malformed')
            self.assertEqual('workspace_conflict', caught.exception.code)
        descriptor.write_bytes(original)
        baseline = folder / 'baseline.json'; baseline.chmod(0o600)
        entries = json.loads(baseline.read_bytes()); name = next(iter(entries)); entries[name]['size'] = True
        raw = module._json(entries); baseline.write_bytes(raw)
        value = json.loads(original); value['snapshot_id'] = module._hash(raw); value['diff_base'] = value['snapshot_id']
        descriptor.write_bytes(module._json(value))
        with self.assertRaises(AutoError) as caught: self.manager.describe(self.root, 'project', 'malformed')
        self.assertEqual('workspace_conflict', caught.exception.code)

    def test_diff_fails_on_unreadable_tree_and_hardlink_outputs(self):
        workspace = self.manager.ensure(self.root, 'project', 'unsafe-diff')
        checkout = Path(workspace['path'])
        actual = os.walk
        def failing(root, **kwargs):
            kwargs['onerror'](PermissionError('fixture denial'))
            return iter(())
        with patch('auto_workspace.os.walk', side_effect=failing), self.assertRaises(AutoError) as caught:
            self.manager.diff(workspace)
        self.assertEqual('snapshot_unreadable', caught.exception.code)
        os.link(self.root / 'README.md', checkout / 'alias.txt')
        with self.assertRaises(AutoError) as caught: self.manager.diff(workspace)
        self.assertEqual('unsafe_workspace', caught.exception.code)
        with self.assertRaises(AutoError) as caught: self.manager.ensure(self.root, 'project', 'unsafe-diff')
        self.assertEqual('unsafe_workspace', caught.exception.code)
        self.assertEqual([], list(checkout.parent.glob('diffs/*/manifest.json')))

    def test_export_retains_binary_and_add_delete_bytes_after_later_turn(self):
        (self.root / 'binary.dat').write_bytes(b'\0old')
        workspace = self.manager.ensure(self.root, 'project', 'export')
        checkout = Path(workspace['path'])
        (checkout / 'binary.dat').write_bytes(b'\0NEW\xff')
        (checkout / 'new.bin').write_bytes(b'\xffadded')
        (checkout / 'README.md').unlink()
        result = self.manager.diff(workspace)
        bundle = Path(result['artifact_directory'])
        self.assertEqual('snapshot_changes_v1', result['export_format'])
        self.assertEqual(bundle / 'manifest.json', Path(result['retained_diff']))
        manifest = json.loads((bundle / 'manifest.json').read_text())
        self.assertEqual(workspace['snapshot_id'], manifest['diff_base'])
        changes = {change['path']: change for change in manifest['changes']}
        self.assertIsNone(changes['README.md']['after'])
        self.assertFalse((bundle / 'after/README.md').exists())
        self.assertEqual(b'\0NEW\xff', (bundle / 'after/binary.dat').read_bytes())
        self.assertEqual(b'\xffadded', (bundle / 'after/new.bin').read_bytes())
        self.assertNotEqual((checkout / 'binary.dat').stat().st_ino, (bundle / 'after/binary.dat').stat().st_ino)
        self.assertEqual(result['artifact_directory'], self.manager.diff(workspace)['artifact_directory'])
        self.assertEqual([], list(checkout.parent.glob('.export-*')))
        self.assertFalse(result['untracked_files_truncated'])
        (checkout / 'binary.dat').write_bytes(b'later turn'); (checkout / 'new.bin').unlink()
        self.assertEqual(b'\0NEW\xff', (bundle / 'after/binary.dat').read_bytes())
        self.assertEqual(b'\xffadded', (bundle / 'after/new.bin').read_bytes())
        next_result = self.manager.diff(workspace)
        self.assertNotEqual(result['artifact_directory'], next_result['artifact_directory'])

    def test_ignored_tracked_included_secrets_and_broker_metadata_omitted(self):
        (self.root / '.gitignore').write_text('ignored.txt\ntracked-ignored.txt\n')
        (self.root / 'tracked-ignored.txt').write_text('tracked visible'); self.git('add', '-f', 'tracked-ignored.txt')
        (self.root / 'ignored.txt').write_text('not copied')
        (self.root / '.env.production').write_text('SECRET'); self.git('add', '-f', '.env.production')
        (self.root / 'private.pem').write_text('KEY')
        (self.root / '.codex').mkdir(); (self.root / '.codex/config.toml').write_text('CREDENTIAL')
        metadata = '.scratch/automatic-task/spec.md'; target = self.root / metadata
        target.parent.mkdir(parents=True, exist_ok=True); target.write_text('broker metadata')
        actual = module._read
        def checked(root, name, *args):
            self.assertFalse(module._omitted(name, [metadata]), 'omitted bytes read')
            return actual(root, name, *args)
        with patch('auto_workspace._read', side_effect=checked):
            workspace = self.manager.ensure(self.root, 'project', 'omitted', metadata_paths=[metadata])
        checkout = Path(workspace['path'])
        self.assertTrue((checkout / 'tracked-ignored.txt').exists())
        for name in ('ignored.txt', '.env.production', 'private.pem', '.codex', metadata): self.assertFalse((checkout / name).exists())
        self.assertIn('.env.production', workspace['omitted_paths'])
        self.assertIn('Embedded secrets', workspace['exclusion_policy'])

    def test_capabilities_does_not_read_bodies_or_capture(self):
        (self.root / 'dirty.txt').write_text('dirty')
        with patch('auto_workspace._read', side_effect=AssertionError('body read')), patch('auto_workspace._write', side_effect=AssertionError('capture')):
            result = capabilities(self.root)
        self.assertTrue(result['supported']); self.assertFalse(result['requires_clean'])
        self.assertEqual('not_captured', result['capture_status'])
        self.assertEqual([], list(self.manager.home.iterdir()))

    def test_filters_and_hooks_never_run_even_when_configured(self):
        marker = self.base / 'SHOULD-NOT-EXIST'
        self.git('config', 'filter.demo.clean', 'touch ' + str(marker))
        self.git('config', 'filter.demo.smudge', 'touch ' + str(marker))
        (self.root / '.gitattributes').write_text('README.md filter=demo\n')
        hook = self.root / '.git/hooks/post-checkout'; hook.write_text('#!/bin/sh\ntouch ' + str(marker)); hook.chmod(0o755)
        workspace = self.manager.ensure(self.root, 'project', 'hooks')
        self.manager.diff(workspace)
        self.assertFalse(marker.exists())

    def test_limits_and_nonregular_symlink_rejected(self):
        with patch('auto_workspace.MAX_FILES', 1): self.assertEqual('snapshot_limit', capabilities(self.root)['reason'])
        (self.root / 'alias').symlink_to(self.root / 'README.md')
        self.assertEqual('unsafe_path', capabilities(self.root)['reason']); (self.root / 'alias').unlink()
        (self.root / 'README.md').unlink(); os.mkfifo(self.root / 'README.md')
        self.assertEqual('unsupported_file_type', capabilities(self.root)['reason'])

    def test_unified_patch_no_final_newline_is_well_formed(self):
        (self.root / 'no-newline.txt').write_bytes(b'old')
        workspace = self.manager.ensure(self.root, 'project', 'newline')
        (Path(workspace['path']) / 'no-newline.txt').write_bytes(b'new')
        patch_text = self.manager.diff(workspace)['tracked_patch']
        self.assertIn('-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file\n', patch_text)
        self.assertNotIn('-old+new', patch_text)

    def test_unicode_control_names_rejected_but_chinese_supported(self):
        for name in ('delete-\x7f', 'control-\x85', 'control-\x9f'):
            with self.assertRaises(AutoError) as caught: module._name(name)
            self.assertEqual('unsupported_filename', caught.exception.code)
        self.assertEqual('文档/说明.md', module._name('文档/说明.md'))

    def test_non_utf8_filename_rejected_without_encoding_crash(self):
        if os.name == 'nt': self.skipTest('byte filenames POSIX only')
        fd = os.open(os.fsencode(self.root) + b'/bad-\xff', os.O_CREAT | os.O_WRONLY, 0o600); os.close(fd)
        self.assertEqual('unsupported_filename', capabilities(self.root)['reason'])

    def test_source_change_during_capture_retries_without_cleanup(self):
        actual = module._read; changed = False
        def racing(root, name, *args):
            nonlocal changed
            result = actual(root, name, *args)
            if root == self.root and not changed:
                changed = True; (self.root / 'README.md').write_text('concurrent USER edit')
            return result
        with patch('auto_workspace._read', side_effect=racing), self.assertRaises(AutoError) as caught:
            self.manager.ensure(self.root, 'project', 'race')
        self.assertEqual('project_changed', caught.exception.code)
        self.assertFalse((self.manager.home / 'project/race').exists())
        self.assertTrue(list((self.manager.home / 'project').glob('.race-capture-*')))
        workspace = self.manager.ensure(self.root, 'project', 'race')
        self.assertEqual('concurrent USER edit', (Path(workspace['path']) / 'README.md').read_text())

    def test_existing_baseline_tampering_rejected_without_recapture(self):
        workspace = self.manager.ensure(self.root, 'project', 'tamper')
        path = Path(workspace['path']).parent / 'baseline/README.md'; path.chmod(0o600); path.write_text('tampered')
        with self.assertRaises(AutoError): self.manager.ensure(self.root, 'project', 'tamper')
        with self.assertRaises(AutoError): self.manager.diff(workspace)

    def test_non_git_unborn_and_service_alias_rejected(self):
        empty = self.base / 'empty'; empty.mkdir(); self.assertEqual('not_git', capabilities(empty)['reason'])
        subprocess.run(['git', 'init', '-q', str(empty)], check=True); self.assertEqual('unborn_git_head', capabilities(empty)['reason'])
        with self.assertRaises(AutoError): WorkspaceManager(self.root / 'managed').ensure(self.root, 'project', 'bad')
        (self.base / 'alias').symlink_to(self.manager.home, target_is_directory=True)
        with self.assertRaises(AutoError): WorkspaceManager(self.base / 'alias')

    def test_unknown_published_directory_not_deleted(self):
        path = self.manager.home / 'project/unknown'; path.mkdir(parents=True); (path / 'keep').write_text('user result')
        with self.assertRaises(AutoError): self.manager.ensure(self.root, 'project', 'unknown')
        self.assertEqual('user result', (path / 'keep').read_text())

    def test_describe_readonly_and_binding_checked(self):
        self.assertIsNone(self.manager.describe(self.root, 'project', 'missing'))
        self.assertFalse((self.manager.home / 'project/missing').exists())
        workspace = self.manager.ensure(self.root, 'project', 'describe')
        with patch('auto_workspace._read', side_effect=AssertionError('body read')):
            self.assertEqual(workspace, self.manager.describe(self.root, 'project', 'describe'))
        bad = dict(workspace, snapshot_id='bad')
        with self.assertRaises(AutoError): self.manager.diff(bad)

    def test_executable_bit_and_binary_addition_diff(self):
        path = self.root / 'run.sh'; path.write_text('echo fixture\n'); path.chmod(0o755)
        workspace = self.manager.ensure(self.root, 'project', 'mode'); checkout = Path(workspace['path'])
        (checkout / 'run.sh').chmod(0o600); (checkout / 'added.bin').write_bytes(b'\0\xff')
        changes = {v['path']: v for v in self.manager.diff(workspace)['changes']}
        self.assertTrue(changes['run.sh']['before']['executable']); self.assertFalse(changes['run.sh']['after']['executable'])
        self.assertTrue(changes['added.bin']['binary_or_large'])

if __name__ == '__main__': unittest.main()
