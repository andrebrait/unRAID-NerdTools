"""Offline release-feed regression tests; run with unittest discovery."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check-unraid-releases.py"
SPEC = importlib.util.spec_from_file_location("unraid_releases", SCRIPT)
releases = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(releases)


def entry(channel, version, download_id="a" * 64):
    return {
        "name": f"Unraid {version}",
        "url": (
            f"https://releases.unraid.net/dl/{channel}/{version}/{download_id}/"
            f"unRAIDServer-{version}-x86_64.zip?utm_source=usb_creator"
        ),
    }


def catalog_entry(channel, version, digest="b" * 64, download_id="a" * 64):
    return {**entry(channel, version, download_id), "version": version,
            "sha256": digest, "public": True, "published": True}


class ReleaseFeedTests(unittest.TestCase):
    def test_parses_nested_stable_and_public_next_without_tracking_query(self):
        payload = {"os_list": [
            entry("stable", "7.3.4"),
            {"subitems": [{"subitems": [entry("next", "7.4.0-rc.2")]}]},
        ]}
        result = releases.parse_releases(payload)
        self.assertEqual(
            [(item["channel"], item["version"]) for item in result],
            [("next", "7.4.0-rc.2"), ("stable", "7.3.4")],
        )
        self.assertTrue(all("?" not in item["url"] for item in result))
        self.assertNotIn("sha256", result[0])

    def test_no_public_next_release_is_valid(self):
        result = releases.parse_releases({"os_list": [
            entry("stable", "7.3.4"), {"subitems": []},
        ]})
        self.assertEqual(len(result), 1)

    def test_input_order_and_identical_duplicates_do_not_change_result(self):
        stable = entry("stable", "7.3.4")
        beta = entry("next", "7.4.0-beta1")
        first = releases.parse_releases({"os_list": [stable, beta, stable]})
        second = releases.parse_releases({"os_list": [beta, stable]})
        self.assertEqual(first, second)

    def test_rejects_conflicting_duplicate_releases(self):
        with self.assertRaisesRegex(ValueError, "conflicting"):
            releases.parse_releases({"os_list": [
                entry("stable", "7.3.4"), entry("stable", "7.3.4", "b" * 64),
            ]})

    def test_rejects_malformed_or_empty_feed_instead_of_reporting_no_changes(self):
        for payload in ({}, {"os_list": {}}, {"os_list": []},
                        {"os_list": [None]}, {"os_list": [{"subitems": None}]},
                        {"os_list": [{"name": "Unraid 7.3.4"}]},
                        {"os_list": [entry("next", "7.4.0-rc.2")]}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                releases.parse_releases(payload)

    def test_rejects_untrusted_or_unrecognized_release_urls(self):
        original = entry("stable", "7.3.4")["url"]
        invalid = [
            original.replace("https:", "http:"),
            original.replace("releases.unraid.net", "example.com"),
            original.replace("/stable/", "/private/"),
            original.replace("a" * 64, "bad-hash"),
            original.replace("unRAIDServer-7.3.4", "unRAIDServer-7.3.5"),
            original.replace("releases.unraid.net", "user@releases.unraid.net"),
            original.replace("7.3.4", "7.4.0-rc.2"),
        ]
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(ValueError):
                releases.parse_releases({"os_list": [{"url": url}]})

    def test_matrix_excludes_older_releases_but_keeps_new_stable_and_public_next(self):
        observed = releases.parse_releases({"os_list": [
            entry("stable", "7.3.5"), entry("stable", "7.3.4"),
            entry("stable", "7.2.10"), entry("next", "7.4.0-rc.3"),
        ]})
        observed = releases.attach_checksums(
            observed, [catalog_entry("stable", version) for version in ("7.3.5", "7.3.4", "7.2.10")],
            {"version": "7.4.0-rc.3", "sha256": "c" * 64},
        )
        report = releases.make_report(observed, "7.3.4")
        self.assertEqual(
            [(item["channel"], item["version"]) for item in report["matrix"]["include"]],
            [("next", "7.4.0-rc.3"), ("stable", "7.3.4"), ("stable", "7.3.5")],
        )
        self.assertEqual(report["compatibility"], "not_verified")
        self.assertEqual(report["observed"], observed)

    def test_version_order_is_numeric_and_prereleases_precede_final(self):
        versions = ["7.4.0-rc.10", "7.4.0-beta2", "7.4.0", "7.4.0-rc.2", "7.3.10"]
        self.assertEqual(sorted(versions, key=releases.version_key),
                         ["7.3.10", "7.4.0-beta2", "7.4.0-rc.2", "7.4.0-rc.10", "7.4.0"])

    def test_checksum_is_authoritative_field_not_opaque_download_id(self):
        observed = releases.parse_releases({"os_list": [
            entry("stable", "7.3.4"),
            {"subitems": [entry("next", "7.4.0-rc.2")]},
        ]})
        resolved = releases.attach_checksums(
            observed, [catalog_entry("stable", "7.3.4")],
            {"version": "7.4.0-rc.2", "sha256": "c" * 64},
        )
        self.assertEqual([record["sha256"] for record in resolved], ["c" * 64, "b" * 64])
        self.assertTrue(all("/" + "a" * 64 + "/" in record["url"] for record in resolved))

    def test_real_stable_and_next_metadata_regression(self):
        stable_id = "ae8169459e2277e8d450b9a590c18f747dd8eaa50ebc54743a36cadd093cbfb8"
        next_id = "e26ae90ab52b8e9726f30a504a96fa393f34ac0dc5ba6b1526519c1749fe42e0"
        stable_sha = "ba608638e743b323f4581e911fd07093158344010dab14bb19607b46999530e9"
        next_sha = "b19bc36698a4ba4ee2df8164a80be35cae77d144d8a4e92cf0368eb24123da96"
        observed = releases.parse_releases({"os_list": [
            entry("stable", "7.3.4", stable_id), entry("next", "7.4.0-rc.2", next_id),
        ]})
        resolved = releases.attach_checksums(
            observed, [catalog_entry("stable", "7.3.4", stable_sha, stable_id)],
            {"version": "7.4.0-rc.2", "sha256": next_sha},
        )
        self.assertEqual([record["sha256"] for record in resolved], [next_sha, stable_sha])
        self.assertEqual([record["checksum_source"] for record in resolved],
                         [releases.NEXT_METADATA_URL, releases.CHECKSUM_CATALOG_URL])

    def test_missing_invalid_private_or_mismatched_catalog_checksum_fails_closed(self):
        observed = releases.parse_releases({"os_list": [entry("stable", "7.3.4")]})
        good = catalog_entry("stable", "7.3.4")
        for catalog in ([], {}, [{**good, "sha256": None}], [{**good, "sha256": "bad"}],
                        [{**good, "version": "7.3.5"}],
                        [catalog_entry("stable", "7.3.4", download_id="d" * 64)],
                        [{**good, "public": False}], [{**good, "published": False}]):
            with self.subTest(catalog=catalog), self.assertRaises(ValueError):
                releases.attach_checksums(observed, catalog, None)

    def test_next_checksum_metadata_version_must_match_creator_version(self):
        observed = releases.parse_releases({"os_list": [
            entry("stable", "7.3.4"), entry("next", "7.4.0-rc.2"),
        ]})
        catalog = [catalog_entry("stable", "7.3.4")]
        for metadata in (None, {}, {"version": "7.4.0-rc.3", "sha256": "b" * 64},
                         {"version": "7.4.0-rc.2", "sha256": "bad"}):
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                releases.attach_checksums(observed, catalog, metadata)

    def test_multiple_next_versions_do_not_block_stable_or_current_next(self):
        observed = releases.parse_releases({"os_list": [
            entry("stable", "7.3.4"),
            {"subitems": [entry("next", "7.3.0-beta1"), entry("next", "7.4.0-beta1"),
                          entry("next", "7.4.0-rc.2")]},
        ]})
        resolved = releases.attach_checksums(
            observed, [catalog_entry("stable", "7.3.4")],
            {"version": "7.4.0-rc.2", "sha256": "c" * 64},
        )
        report = releases.make_report(resolved, "7.3.4")
        self.assertEqual([item["version"] for item in report["matrix"]["include"]],
                         ["7.4.0-rc.2", "7.3.4"])
        self.assertEqual([item["version"] for item in report["blocked"]],
                         ["7.3.0-beta1", "7.4.0-beta1"])
        blocked = report["blocked"][0]
        self.assertEqual(blocked["checksum_status"], "unavailable")
        self.assertNotIn("sha256", blocked)
        self.assertIn("7.4.0-rc.2", blocked["checksum_error"])
        self.assertEqual(len(report["observed"]), 4)

    def test_cli_writes_json_report_without_mutating_feed(self):
        with tempfile.TemporaryDirectory() as directory:
            feed = Path(directory) / "feed.json"
            output = Path(directory) / "report.json"
            checksums = Path(directory) / "checksums.json"
            checksums.write_text(json.dumps([catalog_entry("stable", "7.3.4")]), encoding="utf-8")
            source = json.dumps({"os_list": [entry("stable", "7.3.4")]})
            feed.write_text(source, encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                status = releases.main(["--feed-file", str(feed), "--checksums-file",
                                        str(checksums), "--output", str(output)])
            self.assertEqual(status, 0)
            self.assertEqual(feed.read_text(encoding="utf-8"), source)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["observed"][0]["version"], "7.3.4")

    def test_cli_rejects_overwriting_feed(self):
        with tempfile.TemporaryDirectory() as directory:
            feed = Path(directory) / "feed.json"
            checksums = Path(directory) / "checksums.json"
            checksums.write_text(json.dumps([catalog_entry("stable", "7.3.4")]), encoding="utf-8")
            source = json.dumps({"os_list": [entry("stable", "7.3.4")]})
            feed.write_text(source, encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                status = releases.main(["--feed-file", str(feed), "--checksums-file",
                                        str(checksums), "--output", str(feed)])
            self.assertEqual(status, 1)
            self.assertIn("output must not overwrite", errors.getvalue())
            self.assertEqual(feed.read_text(encoding="utf-8"), source)

    def test_cli_fetch_failure_is_nonzero_and_does_not_write_report(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            with mock.patch.object(releases, "fetch_feed", side_effect=OSError("offline")):
                with contextlib.redirect_stderr(io.StringIO()) as errors:
                    status = releases.main(["--output", str(output)])
            self.assertEqual(status, 1)
            self.assertIn("Unraid release check failed: offline", errors.getvalue())
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
