"""Exercise candidate-aware release propagation and target-run deduplication offline."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/upstream-updates.py'
WORKFLOW = ROOT / '.github/workflows/unraid-releases.yml'
BUILD_WORKFLOW = ROOT / '.github/workflows/build-packages.yml'
CANDIDATE_KEYS = (
    'candidate_pr', 'candidate_head_sha', 'candidate_run_id', 'candidate_run_attempt',
    'candidate_package_artifact_id', 'candidate_package_artifact_name',
    'candidate_manifest_artifact_id', 'candidate_manifest_artifact_name',
    'candidate_manifest_sha256', 'candidate_merge_sha',
)


def module():
    spec = importlib.util.spec_from_file_location('upstream_release_updates', SCRIPT)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def candidate_inputs():
    return {
        'candidate_pr': '12',
        'candidate_head_sha': 'a' * 40,
        'candidate_run_id': '44',
        'candidate_run_attempt': '2',
        'candidate_package_artifact_id': '101',
        'candidate_package_artifact_name': 'candidate-packages-44-2',
        'candidate_manifest_artifact_id': '102',
        'candidate_manifest_artifact_name': 'candidate-manifest-44-2',
        'candidate_manifest_sha256': 'c' * 64,
        'candidate_merge_sha': 'd' * 40,
    }


class ReleasePropagationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.updates = module()

    def candidate_state(self):
        return {
            'head_sha': 'a' * 40, 'run_id': 44, 'run_attempt': 2,
            'package_artifact_id': 101, 'package_artifact_name': 'candidate-packages-44-2',
            'manifest_artifact_id': 102, 'manifest_artifact_name': 'candidate-manifest-44-2',
            'manifest_sha256': 'c' * 64,
            'build_url': 'https://github.com/owner/tools/actions/runs/44',
        }

    def service(self, state):
        head = 'a' * 40
        url = 'https://github.com/owner/tools/pull/12'
        pr = {
            'number': 12, 'merged': True, 'merged_at': '2026-11-16T00:00:00Z',
            'merge_commit_sha': 'd' * 40, 'html_url': url,
            'head': {'ref': self.updates.BRANCH, 'sha': head,
                     'repo': {'full_name': 'owner/tools'}},
            'base': {'ref': 'main', 'repo': {'full_name': 'owner/tools'}},
            'user': {'login': self.updates.BOT, 'type': 'Bot'},
            'body': self.updates.STATE + json.dumps(state) + '\n-->',
        }
        attestation = {
            'context': 'nerdtools/human-approval', 'state': 'success',
            'creator': {'login': self.updates.BOT},
            'description': f'Approved head {head}', 'target_url': url,
        }

        class Service:
            repository = 'owner/tools'

            def __init__(self):
                self.dispatched = []

            def api(self, path, data=None, method=None):
                if path == 'pulls/12':
                    return pr
                raise AssertionError(f'Unexpected API request: {path}')

            def pages(self, path, field=None):
                if path == f"commits/{pr['merge_commit_sha']}/statuses?per_page=100":
                    return [attestation]
                raise AssertionError(f'Unexpected pagination: {path}')

            def dispatch(self, workflow, ref='main', inputs=None):
                self.dispatched.append((workflow, ref, inputs))

        return Service()

    def test_release_dispatches_the_complete_candidate_tuple(self):
        gh = self.service(self.candidate_state())

        self.updates.release(gh, {'pull_request': {'number': 12}})

        self.assertEqual(gh.dispatched, [('unraid-releases.yml', 'main', candidate_inputs())])

    def test_release_rejects_absent_or_merged_head_mismatched_candidate_state(self):
        states = {'absent candidate': {}, 'merged-head mismatch': self.candidate_state() | {
            'head_sha': 'b' * 40,
        }}
        for label, state in states.items():
            with self.subTest(label=label):
                gh = self.service(state)
                with self.assertRaises(ValueError):
                    self.updates.release(gh, {'pull_request': {'number': 12}})
                self.assertEqual(gh.dispatched, [])


class ReleaseDispatchTests(unittest.TestCase):
    def execute(self, sha, publish, candidate=None, runs=None):
        lines = WORKFLOW.read_text().splitlines()
        start = next(index for index, line in enumerate(lines) if line.strip() == 'script: |')
        indent = len(lines[start]) - len(lines[start].lstrip()) + 2
        body = []
        for line in lines[start + 1:]:
            if line.strip() and len(line) - len(line.lstrip()) < indent:
                break
            body.append(line[indent:])
        script = '\n'.join(body)
        target = {'version': '7.3.4', 'sha256': 'abc',
                  'url': 'https://releases.unraid.net/dl/stable/image.zip', 'channel': 'stable'}
        if runs is None:
            runs = [{
                'display_title': f'Unraid compatibility 7.3.4 abc publish={publish}',
                'head_sha': sha, 'conclusion': 'success', 'status': 'completed',
                'html_url': 'https://example.invalid/build/1',
            }]
        setup = """
const dispatched = [];
const listReleases = () => {};
const listWorkflowRuns = () => {};
const github = {
  rest: {repos: {get: async () => ({data:{default_branch:'main'}}), listReleases},
         actions: {listWorkflowRuns, createWorkflowDispatch: async (request) => dispatched.push(request)}},
  paginate: async (method) => method === listReleases ? [] : RUNS,
};
const context = {repo:{owner:'owner', repo:'tools'}, sha:'current',
                 eventName: EVENT, payload:{inputs:INPUTS}};
const summary = new Proxy({}, {get: (_, key) => key === 'write' ? async () => {} : () => summary});
const core = {setOutput:()=>{}, notice:()=>{}, warning:()=>{}, summary};
""".replace('RUNS', json.dumps(runs)).replace(
            'INPUTS', json.dumps(candidate or {})).replace(
            'EVENT', json.dumps('workflow_dispatch' if candidate is not None else 'schedule'))
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'unraid-releases.json').write_text(json.dumps({
                'blocked': [], 'matrix': {'include': [target]},
                'checksum_sources': ['official'],
            }))
            result = subprocess.run(
                ['node', '-e', setup + '\n(async()=>{\n' + script +
                 '\nconsole.log(JSON.stringify(dispatched));\n})().catch(e=>{console.error(e);process.exitCode=1});'],
                cwd=folder, text=True, capture_output=True, check=False)
            if result.returncode:
                raise RuntimeError(result.stderr.strip())
            return json.loads(result.stdout)

    def test_old_source_success_does_not_suppress_updated_recipe_publication(self):
        result = self.execute('older-source', 'true')
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['inputs']['publish'], 'true')
        self.assertEqual(result[0]['ref'], 'main')

    def test_read_only_success_does_not_count_as_a_published_catalog(self):
        result = self.execute('current', 'false')
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['inputs']['unraid_sha256'], 'abc')

    def test_current_source_publishing_success_is_not_dispatched_twice(self):
        self.assertEqual(self.execute('current', 'true'), [])

    def test_candidate_tuple_is_forwarded_to_every_target_dispatch(self):
        candidate = candidate_inputs()
        result = self.execute('current', 'true', candidate, runs=[])
        self.assertEqual({key: result[0]['inputs'][key] for key in CANDIDATE_KEYS}, candidate)

    def test_partial_candidate_tuples_fail_closed(self):
        complete = candidate_inputs()
        for missing in CANDIDATE_KEYS:
            with self.subTest(missing=missing), self.assertRaisesRegex(
                    RuntimeError, 'complete candidate tuple'):
                self.execute('current', 'true', complete | {missing: ''}, runs=[])

    def test_partial_candidate_guard_precedes_checkout(self):
        lines = WORKFLOW.read_text().splitlines()
        guard = next(index for index, line in enumerate(lines)
                     if line.strip() == '- name: Reject partial candidate identity before discovery')
        checkout = next(index for index, line in enumerate(lines)
                        if line.strip().startswith('- uses: actions/checkout@'))
        self.assertLess(guard, checkout)
        block = '\n'.join(lines[guard:checkout])
        for key in CANDIDATE_KEYS:
            self.assertIn(f'inputs.{key}', block)
        self.assertIn('present != 0 && present != 10', block)
        self.assertIn('exit 1', block)

    def test_candidate_run_and_attempt_are_part_of_title_and_deduplication(self):
        candidate = candidate_inputs()
        old_attempt = [{
            'display_title': 'Unraid compatibility 7.3.4 abc publish=true candidate=44/1',
            'head_sha': 'current', 'conclusion': 'success', 'status': 'completed',
            'html_url': 'https://example.invalid/build/1',
        }]
        exact_attempt = [old_attempt[0] | {
            'display_title': 'Unraid compatibility 7.3.4 abc publish=true candidate=44/2',
        }]
        self.assertEqual(len(self.execute('current', 'true', candidate, old_attempt)), 1)
        self.assertEqual(self.execute('current', 'true', candidate, exact_attempt), [])
        self.assertIn(
            "format(' candidate={0}/{1}', inputs.candidate_run_id, inputs.candidate_run_attempt)",
            BUILD_WORKFLOW.read_text())

    def test_candidate_concurrency_identity_includes_run_and_attempt(self):
        group = next(line.strip() for line in BUILD_WORKFLOW.read_text().splitlines()
                     if line.strip().startswith('group: native-'))
        self.assertIn('inputs.candidate_run_id', group)
        self.assertIn('inputs.candidate_run_attempt', group)


if __name__ == '__main__':
    unittest.main()
