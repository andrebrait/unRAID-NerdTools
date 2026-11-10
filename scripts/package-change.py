#!/usr/bin/env python3
"""Select native package builds from a two-commit semantic diff, never publish.

PRs compare the event's base/head SHAs; pushes compare before/after. Missing
history, new branches and rewritten push history conservatively select all.
Filtering happens here, not in GitHub paths filters (which truncate at 300 files).
"""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess


LOCKS = ("build/locks/slackbuilds.json", "build/locks/slackware.json")
INVENTORY = "build/recipe-files.json"
NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9+_.-]*\Z")
SHA = re.compile(r"[a-fA-F0-9]{40}(?:[a-fA-F0-9]{24})?\Z")


def package_map(locks):
    packages = {}
    for entries in locks:
        if not isinstance(entries, list):
            raise ValueError("Package lock must be an array")
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not NAME.fullmatch(entry["name"]):
                raise ValueError("Invalid package name")
            name = entry["name"]
            if name in packages:
                raise ValueError(f"Duplicate package: {name}")
            for key in ("dependencies", "build_dependencies"):
                dependencies = entry.get(key, [])
                if not isinstance(dependencies, list) or any(not isinstance(dep, str) or not NAME.fullmatch(dep) for dep in dependencies):
                    raise ValueError(f"Invalid {key}: {name}")
            packages[name] = entry
    return packages


def working_packages(repo):
    return package_map(json.loads((repo / path).read_text()) for path in LOCKS)


def select_packages(before, after, old_inventory, inventory, paths):
    """Return current packages impacted through either the old or new graph."""
    if not isinstance(old_inventory, dict) or not isinstance(inventory, dict):
        raise ValueError("Recipe inventory must be an object")
    changed = set()
    for path in paths:
        if path.startswith("build/recipes/"):
            changed.add(path.removeprefix("build/recipes/").split("/", 1)[0])
        elif path in (*LOCKS, INVENTORY):
            continue
        elif (path.startswith("build/") or path.startswith("scripts/package-change")
              or path == "tests/test_package_change.py"
              or path == ".github/workflows/package-build.yml"):
            return sorted(after)
        elif path.startswith(".github/workflows/package-") and path.endswith(".yml"):
            changed.add(path.removeprefix(".github/workflows/package-").removesuffix(".yml"))
    changed.update(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
    changed.update(path.split("/", 1)[0] for path in old_inventory.keys() | inventory.keys()
                   if old_inventory.get(path) != inventory.get(path))
    reverse = {}
    for packages in (before, after):
        for name, entry in packages.items():
            for dependency in entry.get("dependencies", []) + entry.get("build_dependencies", []):
                reverse.setdefault(dependency, set()).add(name)
    pending = list(changed)
    while pending:
        for dependent in reverse.get(pending.pop(), ()):
            if dependent not in changed:
                changed.add(dependent)
                pending.append(dependent)
    return sorted(changed & after.keys())


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True, stderr=subprocess.PIPE)


def valid_sha(value):
    return isinstance(value, str) and SHA.fullmatch(value) and set(value) != {"0"}


def select_git(repo, base, head, event_name):
    repo = Path(repo)
    if not valid_sha(head):
        return sorted(working_packages(repo))
    try:
        after = package_map(json.loads(git(repo, "show", f"{head}:{path}")) for path in LOCKS)
        inventory = json.loads(git(repo, "show", f"{head}:{INVENTORY}"))
    except subprocess.CalledProcessError:
        return sorted(working_packages(repo))
    if not valid_sha(base):
        return sorted(after)
    try:
        if event_name == "push":
            git(repo, "merge-base", "--is-ancestor", base, head)
        before = package_map(json.loads(git(repo, "show", f"{base}:{path}")) for path in LOCKS)
        old_inventory = json.loads(git(repo, "show", f"{base}:{INVENTORY}"))
        paths = git(repo, "diff", "--no-renames", "--name-only", "-z", base, head, "--").split("\0")
        return select_packages(before, after, old_inventory, inventory, paths)
    except (subprocess.CalledProcessError, ValueError, UnicodeError):
        return sorted(after)


def package_selected(repo, package, event_name, event):
    if package not in working_packages(Path(repo)):
        raise ValueError(f"Unknown package: {package}")
    if event_name == "workflow_dispatch":
        return True
    if event_name == "pull_request":
        request = event.get("pull_request", {})
        base, head = request.get("base", {}).get("sha"), request.get("head", {}).get("sha")
    elif event_name == "push":
        base, head = event.get("before"), event.get("after")
    else:
        raise ValueError(f"Unsupported event: {event_name}")
    return package in select_git(repo, base, head, event_name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--event", default=os.environ.get("GITHUB_EVENT_NAME"))
    parser.add_argument("--event-file", type=Path, default=os.environ.get("GITHUB_EVENT_PATH"))
    args = parser.parse_args()
    event = json.loads(args.event_file.read_text()) if args.event_file else {}
    selected = package_selected(args.repo, args.package, args.event, event)
    value = f"selected={str(selected).lower()}\n"
    print(value, end="")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write(value)


if __name__ == "__main__":
    main()
