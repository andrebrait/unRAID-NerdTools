"""Large pinned module feeds must not require a whole-feed model round trip."""
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.error

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/upstream-agent.py'
SPEC = importlib.util.spec_from_file_location('large_lock_agent', SCRIPT)
agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent)


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
        with mock.patch.object(agent, 'fetch', return_value={'sha256': 'b' * 64, 'bytes': 6}):
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
        with mock.patch.object(agent, 'fetch', side_effect=[
                {'url': pins[0]['url'], 'sha256': 'b' * 64, 'bytes': 8},
                {'url': pins[1]['url'], 'sha256': 'c' * 64, 'bytes': 9}]):
            self.session.call('patch_package', {'name': 'alpha', 'changes': {'version': '2'}, 'set_inputs': pins})
        self.assertEqual(self.lock()['inputs'][-2:], [
            pins[0] | {'sha256': 'b' * 64}, pins[1] | {'sha256': 'c' * 64}])

    def test_large_upstream_manifest_can_be_read_after_the_first_page(self):
        response = mock.Mock(status=200)
        response.getheader.return_value = None
        response.read.side_effect = io.BytesIO(b'x' * agent.FETCH_TEXT + b'tail').read
        with mock.patch.object(agent, 'resolve_public', return_value=['93.184.216.34']), mock.patch.object(
                agent, '_request', return_value=(mock.Mock(), response)):
            result = agent.fetch('https://example.org/go.sum', 'text', offset=agent.FETCH_TEXT)
        self.assertEqual(result['text'], 'tail')
        self.assertFalse(result['truncated'])


class ProviderSecretTests(unittest.TestCase):
    def test_provider_auth_error_never_logs_an_echoed_api_key(self):
        secret = 'sk-example-never-log-this-key'
        error = urllib.error.HTTPError('https://api.openai.com/v1/responses', 401, 'Unauthorized', {},
                                       io.BytesIO(json.dumps({'error': f'Incorrect API key: {secret}'}).encode()))
        with mock.patch.object(agent._OPENER, 'open', side_effect=error), self.assertRaises(agent.AgentError) as caught:
            agent.post('https://api.openai.com/v1/responses', {'Authorization': f'Bearer {secret}'}, {})
        self.assertNotIn(secret, str(caught.exception))
        self.assertIn('401', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
