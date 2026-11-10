#!/usr/bin/env python3
"""Run with python3 -m unittest discover -s build -p 'test_*.py'."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
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

    def test_dependency_order_is_topological_and_rejects_cycles(self):
        packages = [{"name": "app", "dependencies": ["lib"], "build_dependencies": []}, {"name": "lib", "dependencies": [], "build_dependencies": []}]
        self.assertEqual([p["name"] for p in native.build_order(packages)], ["lib", "app"])
        packages[1]["build_dependencies"] = ["app"]
        with self.assertRaisesRegex(ValueError, "cycle"):
            native.build_order(packages)
        with self.assertRaisesRegex(ValueError, "Unknown dependency"):
            native.build_order([{"name": "app", "dependencies": ["missing"]}])

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

    def test_manifest_hashes_actual_asset_and_keeps_external_rar_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            asset = Path(tmp) / "nerdtools-example-1.0-x86_64-1_NT73.txz"
            asset.write_bytes(b"abc")
            record = native.asset_record({"name": "example", "version": "1.0", "dependencies": ["libexample"]}, asset)
            self.assertEqual(record, {"name": "nerdtools-example", "tool": "example", "version": "1.0", "file": asset.name, "sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", "size": 3, "dependencies": ["nerdtools-libexample"]})
            manifest = native.catalog([record])
            self.assertEqual(manifest["family"], "7.3")
            self.assertEqual(manifest["min_unraid"], "7.3.4")
            self.assertEqual(manifest["external_packages"][0]["name"], "rar")
            self.assertNotIn("file", manifest["external_packages"][0])

    def test_official_image_inputs_reject_arbitrary_hosts_and_bad_digests(self):
        for url, digest in [("https://attacker.example/root.zip", "a" * 64), ("https://releases.unraid.net/dl/stable/root.zip", "bad")]:
            with self.assertRaises(ValueError):
                rootfs.validate_target("7.3.4", url, digest)
        rootfs.validate_target("7.4.0-rc.2", "https://releases.unraid.net/dl/next/7.4.0-rc.2/root.zip", "a" * 64)

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




if __name__ == "__main__":
    unittest.main()
