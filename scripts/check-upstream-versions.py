#!/usr/bin/env python3
"""Report recipes whose Slackware-current or SlackBuilds.org version is newer than ours.

Only reports; updating a recipe stays a reviewed change. Packages that upstream does not
ship, or where we are intentionally ahead, are not reported.
"""
import json
import re
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOURCES = {
    "slackware.json": ("Slackware-current", "https://slackware.uk/slackware/slackware64-current/PACKAGES.TXT"),
    "slackbuilds.json": ("SlackBuilds.org 15.0", "https://slackbuilds.org/slackbuilds/15.0/SLACKBUILDS.TXT"),
}
PRERELEASE = {"alpha": -4, "a": -4, "beta": -3, "b": -3, "pre": -2, "rc": -1}


def version_key(version):
    """Natural order where 1.0pre4 < 1.0 < 1.0.1 and 9.2.1252 < 9.2.1300."""
    key = []
    for token in re.findall(r"\d+|[a-zA-Z]+", version):
        if token.isdigit():
            key.append((1, int(token)))
        else:
            key.append((0, PRERELEASE.get(token.lower(), 0)))
    return key + [(1, -1)]  # a release sorts after its own prereleases


def slackware_versions(text):
    versions = {}
    for match in re.finditer(r"^PACKAGE NAME:\s+(.+)-([^-]+)-[^-]+-[^-]+\.t.z\s*$", text, re.M):
        versions[match.group(1)] = match.group(2)
    return versions


def sbo_versions(text):
    versions = {}
    for block in text.split("\n\n"):
        name = re.search(r"^SLACKBUILD NAME: (\S+)", block, re.M)
        version = re.search(r"^SLACKBUILD VERSION: (\S+)", block, re.M)
        if name and version:
            versions[name.group(1)] = version.group(1)
    return versions


def newer(locks, upstream):
    """[(name, ours, theirs)] where upstream sorts strictly after our locked version."""
    return [(p["name"], p["version"], upstream[p["name"]]) for p in locks
            if p["name"] in upstream and version_key(upstream[p["name"]]) > version_key(p["version"])]


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "NerdTools-upstream-monitor"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read().decode("utf-8", "replace")


def main():
    report = []
    for lock, (label, url) in SOURCES.items():
        locks = json.loads((REPO / "build/locks" / lock).read_text())
        text = fetch(url)
        upstream = slackware_versions(text) if lock == "slackware.json" else sbo_versions(text)
        if not upstream:
            raise SystemExit(f"No versions parsed from {url}")
        report += [{"name": n, "ours": o, "upstream": u, "source": label, "url": url} for n, o, u in newer(locks, upstream)]
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    sys.exit(main())
