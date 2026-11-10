import hashlib
import pathlib
import subprocess
import tarfile
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/build-plugin.sh'


class PluginPackagingTests(unittest.TestCase):
    def test_archive_and_manifest_match(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(['bash', str(SCRIPT), '2026.11.10', directory], check=True)
            output = pathlib.Path(directory)
            package = output / 'NerdTools-2026.11.10-x86_64-1.txz'
            manifest = (output / 'NerdTools.plg').read_text()
            digest = hashlib.sha256(package.read_bytes()).hexdigest()
            self.assertIn(digest, manifest)
            self.assertIn('2026.11.10', manifest)
            self.assertNotIn('@VERSION@', manifest)
            self.assertNotIn('@SHA256@', manifest)
            with tarfile.open(package, 'r:xz') as archive:
                paths = {item.name.removeprefix('./') for item in archive}
                self.assertIn('usr/local/emhttp/plugins/NerdTools/NerdTools.page', paths)
                self.assertNotIn('pkg_build.sh', paths)

    def test_rejects_invalid_version(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(['bash', str(SCRIPT), '../bad', directory],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(list(pathlib.Path(directory).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
