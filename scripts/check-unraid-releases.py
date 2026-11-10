#!/usr/bin/env python3
"""Discover official Unraid releases; observation is not compatibility verification."""

import argparse
import json
from pathlib import Path
import re
import sys
import urllib.request
from urllib.parse import urlsplit


FEED_URL = "https://releases.unraid.net/usb-creator"
CHECKSUM_CATALOG_URL = "https://releases.unraid.net/json"
NEXT_METADATA_URL = "https://releases.unraid.net/os?branch=next"
VERSION = r"[0-9]+\.[0-9]+\.[0-9]+(?:-(?:beta|rc)\.?[0-9]+)?"
RELEASE_PATH = re.compile(
    rf"/dl/(?P<channel>stable|next)/(?P<version>{VERSION})/"
    r"(?P<download_id>[0-9a-f]{64})/unRAIDServer-(?P=version)-x86_64\.zip"
)


def parse_download_url(raw):
    if not isinstance(raw, str):
        raise ValueError("release URL must be a string")
    url = urlsplit(raw)
    match = RELEASE_PATH.fullmatch(url.path)
    if url.scheme != "https" or url.netloc != "releases.unraid.net" or not match:
        raise ValueError("unrecognized official release URL")
    channel, version, _download_id = match.groups()
    if channel == "stable" and "-" in version:
        raise ValueError("stable release has a prerelease version")
    # The opaque download ID in the URL is NOT the archive's SHA256.
    return {"channel": channel, "version": version,
            "url": url._replace(query="", fragment="").geturl()}


def parse_releases(payload):
    """Flatten os_list/subitems, validate official download URLs, and sort/dedupe."""
    if not isinstance(payload, dict) or not isinstance(payload.get("os_list"), list):
        raise ValueError("feed must contain an os_list array")
    found = {}
    pending = list(payload["os_list"])
    while pending:
        item = pending.pop()
        if not isinstance(item, dict):
            raise ValueError("release entries must be objects")
        if "subitems" in item:
            if not isinstance(item["subitems"], list):
                raise ValueError("subitems must be an array")
            pending.extend(item["subitems"])
        if "url" not in item:
            if "subitems" in item:
                continue
            raise ValueError("release entry has no download URL")
        record = parse_download_url(item["url"])
        key = (record["channel"], record["version"])
        if key in found and found[key] != record:
            raise ValueError(f"conflicting downloads for {key[0]}/{key[1]}")
        found[key] = record
    if not any(channel == "stable" for channel, _ in found):
        raise ValueError("feed contains no stable Unraid releases")
    return [found[key] for key in sorted(found)]


def fetch_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "NerdTools-release-monitor"})
    with urllib.request.urlopen(request, timeout=30) as response:
        # A creator catalog is small; never consume an unbounded error response.
        data = response.read(2_000_001)
    if len(data) > 2_000_000:
        raise ValueError(f"release metadata exceeds 2 MB: {url}")
    return json.loads(data)


def fetch_feed():
    return fetch_json(FEED_URL)


def checked_digest(payload):
    digest = payload.get("sha256") if isinstance(payload, dict) else None
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("authoritative release metadata has no valid SHA256")
    return digest


def attach_checksums(observed, catalog, next_metadata):
    """Join creator URLs with authoritative checksum fields, never URL path IDs."""
    if not isinstance(catalog, list):
        raise ValueError("checksum catalog must be an array")
    stable = {}
    for item in catalog:
        if not isinstance(item, dict):
            raise ValueError("checksum catalog entries must be objects")
        if item.get("public") is not True or item.get("published") is not True:
            continue
        record = parse_download_url(item.get("url"))
        if item.get("version") != record["version"]:
            raise ValueError("checksum catalog version does not match its URL")
        if record["channel"] != "stable":
            continue
        record.update(sha256=checked_digest(item), checksum_source=CHECKSUM_CATALOG_URL)
        version = record["version"]
        if version in stable and stable[version] != record:
            raise ValueError(f"conflicting checksum catalog records for {version}")
        stable[version] = record
    next_versions = {record["version"] for record in observed if record["channel"] == "next"}
    if next_versions:
        if not isinstance(next_metadata, dict) or next_metadata.get("version") not in next_versions:
            raise ValueError("next checksum metadata does not match any creator version")
        next_digest = checked_digest(next_metadata)
    resolved = []
    for record in observed:
        if record["channel"] == "stable":
            authoritative = stable.get(record["version"])
            if authoritative is None or authoritative["url"] != record["url"]:
                raise ValueError(f"no matching public checksum catalog URL for {record['version']}")
            resolved.append(authoritative)
        else:
            if next_metadata["version"] == record["version"]:
                resolved.append({**record, "sha256": next_digest,
                                 "checksum_source": NEXT_METADATA_URL})
            else:
                # The public next endpoint exposes only its current version.
                # Retain other creator entries visibly, but never dispatch an unpinned archive.
                resolved.append({
                    **record, "checksum_status": "unavailable",
                    "checksum_source": NEXT_METADATA_URL,
                    "checksum_error": (
                        f"No authoritative SHA256 for next {record['version']}; "
                        f"the current next metadata only covers {next_metadata['version']}"
                    ),
                })
    return resolved


def version_key(version):
    match = re.fullmatch(
        r"([0-9]+)\.([0-9]+)\.([0-9]+)(?:-(beta|rc)\.?([0-9]+))?", version
    )
    if not match:
        raise ValueError(f"unrecognized Unraid version: {version}")
    major, minor, patch, stage, number = match.groups()
    return (int(major), int(minor), int(patch),
            {None: 2, "rc": 1, "beta": 0}[stage], int(number or 0))


def make_report(observed, baseline):
    """Return candidates, not a claim that a release or build was tested."""
    minimum = version_key(baseline)
    return {
        "source": FEED_URL,
        "checksum_sources": sorted({record["checksum_source"] for record in observed
                                    if "checksum_source" in record}),
        "baseline": baseline,
        "compatibility": "not_verified",
        "observed": observed,
        "matrix": {"include": [
            record for record in observed
            if "sha256" in record and version_key(record["version"]) >= minimum
        ]},
        "blocked": [
            record for record in observed
            if "sha256" not in record
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feed-file", type=Path, help="read an offline USB-creator JSON feed")
    parser.add_argument("--checksums-file", type=Path, help="offline /json checksum catalog")
    parser.add_argument("--next-file", type=Path, help="offline /os?branch=next metadata")
    parser.add_argument("--baseline", default="7.3.4", help="minimum supported Unraid version")
    parser.add_argument("--output", type=Path, help="also write the JSON report to this path")
    args = parser.parse_args(argv)
    try:
        payload = (json.loads(args.feed_file.read_text(encoding="utf-8"))
                   if args.feed_file else fetch_feed())
        observed = parse_releases(payload)
        if args.feed_file and not args.checksums_file:
            raise ValueError("--feed-file requires --checksums-file; URL IDs are not checksums")
        catalog = (json.loads(args.checksums_file.read_text(encoding="utf-8"))
                   if args.checksums_file else fetch_json(CHECKSUM_CATALOG_URL))
        next_metadata = None
        if any(record["channel"] == "next" for record in observed):
            next_metadata = (json.loads(args.next_file.read_text(encoding="utf-8"))
                             if args.next_file else
                             None if args.feed_file else fetch_json(NEXT_METADATA_URL))
        report = make_report(attach_checksums(observed, catalog, next_metadata), args.baseline)
        encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.output:
            inputs = (args.feed_file, args.checksums_file, args.next_file)
            if any(path and args.output.resolve() == path.resolve() for path in inputs):
                raise ValueError("output must not overwrite an input metadata file")
            args.output.write_text(encoded, encoding="utf-8")
        print(encoded, end="")
        for record in report["blocked"]:
            print(f"Unraid release check warning: {record['checksum_error']}; not dispatched",
                  file=sys.stderr)
    except (OSError, ValueError) as error:
        print(f"Unraid release check failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
