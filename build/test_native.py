#!/usr/bin/env python3
"""Run with python3 -m unittest discover -s build -p 'test_*.py'."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("native", Path(__file__).with_name("native.py"))
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)
import bootstrap as rootfs


class NativeBuildTests(unittest.TestCase):
    def test_wrong_input_digest_is_rejected_even_in_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            expected = hashlib.sha256(b"original").hexdigest()
            (cache / expected).write_bytes(b"modified")
            with self.assertRaisesRegex(ValueError, "SHA256"):
                rootfs.fetch({"url": "https://example.com/source.tar.gz", "file": "source.tar.gz", "sha256": expected}, cache)

    def test_toolchain_extraction_preserves_existing_files_and_directory_modes(self):
        tar_bin = shutil.which("gtar") or shutil.which("tar")
        self.assertIsNotNone(tar_bin)
        self.assertIn("GNU tar", subprocess.run([tar_bin, "--version"], capture_output=True, text=True, check=True).stdout)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            cache = root / "cache"
            tools = root / "tools"
            for path in (target / "etc", target / "usr/bin", target / "usr/include", target / "usr/lib64", cache, tools):
                path.mkdir(parents=True, exist_ok=True)
            (target / "etc").chmod(0o755)
            (target / "etc/config").write_text("existing")
            (target / "usr/lib64/libicuuc.so.78.3").write_text("baseline ICU78")
            (target / "usr/lib64/libicuuc.so").symlink_to("libicuuc.so.78.3")
            (target / "usr/include/uversion.h").write_text("ICU78 headers")
            (target / "var").mkdir()
            outside = root / "outside"
            outside.mkdir()
            (target / "var/run").symlink_to(outside)
            for path in ("usr/bin/gcc", "usr/bin/g++", "usr/bin/make", "usr/include/stdio.h"):
                (target / path).touch()
            (tools / "tar").symlink_to(tar_bin)
            archive = root / "toolchain.txz"
            with tarfile.open(archive, "w:xz") as tar:
                directory = tarfile.TarInfo("etc")
                directory.type, directory.mode = tarfile.DIRTYPE, 0o777
                tar.addfile(directory)
                for name, data in (("etc/config", b"replacement"), ("etc/new", b"new"),
                                   ("usr/include/uversion.h", b"ICU79 headers"),
                                   ("usr/lib64/libicuuc.so.79.1", b"additional ICU79"),
                                   ("install/doinst.sh", b"( cd usr/lib64 ; ln -sf libicuuc.so.79.1 libicuuc.so )\n"
                                                        b"( cd usr/lib64 ; ln -sf libicuuc.so.79.1 libicuuc.so.79 )\n"),
                                   ("var/run/nscd", b"toolchain runtime state")):
                    entry = tarfile.TarInfo(name)
                    entry.size = len(data)
                    tar.addfile(entry, io.BytesIO(data))
            pin = hashlib.sha256(archive.read_bytes()).hexdigest()
            shutil.copyfile(archive, cache / pin)
            lock = root / "toolchain.json"
            lock.write_text(json.dumps([{"name": "tool", "url": "https://example.com/tool.txz", "file": "tool.txz", "sha256": pin}]))
            with patch.dict(os.environ, {"PATH": str(tools) + os.pathsep + os.environ["PATH"]}):
                rootfs.prepare_toolchain(target, cache, lock)
            self.assertEqual((target / "etc/config").read_text(), "existing")
            self.assertEqual((target / "etc/new").read_text(), "new")
            self.assertEqual((target / "etc").stat().st_mode & 0o777, 0o755)
            self.assertEqual(os.readlink(target / "var/run"), str(outside))
            self.assertFalse((outside / "nscd").exists())
            self.assertEqual((target / "usr/lib64/libicuuc.so").read_text(), "baseline ICU78")
            self.assertEqual(os.readlink(target / "usr/lib64/libicuuc.so"), "libicuuc.so.78.3")
            self.assertEqual((target / "usr/include/uversion.h").read_text(), "ICU78 headers")
            self.assertEqual((target / "usr/lib64/libicuuc.so.79").read_text(), "additional ICU79")

    def test_dependency_order_is_topological_and_rejects_cycles(self):
        packages = [{"name": "app", "dependencies": ["lib"], "build_dependencies": []}, {"name": "lib", "dependencies": [], "build_dependencies": []}]
        self.assertEqual([p["name"] for p in native.build_order(packages)], ["lib", "app"])
        packages[1]["build_dependencies"] = ["app"]
        with self.assertRaisesRegex(ValueError, "cycle"):
            native.build_order(packages)
        with self.assertRaisesRegex(ValueError, "Unknown dependency"):
            native.build_order([{"name": "app", "dependencies": ["missing"]}])

    def test_dependency_closure_includes_runtime_and_build_dependencies_once(self):
        packages = [
            {"name": "app", "dependencies": ["runtime"], "build_dependencies": ["compiler"]},
            {"name": "unrelated", "dependencies": [], "build_dependencies": []},
            {"name": "compiler", "dependencies": ["shared"], "build_dependencies": []},
            {"name": "runtime", "dependencies": ["shared"], "build_dependencies": []},
            {"name": "shared", "dependencies": [], "build_dependencies": []},
        ]
        self.assertEqual([p["name"] for p in native.dependency_closure(packages, "app")],
                         ["shared", "runtime", "compiler", "app"])
        with self.assertRaisesRegex(ValueError, "Unknown package"):
            native.dependency_closure(packages, "missing")

    def test_build_toolchain_finds_pinned_go_and_rejects_ambiguity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            go = root / "usr/lib64/go1.27.3/go/bin/go"
            go.parent.mkdir(parents=True)
            go.write_bytes(b"go")
            self.assertEqual(native.go_root(root), "/usr/lib64/go1.27.3/go")
            other = root / "usr/lib64/go1.28/go/bin/go"
            other.parent.mkdir(parents=True)
            other.write_bytes(b"go")
            with self.assertRaisesRegex(ValueError, "Go toolchain"):
                native.go_root(root)

    def test_aggregate_requires_every_catalog_package_and_nothing_unknown(self):
        packages = [{"name": "app", "version": "1.0"}, {"name": "dep", "version": "2.0", "catalog": False}]
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            app = directory / "nerdtools-app-1.0-x86_64-1_NT73.txz"
            dep = directory / "nerdtools-dep-2.0-x86_64-1_NT73.txz"
            dep.write_bytes(b"dep")
            with self.assertRaisesRegex(ValueError, "package artifact set"):
                native.checked_artifacts(packages, directory)
            app.write_bytes(b"app")
            self.assertEqual(native.checked_artifacts(packages, directory), {"app": app, "dep": dep})
            dep.unlink()  # build-only helpers are optional
            self.assertEqual(native.checked_artifacts(packages, directory), {"app": app})
            (directory / "unexpected").write_text("bad")
            with self.assertRaisesRegex(ValueError, "package artifact set"):
                native.checked_artifacts(packages, directory)

    def test_only_packages_with_an_identical_build_identity_are_carried_over(self):
        packages = [{"name": name, "version": "1.0", "dependencies": [], "build_dependencies": []} for name in ("same", "changed", "new", "renamed")]
        packages.append({"name": "helper", "version": "1.0", "catalog": False})
        record = lambda name, file=None: {"tool": name, "file": file or f"nerdtools-{name}-1.0-x86_64-1_NT73.txz", "sha256": "a" * 64}
        previous = {"packages": [record("same", "nerdtools-same-1.0-x86_64-3_NT73.txz"), record("changed", "nerdtools-changed-1.0-x86_64-2_NT73.txz"),
                                 record("renamed", "nerdtools-renamed-0.9-x86_64-4_NT73.txz")]}
        with patch.object(native, "build_keys", return_value={p["name"]: "now" for p in packages}):
            plan = native.carry_plan(packages, {"same": "now", "changed": "before", "renamed": "now", "new": "now"}, previous)
            self.assertEqual(plan["build"], ["changed", "new", "renamed"])
            self.assertEqual(plan["reuse"], [{"name": "same", "file": "nerdtools-same-1.0-x86_64-3_NT73.txz", "sha256": "a" * 64}])
            # Carried over keeps its number; a rebuild of the same version gets the next; a new version restarts at 1.
            self.assertEqual(plan["builds"], {"same": 3, "changed": 3, "new": 1, "renamed": 1})
            self.assertEqual(native.carry_plan(packages, {}, {})["reuse"], [])
        with patch.dict(native.BUILDS, {"changed": 3}):
            self.assertEqual(native.artifact_name(packages[1]), "nerdtools-changed-1.0-x86_64-3_NT73.txz")

    def test_build_identity_follows_recipe_inputs_and_dependencies_but_not_smoke_commands(self):
        packages = native.load_packages()
        before = native.build_keys(packages)
        changed = [dict(p) for p in packages]
        target = next(p for p in changed if p["name"] == "libassuan")
        target["smoke"] = [["/bin/true"]]
        self.assertEqual(native.build_keys(changed), before)
        target["version"] += ".1"
        after = native.build_keys(changed)
        differing = {name for name in before if before[name] != after[name]}
        self.assertIn("gnupg2", differing)  # dependents rebuild with their dependency
        self.assertNotIn("bc", differing)

    def test_build_one_refuses_publication_and_requires_package_and_prepared_roots(self):
        args = native.argparse.Namespace(package="app", publish=True, baseline_root=Path("/baseline"),
                                        development_root=Path("/development"))
        with self.assertRaisesRegex(ValueError, "cannot publish"):
            native.build_one(args)
        args.publish = False
        for key in ("package", "baseline_root", "development_root"):
            with self.subTest(key=key), patch.object(args, key, None):
                with self.assertRaisesRegex(ValueError, "requires"):
                    native.build_one(args)


    def test_prepared_roots_reject_mismatched_workspace_and_bootstrap_pins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline, development = root / "baseline", root / "development"
            with self.assertRaisesRegex(ValueError, "same bootstrap workspace"):
                native.prepared_roots(baseline, root / "other/development")
            (root / "bootstrap.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "pinned baseline and toolchain"):
                native.prepared_roots(baseline, development)

    def test_package_policy_rejects_host_libraries_and_escaping_links(self):
        for name, target in [("usr/lib64/libc.so.6", None), ("opt/nerdtools/bin/escape", "../../../../etc/shadow"), ("../../etc/shadow", None)]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                archive = Path(tmp) / "unsafe.txz"
                with tarfile.open(archive, "w:xz") as tar:
                    entry = tarfile.TarInfo(name)
                    if target:
                        entry.type = tarfile.SYMTYPE
                        entry.linkname = target
                    else:
                        entry.size = 1
                    tar.addfile(entry, None if target else io.BytesIO(b"x"))
                with self.assertRaises(ValueError):
                    native.inspect_package(archive, "example")

    def test_package_policy_accepts_private_library_and_relative_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "safe.txz"
            with tarfile.open(archive, "w:xz") as tar:
                library = tarfile.TarInfo("opt/nerdtools/lib64/libexample.so.1")
                library.size = 1
                tar.addfile(library, io.BytesIO(b"x"))
                link = tarfile.TarInfo("opt/nerdtools/lib64/libexample.so")
                link.type = tarfile.SYMTYPE
                link.linkname = "libexample.so.1"
                tar.addfile(link)
            self.assertEqual(len(native.inspect_package(archive, "example")), 2)

    def test_package_policy_accepts_alsa_data_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "alsa.txz"
            with tarfile.open(archive, "w:xz") as tar:
                data = tarfile.TarInfo("opt/nerdtools/share/alsa/Librem 5,codec.conf")
                data.size = 1
                tar.addfile(data, io.BytesIO(b"x"))
            self.assertEqual(len(native.inspect_package(archive, "alsa-lib")), 1)

    def test_package_policy_accepts_python_and_ruby_documentation_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "docs.txz"
            with tarfile.open(archive, "w:xz") as tar:
                for name in ("2 to 3 - Module Deletion (docs).tmSnippet", "binmode%3f-i.ri"):
                    data = tarfile.TarInfo("opt/nerdtools/share/" + name)
                    data.size = 1
                    tar.addfile(data, io.BytesIO(b"x"))
            self.assertEqual(len(native.inspect_package(archive, "example")), 2)

    def test_only_nmap_may_install_zenmap_desktop_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "nmap.txz"
            with tarfile.open(archive, "w:xz") as tar:
                for name in ("opt/nerdtools/bin/nmap", "usr/share/applications/nerdtools-zenmap.desktop"):
                    entry = tarfile.TarInfo(name)
                    entry.size = 1
                    tar.addfile(entry, io.BytesIO(b"x"))
            self.assertEqual(len(native.inspect_package(archive, "nmap")), 2)
            with self.assertRaises(ValueError):
                native.inspect_package(archive, "vim")

    def test_manifest_hashes_actual_asset_and_keeps_external_rar_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            asset = Path(tmp) / "nerdtools-example-1.0-x86_64-1_NT73.txz"
            asset.write_bytes(b"abc")
            record = native.asset_record({"name": "example", "version": "1.0", "dependencies": ["libexample"]}, asset)
            self.assertEqual(record, {"name": "nerdtools-example", "tool": "example", "version": "1.0", "file": asset.name, "sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", "size": 3, "dependencies": ["nerdtools-libexample"]})
            manifest = native.catalog([record], "7.3.5")
            self.assertEqual(manifest["family"], "7.3")
            self.assertEqual(manifest["min_unraid"], "7.3.4")
            # A later family starts at the first version that passed the gates, prereleases included.
            later = native.catalog([record], "7.4.0-rc.2")
            self.assertEqual((later["family"], later["min_unraid"]), ("7.4", "7.4.0-rc.2"))
            versions = ["7.4.0", "7.4.0-rc.10", "7.3.10", "7.4.0-beta.2", "7.4.0-rc.2", "7.3.4"]
            self.assertEqual(sorted(versions, key=native.version_key),
                             ["7.3.4", "7.3.10", "7.4.0-beta.2", "7.4.0-rc.2", "7.4.0-rc.10", "7.4.0"])
            self.assertEqual(manifest["external_packages"][0]["name"], "rar")
            self.assertNotIn("file", manifest["external_packages"][0])

    def test_official_image_inputs_reject_arbitrary_hosts_and_bad_digests(self):
        for url, digest in [("https://attacker.example/root.zip", "a" * 64), ("https://releases.unraid.net/dl/stable/root.zip", "bad")]:
            with self.assertRaises(ValueError):
                rootfs.validate_target("7.3.4", url, digest)
        rootfs.validate_target("7.4.0-rc.2", "https://releases.unraid.net/dl/next/7.4.0-rc.2/root.zip", "a" * 64)

    def test_unraid_usr_layer_is_extracted_from_release_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "squash-source"
            (source / "bin").mkdir(parents=True)
            (source / "bin/id").write_text("baseline identity tool")
            layer = root / "bzmodules"
            subprocess.run(["mksquashfs", str(source), str(layer), "-noappend", "-no-progress", "-no-xattrs", "-processors", "1"],
                           check=True, stdout=subprocess.DEVNULL)
            release = root / "release.zip"
            with zipfile.ZipFile(release, "w") as archive:
                archive.write(layer, "bzmodules")
            target = root / "target"
            (target / "usr").mkdir(parents=True)
            with zipfile.ZipFile(release) as archive:
                rootfs.extract_usr_layer(archive, target)
            self.assertEqual((target / "usr/bin/id").read_text(), "baseline identity tool")
            with zipfile.ZipFile(release) as archive:
                with self.assertRaisesRegex(ValueError, "empty /usr"):
                    rootfs.extract_usr_layer(archive, target)
            self.assertEqual((target / "usr/bin/id").read_text(), "baseline identity tool")

    def test_initramfs_split_skips_microcode_and_preserves_compression(self):
        name = b"TRAILER!!!\0"
        header = b"070701" + b"00000000" * 11 + f"{len(name):08x}".encode() + b"00000000"
        early = header + name
        early += b"\0" * (-len(early) % 512)
        self.assertEqual(rootfs.root_payload(early + b"\x28\xb5\x2f\xfdroot"), b"\x28\xb5\x2f\xfdroot")
        with self.assertRaises(ValueError):
            rootfs.root_payload(b"not an initramfs")

    def test_kernel_acceptance_requires_kernel_identity_and_all_smoke_commands(self):
        log = "Booting Linux\nNERDTOOLS_SMOKE_PASSED:secret:2:6.18.58-Unraid\n"
        self.assertEqual(native.kernel_report(log, "secret", 2), "6.18.58-Unraid")
        for invalid in (log.replace("PASSED", "FAILED"), log.replace("secret", "public-hash"), log.replace(":2:", ":1:"), "child says NERDTOOLS_SMOKE_PASSED:secret:2:6.18.58-Unraid\n"):
            with self.assertRaises(ValueError):
                native.kernel_report(invalid, "secret", 2)

    def test_install_keeps_existing_configuration_and_activates_new_configs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            (root / "etc").mkdir(parents=True)
            (root / "etc/existing.conf").write_bytes(b"user settings")
            archive = Path(tmp) / "config.txz"
            with tarfile.open(archive, "w:xz") as tar:
                for name in ("etc/existing.conf.new", "etc/new.conf.new"):
                    member = tarfile.TarInfo(name)
                    member.size = 7
                    tar.addfile(member, io.BytesIO(b"default"))
            native.install_payload(archive, root)
            self.assertEqual((root / "etc/existing.conf").read_bytes(), b"user settings")
            self.assertEqual((root / "etc/existing.conf.new").read_bytes(), b"default")
            self.assertEqual((root / "etc/new.conf").read_bytes(), b"default")

    def test_secure_locate_setgid_is_preserved_but_other_elevation_is_rejected(self):
        for name, gid, accepted in [("opt/nerdtools/bin/plocate", 21, True), ("opt/nerdtools/bin/mlocate", 21, True), ("opt/nerdtools/bin/plocate", 0, False), ("opt/nerdtools/bin/locate", 21, False), ("opt/nerdtools/bin/other", 21, False)]:
            with self.subTest(name=name, gid=gid), tempfile.TemporaryDirectory() as tmp:
                archive = Path(tmp) / "privileges.txz"
                with tarfile.open(archive, "w:xz") as tar:
                    member = tarfile.TarInfo(name)
                    member.mode = 0o2711
                    member.gid = gid
                    member.size = 1
                    tar.addfile(member, io.BytesIO(b"x"))
                if accepted:
                    self.assertEqual(native.inspect_package(archive, Path(name).name)[0].gid, 21)
                else:
                    with self.assertRaises(ValueError):
                        native.inspect_package(archive, Path(name).name)

    def test_two_packages_cannot_share_even_identical_library_payloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            archive = Path(tmp) / "library.txz"
            with tarfile.open(archive, "w:xz") as tar:
                member = tarfile.TarInfo("opt/nerdtools/lib64/libexample.so.1")
                member.size = 1
                tar.addfile(member, io.BytesIO(b"x"))
            native.install_payload(archive, root)
            with self.assertRaisesRegex(ValueError, "collision"):
                native.install_payload(archive, root)

    def test_generated_hook_preserves_dangling_config_and_activates_only_new_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "etc").mkdir()
            (root / "etc/existing").symlink_to("missing")
            (root / "etc/existing.new").write_text("default")
            (root / "etc/first.new").write_text("new default")
            script = native.config_hook(["etc/first.new", "etc/existing.new"])
            subprocess.run(["/bin/sh"], input=script, text=True, cwd=root, check=True)
            self.assertTrue((root / "etc/existing").is_symlink())
            self.assertTrue((root / "etc/existing.new").exists())
            self.assertEqual((root / "etc/first").read_text(), "new default")
        for invalid in ("tmp/unsafe.new", "etc/not-new", "etc/../root/unsafe.new", "etc/name with spaces.new"):
            with self.assertRaises(ValueError):
                native.config_hook([invalid])

    def test_archive_extraction_refuses_existing_host_symlink_ancestors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            (root / "escape").symlink_to(Path(tmp), target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "host symlink"):
                rootfs.contained(root, "escape/file")

    def test_baseline_provided_library_must_exist_at_the_required_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = root / "var/lib/pkgtools/packages"
            records.mkdir(parents=True)
            (records / "liburing-2.14-x86_64-1").touch()
            with self.assertRaisesRegex(ValueError, "liburing"):
                native.require_provided(root)
            (records / "liburing-2.16-x86_64-1").touch()
            native.require_provided(root)

    def test_retained_corresponding_sources_restore_only_matching_pinned_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "sources.tar.xz"
            with tarfile.open(bundle, "w:xz") as tar:
                member = tarfile.TarInfo("./sources/example/source.tar.gz")
                member.size = 3
                tar.addfile(member, io.BytesIO(b"abc"))
            cache = Path(tmp) / "cache"
            pin = {"file": "source.tar.gz", "sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"}
            native.seed_sources([{"name": "example", "inputs": [pin]}], bundle, cache)
            self.assertEqual((cache / pin["sha256"]).read_bytes(), b"abc")
            pin["sha256"] = "f" * 64
            with self.assertRaisesRegex(ValueError, "SHA256"):
                native.seed_sources([{"name": "example", "inputs": [pin]}], bundle, cache)

    def test_manifest_commands_are_derived_from_qualified_archive_launchers(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "commands.txz"
            with tarfile.open(archive, "w:xz") as tar:
                for name in ("usr/local/bin/nerdtools-vim", "opt/nerdtools/bin/private-helper"):
                    tar.addfile(tarfile.TarInfo(name), io.BytesIO(b""))
            self.assertEqual(native.package_commands(archive), ["vim"])

    def test_oversized_release_assets_are_refused_before_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            asset = root / "sources.tar.xz"
            with asset.open("wb") as stream:
                stream.truncate(2**31 - 1)
            native.asset_limits(root)
            with asset.open("r+b") as stream:
                stream.truncate(2**31)
            with self.assertRaisesRegex(ValueError, "asset limit"):
                native.asset_limits(root)

    def test_known_unlicensed_input_blocks_publication_without_blocking_private_builds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "build").mkdir()
            (root / "build/source-blockers.json").write_text(json.dumps([{"module": "unlicensed/module", "sha256": ["f" * 64]}]))
            with patch.object(native, "REPO", root):
                self.assertEqual(native.publication_blockers([{"inputs": [{"sha256": "f" * 64}]}]), ["unlicensed/module"])
                self.assertEqual(native.publication_blockers([{"inputs": [{"sha256": "a" * 64}]}]), [])
                self.assertEqual(native.publication_blockers([{"name": "powershell", "inputs": [], "provenance": {"kind": "upstream-binary"}}]), ["powershell: source build required"])

    def test_input_and_toolchain_pins_are_validated_without_downloading(self):
        valid = {"name": "gcc", "url": "https://example.com/gcc.txz", "file": "gcc.txz", "sha256": "a" * 64}
        rootfs.validate_input(valid)
        for key, value in (("url", "http://example.com/a"), ("url", "https://example.com:invalid/a"), ("file", "../a"), ("sha256", "bad")):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                rootfs.validate_input({**valid, key: value})
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "toolchain.json"
            lock.write_text(json.dumps([{**valid, "sha256": "bad"}]))
            with self.assertRaises(ValueError):
                rootfs.validate_toolchain(lock)

    def test_recipe_inventory_rejects_changed_sidecars_and_unlisted_additions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recipes = root / "build/recipes/example"
            recipes.mkdir(parents=True)
            (recipes / "example.SlackBuild").write_text("exit 0\n")
            sidecar = recipes / "fix.patch"
            sidecar.write_text("checked patch")
            inventory = native.recipe_inventory(root / "build/recipes")
            (root / "build/recipe-files.json").write_text(json.dumps(inventory))
            with patch.object(native, "REPO", root):
                native.validate_recipe_inventory()
                sidecar.write_text("changed patch")
                with self.assertRaisesRegex(ValueError, "inventory"):
                    native.validate_recipe_inventory()
                sidecar.write_text("checked patch")
                (recipes / "unlisted.sh").write_text("malicious addition")
                with self.assertRaisesRegex(ValueError, "inventory"):
                    native.validate_recipe_inventory()

    def test_install_preserves_existing_directory_metadata_and_rejects_unsafe_new_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            (root / "etc").mkdir(parents=True, mode=0o700)
            archive = Path(tmp) / "directory.txz"
            with tarfile.open(archive, "w:xz") as tar:
                member = tarfile.TarInfo("etc")
                member.type, member.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(member)
            native.install_payload(archive, root)
            self.assertEqual((root / "etc").stat().st_mode & 0o7777, 0o700)
            with tarfile.open(archive, "w:xz") as tar:
                member = tarfile.TarInfo("etc/unsafe")
                member.type, member.mode = tarfile.DIRTYPE, 0o777
                tar.addfile(member)
            with self.assertRaisesRegex(ValueError, "directory"):
                native.install_payload(archive, root)

    def test_uid_mapping_is_bounded_and_round_trips_root_and_slocate(self):
        self.assertEqual(native.map_id(0, False), 100000)
        self.assertEqual(native.map_id(21, False), 100021)
        self.assertEqual(native.map_id(100021, True), 21)
        with self.assertRaises(ValueError):
            native.map_id(65536, False)
        with self.assertRaises(ValueError):
            native.map_id(0, True)

    def test_unchecked_bluetooth_root_execution_rule_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "rule.txz"
            with tarfile.open(archive, "w:xz") as tar:
                for name, content in (("etc/udev/rules.d/97-hid2hci.rules", b"RUN+=evil"), ("opt/nerdtools/bin/tool", b"x")):
                    member = tarfile.TarInfo(name)
                    member.size = len(content)
                    tar.addfile(member, io.BytesIO(content))
            with self.assertRaisesRegex(ValueError, "rule"):
                native.inspect_package(archive, "bluez")

    def test_validate_rejects_bad_package_and_toolchain_pins_before_building(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recipe = root / "build/recipes/example/example.SlackBuild"
            recipe.parent.mkdir(parents=True)
            recipe.write_text("exit 0\n")
            locks = root / "build/locks"
            locks.mkdir()
            pin = {"name": "gcc", "url": "https://example.com/source.txz", "file": "source.txz", "sha256": "a" * 64}
            package = {"name": "example", "version": "1.0", "recipe": "build/recipes/example/example.SlackBuild",
                       "recipe_sha256": hashlib.sha256(b"exit 0\n").hexdigest(), "inputs": [pin],
                       "dependencies": [], "build_dependencies": [], "provenance": {"kind": "source"}, "smoke": []}
            (root / "build/recipe-files.json").write_text(json.dumps(native.recipe_inventory(root / "build/recipes")))
            (locks / "slackbuilds.json").write_text("[]")
            (locks / "toolchain.json").write_text(json.dumps([pin]))
            with patch.object(native, "REPO", root), patch.object(native, "BASELINE", {**native.BASELINE, "tools": ["example"], "provided_packages": []}):
                for key, value in (("url", "http://example.com/a"), ("file", "../a"), ("sha256", "bad")):
                    (locks / "slackware.json").write_text(json.dumps([{**package, "inputs": [{**pin, key: value}]}]))
                    with self.assertRaises(ValueError):
                        native.load_packages()
                identical_sidecar = {**pin, "file": "example.SlackBuild", "sha256": package["recipe_sha256"]}
                (locks / "slackware.json").write_text(json.dumps([{**package, "inputs": [identical_sidecar]}]))
                with self.assertRaisesRegex(ValueError, "overwrites"):
                    native.load_packages()
                (locks / "slackware.json").write_text(json.dumps([package]))
                (locks / "toolchain.json").write_text(json.dumps([{**pin, "sha256": "bad"}]))
                with self.assertRaises(ValueError):
                    native.load_packages()

    def test_screen_pam_is_exactly_reviewed_and_other_authentication_files_are_forbidden(self):
        reviewed = b"#%PAM-1.0\n\nauth\t\trequired\tpam_securetty.so debug\nauth\t\tinclude\t\tsystem-auth\n\n"
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "screen.txz"
            for path, content, accepted in (("etc/pam.d/screen.new", reviewed, True),
                                             ("etc/pam.d/screen.new", b"auth sufficient pam_permit.so\n", False),
                                             ("etc/ld.so.preload", b"/opt/nerdtools/lib64/evil.so\n", False)):
                with tarfile.open(archive, "w:xz") as tar:
                    for name, value in ((path, content), ("opt/nerdtools/bin/screen", b"x")):
                        member = tarfile.TarInfo(name)
                        member.size = len(value)
                        tar.addfile(member, io.BytesIO(value))
                if accepted:
                    native.inspect_package(archive, "screen")
                else:
                    with self.assertRaises(ValueError):
                        native.inspect_package(archive, "screen")

    def test_recipe_inventory_cannot_follow_a_top_level_package_symlink_outside_the_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recipes = root / "recipes"
            recipes.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (outside / "secret").write_text("not a build input")
            (recipes / "alias").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "escapes"):
                native.recipe_inventory(recipes)




class CandidateArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.package_dir = self.root / "packages"
        self.package_dir.mkdir()
        self.packages = [{"name": "zed", "version": "1.0"},
                         {"name": "alpha", "version": "2.1"}]
        self.plan = {"build": ["zed", "alpha"], "reuse": [],
                     "builds": {"zed": 2, "alpha": 4}}
        self.keys = {"zed": "a" * 64, "alpha": "b" * 64}
        self.identity = {
            "repository": "example/unraid-nerdtools", "pr": 17,
            "head": "c" * 40, "tree": "d" * 40, "workflow": "build-packages.yml",
            "run": 12345, "attempt": 2,
            "artifact_name": "candidate-packages-pr-17-run-12345-attempt-2",
        }
        self.files = {
            "zed": "nerdtools-zed-1.0-x86_64-2_NT73.txz",
            "alpha": "nerdtools-alpha-2.1-x86_64-4_NT73.txz",
        }
        for package in self.packages:
            self.write_package(self.package_dir / self.files[package["name"]], package["name"])

    def write_package(self, path, name, member_name=None):
        payload = b"verified package payload for " + name.encode()
        info = tarfile.TarInfo(member_name or f"opt/nerdtools/bin/{name}")
        info.mode, info.uid, info.gid, info.size = 0o755, 0, 0, len(payload)
        with tarfile.open(path, "w:xz") as archive:
            archive.addfile(info, io.BytesIO(payload))

    def make_manifest(self, plan=None, package_dir=None):
        self.assertTrue(callable(getattr(native, "candidate_manifest", None)))
        with patch.object(native, "build_keys", return_value=self.keys):
            return native.candidate_manifest(self.packages, plan or self.plan,
                                             package_dir or self.package_dir, self.identity)

    def verify(self, manifest, plan=None, package_dir=None, expected=None, keys=None):
        self.assertTrue(callable(getattr(native, "verify_candidate_bundle", None)))
        with patch.object(native, "build_keys", return_value=keys or self.keys):
            return native.verify_candidate_bundle(
                self.packages, plan or self.plan, package_dir or self.package_dir,
                manifest, expected or self.identity)

    def clone(self, value):
        return json.loads(json.dumps(value))

    def test_manifest_is_deterministic_sorted_and_binds_changed_packages(self):
        manifest = self.make_manifest()
        self.assertEqual(manifest, self.make_manifest())
        self.assertEqual(manifest, {
            "schema": 1,
            **self.identity,
            "baseline_version": native.BASELINE["version"],
            "baseline_image_sha256": native.BASELINE["image"]["sha256"],
            "toolchain_sha256": rootfs.digest(native.REPO / "build/locks/toolchain.json"),
            "rebuild_generation": native.REBUILD_GENERATION,
            "source_date_epoch": int(native.EPOCH),
            "packages": [
                {"name": "alpha", "build": 4, "build_key": self.keys["alpha"],
                 "file": self.files["alpha"],
                 "sha256": hashlib.sha256((self.package_dir / self.files["alpha"]).read_bytes()).hexdigest(),
                 "size": (self.package_dir / self.files["alpha"]).stat().st_size},
                {"name": "zed", "build": 2, "build_key": self.keys["zed"],
                 "file": self.files["zed"],
                 "sha256": hashlib.sha256((self.package_dir / self.files["zed"]).read_bytes()).hexdigest(),
                 "size": (self.package_dir / self.files["zed"]).stat().st_size},
            ],
        })
        self.assertEqual([item["name"] for item in manifest["packages"]], ["alpha", "zed"])

    def test_manifest_writer_uses_stable_json(self):
        manifest = self.make_manifest()
        self.assertTrue(callable(getattr(native, "write_candidate_manifest", None)))
        path = self.root / "candidate-manifest.json"
        native.write_candidate_manifest(path, manifest)
        self.assertEqual(path.read_text(), json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    def test_empty_candidate_set_is_rejected(self):
        plan = {"build": [], "reuse": [{"name": "zed"}, {"name": "alpha"}],
                "builds": {"zed": 2, "alpha": 4}}
        with self.assertRaises(ValueError):
            self.make_manifest(plan=plan)

    def test_manifest_requires_exact_regular_package_files(self):
        extra = self.package_dir / "unexpected.txz"
        extra.write_bytes(b"extra")
        with self.assertRaises(ValueError):
            self.make_manifest()
        extra.unlink()
        path = self.package_dir / self.files["zed"]
        path.unlink()
        path.symlink_to(self.package_dir / self.files["alpha"])
        with self.assertRaises(ValueError):
            self.make_manifest()

    def test_verification_checks_every_archive_and_returns_builds_and_sorted_files(self):
        result = self.verify(self.make_manifest())
        self.assertEqual(result, {"builds": {"alpha": 4, "zed": 2},
                                  "files": sorted(self.files.values())})

    def test_wrong_run_attempt_head_or_tree_is_rejected(self):
        manifest = self.make_manifest()
        for field, value in (("run", 99999), ("attempt", 3), ("head", "e" * 40), ("tree", "f" * 40)):
            with self.subTest(field=field):
                changed = self.clone(manifest)
                changed[field] = value
                with self.assertRaises(ValueError):
                    self.verify(changed)

    def test_changed_compatibility_key_or_build_number_is_rejected(self):
        manifest = self.make_manifest()
        for field, value in (("build_key", "e" * 64), ("build", 8)):
            with self.subTest(field=field):
                changed = self.clone(manifest)
                changed["packages"][0][field] = value
                with self.assertRaises(ValueError):
                    self.verify(changed)

    def test_missing_extra_and_duplicate_manifest_packages_are_rejected(self):
        manifest = self.make_manifest()
        variants = []
        missing = self.clone(manifest)
        missing["packages"].pop()
        variants.append(missing)
        extra = self.clone(manifest)
        extra["packages"].append({"name": "new", "build": 1, "build_key": "f" * 64,
                                  "file": "nerdtools-new-1.0-x86_64-1_NT73.txz",
                                  "sha256": "a" * 64, "size": 1})
        variants.append(extra)
        duplicate = self.clone(manifest)
        duplicate["packages"].append(self.clone(duplicate["packages"][0]))
        variants.append(duplicate)
        for changed in variants:
            with self.subTest(records=changed["packages"]):
                with self.assertRaises(ValueError):
                    self.verify(changed)

    def test_missing_and_extra_artifact_files_are_rejected(self):
        manifest = self.make_manifest()
        path = self.package_dir / self.files["zed"]
        path.unlink()
        with self.assertRaises(ValueError):
            self.verify(manifest)
        self.write_package(path, "zed")
        (self.package_dir / "unexpected.txz").write_bytes(b"extra")
        with self.assertRaises(ValueError):
            self.verify(manifest)

    def test_symlink_and_special_package_files_are_rejected(self):
        manifest = self.make_manifest()
        path = self.package_dir / self.files["zed"]
        path.unlink()
        path.symlink_to(self.package_dir / self.files["alpha"])
        with self.assertRaises(ValueError):
            self.verify(manifest)
        path.unlink()
        os.mkfifo(path)
        with self.assertRaises(ValueError):
            self.verify(manifest)

    def test_filename_sha256_and_size_mismatches_are_rejected(self):
        manifest = self.make_manifest()
        variants = []
        filename = self.clone(manifest)
        filename["packages"][0]["file"] = self.files["zed"]
        variants.append(filename)
        sha = self.clone(manifest)
        sha["packages"][0]["sha256"] = "e" * 64
        variants.append(sha)
        size = self.clone(manifest)
        size["packages"][0]["size"] += 1
        variants.append(size)
        for changed in variants:
            with self.subTest(record=changed["packages"][0]):
                with self.assertRaises(ValueError):
                    self.verify(changed)

    def test_malformed_manifest_schema_is_rejected(self):
        manifest = self.make_manifest()
        variants = []
        missing = self.clone(manifest)
        del missing["tree"]
        variants.append(missing)
        extra = self.clone(manifest)
        extra["target_evidence"] = {}
        variants.append(extra)
        version = self.clone(manifest)
        version["schema"] = 2
        variants.append(version)
        for changed in variants:
            with self.subTest(manifest=changed):
                with self.assertRaises(ValueError):
                    self.verify(changed)

    def test_non_object_records_and_boolean_schema_values_fail_closed(self):
        manifest = self.make_manifest()
        variants = []
        non_object = self.clone(manifest)
        non_object["packages"].append(None)
        variants.append(non_object)
        boolean_schema = self.clone(manifest)
        boolean_schema["schema"] = True
        variants.append(boolean_schema)
        boolean_generation = self.clone(manifest)
        boolean_generation["rebuild_generation"] = True
        variants.append(boolean_generation)
        for changed in variants:
            with self.subTest(manifest=changed):
                with self.assertRaises(ValueError):
                    self.verify(changed)

    def test_empty_candidate_file_is_rejected(self):
        (self.package_dir / self.files["zed"]).write_bytes(b"")
        with self.assertRaises(ValueError):
            self.make_manifest()

    def test_candidate_and_reuse_plan_overlap_is_rejected(self):
        manifest = self.make_manifest()
        overlap = {"build": ["zed", "alpha"],
                   "reuse": [{"name": "alpha", "file": "old.txz", "sha256": "a" * 64}],
                   "builds": {"zed": 2, "alpha": 4}}
        with self.assertRaises(ValueError):
            self.verify(manifest, plan=overlap)

    def test_current_plan_drift_is_rejected(self):
        manifest = self.make_manifest()
        drift = {"build": ["alpha"], "reuse": [{"name": "zed", "file": self.files["zed"],
                                                 "sha256": "a" * 64}],
                 "builds": {"zed": 2, "alpha": 4}}
        with self.assertRaises(ValueError):
            self.verify(manifest, plan=drift)

    def test_invalid_archive_is_inspected_and_rejected(self):
        path = self.package_dir / self.files["zed"]
        self.write_package(path, "zed", "opt/nerdtools/../../etc/shadow")
        with self.assertRaises(ValueError):
            self.verify(self.make_manifest())

    def test_cli_creates_and_verifies_candidate_manifest(self):
        plan_path = self.root / "plan.json"
        manifest_path = self.root / "manifest.json"
        expected_path = self.root / "expected.json"
        plan_path.write_text(json.dumps(self.plan))
        expected_path.write_text(json.dumps({"identity": self.identity, "plan": self.plan}))
        create_args = ["native.py", "candidate-create", "--package-artifacts", str(self.package_dir),
                       "--plan", str(plan_path), "--output", str(manifest_path),
                       "--repository", self.identity["repository"], "--pr", str(self.identity["pr"]),
                       "--head", self.identity["head"], "--tree", self.identity["tree"],
                       "--workflow", self.identity["workflow"], "--run", str(self.identity["run"]),
                       "--attempt", str(self.identity["attempt"]), "--artifact-name", self.identity["artifact_name"]]
        with patch.object(native, "load_packages", return_value=self.packages), \
                patch.object(native, "build_keys", return_value=self.keys), \
                patch("sys.argv", create_args), patch("sys.stdout", new=io.StringIO()):
            try:
                native.main()
            except SystemExit as error:
                exit_code = error.code
            else:
                exit_code = 0
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(manifest_path.read_text()), self.make_manifest())
        verify_args = ["native.py", "candidate-verify", "--package-artifacts", str(self.package_dir),
                       "--manifest", str(manifest_path), "--expected-json", str(expected_path)]
        with patch.object(native, "load_packages", return_value=self.packages), \
                patch.object(native, "build_keys", return_value=self.keys), \
                patch("sys.argv", verify_args), patch("sys.stdout", new=io.StringIO()) as output:
            try:
                native.main()
            except SystemExit as error:
                exit_code = error.code
            else:
                exit_code = 0
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue()),
                         {"builds": {"alpha": 4, "zed": 2}, "files": sorted(self.files.values())})


if __name__ == "__main__":
    unittest.main()
