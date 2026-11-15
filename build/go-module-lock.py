#!/usr/bin/env python3
"""Regenerate one recipe's offline Go proxy inputs from an extracted source tree."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import subprocess
import sys

PROXY = "https://proxy.golang.org/"
FILE = re.compile(r"[A-Za-z0-9._-]+\Z")


def json_stream(text):
    decoder = json.JSONDecoder()
    index = 0
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if index == len(text):
            break
        value, index = decoder.raw_decode(text, index)
        yield value


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stage_file(path, data):
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        mode = (path.stat().st_mode & 0o777) if path.exists() else 0o644
        staged.chmod(mode)
        return staged
    except Exception:
        staged.unlink(missing_ok=True)
        raise


def replace_pair(first, first_data, second, second_data):
    if first.is_symlink() or second.is_symlink():
        raise ValueError("output files must not be symlinks")
    old_first = first.read_bytes() if first.exists() else None
    first_stage = second_stage = rollback_stage = None
    try:
        first_stage = stage_file(first, first_data)
        second_stage = stage_file(second, second_data)
        os.replace(first_stage, first)
        try:
            os.replace(second_stage, second)
        except Exception:
            if old_first is None:
                first.unlink(missing_ok=True)
            else:
                rollback_stage = stage_file(first, old_first)
                os.replace(rollback_stage, first)
            raise
    finally:
        for staged in (first_stage, second_stage, rollback_stage):
            if staged is not None:
                staged.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--recipe", required=True, type=Path)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--package", required=True)
    parser.add_argument("--work", required=True, type=Path)
    args = parser.parse_args()
    if any(path.is_symlink() for path in (args.source, args.recipe, args.lock, args.work)):
        raise ValueError("input and output paths must not be symlinks")
    args.source = args.source.resolve()
    args.recipe = args.recipe.resolve()
    args.lock = args.lock.resolve()
    args.work = args.work.resolve()

    if not (args.source / "go.mod").is_file() or not args.recipe.is_dir():
        raise ValueError("source go.mod and recipe directory are required")
    entries = json.loads(args.lock.read_text())
    matches = [entry for entry in entries if entry.get("name") == args.package]
    if len(matches) != 1:
        raise ValueError("package must identify exactly one lock entry")
    entry = matches[0]

    if args.work.is_symlink() or (args.work.exists() and not args.work.is_dir()):
        raise ValueError("work path must be a directory")
    args.work.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix="go-lock-", dir=args.work)
    cache_root = Path(temporary.name)
    module_cache = cache_root / "module-cache"
    build_cache = cache_root / "build-cache"
    module_cache.mkdir(parents=True)
    build_cache.mkdir(parents=True)
    env = {
        **os.environ,
        "GOMODCACHE": str(module_cache),
        "GOCACHE": str(build_cache),
        "GOPROXY": PROXY.removesuffix("/"),
        "GOSUMDB": "sum.golang.org",
        "GOTOOLCHAIN": "auto",
        "GOPRIVATE": "",
        "GONOPROXY": "",
        "GONOSUMDB": "",
        "GOWORK": "off",
        "GOENV": "off",
    }
    result = subprocess.run(
        ["go", "mod", "download", "-json", "all"], cwd=args.source,
        env=env, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)

    download_root = (module_cache / "cache/download").resolve()
    generated = {}
    for module in json_stream(result.stdout):
        if not isinstance(module, dict):
            raise ValueError("Go emitted a non-object module record")
        if module.get("Error"):
            raise ValueError(f"Go module download failed: {module['Error']}")
        for key in ("Info", "GoMod", "Zip"):
            value = module.get(key)
            if not value:
                continue
            path = Path(value)
            try:
                resolved = path.resolve(strict=True)
                relative = resolved.relative_to(download_root)
            except (OSError, ValueError) as error:
                raise ValueError("Go emitted a path outside module cache") from error
            if path.is_symlink() or not resolved.is_file():
                raise ValueError("Go module cache entry must be a regular file")
            destination = relative.as_posix()
            url = PROXY + destination
            suffix = resolved.suffix
            if suffix not in (".info", ".mod", ".zip"):
                raise ValueError("unexpected Go proxy file type")
            name = hashlib.sha256(url.encode()).hexdigest()[:24] + suffix
            item = (name, destination, url, sha256_file(resolved))
            previous = generated.setdefault(destination, item)
            if previous != item:
                raise ValueError("conflicting Go module cache entries")

    if not generated:
        raise ValueError("Go produced no module proxy inputs")
    records = [generated[key] for key in sorted(generated)]
    names = {record[0] for record in records}
    if len(names) != len(records):
        raise ValueError("hashed Go module filenames collided")

    manifest = args.recipe / "go-modules.tsv"
    if manifest.is_symlink() or args.lock.is_symlink():
        raise ValueError("output files must not be symlinks")
    if manifest.exists():
        for line in manifest.read_text().splitlines():
            fields = line.split("\t")
            if len(fields) != 2 or not FILE.fullmatch(fields[0]):
                raise ValueError("invalid existing Go module manifest")

    inputs = entry.get("inputs", [])
    if not isinstance(inputs, list):
        raise ValueError("package inputs must be a list")
    retained = []
    for item in inputs:
        if (not isinstance(item, dict) or not isinstance(item.get("url"), str)
                or not isinstance(item.get("file"), str) or not FILE.fullmatch(item["file"])
                or not isinstance(item.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
            raise ValueError("invalid retained package input")
        if not item["url"].startswith(PROXY):
            retained.append(dict(item))
    generated_inputs = [{"url": url, "file": name, "sha256": digest}
                        for name, _, url, digest in records]
    final_inputs = generated_inputs + retained
    files = [item["file"] for item in final_inputs]
    if len(files) != len(set(files)):
        raise ValueError("package input filenames must be unique")
    entry["inputs"] = final_inputs
    manifest_data = "".join(f"{name}\t{destination}\n"
                            for name, destination, _, _ in records).encode()
    lock_data = (json.dumps(entries, indent=2) + "\n").encode()
    replace_pair(manifest, manifest_data, args.lock, lock_data)
    temporary.cleanup()
    print(f"Locked {len(records)} Go proxy files for {args.package}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(error, file=sys.stderr)
        raise SystemExit(1)
