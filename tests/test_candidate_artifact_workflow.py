"""Contract checks for trusted recipe candidate artifact retention."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/build-packages.yml'
TRUSTED_GUARD = (
    "github.event_name == 'workflow_dispatch' && "
    "github.ref == 'refs/heads/automation/upstream-recipes' && inputs.publish == false && "
    "needs.package-list.outputs.names != '[]'"
)


class CandidateArtifactWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text()
        cls.lines = cls.text.splitlines()

    def step(self, name):
        start = self.lines.index(f'      - name: {name}')
        end = next((index for index in range(start + 1, len(self.lines))
                    if self.lines[index].startswith('      - name: ')
                    or self.lines[index].startswith('  publish:')), len(self.lines))
        return '\n'.join(self.lines[start:end])

    def run_script(self, block):
        lines = block.splitlines()
        start = next(index for index, line in enumerate(lines) if line.strip() == 'run: |')
        return [line[10:] for line in lines[start + 1:]]

    def python_heredoc(self, block, command):
        lines = self.run_script(block)
        start = next(index for index, line in enumerate(lines) if line.strip() == command)
        end = next(index for index in range(start + 1, len(lines)) if lines[index].strip() == 'PY')
        return '\n'.join(lines[start + 1:end])

    def test_candidate_creation_and_uploads_follow_successful_read_only_target_gates(self):
        build = self.step('Build from locked inputs, audit target ABI, and boot the exact target kernel')
        candidate = self.step('Seal plan build packages for candidate promotion')
        package_upload = self.step('Upload changed-package candidate artifact')
        manifest_upload = self.step('Upload candidate manifest artifact')
        for block in (candidate, package_upload, manifest_upload):
            self.assertIn(f'if: ${{{{ {TRUSTED_GUARD} }}}}', block)
        self.assertLess(self.text.index(build), self.text.index(candidate))
        self.assertLess(self.text.index(candidate), self.text.index(package_upload))
        self.assertLess(self.text.index(package_upload), self.text.index(manifest_upload))
        self.assertIn("needs.packages.result == 'success'", self.text)
        self.assertIn('PUBLISH: ${{ inputs.publish }}', build)

    def test_candidate_bundle_is_exact_build_set_and_manifest_is_separate(self):
        candidate = self.step('Seal plan build packages for candidate promotion')
        package_upload = self.step('Upload changed-package candidate artifact')
        manifest_upload = self.step('Upload candidate manifest artifact')
        self.assertIn('PLAN: ${{ needs.package-list.outputs.plan }}', candidate)
        self.assertIn('native.artifact_name(package, plan["builds"][name])', candidate)
        self.assertIn('python3 build/native.py candidate-create', candidate)
        self.assertIn('--package-artifacts candidate-packages', candidate)
        self.assertIn('--plan "$plan"', candidate)
        self.assertIn('--output candidate-manifest.json', candidate)
        self.assertIn('--artifact-name "$ARTIFACT_NAME"', candidate)
        self.assertIn('name: candidate-packages-${{ github.run_id }}-${{ github.run_attempt }}', package_upload)
        self.assertIn('path: candidate-packages/', package_upload)
        self.assertNotIn('dist/', package_upload)
        self.assertIn('name: candidate-manifest-${{ github.run_id }}-${{ github.run_attempt }}', manifest_upload)
        manifest_paths = [line.strip() for line in manifest_upload.splitlines()
                          if line.strip().startswith('path:')]
        self.assertEqual(manifest_paths, ['path: candidate-manifest.json'])
        for block in (package_upload, manifest_upload):
            self.assertIn('retention-days: 90', block)
            self.assertIn('if-no-files-found: error', block)

        run_lines = self.run_script(candidate)
        create_start = next(index for index, line in enumerate(run_lines)
                            if line.strip() == 'python3 build/native.py candidate-create \\')
        create_end = next(index for index in range(create_start, len(run_lines))
                          if not run_lines[index].rstrip().endswith('\\'))
        self.assertIn('--package-artifacts candidate-packages',
                      '\n'.join(line.strip() for line in run_lines[create_start:create_end + 1]))
        self.assertFalse(any('candidate-packages' in line for line in run_lines[create_end + 1:]))

    def test_prefix_package_does_not_stage_longer_package_archive(self):
        candidate = self.step('Seal plan build packages for candidate promotion')
        script = self.python_heredoc(candidate, "python3 - \"$plan\" <<'PY'")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'build').mkdir()
            (root / 'package-artifacts').mkdir()
            (root / 'candidate-packages').mkdir()
            (root / 'build/native.py').write_text(
                """def load_packages():
    return [{"name": "foo", "version": "1.0"}, {"name": "foo-bar", "version": "2.0"}]
def artifact_name(package, build):
    return "nerdtools-{}-{}-x86_64-{}_NT73.txz".format(
        package["name"], package["version"], build)
""")
            expected = 'nerdtools-foo-1.0-x86_64-1_NT73.txz'
            longer = 'nerdtools-foo-bar-2.0-x86_64-1_NT73.txz'
            (root / 'package-artifacts' / expected).write_bytes(b'foo archive')
            (root / 'package-artifacts' / longer).write_bytes(b'foo-bar archive')
            (root / 'package-artifacts' / 'candidate-manifest.json').write_text('{}')
            plan = root / 'plan.json'
            plan.write_text(json.dumps({'build': ['foo'], 'builds': {'foo': 1}}))

            subprocess.run([sys.executable, '-c', script, str(plan)], cwd=root, check=True)

            staged = sorted(path.name for path in (root / 'candidate-packages').iterdir())
            self.assertEqual(staged, [expected])
            self.assertTrue(all(name.endswith('.txz') for name in staged))


if __name__ == '__main__':
    unittest.main()
