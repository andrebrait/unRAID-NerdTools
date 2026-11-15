#!/usr/bin/env python3
"""Offline regression tests for deterministic Go module lock regeneration."""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "build/go-module-lock.py"
spec = importlib.util.spec_from_file_location("go_module_lock", SCRIPT)
locker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(locker)


class GoModuleLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.recipe = self.root / "build/recipes/example"
        self.work = self.root / "work"
        self.lock = self.root / "build/locks/packages.json"
        self.bin = self.root / "bin"
        for path in (self.source, self.recipe, self.work, self.lock.parent, self.bin):
            path.mkdir(parents=True, exist_ok=True)
        (self.source / "go.mod").write_text("module example.test/app\n\ngo 1.26\n")
        (self.recipe / "go-modules.tsv").write_text(
            "old.info\texample.com/old/@v/v0.1.0.info\n"
            "old.mod\texample.com/old/@v/v0.1.0.mod\n"
            "old.zip\texample.com/old/@v/v0.1.0.zip\n")
        self.source_input = {
            "url": "https://example.test/source.tar.gz",
            "file": "source.tar.gz",
            "sha256": "a" * 64,
        }
        self.lock.write_text(json.dumps([{
            "name": "example",
            "version": "1.0",
            "inputs": [
                {"url": "https://proxy.golang.org/example.com/old/@v/v0.1.0.info",
                 "file": "old.info", "sha256": hashlib.sha256(b"old").hexdigest()},
                self.source_input,
            ],
        }], indent=2) + "\n")
        go = self.bin / "go"
        go.write_text("""#!/usr/bin/env python3
import json, os
from pathlib import Path
assert os.environ['GOFLAGS'] == ''
assert os.environ['GOPROXY'] == 'https://proxy.golang.org'
assert os.environ['GOSUMDB'] == 'sum.golang.org'
assert os.environ['GOTOOLCHAIN'] == 'auto'
assert os.environ['GOPRIVATE'] == os.environ['GONOPROXY'] == os.environ['GONOSUMDB'] == ''
assert os.environ['GOWORK'] == os.environ['GOENV'] == 'off'
cache = Path(os.environ['GOMODCACHE']) / 'cache/download'
records = []
for module, version, contents in [
        ('example.com/mod', 'v1.2.3', [('info', b'info'), ('mod', b'module example.com/mod\\n'), ('zip', b'zip')]),
        ('example.com/other', 'v2.0.0', [('mod', b'module example.com/other\\n')])]:
    folder = cache / module / '@v'
    folder.mkdir(parents=True)
    record = {'Path': module, 'Version': version}
    for suffix, data in contents:
        path = folder / (version + '.' + suffix)
        path.write_bytes(data)
        record[{'info':'Info', 'mod':'GoMod', 'zip':'Zip'}[suffix]] = str(path)
    records.append(record)
extra = cache / 'example.com/transitive/@v/v1.0.0.mod'
extra.parent.mkdir(parents=True)
extra.write_bytes(b'module example.com/transitive\\n')
toolchain = cache / 'golang.org/toolchain/@v/v0.0.1-go1.26.11.linux-amd64.mod'
toolchain.parent.mkdir(parents=True)
toolchain.write_bytes(b'module golang.org/toolchain\\n')
print(''.join(json.dumps(record) for record in records))
""")
        go.chmod(0o755)

    def tearDown(self):
        self.temp.cleanup()

    def run_helper(self, work=None, lock=None):
        env = {**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"]}
        return subprocess.run([
            sys.executable, str(SCRIPT), "--source", str(self.source),
            "--recipe", str(self.recipe), "--lock", str(lock or self.lock),
            "--package", "example", "--work", str(work or self.work),
        ], text=True, capture_output=True, env=env, cwd=self.root)

    def test_replaces_proxy_inputs_and_manifest_deterministically(self):
        first = self.run_helper()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        expected = []
        for suffix, data in (("info", b"info"), ("mod", b"module example.com/mod\n"), ("zip", b"zip")):
            url = f"https://proxy.golang.org/example.com/mod/@v/v1.2.3.{suffix}"
            name = hashlib.sha256(url.encode()).hexdigest()[:24] + "." + suffix
            expected.append((name, f"example.com/mod/@v/v1.2.3.{suffix}", url,
                             hashlib.sha256(data).hexdigest()))
            self.assertFalse((self.recipe / name).exists())
        other_url = "https://proxy.golang.org/example.com/other/@v/v2.0.0.mod"
        other_name = hashlib.sha256(other_url.encode()).hexdigest()[:24] + ".mod"
        expected.append((other_name, "example.com/other/@v/v2.0.0.mod", other_url,
                         hashlib.sha256(b"module example.com/other\n").hexdigest()))
        self.assertFalse((self.recipe / other_name).exists())
        transitive_url = "https://proxy.golang.org/example.com/transitive/@v/v1.0.0.mod"
        transitive_name = hashlib.sha256(transitive_url.encode()).hexdigest()[:24] + ".mod"
        expected.append((
            transitive_name, "example.com/transitive/@v/v1.0.0.mod", transitive_url,
            hashlib.sha256(b"module example.com/transitive\n").hexdigest()))
        self.assertFalse((self.recipe / transitive_name).exists())
        self.assertEqual((self.recipe / "go-modules.tsv").read_text(),
                         "".join(f"{name}\t{destination}\n" for name, destination, _, _ in expected))
        self.assertNotIn("golang.org/toolchain/@v/", (self.recipe / "go-modules.tsv").read_text())
        entry = json.loads(self.lock.read_text())[0]
        self.assertEqual(entry["inputs"], [
            *[{"url": url, "file": name, "sha256": digest}
              for name, _, url, digest in expected],
            self.source_input,
        ])
        snapshot = {path.relative_to(self.root): path.read_bytes()
                    for path in self.root.rglob("*") if path.is_file() and self.work not in path.parents}
        second = self.run_helper()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(snapshot, {path.relative_to(self.root): path.read_bytes()
                                    for path in self.root.rglob("*")
                                    if path.is_file() and self.work not in path.parents})

    def test_relative_work_directory_is_resolved_for_go(self):
        result = self.run_helper(Path("relative-work"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_inherited_goflags_cannot_select_an_alternate_module_file(self):
        with mock.patch.dict(os.environ, {"GOFLAGS": "-modfile=/tmp/alternate.mod"}):
            result = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


    def test_work_directory_preserves_unrelated_contents(self):
        marker = self.work / "keep"
        marker.write_text("unrelated")
        result = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(marker.exists(), "helper deleted unrelated work content")
        self.assertEqual(marker.read_text(), "unrelated")


    def test_invalid_retained_input_does_not_mutate_outputs(self):
        entries = json.loads(self.lock.read_text())
        entries[0]["inputs"].append("malformed")
        self.lock.write_text(json.dumps(entries, indent=2) + "\n")
        manifest, lock = (self.recipe / "go-modules.tsv").read_bytes(), self.lock.read_bytes()
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.recipe / "go-modules.tsv").read_bytes(), manifest)
        self.assertEqual(self.lock.read_bytes(), lock)

    def test_generated_and_retained_filenames_cannot_collide(self):
        url = "https://proxy.golang.org/example.com/mod/@v/v1.2.3.info"
        collision = hashlib.sha256(url.encode()).hexdigest()[:24] + ".info"
        entries = json.loads(self.lock.read_text())
        entries[0]["inputs"][-1]["file"] = collision
        self.lock.write_text(json.dumps(entries, indent=2) + "\n")
        manifest, lock = (self.recipe / "go-modules.tsv").read_bytes(), self.lock.read_bytes()
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.recipe / "go-modules.tsv").read_bytes(), manifest)
        self.assertEqual(self.lock.read_bytes(), lock)

    def test_manifest_symlink_is_rejected_without_touching_target(self):
        manifest = self.recipe / "go-modules.tsv"
        target = self.root / "outside"
        target.write_bytes(manifest.read_bytes())
        manifest.unlink()
        manifest.symlink_to(target)
        lock = self.lock.read_bytes()
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_bytes(), (
            b"old.info\texample.com/old/@v/v0.1.0.info\n"
            b"old.mod\texample.com/old/@v/v0.1.0.mod\n"
            b"old.zip\texample.com/old/@v/v0.1.0.zip\n"))
        self.assertEqual(self.lock.read_bytes(), lock)


    def test_lock_and_work_symlinks_are_rejected_before_resolution(self):
        lock_link = self.root / "lock-link.json"
        lock_link.symlink_to(self.lock)
        manifest, lock = (self.recipe / "go-modules.tsv").read_bytes(), self.lock.read_bytes()
        result = self.run_helper(lock=lock_link)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.recipe / "go-modules.tsv").read_bytes(), manifest)
        self.assertEqual(self.lock.read_bytes(), lock)

        work_link = self.root / "work-link"
        work_link.symlink_to(self.work, target_is_directory=True)
        result = self.run_helper(work=work_link)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(self.work.iterdir()))


    def test_second_stage_failure_removes_first_staged_file(self):
        first, second = self.root / "first", self.root / "second"
        first.write_bytes(b"old-first")
        second.write_bytes(b"old-second")
        real_stage = locker.stage_file
        calls = 0

        def stage(path, data):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("second stage failed")
            return real_stage(path, data)

        with mock.patch.object(locker, "stage_file", side_effect=stage), self.assertRaises(OSError):
            locker.replace_pair(first, b"new-first", second, b"new-second")
        self.assertEqual(first.read_bytes(), b"old-first")
        self.assertEqual(second.read_bytes(), b"old-second")
        self.assertEqual(list(self.root.glob(".first.*")), [])


    def test_second_atomic_replace_failure_restores_first_output(self):
        if not hasattr(locker, "replace_pair"):
            self.fail("replace_pair is missing")
        first, second = self.root / "first", self.root / "second"
        first.write_bytes(b"old-first")
        second.write_bytes(b"old-second")
        real_replace = os.replace
        calls = 0

        def replace(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("second replace failed")
            return real_replace(source, destination)

        with mock.patch.object(locker.os, "replace", side_effect=replace), self.assertRaises(OSError):
            locker.replace_pair(first, b"new-first", second, b"new-second")
        self.assertEqual(first.read_bytes(), b"old-first")
        self.assertEqual(second.read_bytes(), b"old-second")


    def test_rejects_downloads_outside_the_isolated_module_cache(self):
        go = self.bin / "go"
        go.write_text("""#!/usr/bin/env python3
import json
print(json.dumps({'Path':'example.com/mod','Version':'v1.2.3','Zip':'/etc/passwd'}))
""")
        go.chmod(0o755)
        manifest = (self.recipe / "go-modules.tsv").read_text()
        lock = self.lock.read_text()
        result = self.run_helper()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside module cache", result.stderr)
        self.assertEqual((self.recipe / "go-modules.tsv").read_text(), manifest)
        self.assertEqual(self.lock.read_text(), lock)




if __name__ == "__main__":
    unittest.main()
