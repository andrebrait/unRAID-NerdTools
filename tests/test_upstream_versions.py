"""Offline tests for the upstream version monitor."""
import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check-upstream-versions.py"
SPEC = importlib.util.spec_from_file_location("upstream", SCRIPT)
upstream = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upstream)


class UpstreamVersionTests(unittest.TestCase):
    def test_orders_prereleases_before_releases_and_numbers_naturally(self):
        ordered = ["1.0pre3", "1.0pre4", "1.0rc1", "1.0", "1.0.1", "9.2.1252", "9.2.1300", "20211210_ccd5d9f5"]
        self.assertEqual(sorted(ordered, key=upstream.version_key), ordered)

    def test_reports_only_strictly_newer_upstream_versions(self):
        locks = [{"name": "vim", "version": "9.2.1252"}, {"name": "llfuse", "version": "1.5.2"},
                 {"name": "iftop", "version": "1.0pre4"}, {"name": "ours-only", "version": "1"}]
        theirs = {"vim": "9.2.1300", "llfuse": "1.4.1", "iftop": "1.0pre4"}
        self.assertEqual(upstream.newer(locks, theirs), [("vim", "9.2.1252", "9.2.1300")])

    def test_parses_both_package_lists(self):
        slack = "PACKAGE NAME:  iftop-1.0pre4-x86_64-6.txz\nPACKAGE NAME:  python-pip-25.3-x86_64-1.txz\n"
        self.assertEqual(upstream.slackware_versions(slack), {"iftop": "1.0pre4", "python-pip": "25.3"})
        sbo = "SLACKBUILD NAME: llfuse\nSLACKBUILD LOCATION: ./x\nSLACKBUILD VERSION: 1.4.1\n\nSLACKBUILD NAME: fzf\nSLACKBUILD VERSION: 0.74.5\n"
        self.assertEqual(upstream.sbo_versions(sbo), {"llfuse": "1.4.1", "fzf": "0.74.5"})


if __name__ == "__main__":
    unittest.main()
