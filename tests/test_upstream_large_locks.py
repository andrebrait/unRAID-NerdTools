"""Large pinned module feeds must not require a whole-feed model round trip."""
import copy
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.error

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/upstream-agent.py'
SPEC = importlib.util.spec_from_file_location('large_lock_agent', SCRIPT)
agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def scripted_response(body, length=True, status=200, headers=None):
    """HTTP response stand-in: declares Content-Length unless length=False; read() serves the body like a socket."""
    response = mock.Mock(status=status)
    fields = {**({'Content-Length': str(len(body))} if length else {}), **(headers or {})}
    response.getheader.side_effect = lambda name, default=None: fields.get(name, default)
    response.read.side_effect = io.BytesIO(body).read
    return response


def streamed(downloads):
    """fetch stand-in for {url: (sha256, size)}: streams each body through on_bytes, as a real transfer does."""
    def fake(url, mode, offset=0, *, max_bytes=None, on_bytes=None):
        digest, size = downloads[url]
        on_bytes(size)
        return {'url': url, 'sha256': digest, 'bytes': size}
    return fake


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


class TrickleBody:
    """Provider response whose body arrives piece by piece; each read1 advances the fake clock by `step` seconds."""
    def __init__(self, clock, pieces, step=0.0):
        self.clock, self.pieces, self.step, self.reads = clock, list(pieces), step, 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read1(self, size):
        self.reads += 1
        if self.reads > 1000:
            raise AssertionError('provider body was never cut off')
        self.clock.now += self.step
        return self.pieces.pop(0) if self.pieces else b''


def anthropic_finish():
    finish = {'summary': 'done', 'recipes': [{'name': 'alpha', 'decision': 'reject', 'reason': 'alpha reason'}]}
    return {'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'stop_reason': 'tool_use',
            'content': [{'type': 'tool_use', 'id': 'toolu_0', 'name': 'finish', 'input': finish}]}


class LargeLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        (self.repo / 'build/locks').mkdir(parents=True)
        (self.repo / 'build/recipes/alpha').mkdir(parents=True)
        recipe = b'#!/bin/sh\nexit 0\n'
        (self.repo / 'build/recipes/alpha/alpha.SlackBuild').write_bytes(recipe)
        self.inputs = [{'file': f'module-{n}.zip', 'url': f'https://example.org/module-{n}.zip',
                        'sha256': 'a' * 64} for n in range(2100)]
        self.entry = {'name': 'alpha', 'version': '1', 'recipe': 'build/recipes/alpha/alpha.SlackBuild',
                      'recipe_sha256': hashlib.sha256(recipe).hexdigest(),
                      'provenance': {'kind': 'source', 'url': 'https://example.org/recipe', 'revision': 'abc123'},
                      'inputs': self.inputs, 'dependencies': [], 'build_dependencies': [],
                      'smoke': [['/opt/nerdtools/bin/alpha', '--version']]}
        (self.repo / 'build/locks/slackware.json').write_text(json.dumps([self.entry]))
        for name in ('slackbuilds', 'toolchain'):
            (self.repo / f'build/locks/{name}.json').write_text('[]')
        self.session = agent.Session(self.repo, {'candidates': [{'name': 'alpha', 'ours': '1', 'upstream': '2',
                                      'source': 'Slackware-current', 'url': 'https://example.org/versions'}],
                                      'guide': '', 'feedback': ''})

    def lock(self):
        return json.loads((self.repo / 'build/locks/slackware.json').read_text())[0]

    @contextlib.contextmanager
    def network(self, *responses):
        """Scripted transport: connection n gets responses[n]; DNS is stubbed, so nothing leaves the test."""
        request = mock.Mock(side_effect=[(mock.Mock(), response) for response in responses])
        with mock.patch.object(agent, 'resolve_public', return_value=['93.184.216.34']), mock.patch.object(agent, '_request', request):
            yield request

    def test_existing_module_feed_is_paged_without_dropping_or_repeating_inputs(self):
        page = self.session.call('read_package', {'name': 'alpha'})
        self.assertLess(len(json.dumps(page)), 12000)
        self.assertNotIn('inputs', page['entry'])
        self.assertEqual(page['input_count'], 2100)
        found = page['inputs']
        while page['next_input_offset'] is not None:
            page = self.session.call('read_package', {'name': 'alpha', 'input_offset': page['next_input_offset']})
            found += page['inputs']
        self.assertEqual(found, self.inputs)

    def test_small_header_patch_preserves_all_existing_module_pins_and_smoke(self):
        result = self.session.call('patch_package', {'name': 'alpha', 'changes': {'version': '2'}})
        self.assertLess(len(json.dumps(result)), 2000)
        actual = self.lock()
        self.assertEqual(actual['version'], '2')
        self.assertEqual(actual['inputs'], self.inputs)
        self.assertEqual(actual['smoke'], [['/opt/nerdtools/bin/alpha', '--version']])

    def test_pin_upsert_and_removal_preserve_the_other_thousands_of_inputs(self):
        replacement = {'file': 'module-1.zip', 'url': 'https://example.org/new-module-1.zip', 'sha256': 'b' * 64}
        with mock.patch.object(agent, 'fetch', side_effect=streamed({replacement['url']: ('b' * 64, 6)})):
            self.session.call('patch_package', {'name': 'alpha', 'changes': {'version': '2'},
                              'set_inputs': [{key: value for key, value in replacement.items() if key != 'sha256'}],
                              'remove_inputs': ['module-0.zip']})
        expected = copy.deepcopy(self.inputs[1:])
        expected[0] = replacement
        self.assertEqual(self.lock()['inputs'], expected)
        matched = self.session.call('read_package', {'name': 'alpha', 'input_match': 'module-2099.zip'})
        self.assertEqual(matched['inputs'], [self.inputs[-1]])

    def test_partial_updates_cannot_weaken_acceptance_or_add_unpinned_inputs(self):
        for changes in ({'catalog': False}, {'smoke': []}, {'recipe': '../outside'}):
            with self.subTest(changes=changes), self.assertRaises(agent.ToolError):
                self.session.call('patch_package', {'name': 'alpha', 'changes': changes})
        with self.assertRaises(agent.ToolError):
            self.session.call('patch_package', {'name': 'alpha', 'changes': {'version': '2'},
                              'set_inputs': [{'file': 'bad.zip'}]})
        self.assertEqual(self.lock(), self.entry)

    def test_expired_session_cannot_apply_another_write(self):
        self.session.deadline = 0
        with self.assertRaises(agent.AgentError):
            self.session.call('write_recipe', {'path': 'build/recipes/alpha/new-file', 'content': 'bad', 'executable': False})
        self.assertFalse((self.repo / 'build/recipes/alpha/new-file').exists())


    def test_input_batch_uses_trusted_download_hashes_not_model_supplied_hashes(self):
        pins = [{'file': 'new-a.zip', 'url': 'https://example.org/new-a.zip'},
                {'file': 'new-b.zip', 'url': 'https://example.org/new-b.zip'}]
        downloads = {pins[0]['url']: ('b' * 64, 8), pins[1]['url']: ('c' * 64, 9)}
        with mock.patch.object(agent, 'fetch', side_effect=streamed(downloads)):
            self.session.call('patch_package', {'name': 'alpha', 'changes': {'version': '2'}, 'set_inputs': pins})
        self.assertEqual(self.lock()['inputs'][-2:], [
            pins[0] | {'sha256': 'b' * 64}, pins[1] | {'sha256': 'c' * 64}])
        self.assertEqual(self.session.downloaded, 17)

    def test_large_upstream_manifest_can_be_read_after_the_first_page(self):
        response = mock.Mock(status=200)
        response.getheader.return_value = None
        response.read.side_effect = io.BytesIO(b'x' * agent.FETCH_TEXT + b'tail').read
        with mock.patch.object(agent, 'resolve_public', return_value=['93.184.216.34']), mock.patch.object(
                agent, '_request', return_value=(mock.Mock(), response)):
            result = agent.fetch('https://example.org/go.sum', 'text', offset=agent.FETCH_TEXT)
        self.assertEqual(result['text'], 'tail')
        self.assertFalse(result['truncated'])

    def test_exhausted_download_budget_starts_no_transfer(self):
        self.session.downloaded = 2 * 2**30
        with self.network(scripted_response(b'x')) as request, self.assertRaises(agent.ToolError):
            self.session.call('fetch_url', {'url': 'https://example.org/new.tgz', 'mode': 'sha256'})
        request.assert_not_called()

    def test_cached_hash_still_answers_after_the_budget_is_spent(self):
        with self.network(scripted_response(b'cached')):
            first = self.session.call('fetch_url', {'url': 'https://example.org/a.tgz', 'mode': 'sha256'})
        self.session.downloaded = 2 * 2**30
        with self.network() as request:
            self.assertEqual(self.session.call('fetch_url', {'url': 'https://example.org/a.tgz', 'mode': 'sha256'}), first)
        request.assert_not_called()

    def test_unknown_length_stream_spends_the_remaining_budget_once_and_blocks_later_fetches(self):
        self.session.downloaded = 2 * 2**30 - 10
        stream = scripted_response(b'x' * 50, length=False)
        with self.network(stream), self.assertRaises(agent.ToolError):
            self.session.call('fetch_url', {'url': 'https://example.org/big.tgz', 'mode': 'sha256'})
        self.assertEqual(stream.read.call_args_list, [mock.call(10)])
        self.assertEqual(self.session.downloaded, 2 * 2**30)
        self.assertNotIn('https://example.org/big.tgz', self.session.hash_cache)
        with self.network(scripted_response(b'y')) as later, self.assertRaises(agent.ToolError):
            self.session.call('fetch_url', {'url': 'https://example.org/other.tgz', 'mode': 'sha256'})
        later.assert_not_called()

    def test_unknown_length_file_filling_the_budget_exactly_fails_closed(self):
        self.session.downloaded = 2 * 2**30 - 10
        stream = scripted_response(b'0123456789', length=False)
        with self.network(stream), self.assertRaises(agent.ToolError):
            self.session.call('fetch_url', {'url': 'https://example.org/exact.tgz', 'mode': 'sha256'})
        self.assertEqual(stream.read.call_args_list, [mock.call(10)])
        self.assertNotIn('https://example.org/exact.tgz', self.session.hash_cache)
        self.assertEqual(self.session.downloaded, 2 * 2**30)

    def test_known_size_file_that_exactly_fits_the_remaining_budget_succeeds(self):
        self.session.downloaded = 2 * 2**30 - 10
        with self.network(scripted_response(b'0123456789')):
            result = self.session.call('fetch_url', {'url': 'https://example.org/fits.tgz', 'mode': 'sha256'})
        self.assertEqual((result['sha256'], result['bytes']), (sha(b'0123456789'), 10))
        self.assertEqual(self.session.downloaded, 2 * 2**30)

    def test_declared_size_over_the_remaining_budget_is_refused_before_the_body(self):
        self.session.downloaded = 2 * 2**30 - 10
        stream = scripted_response(b'x' * 11)
        with self.network(stream), self.assertRaises(agent.ToolError):
            self.session.call('fetch_url', {'url': 'https://example.org/over.tgz', 'mode': 'sha256'})
        stream.read.assert_not_called()
        self.assertEqual(self.session.downloaded, 2 * 2**30 - 10)

    def test_truncated_stream_publishes_no_hash_and_keeps_its_bytes_counted(self):
        stream = scripted_response(b'partial', headers={'Content-Length': '100'})
        with self.network(stream), self.assertRaises(agent.ToolError):
            self.session.call('fetch_url', {'url': 'https://example.org/cut.tgz', 'mode': 'sha256'})
        self.assertNotIn('https://example.org/cut.tgz', self.session.hash_cache)
        self.assertEqual(self.session.downloaded, 7)

    def test_budget_is_enforced_on_the_final_hop_of_a_redirect(self):
        redirect = scripted_response(b'', status=302, headers={'Location': 'https://example.org/final.tgz'})
        final = scripted_response(b'x' * 20, length=False)
        seen = []
        with self.network(redirect, final), self.assertRaises(agent.ToolError):
            agent.fetch('https://example.org/start.tgz', 'sha256', max_bytes=5, on_bytes=seen.append)
        self.assertEqual(final.read.call_args_list, [mock.call(5)])
        self.assertEqual(sum(seen), 5)

    def test_bytes_read_before_a_transfer_error_are_still_accounted(self):
        stream = scripted_response(b'abc' * 10, length=False)
        stream.read.side_effect = [b'abc', OSError('connection reset')]
        seen = []
        with self.network(stream), self.assertRaises(OSError):
            agent.fetch('https://example.org/reset.tgz', 'sha256', on_bytes=seen.append)
        self.assertEqual(seen, [3])

    def test_provider_timeout_switches_to_anthropic_and_applies_its_finish(self):
        clock = FakeClock()
        openai = TrickleBody(clock, [b' '] * 1000, step=2)
        anthropic = TrickleBody(clock, [json.dumps(anthropic_finish()).encode()])
        opened = []

        def fake_open(request, timeout=None):
            opened.append(request.full_url)
            return openai if request.full_url == agent.OPENAI_URL else anthropic

        context = {'candidates': [{'name': 'alpha', 'ours': '1', 'upstream': '2', 'source': 'Slackware-current',
                                   'url': 'https://example.org/versions'}], 'guide': '', 'feedback': ''}
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'sk-openai-test', 'ANTHROPIC_API_KEY': 'sk-ant-test'}), \
                mock.patch.object(agent, 'time', clock), mock.patch.object(agent, 'PROVIDER_TIMEOUT', 5), \
                mock.patch.object(agent._OPENER, 'open', side_effect=fake_open), contextlib.redirect_stderr(io.StringIO()):
            result = agent.run_agent(self.repo, context)
        self.assertEqual(opened, [agent.OPENAI_URL, agent.ANTHROPIC_URL])
        self.assertEqual(result, {'summary': 'done', 'recipes': [{'name': 'alpha', 'decision': 'reject', 'reason': 'alpha reason'}]})



class ProviderSecretTests(unittest.TestCase):
    def test_provider_auth_error_never_logs_an_echoed_api_key(self):
        secret = 'sk-example-never-log-this-key'
        error = urllib.error.HTTPError('https://api.openai.com/v1/responses', 401, 'Unauthorized', {},
                                       io.BytesIO(json.dumps({'error': f'Incorrect API key: {secret}'}).encode()))
        with mock.patch.object(agent._OPENER, 'open', side_effect=error), self.assertRaises(agent.AgentError) as caught:
            agent.post('https://api.openai.com/v1/responses', {'Authorization': f'Bearer {secret}'}, {})
        self.assertNotIn(secret, str(caught.exception))
        self.assertIn('401', str(caught.exception))


class ProviderDeadlineTests(unittest.TestCase):
    URL = 'https://api.example.org/v1/messages'

    def setUp(self):
        self.clock = FakeClock()
        for patcher in (mock.patch.object(agent, 'time', self.clock), mock.patch.object(agent, 'PROVIDER_TIMEOUT', 5)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_provider_timeout_while_the_body_trickles_is_transient(self):
        body = TrickleBody(self.clock, [b' '] * 1000, step=2)
        with mock.patch.object(agent._OPENER, 'open', return_value=body), self.assertRaises(agent.Transient):
            agent.post(self.URL, {}, {}, deadline=self.clock.now + 1000)

    def test_session_deadline_while_the_body_trickles_is_agent_error(self):
        body = TrickleBody(self.clock, [b' '] * 1000, step=2)
        with mock.patch.object(agent._OPENER, 'open', return_value=body), self.assertRaises(agent.AgentError):
            agent.post(self.URL, {}, {}, deadline=self.clock.now + 3)

    def test_expired_session_deadline_sends_no_request(self):
        with mock.patch.object(agent._OPENER, 'open') as opener, self.assertRaises(agent.AgentError):
            agent.post(self.URL, {}, {}, deadline=self.clock.now - 1)
        opener.assert_not_called()


if __name__ == '__main__':
    unittest.main()
