"""Cross-refresh deltas: unchanged projections stay cached, mutations fail closed."""
import copy
import json
import os
import shutil
import time
import unittest
from pathlib import Path
from unittest import mock

import test_ui_batches as batches
import human_events

server = batches.ui_server


class IncrementalSnapshotTests(unittest.TestCase):
    setUp = batches.ProgressiveSnapshotTests.setUp
    tearDown = batches.ProgressiveSnapshotTests.tearDown
    project = batches.ProgressiveSnapshotTests.project

    def complete(self, since=None):
        pages = [server.progressive_snapshot(path=self.registry, since=since)]
        while not pages[-1]['progress']['complete']:
            pages.append(server.progressive_snapshot(pages[-1]['progress']['cursor']))
        return pages

    def test_unchanged_navigation_has_zero_scans_or_turn_parses_and_zero_thread_payloads(self):
        self.project('a', 20)
        first = self.complete()
        version = first[-1]['sync']['version']
        with mock.patch.object(server, 'scan_thread', side_effect=AssertionError('unchanged thread scan')), \
             mock.patch.object(server.collab, 'parse_turn', side_effect=AssertionError('unchanged turn parse')):
            pages = self.complete(version)
        self.assertEqual(len(pages), 1)
        self.assertFalse(pages[0]['sync']['reset'])
        self.assertEqual(pages[0]['sync']['base_version'], version)
        self.assertEqual(pages[0]['sync']['changed'], [])
        self.assertEqual(pages[0]['progress']['total'], 0)
        self.assertEqual(sum(len(p['threads']) for p in pages[0]['projects']), 0)
        self.assertEqual(pages[0]['sync']['summary']['overview']['threads'], 20)
        self.assertEqual(pages[0]['sync']['summary'], first[-1]['sync']['summary'])

    def test_change_add_delete_and_project_removal_only_scan_changed_records(self):
        a = self.project('a', 3)
        self.project('z', 1)
        version = self.complete()[-1]['sync']['version']
        changed = a / '.ai-collab/threads/thread-000/thread.json'
        data = json.loads(changed.read_text()); data['topic'] = 'changed'; changed.write_text(json.dumps(data))
        shutil.rmtree(a / '.ai-collab/threads/thread-001')
        source = a / '.ai-collab/threads/thread-002/thread.json'
        added = a / '.ai-collab/threads/new/thread.json'; added.parent.mkdir()
        data = json.loads(source.read_text()); data.update(thread_id='new', topic='new'); added.write_text(json.dumps(data))
        registry = json.loads(self.registry.read_text()); registry['projects'] = registry['projects'][:1]
        self.registry.write_text(json.dumps(registry))
        with mock.patch.object(server, 'scan_thread', wraps=server.scan_thread) as scan:
            pages = self.complete(version)
        self.assertEqual({call.args[1] for call in scan.call_args_list}, {'thread-000', 'new'})
        self.assertEqual(len(pages[0]['sync']['removed']), 2)
        self.assertEqual(len(pages[0]['sync']['project_ids']), 1)
        self.assertEqual(pages[-1]['sync']['summary']['overview']['threads'], 3)
        self.assertEqual(pages[-1]['sync']['summary']['overview']['projects'], 1)

    def test_local_human_identity_and_state_changes_invalidate_the_right_threads(self):
        root = self.project('a', 2)
        version = self.complete()[-1]['sync']['version']
        human_events.record_human_event(root, 'thread-000', {
            'schema_version': 1, 'event_id': 'original-1', 'thread_id': 'thread-000',
            'actor_type': 'human', 'author': 'lead', 'body': 'Synthetic private original',
            'created_at': '2026-10-30T09:01:00+00:00', 'recipients': ['builder'],
            'provenance': {'kind': 'manual', 'supplied_authorized': True, 'original_text': True},
        })
        with mock.patch.object(server, 'scan_thread', wraps=server.scan_thread) as scan:
            pages = self.complete(version)
        self.assertEqual([call.args[1] for call in scan.call_args_list], ['thread-000'])
        self.assertEqual(pages[-1]['sync']['summary']['collaborators'][1]['actor_type'], 'human')
        self.assertNotIn('Synthetic private original', json.dumps(pages))
        version = pages[-1]['sync']['version']
        state = root / '.ai-collab/threads/thread-001/state/lead.json'; state.parent.mkdir()
        state.write_text(json.dumps({'protocol_version': 1, 'agent': 'lead', 'processed_message_ids': [], 'updated_at': '2026-01-01T00:00:00Z'}))
        pages = self.complete(version)
        self.assertEqual([item['thread_id'] for item in pages[0]['sync']['changed']], ['thread-001'])
        version = pages[-1]['sync']['version']
        (root / '.ai-collab/identities.json').write_text(json.dumps({'schema_version': 1, 'participants': {'builder': 'stable-person'}}))
        pages = self.complete(version)
        self.assertEqual(len(pages[0]['sync']['changed']), 2)
        self.assertTrue(any(person['id'] == 'stable-person' for person in pages[-1]['sync']['summary']['collaborators']))

    def test_same_size_rewrite_restoring_mtime_is_detected_by_ctime(self):
        root = self.project('a', 1)
        version = self.complete()[-1]['sync']['version']
        manifest = root / '.ai-collab/threads/thread-000/thread.json'
        old = manifest.stat(); text = manifest.read_text()
        manifest.write_text(text.replace('Synthetic batch acceptance', 'Synthetic batch acceptancE'))
        os.utime(manifest, ns=(old.st_atime_ns, old.st_mtime_ns))
        pages = self.complete(version)
        self.assertEqual(pages[0]['progress']['total'], 1)

    def test_versions_expire_reset_and_cache_limits_are_bounded(self):
        self.project('a', 1)
        first = self.complete()[-1]['sync']['version']
        with mock.patch.object(server.time, 'monotonic', return_value=time.monotonic() + server.SNAPSHOT_VERSION_TTL + 1):
            reset = self.complete(first)
        self.assertTrue(reset[0]['sync']['reset'])
        self.assertEqual(reset[0]['sync']['base_version'], None)
        for _ in range(7):
            self.complete()
            self.assertLessEqual(len(server.SNAPSHOT_VERSIONS), server.SNAPSHOT_MAX_VERSIONS)
            self.assertLessEqual(sum(item['bytes'] for item in server.SNAPSHOT_VERSIONS.values()), server.SNAPSHOT_MAX_CACHE_BYTES)
        self.assertTrue(self.complete('f' * 32)[0]['sync']['reset'])
        for invalid in ['', '../outside', 'x' * 32]:
            with self.assertRaises(server.SnapshotRequestError):
                self.complete(invalid)

    def test_midscan_mutation_does_not_publish_a_version_and_retry_starts_clean(self):
        root = self.project('a', 2)
        version = self.complete()[-1]['sync']['version']
        manifest = root / '.ai-collab/threads/thread-000/thread.json'
        manifest.write_text(manifest.read_text().replace('Synthetic', 'Modified'))
        shell = server.progressive_snapshot(path=self.registry, since=version)
        before = set(server.SNAPSHOT_VERSIONS)
        original = server.scan_thread
        def mutate(*args):
            record = original(*args)
            manifest.write_text(manifest.read_text().replace('Modified', 'Mutating'))
            return record
        with mock.patch.object(server, 'scan_thread', side_effect=mutate):
            with self.assertRaises(server.SnapshotRequestError) as exc:
                server.progressive_snapshot(shell['progress']['cursor'])
        self.assertEqual(exc.exception.status, 409)
        self.assertEqual(set(server.SNAPSHOT_VERSIONS), before)
        pages = self.complete(version)
        self.assertEqual(pages[0]['progress']['total'], 1)

    def test_bad_thread_or_unavailable_project_does_not_poison_completed_baseline(self):
        root = self.project('a', 1)
        version = self.complete()[-1]['sync']['version']
        manifest = root / '.ai-collab/threads/thread-000/thread.json'
        saved = manifest.read_text(); manifest.write_text('{"bad":true}')
        before = set(server.SNAPSHOT_VERSIONS)
        with self.assertRaises(server.SnapshotRequestError):
            self.complete(version)
        self.assertEqual(set(server.SNAPSHOT_VERSIONS), before)
        manifest.write_text(saved)
        shutil.rmtree(root)
        with self.assertRaises(server.SnapshotRequestError) as exc:
            self.complete(version)
        self.assertEqual(exc.exception.status, 409)

    def test_symlink_metadata_never_reads_external_content(self):
        root = self.project('a', 1)
        outside = self.base / 'outside'; outside.mkdir(); (outside / 'secret').write_text('not readable by scanner')
        (root / 'link').symlink_to(outside, target_is_directory=True)
        before = server.metadata_fingerprint(root, '.')
        (outside / 'secret').write_text('changed private contents')
        self.assertEqual(server.metadata_fingerprint(root, '.'), before)
        (root / '.ai-collab/local').symlink_to(outside, target_is_directory=True)
        with mock.patch.object(Path, 'read_text', side_effect=AssertionError('metadata must not read text')):
            server.metadata_fingerprint(root, '.ai-collab/local/human-events/thread-000')

    def test_coding_acceptance_invalidates_for_source_and_external_evidence_log_changes(self):
        import test_task_model
        fixture = test_task_model.CodingTaskTest()
        fixture.setUp()
        try:
            fixture.prepare_ready()
            server.register_project(fixture.root, self.registry)
            first = self.complete()
            record = next(t for page in first for p in page['projects'] for t in p['threads'])
            self.assertTrue(record['coding_task']['acceptance']['ready'])
            version = first[-1]['sync']['version']
            with mock.patch.object(server, 'scan_thread', side_effect=AssertionError('unchanged task scan')):
                unchanged = self.complete(version)
            self.assertEqual(unchanged[0]['progress']['total'], 0)
            log = fixture.root / '.ai-collab/logs/evidence-1.log'
            log.write_text('Evidence has changed')
            changed = self.complete(unchanged[-1]['sync']['version'])
            record = next(t for page in changed for p in page['projects'] for t in p['threads'])
            self.assertTrue(any(b['code'] == 'changed-log' for b in record['coding_task']['acceptance']['blockers']))
            (fixture.root / 'product.py').write_text('VALUE = 2\n')
            changed = self.complete(changed[-1]['sync']['version'])
            record = next(t for page in changed for p in page['projects'] for t in p['threads'])
            self.assertIn('code-changed', record['coding_task']['recovery']['reasons'])
            self.assertFalse(record['coding_task']['acceptance']['ready'])
        finally:
            fixture.tearDown()

    def test_oversized_generation_fails_without_replacing_baseline(self):
        root = self.project('a', 1)
        version = self.complete()[-1]['sync']['version']
        manifest = root / '.ai-collab/threads/thread-000/thread.json'
        manifest.write_text(manifest.read_text().replace('Synthetic', 'Modified'))
        before = set(server.SNAPSHOT_VERSIONS)
        with mock.patch.object(server, 'SNAPSHOT_MAX_VERSION_BYTES', 1):
            with self.assertRaises(server.SnapshotRequestError) as error:
                self.complete(version)
        self.assertEqual(error.exception.status, 413)
        self.assertEqual(set(server.SNAPSHOT_VERSIONS), before)

    def test_parallel_generations_reuse_one_baseline_without_mutating_it(self):
        root = self.project('a', 2)
        version = self.complete()[-1]['sync']['version']
        baseline = copy.deepcopy(server.SNAPSHOT_VERSIONS[version]['records'])
        manifest = root / '.ai-collab/threads/thread-000/thread.json'
        manifest.write_text(manifest.read_text().replace('Synthetic', 'Modified'))
        first = server.progressive_snapshot(path=self.registry, since=version)
        second = server.progressive_snapshot(path=self.registry, since=version)
        result2 = server.progressive_snapshot(second['progress']['cursor'])
        result1 = server.progressive_snapshot(first['progress']['cursor'])
        self.assertEqual(server.SNAPSHOT_VERSIONS[version]['records'], baseline)
        self.assertEqual(result1['sync']['summary'], result2['sync']['summary'])
        self.assertNotEqual(result1['sync']['version'], result2['sync']['version'])

    def test_git_index_only_force_add_and_remove_invalidate_task_context(self):
        import test_task_model
        fixture = test_task_model.CodingTaskTest()
        fixture.setUp()
        try:
            fixture.initialize()
            (fixture.root / 'ignored.txt').write_text('preexisting ignored source')
            server.register_project(fixture.root, self.registry)
            first = self.complete()
            fingerprint = server.project_code_fingerprint(fixture.root)
            fixture.git('add', '-f', 'ignored.txt')
            self.assertNotEqual(server.project_code_fingerprint(fixture.root), fingerprint)
            added = self.complete(first[-1]['sync']['version'])
            self.assertEqual(added[0]['progress']['total'], 1)
            fingerprint = server.project_code_fingerprint(fixture.root)
            fixture.git('rm', '--cached', '-f', 'ignored.txt')
            self.assertNotEqual(server.project_code_fingerprint(fixture.root), fingerprint)
            removed = self.complete(added[-1]['sync']['version'])
            self.assertEqual(removed[0]['progress']['total'], 1)
        finally:
            fixture.tearDown()

    def test_version_from_different_registry_is_explicit_reset(self):
        self.project('a', 1)
        version = self.complete()[-1]['sync']['version']
        other = self.base / 'other-registry.json'
        other.write_text(self.registry.read_text())
        page = server.progressive_snapshot(path=other, since=version)
        self.assertTrue(page['sync']['reset'])
        self.assertIsNone(page['sync']['base_version'])
        self.assertEqual(page['progress']['total'], 1)

    def test_nested_git_head_changes_are_part_of_source_fingerprint(self):
        import subprocess
        root = self.project('a', 1)
        def git(path, *args):
            subprocess.run(['git', '-C', str(path), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        git(root, 'init', '-q')
        nested = root / 'nested'; nested.mkdir(); git(nested, 'init', '-q')
        (nested / 'source.txt').write_text('same source')
        git(nested, 'add', '.')
        git(nested, '-c', 'user.name=Fixture', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'first')
        git(root, 'add', 'nested')
        first = server.project_code_fingerprint(root)
        git(nested, '-c', 'user.name=Fixture', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-qm', 'head-only')
        self.assertNotEqual(server.project_code_fingerprint(root), first)

    def test_partial_turn_read_error_does_not_become_cached_success(self):
        self.project('a', 1)
        original = server.scan_thread
        def failed(*args):
            result = original(*args)
            result['turns'] = [{'error': 'temporary read error', 'document': 'synthetic'}]
            return result
        with mock.patch.object(server, 'scan_thread', side_effect=failed):
            with self.assertRaises(server.SnapshotRequestError) as error:
                self.complete()
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(server.SNAPSHOT_VERSIONS, {})

    def test_identity_or_wake_change_between_shell_and_batch_rejects_mixed_generation(self):
        root = self.project('a', 1)
        identity = root / '.ai-collab/identities.json'
        identity.write_text(json.dumps({'schema_version': 1, 'participants': {'builder': 'old'}}))
        for config in ['identities.json', 'wake.json']:
            shell = server.progressive_snapshot(path=self.registry)
            before_versions = set(server.SNAPSHOT_VERSIONS)
            if config == 'identities.json':
                identity.write_text(json.dumps({'schema_version': 1, 'participants': {'builder': 'new'}}))
            else:
                (root / '.ai-collab/wake.json').write_text(json.dumps({'agents': {'builder': {'tool': 'codex'}}}))
            with mock.patch.object(server, 'scan_thread', side_effect=AssertionError('must reject before scanning')):
                with self.assertRaises(server.SnapshotRequestError) as error:
                    server.progressive_snapshot(shell['progress']['cursor'])
            self.assertEqual(error.exception.status, 409)
            self.assertEqual(set(server.SNAPSHOT_VERSIONS), before_versions)
        pages = self.complete()
        actual = pages[-1]['sync']['summary']
        expected = server.snapshot(self.registry)
        self.assertEqual(actual, {key: expected[key] for key in ('collaborators', 'overview')})
        self.assertNotIn('old', [person['id'] for person in actual['collaborators']])

    def test_internal_symlink_target_changes_invalidate_without_following_external_links(self):
        root = self.project('a', 1)
        manifest = root / '.ai-collab/threads/thread-000/thread.json'
        target = root / 'manifest.json'; manifest.rename(target); manifest.symlink_to(target)
        first = self.complete()
        data = json.loads(target.read_text()); data['topic'] = 'changed-target'; target.write_text(json.dumps(data))
        with mock.patch.object(server, 'scan_thread', wraps=server.scan_thread) as scan:
            changed = self.complete(first[-1]['sync']['version'])
        self.assertEqual(scan.call_count, 1)
        records = [t for page in changed for p in page['projects'] for t in p['threads']]
        self.assertEqual(records[0]['topic'], 'changed-target')
        (root / 'cycle-a').symlink_to(root / 'cycle-b')
        (root / 'cycle-b').symlink_to(root / 'cycle-a')
        server.metadata_fingerprint(root, 'cycle-a')
        outside = self.base / 'private'; outside.mkdir(); (outside / 'secret').write_text('private')
        (root / 'external').symlink_to(outside, target_is_directory=True)
        original = Path.lstat
        def guarded(path, *args, **kwargs):
            if path == outside or outside in path.parents:
                raise AssertionError('external symlink target metadata must not be accessed')
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, 'lstat', guarded):
            server.metadata_fingerprint(root, 'external/secret')
