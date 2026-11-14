"""Execute the real GitHub-script step with recorded API responses, without dispatching builds."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/unraid-releases.yml'


class ReleaseDispatchTests(unittest.TestCase):
    def execute(self, sha, publish):
        lines = WORKFLOW.read_text().splitlines()
        start = next(index for index, line in enumerate(lines) if line.strip() == 'script: |')
        indent = len(lines[start]) - len(lines[start].lstrip()) + 2
        body = []
        for line in lines[start + 1:]:
            if line.strip() and len(line) - len(line.lstrip()) < indent:
                break
            body.append(line[indent:])
        script = '\n'.join(body)
        target = {'version': '7.3.4', 'sha256': 'abc', 'url': 'https://releases.unraid.net/dl/stable/image.zip', 'channel': 'stable'}
        runs = [{'display_title': f'Unraid compatibility 7.3.4 abc publish={publish}', 'head_sha': sha,
                 'conclusion': 'success', 'status': 'completed', 'html_url': 'https://example.invalid/build/1'}]
        setup = """
const dispatched = [];
const listReleases = () => {};
const listWorkflowRuns = () => {};
const github = {
  rest: {repos: {get: async () => ({data:{default_branch:'main'}}), listReleases},
         actions: {listWorkflowRuns, createWorkflowDispatch: async (request) => dispatched.push(request)}},
  paginate: async (method) => method === listReleases ? [] : RUNS,
};
const context = {repo:{owner:'owner', repo:'tools'}, sha:'current'};
const summary = new Proxy({}, {get: (_, key) => key === 'write' ? async () => {} : () => summary});
const core = {setOutput:()=>{}, notice:()=>{}, warning:()=>{}, summary};
""".replace('RUNS', json.dumps(runs))
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'unraid-releases.json').write_text(json.dumps({
                'blocked': [], 'matrix': {'include': [target]}, 'checksum_sources': ['official']}))
            result = subprocess.check_output(['node', '-e', setup + '\n(async()=>{\n' + script +
                '\nconsole.log(JSON.stringify(dispatched));\n})().catch(e=>{console.error(e);process.exitCode=1});'],
                cwd=folder, text=True)
            return json.loads(result)

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


if __name__ == '__main__':
    unittest.main()
