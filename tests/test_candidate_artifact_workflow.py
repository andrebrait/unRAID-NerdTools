"""Contract checks for trusted recipe candidate artifact retention."""
from pathlib import Path
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
        self.assertIn("jq -r '.build[]'", candidate)
        self.assertIn('cp package-artifacts/nerdtools-"$package"-*.txz candidate-packages/', candidate)
        self.assertNotIn("'.reuse'", candidate)
        self.assertIn('python3 build/native.py candidate-create', candidate)
        self.assertIn('--package-artifacts candidate-packages', candidate)
        self.assertIn('--plan "$plan"', candidate)
        self.assertIn('--output candidate-manifest.json', candidate)
        self.assertIn('--artifact-name "$ARTIFACT_NAME"', candidate)
        self.assertIn('name: candidate-packages-${{ github.run_id }}-${{ github.run_attempt }}', package_upload)
        self.assertIn('path: candidate-packages/', package_upload)
        self.assertNotIn('dist/', package_upload)
        self.assertIn('name: candidate-manifest-${{ github.run_id }}-${{ github.run_attempt }}', manifest_upload)
        self.assertIn('path: candidate-manifest.json', manifest_upload)
        for block in (package_upload, manifest_upload):
            self.assertIn('retention-days: 90', block)
            self.assertIn('if-no-files-found: error', block)


if __name__ == '__main__':
    unittest.main()
