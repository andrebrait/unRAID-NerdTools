"""Untrusted vendor workspace and provider-fallback boundary tests."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location('upstream_runner', Path(__file__).resolve().parents[1] / 'scripts/upstream-runner.py')
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'
        self.manifest = self.root / 'manifest.json'
        self.archive = self.root / 'input.tar'
        self.report = self.root / 'report.json'
        self.output = self.root / 'output'
        self.report.write_text(json.dumps({'summary': 'done', 'recipes': []}))

    def pack(self, entries=None):
        with tarfile.open(self.archive, 'w') as archive:
            for name, data, kind in entries or [('README.md', b'readme', None), ('build/recipe', b'original', None)]:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o755 if name.startswith('build/') else 0o644
                if kind:
                    info.type = kind
                    info.linkname = '/etc/passwd'
                archive.addfile(info, io.BytesIO(data) if info.isfile() else None)

    def extract(self):
        self.pack()
        runner.extract(self.archive, self.workspace, self.manifest)

    def collect(self, outcome='success'):
        runner.collect(self.workspace, self.manifest, self.report, self.output, outcome)
        return json.loads((self.output / 'status.json').read_text())

    def test_safe_roundtrip_preserves_content_and_executable_mode(self):
        self.extract()
        self.assertEqual(self.collect(), {'outcome': 'success', 'changed': False, 'unsafe': False})
        with tarfile.open(self.output / 'workspace.tar') as archive:
            self.assertEqual(archive.getnames(), ['README.md', 'build/recipe'])
            self.assertEqual(archive.getmember('build/recipe').mode, 0o755)
        self.assertEqual(json.loads((self.output / 'report.json').read_text()), {'summary': 'done', 'recipes': []})

    def test_extraction_rejects_paths_types_and_nonempty_workspace(self):
        for name, kind in [('../escape', None), ('/absolute', None), ('build/../escape', None), ('build//bad', None), ('build/link', tarfile.SYMTYPE), ('build/link', tarfile.LNKTYPE), ('build/device', tarfile.CHRTYPE), ('.git/config', None)]:
            with self.subTest(name=name, kind=kind):
                self.pack([(name, b'x', kind)])
                with self.assertRaises(ValueError):
                    runner.extract(self.archive, self.workspace, self.manifest)
        self.extract()
        with self.assertRaises(ValueError):
            runner.extract(self.archive, self.workspace, self.manifest)

    def test_manifest_and_output_must_be_outside_workspace(self):
        self.pack()
        with self.assertRaises(ValueError):
            runner.extract(self.archive, self.workspace, self.workspace / 'manifest.json')
        self.extract()
        with self.assertRaises(ValueError):
            runner.collect(self.workspace, self.manifest, self.report, self.workspace / 'out')

    def test_changes_deletions_and_mode_changes_prevent_fallback(self):
        self.extract()
        path = self.workspace / 'build/recipe'
        for change in [lambda: path.write_text('changed'), lambda: path.chmod(0o644), lambda: path.unlink()]:
            change()
            self.assertTrue(self.collect('failure')['changed'])
            self.assertFalse((self.output / 'report.json').exists())

    def test_invalid_or_missing_report_does_not_export_success(self):
        self.extract()
        for text in ['not json', '{}', '{"summary":"ok","recipes":[],"extra":1}']:
            self.report.write_text(text)
            status = self.collect()
            self.assertTrue(status['unsafe'])
            self.assertFalse((self.output / 'workspace.tar').exists())
            self.assertFalse((self.output / 'report.json').exists())
        self.report.unlink()
        self.assertTrue(self.collect()['unsafe'])

    def test_missing_source_or_partial_source_is_a_change(self):
        self.extract()
        (self.workspace / 'README.md').unlink()
        self.assertTrue(self.collect('failure')['changed'])
        (self.workspace / 'build/recipe').unlink()
        self.assertTrue(self.collect('failure')['changed'])

    def test_links_special_files_and_extra_sources_fail_closed(self):
        self.extract()
        for name, make in [('link', lambda p: p.symlink_to('/etc/passwd')), ('fifo', os.mkfifo), ('.git', lambda p: p.mkdir()), ('.github', lambda p: p.mkdir())]:
            with self.subTest(name=name):
                path = self.workspace / name
                make(path)
                status = self.collect('failure')
                self.assertTrue(status['unsafe'])
                self.assertTrue(status['changed'])
                self.assertFalse((self.output / 'workspace.tar').exists())
                path.rmdir() if path.is_dir() else path.unlink()

    def test_scratch_downloads_are_omitted(self):
        self.extract()
        scratch = self.workspace / '.upstream-downloads'
        scratch.mkdir()
        (scratch / 'source.tar').write_bytes(b'download')
        self.assertFalse(self.collect()['changed'])

    def eligible(self, text, status=None):
        log = self.root / 'log'
        log.write_text(text)
        status_path = self.root / 'status.json'
        status_path.write_text(json.dumps(status or {'outcome': 'failure', 'changed': False}))
        return runner.fallback(log, status_path)

    def test_provider_events_only_and_unchanged_failure_required(self):
        event = json.dumps({'type': 'turn.failed', 'error': {'message': 'Rate limit exceeded (429)'}})
        self.assertTrue(self.eligible(event))
        self.assertTrue(self.eligible('codex\tRun Codex\t2026-11-15T00:00:00Z ' + event))
        for status in [{'outcome': 'success', 'changed': False}, {'outcome': 'failure', 'changed': True}, {'outcome': 'failure', 'changed': False, 'unsafe': True}]:
            self.assertFalse(self.eligible(event, status))
        self.assertFalse(self.eligible(''))

    def test_model_tool_and_embedded_provider_errors_are_not_evidence(self):
        for event in [
            {'type': 'item.completed', 'item': {'type': 'command_execution', 'aggregated_output': '{"type":"error","message":"quota exceeded"}'}},
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'quota exceeded'}},
            {'type': 'error', 'message': 'build failed: quota exceeded'},
            {'type': 'error', 'message': 'schema validation failed'},
            {'type': 'error', 'message': 'safety refusal'},
        ]:
            self.assertFalse(self.eligible(json.dumps(event)))
        self.assertFalse(self.eligible('prefix {"type":"error","message":"quota exceeded"}'))
        self.assertFalse(self.eligible('other\tRun tests\t2026-11-15T00:00:00Z {"type":"error","message":"quota exceeded"}'))

    def test_limits_apply_before_extracting_content(self):
        self.pack()
        with mock.patch.object(runner, 'MAX_FILE', 2):
            with self.assertRaises(ValueError):
                runner.extract(self.archive, self.workspace, self.manifest)

    def test_member_and_total_limits(self):
        self.pack()
        for constant, limit in [('MAX_TOTAL', 2), ('MAX_MEMBERS', 1)]:
            with mock.patch.object(runner, constant, limit):
                with self.assertRaises(ValueError):
                    runner.extract(self.archive, self.workspace, self.manifest)
        self.assertFalse(self.workspace.exists())

    def test_pure_rollback_is_unchanged(self):
        self.extract()
        path = self.workspace / 'build/recipe'
        path.write_text('temporary change')
        path.write_bytes(b'original')
        path.chmod(0o755)
        self.assertFalse(self.collect('failure')['changed'])

    def test_scratch_link_and_report_link_are_not_followed(self):
        self.extract()
        scratch = self.workspace / '.upstream-downloads'
        scratch.symlink_to(self.root, target_is_directory=True)
        self.assertTrue(self.collect('failure')['unsafe'])
        scratch.unlink()
        target = self.root / 'real-report.json'
        self.report.rename(target)
        self.report.symlink_to(target)
        self.assertTrue(self.collect()['unsafe'])

    def test_collector_caps_files_and_marks_invalid_manifest_unsafe(self):
        self.extract()
        with mock.patch.object(runner, 'MAX_FILE', 2):
            self.assertTrue(self.collect('failure')['unsafe'])
        self.manifest.chmod(0o644)
        self.manifest.write_text('not json')
        self.assertTrue(self.collect('failure')['unsafe'])

    def test_refusal_after_provider_error_still_prevents_fallback(self):
        events = [{'type': 'error', 'message': 'quota exceeded'},
                  {'type': 'turn.failed', 'error': {'message': 'safety refusal'}}]
        self.assertFalse(self.eligible('\n'.join(map(json.dumps, events))))

    def test_provider_codes_and_timeout_events(self):
        for error in ['insufficient_quota', 'rate_limit_exceeded', 'billing_hard_limit_reached',
                      'server_error', 'overloaded_error', 'request_timeout',
                      'HTTP 503 Service Unavailable', 'connection timed out']:
            with self.subTest(error=error):
                self.assertTrue(self.eligible(json.dumps({'type': 'error', 'error': {'code': error}})))

    def test_raw_job_log_timestamp_prefix(self):
        event = '{"type":"turn.failed","error":{"message":"quota exceeded"}}'
        self.assertTrue(self.eligible('2026-11-15T00:00:00.1234567Z ' + event))
        self.assertFalse(self.eligible('2026-11-15T00:00:00Z model said ' + event))

    def test_fallback_cli_outputs_only_boolean_and_handles_missing_fetch(self):
        missing = self.root / 'missing'
        self.assertFalse(runner.fallback(missing, missing))
        output = self.root / 'github-output'
        with mock.patch.dict(os.environ, {'GITHUB_OUTPUT': str(output)}), contextlib.redirect_stdout(io.StringIO()) as stdout:
            runner.main(['fallback', '--log', str(missing), '--status', str(missing)])
        self.assertEqual(stdout.getvalue(), 'eligible=false\n')
        self.assertEqual(output.read_text(), 'eligible=false\n')


if __name__ == '__main__':
    unittest.main()
