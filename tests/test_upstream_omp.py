"""Trust-boundary checks for the OMP adapter (no model requests)."""
import importlib.util
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


agent = load('omp_agent', 'upstream-agent.py')
bridge = load('omp_tool', 'upstream-tool.py')


class OmpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.work = self.root / 'workspace'
        self.work.mkdir()
        self.config = {'workspace': str(self.work), 'control': str(self.root),
                       'image': os.environ.get('OMP_SANDBOX_IMAGE', 'python@sha256:' + 'a' * 64),
                       'container': 'nerdtools-test-' + str(os.getpid()),
                       'deadline': time.monotonic() + 120, 'candidates': ['demo']}

    def tearDown(self):
        self.temp.cleanup()

    def test_docker_arguments_expose_only_workspace(self):
        args = bridge.docker_args(self.config, 'echo hello')
        self.assertIn('--read-only', args)
        for flag, value in [('--network', 'none'), ('--cap-drop', 'ALL'),
                            ('--security-opt', 'no-new-privileges'), ('--pids-limit', '128'),
                            ('--memory', '1g'), ('--cpus', '2')]:
            self.assertEqual(args[args.index(flag) + 1], value)
        self.assertEqual(args.count('--mount'), 1)
        self.assertEqual(args[args.index('--mount') + 1], f'type=bind,src={self.work},dst=/work')
        self.assertNotIn('--privileged', args)
        self.assertEqual(args[-3:], ['sh', '-c', 'echo hello'])

    def test_fetch_path_rejects_symlinks_and_secrets(self):
        (self.work / 'link').symlink_to(self.root, target_is_directory=True)
        for path in ['../outside', '/tmp/outside', 'link/output', '.git/config', '.env', 'api_token.txt']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                bridge.destination(self.work, path)

    def test_fetch_computes_hash_and_bounds_text(self):
        data = b'a' * 70000
        class Response(io.BytesIO):
            headers = {'Content-Length': str(len(data))}
        class Opener:
            def open(self, *args, **kwargs):
                return Response(data)
        self.config['bootstrap'] = str(ROOT / 'build/unraid-build-root/bootstrap.py')
        with patch.object(bridge.urllib.request, 'build_opener', return_value=Opener()) as opener:
            result = bridge.fetch_source(self.config, 'https://example.org/source', offset=5)
        self.assertEqual(result['sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(len(result['text']), 65536)
        self.assertEqual(result['next_offset'], 65541)
        self.assertEqual((self.work / result['path']).read_bytes(), data)
        self.assertEqual(opener.call_args.args[0].proxies, {})
        self.assertEqual(type(opener.call_args.args[1]).__name__, 'PinnedHTTPSHandler')

    def test_fetch_byte_limit_removes_partial_download(self):
        class Response(io.BytesIO):
            headers = {}
        class Opener:
            def open(self, *args, **kwargs):
                return Response(b'x' * 100)
        self.config['bootstrap'] = str(ROOT / 'build/unraid-build-root/bootstrap.py')
        with patch.object(bridge.urllib.request, 'build_opener', return_value=Opener()), \
                patch.object(bridge, 'MAX_DOWNLOAD', 10), self.assertRaises(ValueError):
            bridge.fetch_source(self.config, 'https://example.org/source', path='source.tar')
        self.assertFalse((self.work / 'source.tar').exists())
        self.assertEqual((self.root / 'download-total').read_text(), '10')
        with patch.object(bridge.urllib.request, 'build_opener') as opener, \
                patch.object(bridge, 'MAX_TOTAL', 10), self.assertRaises(ValueError):
            bridge.fetch_source(self.config, 'https://example.org/source', path='next.tar')
        opener.assert_not_called()

    def test_fetch_accepts_declared_exact_boundary(self):
        class Response(io.BytesIO):
            headers = {'Content-Length': '10'}
        class Opener:
            def open(self, *args, **kwargs):
                return Response(b'x' * 10)
        self.config['bootstrap'] = str(ROOT / 'build/unraid-build-root/bootstrap.py')
        with patch.object(bridge.urllib.request, 'build_opener', return_value=Opener()), \
                patch.object(bridge, 'MAX_TOTAL', 10):
            result = bridge.fetch_source(self.config, 'https://example.org/source')
        self.assertEqual(result['sha256'], hashlib.sha256(b'x' * 10).hexdigest())

    def test_fetch_rejects_ambiguous_framing_before_reads(self):
        class Response(io.BytesIO):
            headers = {'Content-Length': '10', 'Transfer-Encoding': 'chunked'}
            def read1(self, _size):
                raise AssertionError('Ambiguous framing must not read bytes')
        class Opener:
            def open(self, *args, **kwargs):
                return Response(b'x' * 10)
        self.config['bootstrap'] = str(ROOT / 'build/unraid-build-root/bootstrap.py')
        with patch.object(bridge.urllib.request, 'build_opener', return_value=Opener()), self.assertRaises(ValueError):
            bridge.fetch_source(self.config, 'https://example.org/source')

    def test_finish_requires_unique_complete_decisions_and_owns_report(self):
        report = {'summary': 'done', 'recipes': [{'name': 'demo', 'decision': 'accept', 'reason': 'updated'}]}
        forged = self.work / 'report.json'
        forged.write_text('{}')
        with self.assertRaises(ValueError):
            bridge.finish(self.config, {'summary': 'bad', 'recipes': []})
        bridge.finish(self.config, report)
        self.assertEqual(json.loads((self.root / 'report.json').read_text()), report)
        with self.assertRaises((ValueError, FileExistsError)):
            bridge.finish(self.config, report)

    def test_snapshot_excludes_git_and_preserves_repair_edits(self):
        repo = self.root / 'repo'
        repo.mkdir()
        def git(*args):
            return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)
        git('init')
        git('config', 'user.name', 'Test')
        git('config', 'user.email', 'test@example.org')
        recipe = repo / 'build/recipes/demo/demo.sh'
        recipe.parent.mkdir(parents=True)
        recipe.write_text('old')
        (repo / 'README.md').write_text('public guide')
        (repo / 'legacy-package.txz').write_bytes(b'not exported')
        git('add', '.')
        git('commit', '-m', 'initial')
        recipe.write_text('repair')
        (repo / '.env').write_text('SECRET=hidden')
        agent.snapshot(repo, self.work)
        self.assertEqual((self.work / 'build/recipes/demo/demo.sh').read_text(), 'repair')
        self.assertFalse((self.work / '.git').exists())
        self.assertFalse((self.work / '.env').exists())
        self.assertFalse((self.work / 'legacy-package.txz').exists())
        self.assertEqual((self.work / 'README.md').read_text(), 'public guide')

    def test_fallback_requires_availability_and_no_tool_mutation(self):
        for message in ['HTTP 429 rate limit', 'HTTP 503 service unavailable', 'No API key found for openai']:
            self.assertTrue(agent.availability_error(message))
        for message in ['HTTP 401 unauthorized', 'refusal', 'max cost exceeded', 'invalid tool arguments']:
            self.assertFalse(agent.availability_error(message))
        (self.root / 'touched').touch()
        self.assertFalse(agent.can_fallback(self.root, 'HTTP 503 service unavailable'))

    def test_provider_failure_ignores_source_text_and_refusals(self):
        self.assertEqual(agent.provider_failure('HTTP 503 service unavailable'), '')
        self.assertEqual(agent.provider_failure(json.dumps({'type': 'message_end', 'message': {
            'role': 'assistant', 'stopReason': 'stop', 'content': 'HTTP 503'}})), '')
        event = {'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'error',
                                                  'errorMessage': 'HTTP 503 service unavailable'}}
        self.assertEqual(agent.provider_failure(json.dumps(event)), 'HTTP 503 service unavailable')

    def test_launcher_isolates_environment_and_requires_owned_finish(self):
        binary = self.root / 'omp'
        binary.write_bytes(b'trusted test binary')
        report = {'summary': 'done', 'recipes': [{'name': 'demo', 'decision': 'reject', 'reason': 'unverified'}]}
        calls = []
        prompts = []
        class Process:
            def __init__(self, args, **kwargs):
                calls.append((args, kwargs))
                prompts.append(Path(args[-1][1:]).read_text())
                config = json.loads(Path(kwargs['env']['UPSTREAM_CONFIG']).read_text())
                self.control = Path(config['control'])
                kwargs['stdout'].write(b'private trace\n')
            def wait(self, timeout):
                (self.control / 'report.json').write_text(json.dumps(report))
                return 0
        with patch.dict(os.environ, {'OMP_BINARY': str(binary),
                        'OMP_SANDBOX_IMAGE': self.config['image'], 'OPENAI_API_KEY': 'provider-secret',
                        'OPENAI_MODEL': 'gpt-test', 'ANTHROPIC_MODEL': 'anthropic/claude-test',
                        'OMP_TRACE_FILE': str(self.root / 'trace.jsonl'),
                        'GH_TOKEN': 'host-secret', 'GITHUB_TOKEN': 'host-secret'}, clear=True), \
                patch.object(agent, 'OMP_SHA256', __import__('hashlib').sha256(binary.read_bytes()).hexdigest()), \
                patch.object(agent, 'snapshot'), patch.object(agent.subprocess, 'Popen', Process), \
                patch.object(agent.subprocess, 'run'):
            result = agent.run_agent(self.root, {'candidates': [{'name': 'demo'}], 'guide': 'real guide'})
        work = Path(result.pop('workspace'))
        try:
            self.assertEqual(result, report)
            args, kwargs = calls[0]
            self.assertEqual(args[args.index('--tools') + 1], 'workspace,fetch_source,finish')
            for flag in ['--no-tools', '--no-extensions', '--no-skills', '--no-rules', '--no-context-files',
                         '--no-lsp', '--no-session', '--no-prewalk']:
                self.assertIn(flag, args)
            self.assertEqual(args[args.index('--max-cost') + 1], '2.5')
            self.assertLessEqual(float(args[args.index('--max-time') + 1]), 1800)
            self.assertEqual(args[args.index('--model') + 1], 'openai/gpt-test')
            self.assertEqual((self.root / 'trace.jsonl').read_bytes(), b'private trace\n')
            self.assertEqual((self.root / 'trace.jsonl').stat().st_mode & 0o777, 0o600)
            self.assertNotIn('OMP_TRACE_FILE', kwargs['env'])
            self.assertNotIn('GH_TOKEN', kwargs['env'])
            self.assertNotIn('GITHUB_TOKEN', kwargs['env'])
            self.assertNotEqual(Path(kwargs['cwd']), work)
            self.assertIn('real guide', prompts[0])
        finally:
            shutil.rmtree(work)

    def test_failed_session_removes_workspace(self):
        binary = self.root / 'omp'
        binary.write_bytes(b'test')
        workspaces = []
        def snapshot(_repo, work):
            workspaces.append(work)
        class Process:
            def __init__(self, *args, **kwargs):
                pass
            def wait(self, timeout):
                return 1
        with patch.dict(os.environ, {'OMP_BINARY': str(binary), 'OMP_SANDBOX_IMAGE': self.config['image']}, clear=True), \
                patch.object(agent, 'OMP_SHA256', __import__('hashlib').sha256(binary.read_bytes()).hexdigest()), \
                patch.object(agent, 'snapshot', snapshot), patch.object(agent.subprocess, 'Popen', Process), \
                patch.object(agent.subprocess, 'run'), self.assertRaises(agent.AgentError):
            agent.run_agent(self.root, {'candidates': [{'name': 'demo'}]})
        self.assertEqual(len(workspaces), 1)
        self.assertFalse(workspaces[0].exists())

    @unittest.skipUnless(shutil.which('docker') and os.environ.get('OMP_SANDBOX_IMAGE'), 'requires pinned Docker image')
    def test_real_workspace_command_is_offline_and_unprivileged(self):
        command = """python - <<'PY'
import os, socket
from pathlib import Path
assert not any(name in os.environ for name in ('GH_TOKEN', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY'))
assert not Path('/work/.git').exists()
assert not Path('/var/run/docker.sock').exists()
assert not Path('/root/.omp').exists()
assert 'CapEff:\\t0000000000000000' in Path('/proc/self/status').read_text()
try:
    Path('/etc/nerdtools-isolation-probe').write_text('bad')
except OSError:
    pass
else:
    raise AssertionError('Container root filesystem is writable')
try:
    socket.create_connection(('1.1.1.1', 443), timeout=0.2)
except OSError:
    pass
else:
    raise AssertionError('Workspace can access the network')
Path('/work/result').write_text('isolated')
PY"""
        with patch.dict(os.environ, {'GH_TOKEN': 'host-secret', 'OPENAI_API_KEY': 'host-key'}):
            result = bridge.workspace(self.config, command)
        self.assertEqual(result['exit_code'], 0, result)
        self.assertEqual((self.work / 'result').read_text(), 'isolated')


if __name__ == '__main__':
    unittest.main()
