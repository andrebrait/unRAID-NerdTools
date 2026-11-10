"""Offline package-impact and workflow-generation tests; no native builds."""

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


changes = module("package_change", "package-change.py")
workflows = module("package_workflows", "package-change-workflows.py")


def package(name, dependencies=(), build_dependencies=()):
    return {"name": name, "version": "1", "recipe": f"build/recipes/{name}/{name}.SlackBuild",
            "dependencies": list(dependencies), "build_dependencies": list(build_dependencies)}


class ImpactTests(unittest.TestCase):
    def setUp(self):
        self.packages = {item["name"]: item for item in (
            package("library"), package("compiler", build_dependencies=["library"]),
            package("app", dependencies=["compiler"]), package("unrelated"))}
        self.inventory = {"library/patch.diff": {"sha256": "old", "type": "file"}}

    def select(self, paths, before=None, after=None, old_inventory=None, inventory=None):
        return changes.select_packages(
            self.packages if before is None else before,
            self.packages if after is None else after,
            self.inventory if old_inventory is None else old_inventory,
            self.inventory if inventory is None else inventory, paths)

    def test_recipe_sidecar_change_rebuilds_transitive_runtime_and_build_dependents(self):
        self.assertEqual(self.select(["build/recipes/library/patch.diff"]),
                         ["app", "compiler", "library"])

    def test_shared_lock_compares_entries_not_json_text_or_list_order(self):
        after = copy.deepcopy(self.packages)
        after["library"]["version"] = "2"
        self.assertEqual(self.select(["build/locks/slackbuilds.json"], after=after),
                         ["app", "compiler", "library"])
        self.assertEqual(self.select(["build/locks/slackbuilds.json"]), [])

    def test_inventory_change_selects_only_owning_recipe_and_dependents(self):
        self.assertEqual(self.select(["build/recipe-files.json"], inventory={}),
                         ["app", "compiler", "library"])

    def test_removed_dependency_uses_old_graph_but_never_builds_removed_package(self):
        after = copy.deepcopy(self.packages)
        del after["library"]
        after["compiler"]["build_dependencies"] = []
        self.assertEqual(self.select(["build/locks/slackbuilds.json"], after=after),
                         ["app", "compiler"])

    def test_dependency_cycle_terminates(self):
        after = copy.deepcopy(self.packages)
        after["library"]["dependencies"] = ["app"]
        self.assertEqual(self.select(["build/recipes/library/file"], after=after),
                         ["app", "compiler", "library"])

    def test_shared_infrastructure_changes_rebuild_every_package(self):
        for path in ("build/baseline.json", "build/locks/toolchain.json", "build/native.py",
                     "build/unraid-build-root/bootstrap.py", ".github/workflows/package-build.yml",
                     "scripts/package-change.py", "scripts/package-change-workflows.py"):
            with self.subTest(path=path):
                self.assertEqual(self.select([path]), ["app", "compiler", "library", "unrelated"])

    def test_unrelated_files_do_not_build_packages(self):
        self.assertEqual(self.select(["README.md", "plugin/NerdTools.plg"]), [])

    def test_changed_wrapper_selects_its_package(self):
        self.assertEqual(self.select([".github/workflows/package-unrelated.yml"]), ["unrelated"])


class GitImpactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git("init", "--quiet")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        (self.repo / "build/locks").mkdir(parents=True)
        self.write("build/locks/slackbuilds.json", [package("app", ["library"]), package("library")])
        self.write("build/locks/slackware.json", [])
        self.write("build/recipe-files.json", {})
        self.base = self.commit()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], text=True).strip()

    def write(self, path, value):
        (self.repo / path).write_text(json.dumps(value))

    def commit(self):
        self.git("add", ".")
        self.git("commit", "--quiet", "--allow-empty", "-m", "fixture")
        return self.git("rev-parse", "HEAD")

    def test_json_reformatting_and_entry_reordering_select_nothing(self):
        target = self.repo / "build/locks/slackbuilds.json"
        target.write_text(json.dumps([package("library"), package("app", ["library"])], indent=4))
        self.assertEqual(changes.select_git(self.repo, self.base, self.commit(), "push"), [])

    def test_missing_and_zero_bases_fail_closed(self):
        for base in ("0" * 40, "a" * 40, "invalid"):
            with self.subTest(base=base):
                self.assertEqual(changes.select_git(self.repo, base, self.base, "push"), ["app", "library"])

    def test_force_push_fails_closed_but_pr_compares_base_and_head(self):
        self.write("README.md", "base-side change")
        divergent = self.commit()
        self.git("checkout", "--quiet", "--detach", self.base)
        self.write("README.md", "head-side change")
        head = self.commit()
        self.assertEqual(changes.select_git(self.repo, divergent, head, "push"), ["app", "library"])
        self.assertEqual(changes.select_git(self.repo, divergent, head, "pull_request"), [])

    def test_unreadable_old_metadata_fails_closed(self):
        self.git("rm", "build/recipe-files.json")
        missing = self.commit()
        self.write("build/recipe-files.json", {})
        self.assertEqual(changes.select_git(self.repo, missing, self.commit(), "push"), ["app", "library"])

    def test_pr_compares_target_base_not_merge_base(self):
        self.write("build/locks/slackbuilds.json", [package("app", ["library"]), {**package("library"), "version": "2"}])
        target_base = self.commit()
        self.git("checkout", "--quiet", "--detach", self.base)
        self.write("README.md", "head-side change")
        head = self.commit()
        self.assertEqual(changes.select_git(self.repo, target_base, head, "pull_request"), ["app", "library"])

    def test_pr_uses_merge_result_for_new_base_dependents(self):
        self.write("build/locks/slackbuilds.json",
                   [package("app", ["library"]), package("library"), package("baseapp", ["library"])])
        target = self.commit()
        self.git("checkout", "--quiet", "--detach", self.base)
        (self.repo / "build/recipes/library").mkdir(parents=True)
        self.write("build/recipes/library/patch.diff", "changed")
        head = self.commit()
        self.git("merge", "--quiet", "--no-edit", target)
        merge = self.git("rev-parse", "HEAD")
        event = {"pull_request": {"base": {"sha": target}, "head": {"sha": head}}}
        self.assertTrue(changes.package_selected(self.repo, "baseapp", "pull_request", event, merge))

    def test_complete_diff_is_not_limited_to_githubs_first_300_files(self):
        (self.repo / "build/recipes/library").mkdir(parents=True)
        self.write("build/recipes/library/patch.diff", "changed")
        for number in range(350):
            self.write(f"aaa-unrelated-{number:03d}.txt", "unrelated")
        self.assertEqual(changes.select_git(self.repo, self.base, self.commit(), "push"), ["app", "library"])


    def test_manual_dispatch_selects_requested_package_only(self):
        self.assertTrue(changes.package_selected(self.repo, "app", "workflow_dispatch", {}))
        with self.assertRaisesRegex(ValueError, "Unknown package"):
            changes.package_selected(self.repo, "not-a-package", "workflow_dispatch", {})


class WorkflowGenerationTests(unittest.TestCase):
    def test_generation_is_deterministic_checks_drift_and_removes_obsolete_wrappers(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "build/locks").mkdir(parents=True)
            lock = repo / "build/locks/slackbuilds.json"
            lock.write_text(json.dumps([package("zeta"), package("alpha")]))
            (repo / "build/locks/slackware.json").write_text("[]")
            self.assertFalse(workflows.generate(repo, check=True))
            self.assertTrue(workflows.generate(repo))
            folder = repo / ".github/workflows"
            self.assertEqual(sorted(path.name for path in folder.iterdir()),
                             ["package-alpha.yml", "package-zeta.yml"])
            first = {path.name: path.read_bytes() for path in folder.iterdir()}
            lock.write_text(json.dumps([package("alpha"), package("zeta")], indent=2))
            self.assertTrue(workflows.generate(repo, check=True))
            self.assertTrue(workflows.generate(repo))
            self.assertEqual({path.name: path.read_bytes() for path in folder.iterdir()}, first)
            (folder / "package-alpha.yml").write_text("edited wrapper")
            self.assertFalse(workflows.generate(repo, check=True))
            workflows.generate(repo)
            lock.write_text(json.dumps([package("alpha")]))
            (folder / "package-build.yml").write_text("reusable job")
            workflows.generate(repo)
            self.assertEqual(sorted(path.name for path in folder.iterdir()),
                             ["package-alpha.yml", "package-build.yml"])

    def test_invalid_and_duplicate_package_names_cannot_generate_workflow_paths(self):
        for packages in ([package("../escape")], [package("app"), package("app")]):
            with self.subTest(packages=packages):
                with self.assertRaises(ValueError):
                    changes.package_map([packages, []])


if __name__ == "__main__":
    unittest.main()
