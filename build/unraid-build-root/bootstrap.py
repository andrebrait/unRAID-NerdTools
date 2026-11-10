#!/usr/bin/env python3
"""Pinned official Unraid rootfs plus additive, SHA256-locked development inputs.

This module has no package catalog, build recipe, publication or QEMU policy.
Use only on a disposable Linux x86_64 runner; roots stay private to that job.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile

SHA256 = re.compile(r"[a-f0-9]{64}\Z")
NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9+_.-]*\Z")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-.][a-zA-Z0-9.]+)?\Z")


def run(args, check=True, **kwargs):
    print("+", shlex.join(map(str, args)), flush=True)
    return subprocess.run(list(map(str, args)), check=check, **kwargs)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def checked_path(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Unsafe archive path: {name}")
    return path


def https_url(url):
    if not isinstance(url, str):
        raise ValueError("Input URL must be a string")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError(f"HTTPS URL required: {url}")
    parsed.port
    return parsed


class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def validate_input(item):
    if not isinstance(item, dict) or any(not isinstance(item.get(key), str) for key in ("url", "file", "sha256")):
        raise ValueError("Input requires string URL, filename and SHA256")
    https_url(item["url"])
    if not SHA256.fullmatch(item["sha256"]):
        raise ValueError("Invalid SHA256 input pin")
    if Path(item["file"]).name != item["file"] or not NAME.fullmatch(item["file"]):
        raise ValueError("Input filename must be a safe basename")


def validate_toolchain(lock):
    inputs = json.loads(Path(lock).read_text())
    if not isinstance(inputs, list) or not inputs:
        raise ValueError("An exact, nonempty development toolchain lock is required")
    names, files = set(), set()
    for item in inputs:
        validate_input(item)
        if not isinstance(item.get("name"), str) or not NAME.fullmatch(item["name"]) or item["name"] in names or item["file"] in files:
            raise ValueError("Toolchain requires unique safe names and filenames")
        names.add(item["name"])
        files.add(item["file"])
    return inputs


def fetch(item, cache):
    validate_input(item)
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / item["sha256"]
    if not path.exists():
        opener = urllib.request.build_opener(HTTPSRedirect())
        request = urllib.request.Request(item["url"], headers={"User-Agent": "NerdTools-reproducible-build"})
        with opener.open(request, timeout=120) as response, tempfile.NamedTemporaryFile(dir=cache, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                shutil.copyfileobj(response, stream)
                stream.flush()
                if digest(temporary) != item["sha256"]:
                    raise ValueError(f"SHA256 mismatch: {item['url']}")
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
    if digest(path) != item["sha256"]:
        raise ValueError(f"SHA256 mismatch in cached input: {item['file']}")
    return path
def validate_target(version, url, sha256):
    parsed = https_url(url)
    if not VERSION.fullmatch(version) or not SHA256.fullmatch(sha256):
        raise ValueError("Invalid target version or SHA256")
    if parsed.hostname != "releases.unraid.net" or not parsed.path.startswith("/dl/"):
        raise ValueError("Target image must originate at releases.unraid.net/dl/")


def root_payload(data):
    """Skip the uncompressed early microcode cpio, not arbitrary magic-byte hits."""
    if not data.startswith(b"070701"):
        if data.startswith((b"\x28\xb5\x2f\xfd", b"\xfd7zXZ\x00", b"\x1f\x8b")):
            return data
        raise ValueError("Unsupported initramfs framing")
    offset = 0
    while True:
        if data[offset:offset + 6] != b"070701" or len(data) - offset < 110:
            raise ValueError("Invalid early cpio header")
        header = data[offset:offset + 110]
        size = int(header[54:62], 16)
        namesize = int(header[94:102], 16)
        if not namesize or offset + 110 + namesize > len(data):
            raise ValueError("Invalid early cpio name")
        name = data[offset + 110:offset + 110 + namesize - 1]
        if name.startswith((b"etc/", b"./etc/")):
            return data
        offset = (offset + 110 + namesize + 3) & ~3
        offset = (offset + size + 3) & ~3
        if offset > len(data):
            raise ValueError("Truncated early cpio")
        if name == b"TRAILER!!!":
            while offset < len(data) and data[offset] == 0:
                offset += 1
            payload = data[offset:]
            if not payload.startswith((b"\x28\xb5\x2f\xfd", b"\xfd7zXZ\x00", b"\x1f\x8b", b"070701")):
                raise ValueError("Unsupported main initramfs compression")
            return payload
def prepare_image(item, version, cache, root, bzroot_sha256=None, kernel=None):
    archive = fetch(item, cache)
    with zipfile.ZipFile(archive) as image:
        data = image.read("bzroot")
        if bzroot_sha256 and hashlib.sha256(data).hexdigest() != bzroot_sha256:
            raise ValueError("Baseline bzroot SHA256 mismatch")
        if kernel is not None:
            kernel.write_bytes(image.read("bzimage"))
    payload = root_payload(data)
    with tempfile.TemporaryFile() as source:
        source.write(payload)
        source.seek(0)
        if payload.startswith(b"\x28\xb5\x2f\xfd"):
            decoder = ["zstd", "--decompress", "--stdout", "--quiet"]
        elif payload.startswith(b"\xfd7zXZ\x00"):
            decoder = ["xz", "--decompress", "--stdout"]
        elif payload.startswith(b"\x1f\x8b"):
            decoder = ["gzip", "--decompress", "--stdout"]
        else:
            decoder = ["cat"]
        process = subprocess.Popen(decoder, stdin=source, stdout=subprocess.PIPE)
        try:
            run(["cpio", "--extract", "--make-directories", "--preserve-modification-time", "--no-absolute-filenames", "--quiet"], stdin=process.stdout, cwd=root)
        finally:
            process.stdout.close()
        if process.wait() != 0:
            raise RuntimeError("Initramfs decompression failed")
    actual = (root / "etc/unraid-version").read_text()
    if not re.search(r'^version="?' + re.escape(version) + r'"?\s*$', actual, re.MULTILINE):
        raise ValueError(f"Downloaded image version mismatch: wanted {version}")
    (root / "proc").mkdir(exist_ok=True)
    (root / "tmp").mkdir(exist_ok=True)
    (root / "dev").mkdir(exist_ok=True)
    for name, major, minor in (("null", 1, 3), ("zero", 1, 5), ("random", 1, 8), ("urandom", 1, 9)):
        node = root / "dev" / name
        if not node.exists():
            os.mknod(node, stat.S_IFCHR | 0o666, os.makedev(major, minor))


def restore_toolchain_links(script, root):
    # Only makepkg's generated link statements; never execute package install scripts.
    pattern = r"\(\s*cd\s+([^;]+)\s*;\s*ln\s+-sf\s+([^\s]+)\s+([^\s)]+)\s*\)"
    for directory, target, name in re.findall(pattern, script):
        directory = checked_path(directory.strip())
        if "/" in name or name in (".", "..") or "\0" in target:
            raise ValueError("Unsafe toolchain symlink")
        link = contained(root, str(directory / name))
        if not link.exists() and not link.is_symlink():
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target)


def prepare_toolchain(root, cache, lock):
    inputs = validate_toolchain(lock)
    for item in inputs:
        archive = fetch(item, cache)
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                contained(root, member.name)
            try:
                script = tar.extractfile("install/doinst.sh").read().decode()
            except KeyError:
                script = ""
        run(["tar", "--extract", "--file", archive, "--directory", root, "--skip-old-files", "--no-overwrite-dir", "--exclude=install"])
        restore_toolchain_links(script, root)
    for executable in ("usr/bin/gcc", "usr/bin/g++", "usr/bin/make", "usr/include/stdio.h"):
        if not (root / executable).exists():
            raise ValueError(f"Development root is missing {executable}")


def clone_root(source, destination):
    destination.mkdir()
    run(["cp", "--archive", "--reflink=auto", str(source) + "/.", destination])


def contained(root, name):
    root = root.resolve()
    path = root / checked_path(name)
    if path != root and not path.parent.resolve().is_relative_to(root):
        raise ValueError(f"Archive path follows a host symlink: {name}")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--bzroot-sha256")
    parser.add_argument("--toolchain-lock", required=True, type=Path)
    parser.add_argument("--work", type=Path, default=Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())))
    args = parser.parse_args()
    if sys.platform != "linux" or os.uname().machine != "x86_64" or os.geteuid() != 0:
        raise ValueError("Bootstrap requires a disposable Linux x86_64 runner and root")
    validate_target(args.version, args.url, args.sha256)
    args.work.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="unraid-root-", dir=args.work))
    baseline = workspace / "baseline"
    baseline.mkdir()
    kernel = workspace / "bzimage"
    image = {"url": args.url, "sha256": args.sha256, "file": f"unRAIDServer-{args.version}-x86_64.zip"}
    prepare_image(image, args.version, workspace / "inputs", baseline, args.bzroot_sha256, kernel)
    development = workspace / "development"
    clone_root(baseline, development)
    prepare_toolchain(development, workspace / "inputs", args.toolchain_lock)
    evidence = {"version": args.version, "image_sha256": args.sha256,
                "bzroot_sha256": args.bzroot_sha256, "toolchain_sha256": digest(args.toolchain_lock)}
    (workspace / "bootstrap.json").write_text(json.dumps(evidence, indent=2) + "\n")
    outputs = {"baseline-root": str(baseline), "development-root": str(development), "kernel": str(kernel)}
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            stream.write("".join(f"{key}={value}\n" for key, value in outputs.items()))
    print(json.dumps(outputs, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError, tarfile.TarError, zipfile.BadZipFile) as error:
        sys.exit(f"Bootstrap refused: {error}")
