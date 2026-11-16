#!/usr/bin/env python3
"""Build locked public recipes in a disposable, baseline-matched Unraid root."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
sys.path.insert(0, str(Path(__file__).with_name("unraid-build-root")))
from bootstrap import checked_path, clone_root, contained, digest, fetch, prepare_image, prepare_toolchain, run, validate_input, validate_target, validate_toolchain
from sandbox import map_id, mapped_root, run_sandbox
from package_policy import check_destination


# NERDTOOLS_REPO lets `keys` compute identities for an older checkout with this code (see `plan`).
REPO = Path(os.environ.get("NERDTOOLS_REPO") or Path(__file__).resolve().parent.parent)
BASELINE = json.loads((REPO / "build/baseline.json").read_text())
PRIVATE = PurePosixPath("opt/nerdtools")
SHA256 = re.compile(r"[a-f0-9]{64}\Z")
NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9+_.-]*\Z")
INTEGRATION_DIGESTS = {
    "etc/udev/rules.d/97-hid2hci.rules": "8d4ed90eae12d0c0cac3c65c1960fb90c3f3fde264696bbe30f590a8cd4aa2ed",
    "etc/pam.d/screen.new": "4e631aaf91babb312ec39a09d6cfd6e0a57d8ac5a48eb58a9bade25af3a937aa",
}
# Build date shown by tools (e.g. "compiled ..."): the commit being built, set by CI, so a release says
# when it was built while staying repeatable from that commit. The baseline date is the local fallback.
EPOCH = os.environ.get("SOURCE_DATE_EPOCH") or str(BASELINE["source_date_epoch"])
# Slackware BUILD number per package, chosen by `plan` and passed in by CI: a rebuild of the same upstream
# version gets the next number, so a different binary always has a different package name.
BUILDS = json.loads(os.environ.get("NERDTOOLS_BUILDS") or "{}")


def artifact_name(package, build=None):
    build = int(BUILDS.get(package["name"], 1) if build is None else build)
    return f"nerdtools-{package['name']}-{package['version'].replace('-', '_')}-x86_64-{build}_NT73.txz"


ENV = {
    "PATH": "/opt/nerdtools/bin:/opt/nerdtools/sbin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "HOME": "/root", "LC_ALL": "C", "TZ": "UTC", "ARCH": "x86_64", "PKGTYPE": "txz",
    "BUILD": "1", "TAG": "_NT73", "TMP": "/tmp/nerdtools-build", "OUTPUT": "/out",
    "SOURCE_DATE_EPOCH": EPOCH,
    "CFLAGS": "-O2 -fPIC -march=x86-64 -mtune=generic",
    "CXXFLAGS": "-O2 -fPIC -march=x86-64 -mtune=generic",
    "LDFLAGS": "-Wl,-rpath,/opt/nerdtools/lib64 -Wl,-rpath,/opt/nerdtools/lib",
    "LD_LIBRARY_PATH": "/opt/nerdtools/lib64:/opt/nerdtools/lib",
    "PKG_CONFIG_PATH": "/opt/nerdtools/lib64/pkgconfig:/opt/nerdtools/lib/pkgconfig:/opt/nerdtools/share/pkgconfig",
    "GI_TYPELIB_PATH": "/opt/nerdtools/lib64/girepository-1.0",
    "DOTNET_GENERATE_ASPNET_CERTIFICATE": "false",
    "DOTNET_CLI_HOME": "/tmp/nerdtools-dotnet",
}




def build_order(packages):
    by_name = {p["name"]: p for p in packages}
    if len(by_name) != len(packages):
        raise ValueError("Duplicate package name")
    active, complete, result = set(), set(), []

    def visit(name):
        if name not in by_name:
            raise ValueError(f"Unknown dependency: {name}")
        if name in active:
            raise ValueError(f"Dependency cycle: {name}")
        if name in complete:
            return
        active.add(name)
        package = by_name[name]
        for dependency in package.get("dependencies", []) + package.get("build_dependencies", []):
            visit(dependency)
        active.remove(name)
        complete.add(name)
        result.append(package)

    for name in by_name:
        visit(name)
    return result


def dependency_closure(packages, name):
    ordered = build_order(packages)
    by_name = {package["name"]: package for package in ordered}
    if name not in by_name:
        raise ValueError(f"Unknown package: {name}")
    needed = set()

    def include(name):
        if name in needed:
            return
        needed.add(name)
        package = by_name[name]
        for dependency in package["dependencies"] + package["build_dependencies"]:
            include(dependency)

    include(name)
    return [package for package in ordered if package["name"] in needed]


def recipe_inventory(root):
    inventory = {}
    for path in sorted(root.rglob("*")):
        name = str(path.relative_to(root))
        if path.is_symlink():
            target = os.readlink(path)
            package_root = root / path.relative_to(root).parts[0]
            if package_root.is_symlink() or PurePosixPath(target).is_absolute() or not (path.parent / target).resolve().is_relative_to(package_root.resolve()):
                raise ValueError(f"Recipe sidecar link escapes its package: {name}")
            inventory[name] = {"type": "symlink", "target": target}
        elif path.is_dir():
            inventory[name] = {"type": "directory"}
        elif path.is_file():
            inventory[name] = {"type": "file", "sha256": digest(path), "executable": bool(path.stat().st_mode & 0o111)}
        else:
            raise ValueError(f"Unsupported recipe sidecar: {name}")
    return inventory


def validate_recipe_inventory():
    expected = json.loads((REPO / "build/recipe-files.json").read_text())
    actual = recipe_inventory(REPO / "build/recipes")
    if actual != expected:
        changed = sorted(name for name in set(actual) | set(expected) if actual.get(name) != expected.get(name))
        raise ValueError("Recipe input inventory mismatch: " + ", ".join(changed))


def load_packages():
    validate_recipe_inventory()
    validate_input(BASELINE["image"])
    validate_toolchain(REPO / "build/locks/toolchain.json")
    packages = []
    for filename in ("slackware.json", "slackbuilds.json"):
        data = json.loads((REPO / "build/locks" / filename).read_text())
        if not isinstance(data, list):
            raise ValueError(f"{filename} must contain a package array")
        packages.extend(data)
    for package in packages:
        if not isinstance(package, dict):
            raise ValueError("Package lock entries must be objects")
        for key in ("name", "version", "recipe", "recipe_sha256", "inputs", "dependencies", "build_dependencies", "provenance", "smoke"):
            if key not in package:
                raise ValueError(f"Missing {key}: {package.get('name')}")
        if any(not isinstance(package[key], str) for key in ("name", "version", "recipe", "recipe_sha256")) or not NAME.fullmatch(package["name"]) or not NAME.fullmatch(package["version"]):
            raise ValueError("Unsafe package name or version")
        recipe = checked_path(package["recipe"])
        if recipe.parts[:2] != ("build", "recipes") or recipe.suffix != ".SlackBuild":
            raise ValueError(f"Unexpected recipe path: {recipe}")
        if not SHA256.fullmatch(package["recipe_sha256"]) or digest(REPO / recipe) != package["recipe_sha256"]:
            raise ValueError(f"Recipe SHA256 mismatch: {recipe}")
        if not isinstance(package["inputs"], list) or not package["inputs"] or not isinstance(package["smoke"], list):
            raise ValueError(f"Missing recipe inputs/smoke definition: {package['name']}")
        filenames = set()
        for item in package["inputs"]:
            validate_input(item)
            if item["file"] in filenames:
                raise ValueError(f"Duplicate input filename: {package['name']}/{item['file']}")
            filenames.add(item["file"])
            sidecar = REPO / recipe.parent / item["file"]
            if sidecar.exists() or sidecar.is_symlink():
                raise ValueError(f"Input overwrites a checked recipe sidecar: {package['name']}/{item['file']}")
        for key in ("dependencies", "build_dependencies"):
            if not isinstance(package[key], list) or any(not isinstance(name, str) or not NAME.fullmatch(name) for name in package[key]):
                raise ValueError(f"Invalid dependency list: {package['name']}")
        if package["provenance"]["kind"] not in ("source", "upstream-binary"):
            raise ValueError("Unknown provenance kind")
        for command in package["smoke"]:
            if not isinstance(command, list) or not command or any(not isinstance(arg, str) or "\0" in arg for arg in command):
                raise ValueError(f"Invalid smoke command: {package['name']}")
    catalog_names = {p["name"] for p in packages if p.get("catalog", True)}
    expected = set(BASELINE["tools"])
    provided_names = {p["name"] for p in BASELINE["provided_packages"]}
    if catalog_names & provided_names or not expected.issubset(catalog_names | provided_names):
        raise ValueError(f"Catalog coverage mismatch: missing={sorted(expected - catalog_names - provided_names)}")
    if any(p["name"] not in expected and p.get("catalog", True) and not p.get("internal", False) for p in packages):
        raise ValueError("Additional runtime packages must be marked internal:true")
    for package in packages:
        if any(name not in catalog_names for name in package["dependencies"]):
            raise ValueError(f"Runtime dependency is not published: {package['name']}")
    return build_order(packages)








def inspect_package(archive, tool):
    with tarfile.open(archive) as tar:
        members = tar.getmembers()
        regular = {checked_path(member.name) for member in members if member.isfile()}
        seen = set()
        locate_binaries = {PRIVATE / "bin" / name for name in ("plocate", "mlocate")}
        locate_databases = {PRIVATE / "var/lib" / name for name in ("plocate", "mlocate")}
        links = set()
        for member in members:
            path = checked_path(member.name)
            if not re.fullmatch(r"[A-Za-z0-9_+.,/ @%()-]+", member.name) or path in seen:
                raise ValueError(f"Unsupported or duplicate package path: {member.name}")
            seen.add(path)
            check_destination(tool, member, path)
            if member.isdir():
                safe_directory(member)
            if member.name.lstrip("./") in INTEGRATION_DIGESTS:
                if not member.isfile() or hashlib.file_digest(tar.extractfile(member), "sha256").hexdigest() != INTEGRATION_DIGESTS[member.name.lstrip("./")]:
                    raise ValueError(f"Unapproved privileged integration file: {member.name}")
            if not (member.isdir() or member.isfile() or member.issym() or member.islnk()):
                raise ValueError(f"Special file in package: {member.name}")
            secure_locate = path in locate_binaries and member.isfile()
            private_database = path in locate_databases and member.isdir()
            if (secure_locate or private_database) and tool != path.name:
                raise ValueError(f"Locate ownership belongs to another tool: {member.name}")
            if not member.issym() and member.mode & 0o1002:
                raise ValueError(f"Unsafe writable/sticky package member: {member.name}")
            if secure_locate or private_database:
                mode = 0o2711 if secure_locate else 0o750
                if member.uid != 0 or member.gid != 21 or member.mode & 0o7777 != mode:
                    raise ValueError(f"Invalid secure locate ownership/mode: {member.name}")
            elif member.uid != 0 or member.gid != 0:
                raise ValueError(f"Unexpected package ownership: {member.name}")
            if member.mode & stat.S_ISUID or (member.mode & stat.S_ISGID and not secure_locate):
                raise ValueError(f"Unapproved privileged file in package: {member.name}")
            if member.issym() or member.islnk():
                if not re.fullmatch(r"[A-Za-z0-9_+.,/ @%()-]+", member.linkname):
                    raise ValueError(f"Unsupported link target: {member.name}")
                if member.islnk() and checked_path(member.linkname) not in regular:
                    raise ValueError(f"Hardlink must name a regular private archive member: {member.name}")
                target = PurePosixPath(member.linkname) if member.islnk() else path.parent / member.linkname
                normalized = PurePosixPath(os.path.normpath(str(target)))
                if normalized.is_absolute():
                    normalized = PurePosixPath(str(normalized).lstrip("/"))
                if not normalized.is_relative_to(PRIVATE) or ".." in normalized.parts:
                    raise ValueError(f"Package link escapes private prefix: {member.name}")
                links.add(path)
        for member in members:
            if any(parent in links for parent in checked_path(member.name).parents):
                raise ValueError(f"Archive traverses a symlink: {member.name}")
        if not any(m.isfile() and m.size and checked_path(m.name).is_relative_to(PRIVATE) for m in members):
            raise ValueError("Empty package payload")
        return members


def canonical_tar(directory, destination, preserve_groups=False):
    groups = [] if preserve_groups else ["--group=0"]
    run(["tar", "--sort=name", "--format=gnu", f"--mtime=@{EPOCH}", "--owner=0", *groups, "--numeric-owner", "--create", "--file", destination, "--use-compress-program=xz -T1 -9", "--directory", directory, "."])


def config_hook(paths):
    for path in paths:
        checked_path(path)
        if not re.fullmatch(r"(?:opt/nerdtools/|etc/)[A-Za-z0-9_+./-]+\.new", path):
            raise ValueError(f"Invalid no-clobber configuration path: {path}")
    script = 'config() {\n  NEW="$1"\n  OLD="${NEW%.new}"\n  if [ ! -e "$OLD" ] && [ ! -L "$OLD" ]; then mv "$NEW" "$OLD"; elif cmp -s "$OLD" "$NEW"; then rm "$NEW"; fi\n}\n'
    return script + "".join("config " + path + "\n" for path in sorted(paths))


def normalize_package(package, original, output, staging):
    inspect_package(original, package["name"])
    with tarfile.open(original) as tar:
        tar.extractall(staging, filter="fully_trusted")
    install = staging / "install"
    if install.exists():
        for entry in install.iterdir():
            if entry.name != "slack-desc":
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
    description = install / "slack-desc"
    if description.exists():
        description.write_text(re.sub(r"^" + re.escape(package["name"]) + ":", "nerdtools-" + package["name"] + ":", description.read_text(), flags=re.MULTILINE))
    launchers = {}
    for directory in ("bin", "sbin"):
        bindir = staging / "opt/nerdtools" / directory
        if not bindir.exists():
            continue
        for executable in sorted(bindir.iterdir()):
            if executable.is_dir() or (not executable.is_symlink() and not os.access(executable, os.X_OK)):
                continue
            if not NAME.fullmatch(executable.name):
                raise ValueError(f"Unsafe launcher name: {executable.name}")
            target = f"/opt/nerdtools/{directory}/{executable.name}"
            if executable.name in launchers:
                raise ValueError(f"Ambiguous launcher name: {executable.name}")
            launchers[executable.name] = target
    for name, target in launchers.items():
        launcher = staging / "usr/local/bin" / ("nerdtools-" + name)
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text('#!/bin/sh\nexport PATH=/opt/nerdtools/bin:/opt/nerdtools/sbin:$PATH\nexport LD_LIBRARY_PATH=/opt/nerdtools/lib64:/opt/nerdtools/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}\nexport GI_TYPELIB_PATH=/opt/nerdtools/lib64/girepository-1.0${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}\nexec ' + shlex.quote(target) + ' "$@"\n')
        launcher.chmod(0o755)
    configs = sorted(str(path.relative_to(staging)) for path in staging.rglob("*.new") if path.is_file() and not path.is_symlink())
    if configs:
        install.mkdir(exist_ok=True)
        (install / "doinst.sh").write_text(config_hook(configs))
    destination = output / artifact_name(package)
    canonical_tar(staging, destination, preserve_groups=True)
    return destination




def safe_directory(member):
    mode = member.mode & 0o7777
    if mode & 0o7022 or mode & 0o700 != 0o700:
        raise ValueError(f"Unsafe new directory permissions: {member.name}")


def install_payload(archive, root):
    # These are our already audited, normalized archives, not upstream binaries.
    with tarfile.open(archive) as tar:
        members = [m for m in tar.getmembers() if checked_path(m.name).parts[:1] != ("install",)]
        for member in members:
            path = contained(root, member.name)
            if member.isdir():
                safe_directory(member)
                if path.exists():
                    if path.is_symlink() or not path.is_dir():
                        raise ValueError(f"Directory collision: {member.name}")
                else:
                    path.mkdir(parents=True, mode=member.mode & 0o777)
                    os.chown(path, member.uid, member.gid)
                    os.chmod(path, member.mode & 0o777)
                continue
            if path.exists() or path.is_symlink():
                raise ValueError(f"Package file collision: {member.name}")
        tar.extractall(root, members=[member for member in members if not member.isdir()], filter="fully_trusted")
    for member in members:
        if member.isfile() and member.name.endswith(".new"):
            path = contained(root, member.name)
            original = path.with_name(path.name[:-4])
            if not original.exists() and not original.is_symlink():
                path.rename(original)




def go_root(root):
    binaries = list(root.glob("usr/lib64/go*/go/bin/go"))
    if len(binaries) != 1 or not binaries[0].is_file():
        raise ValueError("Expected exactly one Go toolchain in the development root")
    return "/" + str(binaries[0].parent.parent.relative_to(root))


def build_package(package, development, cache, built, output, work):
    with tempfile.TemporaryDirectory(prefix=package["name"] + "-", dir=work) as temporary:
        temporary = Path(temporary)
        temporary.chmod(0o711)
        root = temporary / "root"
        clone_root(development, root)
        needed = set()

        def add(name):
            if name in needed:
                return
            needed.add(name)
            dependency, artifact = built[name]
            for child in dependency["dependencies"] + dependency["build_dependencies"]:
                add(child)
            install_payload(artifact, root)

        for dependency in package["dependencies"] + package["build_dependencies"]:
            add(dependency)
        recipe = REPO / package["recipe"]
        destination = root / "work/recipe"
        shutil.copytree(recipe.parent, destination, symlinks=True)
        for item in package["inputs"]:
            shutil.copyfile(fetch(item, cache), destination / item["file"])
        (root / "out").mkdir(exist_ok=True)
        goroot = go_root(root)
        environment = {**ENV, "GOROOT": goroot, "PATH": goroot + "/bin:" + ENV["PATH"]}
        with mapped_root(root):
            run_sandbox(root, ["/bin/bash", recipe.name], environment, cwd="/work/recipe", timeout=18000)
        results = list((root / "out").glob("*.txz"))
        if len(results) != 1:
            raise ValueError(f"Expected exactly one built package for {package['name']}, found {len(results)}")
        staging = temporary / "staging"
        staging.mkdir()
        return normalize_package(package, results[0], output, staging)


def audit_runtime(root):
    checked = 0
    inspector = ["/usr/bin/setpriv", "--reuid=100000", "--regid=100000", "--clear-groups", "--no-new-privs",
                 "--bounding-set=-all", "--inh-caps=-all", "--ambient-caps=-all", "/usr/bin/readelf"]
    inspector_env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "HOME": "/nonexistent"}
    with mapped_root(root):
        for path in sorted((root / "opt/nerdtools").rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            with path.open("rb") as stream:
                if stream.read(4) != b"\x7fELF":
                    continue
            header = run([*inspector, "--file-header", path], env=inspector_env, capture_output=True, text=True).stdout
            if "Advanced Micro Devices X86-64" not in header:
                raise ValueError(f"Wrong ELF architecture: {path}")
            dynamic = run([*inspector, "--dynamic", path], env=inspector_env, capture_output=True, text=True).stdout
            if "(NEEDED)" in dynamic:
                relative = "/" + str(path.relative_to(root))
                run_sandbox(root, ["/lib64/ld-linux-x86-64.so.2", "--list", relative], ENV, timeout=60, read_only=True)
            checked += 1
    return checked


def kernel_report(log, token, commands):
    pattern = r"^NERDTOOLS_SMOKE_PASSED:" + re.escape(token) + ":" + str(commands) + r":([A-Za-z0-9_.+-]+)\r?$"
    identities = re.findall(pattern, log, re.MULTILINE)
    if len(identities) != 1:
        raise ValueError("Target kernel boot or protected package completion evidence failed")
    return identities[0]


def kernel_smoke(root, kernel, packages, work, log_path):
    token = secrets.token_hex(32)
    commands = [command for package in packages if package.get("catalog", True) for command in package["smoke"]]
    script = ["#!/bin/bash", "set -euo pipefail", "umask 077",
              "failed() { echo NERDTOOLS_SMOKE_FAILED; /sbin/reboot -f; while :; do sleep 1; done; }",
              "trap failed EXIT", "/sbin/mount -t proc proc /proc", "/sbin/mount -t sysfs sys /sys",
              "/sbin/mount -t devtmpfs dev /dev", "exec </dev/console >/dev/console 2>&1",
              # A booted Unraid provides these; .NET's named synchronization objects need /dev/shm.
              "mkdir -p /dev/shm /dev/pts", "/sbin/mount -t tmpfs -o mode=1777 shm /dev/shm",
              "/sbin/mount -t devpts devpts /dev/pts",
              "kernel=$(/usr/bin/uname -r)", ": > /root/nerdtools-completion"]
    # Unraid runs everything as root, so the tools are exercised as root; the VM has no disks, NIC or host mounts.
    child = ["/usr/bin/env", "-i"]
    child_env = {**ENV, "HOME": "/tmp/nerdtools-smoke", "DOTNET_CLI_HOME": "/tmp/nerdtools-smoke/dotnet"}
    child.extend(f"{key}={value}" for key, value in child_env.items())
    for index, command in enumerate(commands):
        script.append("printf '%s\\n' " + shlex.quote("Testing " + shlex.join(command)))
        # Keep going so one boot reports every failing command; a missing completion record still fails the gate.
        script.append(shlex.join([*child, "/usr/bin/timeout", "120", *command])
                      + " && printf '%s\\n' " + shlex.quote(str(index) + ":0") + " >> /root/nerdtools-completion"
                      + " || printf '%s\\n' " + shlex.quote("NERDTOOLS_COMMAND_FAILED " + shlex.join(command)))
    script.extend(["completed=0", 'while IFS= read -r status; do [ "$status" = "$completed:0" ]; completed=$((completed + 1)); done < /root/nerdtools-completion',
                   f'[ "$completed" = "{len(commands)}" ]',
                   # Tools may end output without a newline or with terminal escapes; start the marker on its own line.
                   "printf '\\nNERDTOOLS_SMOKE_PASSED:%s:%s:%s\\n' " + shlex.quote(token) + ' "$completed" "$kernel"',
                   "trap - EXIT", "/sbin/reboot -f", "while :; do sleep 1; done"])
    home = root / "tmp/nerdtools-smoke"
    if home.is_symlink():
        raise ValueError("Smoke home cannot be a symlink")
    home.mkdir(mode=0o700, exist_ok=True)
    home.chmod(0o700)
    private = root / "root"
    if private.is_symlink():
        raise ValueError("Protected harness state cannot be a symlink")
    private.mkdir(exist_ok=True)
    private.chmod(0o700)
    init = root / "nerdtools-init"
    init.write_text("\n".join(script) + "\n")
    init.chmod(0o700)
    initramfs = work / "target-test-initramfs.zst"
    with initramfs.open("wb") as stream, tempfile.TemporaryFile() as files:
        files.write(b".\0")
        for directory, dirs, names in os.walk(root, followlinks=False):
            dirs.sort()
            for name in sorted(dirs + names):
                files.write(str((Path(directory) / name).relative_to(root)).encode() + b"\0")
        files.seek(0)
        archive = subprocess.Popen(["cpio", "--create", "--null", "--format=newc", "--reproducible", "--quiet"], stdin=files, stdout=subprocess.PIPE, cwd=root)
        try:
            run(["zstd", "--compress", "--stdout", "--quiet", "-T1", "-3"], stdin=archive.stdout, stdout=stream)
        finally:
            archive.stdout.close()
        if archive.wait() != 0:
            raise RuntimeError("Test initramfs creation failed")
    kvm = os.access("/dev/kvm", os.R_OK | os.W_OK)
    # The whole target root, including Unraid's /usr layer, is the initramfs: ramfs lifts tmpfs's half-of-RAM cap.
    command = ["qemu-system-x86_64", "-machine", "q35", "-accel", "kvm" if kvm else "tcg,thread=multi",
               "-cpu", "host" if kvm else "max", "-smp", "2", "-m", "12288",
               "-kernel", kernel, "-initrd", initramfs,
               "-append", "console=ttyS0 rdinit=/nerdtools-init rootfstype=ramfs panic=1",
               "-display", "none", "-serial", "stdio", "-monitor", "none", "-nic", "none", "-no-reboot"]
    try:
        result = run(command, check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=1800)
    except subprocess.TimeoutExpired as error:
        log = error.stdout or b""
        if isinstance(log, bytes):
            log = log.decode(errors="replace")
        log_path.write_text(log)
        print(log, flush=True)
        raise
    log_path.write_text(result.stdout)
    print(result.stdout, flush=True)
    if result.returncode:
        raise ValueError(f"Target QEMU boot failed with exit status {result.returncode}")
    return kernel_report(result.stdout, token, len(commands))


def asset_record(package, asset):
    record = {"name": "nerdtools-" + package["name"], "tool": package["name"],
              "version": package["version"].replace("-", "_"), "file": asset.name,
              "sha256": digest(asset), "size": asset.stat().st_size,
              "dependencies": ["nerdtools-" + name for name in package["dependencies"]]}
    if package.get("internal", False):
        record["internal"] = True
    return record


def family(version):
    return ".".join(version.split(".")[:2])


def version_key(version):
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-(beta|rc)\.?(\d+))?", version)
    if not match:
        raise ValueError(f"Unsupported Unraid version format: {version}")
    return (*map(int, match.group(1, 2, 3)), {"beta": 0, "rc": 1, None: 2}[match.group(4)], int(match.group(5) or 0))


def catalog(records, target):
    # One package set is built on the oldest baseline. A later family is offered from the first
    # version, prereleases included, that actually passed the gates.
    first = BASELINE["version"] if family(target) == BASELINE["family"] else target
    return {"schema": 1, "min_unraid": first, "family": family(target),
            "packages": records, "external_packages": BASELINE["external_packages"],
            "provided_packages": BASELINE["provided_packages"]}




def package_commands(archive):
    with tarfile.open(archive) as tar:
        return sorted(checked_path(member.name).name.removeprefix("nerdtools-") for member in tar.getmembers()
                      if member.isfile() and checked_path(member.name).parent == PurePosixPath("usr/local/bin")
                      and checked_path(member.name).name.startswith("nerdtools-"))


def source_bundle(packages, cache, destination, work):
    with tempfile.TemporaryDirectory(prefix="corresponding-sources-", dir=work) as temporary:
        staging = Path(temporary)
        shutil.copytree(REPO / "build", staging / "build", symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copyfile(REPO / "LICENSE", staging / "LICENSE")
        workflow = staging / ".github/workflows"
        workflow.mkdir(parents=True)
        for name in ("build-packages.yml", "package-build.yml"):
            shutil.copyfile(REPO / ".github/workflows" / name, workflow / name)
        (staging / "scripts").mkdir()
        shutil.copyfile(REPO / "scripts/package-change.py", staging / "scripts/package-change.py")
        for package in packages:
            directory = staging / "sources" / package["name"]
            directory.mkdir(parents=True)
            for item in package["inputs"]:
                shutil.copyfile(fetch(item, cache), directory / item["file"])
        canonical_tar(staging, destination)


def seed_sources(packages, bundle, cache):
    cache.mkdir(parents=True, exist_ok=True)
    with tarfile.open(bundle) as tar:
        members = {checked_path(member.name): member for member in tar.getmembers()}
        for package in packages:
            for item in package["inputs"]:
                if not SHA256.fullmatch(item["sha256"]):
                    raise ValueError("Invalid source SHA256 pin")
                member = members.get(PurePosixPath("sources") / package["name"] / item["file"])
                if member is None:
                    continue
                if not member.isfile():
                    raise ValueError("Retained source input must be a regular file")
                with tempfile.NamedTemporaryFile(dir=cache, delete=False) as stream:
                    path = Path(stream.name)
                    try:
                        shutil.copyfileobj(tar.extractfile(member), stream)
                        stream.flush()
                        if digest(path) != item["sha256"]:
                            raise ValueError(f"Retained source SHA256 mismatch: {item['file']}")
                        path.replace(cache / item["sha256"])
                    finally:
                        path.unlink(missing_ok=True)


def publication_blockers(packages):
    hashes = {item["sha256"] for package in packages for item in package["inputs"]}
    policy = json.loads((REPO / "build/source-blockers.json").read_text())
    blocked = [entry["module"] for entry in policy if hashes.intersection(entry["sha256"])]
    blocked.extend(package["name"] + ": source build required" for package in packages
                   if package.get("catalog", True) and package.get("provenance", {}).get("kind") == "upstream-binary")
    return blocked


def asset_limits(output):
    assets = [path for path in output.iterdir() if path.is_file()]
    if len(assets) >= 1000 or any(path.stat().st_size >= 2**31 for path in assets):
        raise ValueError("GitHub release asset limit exceeded: require fewer than1000 assets, each smaller than2GiB")


def require_provided(root):
    records = list((root / "var/lib/pkgtools/packages").iterdir())
    for package in BASELINE["provided_packages"]:
        prefix = package["name"] + "-"
        versions = [path.name[len(prefix):].split("-", 1)[0] for path in records if path.name.startswith(prefix)]
        minimum = tuple(map(int, package["version"].split(".")))
        if not any(re.fullmatch(r"[0-9.]+", version) and tuple(map(int, version.split("."))) >= minimum for version in versions):
            raise ValueError(f"Missing baseline-provided {package['name']} >= {package['version']}")


def require_slocate_group(root):
    groups = (root / "etc/group").read_text().splitlines()
    if not any(line.startswith("slocate:") and line.split(":")[2] == "21" for line in groups):
        raise ValueError("Target slocate group must match the pinned baseline GID21")


def sandbox_check(args):
    if sys.platform != "linux" or os.geteuid() != 0 or not args.development_root:
        raise ValueError("Sandbox probe requires Linux root and --development-root")
    args.work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sandbox-probe-", dir=args.work) as temporary:
        work = Path(temporary)
        work.chmod(0o711)
        outside = work / "host-only"
        outside.write_text("unchanged")
        root = work / "root"
        clone_root(args.development_root, root)
        probe = ('set -eu; read -r inner outer count < /proc/self/uid_map; '
                 '[ "$inner:$outer:$count" = "0:100000:65536" ]; '
                 '[ "$(/usr/bin/id -u)" = 0 ]; [ "$(/usr/bin/id -g)" = 0 ]; '
                 '[ ! -e "$1" ]; if printf changed > "$1"; then exit 1; fi; '
                 'mkdir -p /tmp/sandbox-mount; if /sbin/mount -t tmpfs tmpfs /tmp/sandbox-mount; then exit 1; fi; '
                 'touch /tmp/group-proof; chown 0:21 /tmp/group-proof; '
                 '[ "$(stat -c %u:%g /tmp/group-proof)" = "0:21" ]')
        with mapped_root(root):
            run_sandbox(root, ["/bin/bash", "-c", probe, "_", str(outside)], ENV, timeout=120)
            run_sandbox(root, ["/bin/bash", "-c", 'if printf forbidden > /etc/nerdtools-readonly-probe; then exit 1; fi; test ! -e /etc/nerdtools-readonly-probe'], ENV, timeout=120, read_only=True)
        if outside.read_text() != "unchanged":
            raise ValueError("Sandbox modified an outside host file")
    print("Sandbox identity mapping, host-path exclusion and capability probe passed.")


def prepared_roots(baseline_root, development_root):
    baseline, development = baseline_root.resolve(), development_root.resolve()
    if baseline.parent != development.parent or baseline.name != "baseline" or development.name != "development":
        raise ValueError("Prepared roots must come from the same bootstrap workspace")
    evidence = json.loads((baseline.parent / "bootstrap.json").read_text())
    expected = {"version": BASELINE["version"], "image_sha256": BASELINE["image"]["sha256"],
                "bzroot_sha256": BASELINE["bzroot_sha256"],
                "toolchain_sha256": digest(REPO / "build/locks/toolchain.json")}
    if evidence != expected:
        raise ValueError("Prepared roots do not match the pinned baseline and toolchain")
    return baseline, development, baseline.parent / "inputs"


def build_one(args):
    """Check one dependency closure; retain only its normalized package, never release metadata."""
    if args.publish:
        raise ValueError("build-one cannot publish; use the full-catalog build compatibility gate")
    if not args.package or not args.baseline_root or not args.development_root:
        raise ValueError("build-one requires --package, --baseline-root and --development-root")
    if os.geteuid() != 0 or sys.platform != "linux" or os.uname().machine != "x86_64":
        raise ValueError("Builds require a disposable Linux x86_64 CI runner, running as root")
    packages = dependency_closure(load_packages(), args.package)
    baseline, development, cache = prepared_roots(args.baseline_root, args.development_root)
    require_slocate_group(baseline)
    require_provided(baseline)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Output directory must be empty")
    args.work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nerdtools-one-", dir=args.work) as temporary:
        work = Path(temporary)
        work.chmod(0o711)
        dependencies = work / "dependencies"
        dependencies.mkdir()
        if args.source_bundle:
            seed_sources(packages, args.source_bundle, cache)
        # An unchanged dependency is taken from the previous release (checksum verified by the workflow)
        # instead of being compiled again; the package itself is always compiled.
        reuse = getattr(args, "reuse", None)
        reused = {p["name"]: reuse / artifact_name(p) for p in packages if reuse and p["name"] != args.package
                  and (reuse / artifact_name(p)).is_file() and not (reuse / artifact_name(p)).is_symlink()}
        for package in packages:
            if package["name"] not in reused:
                for item in package["inputs"]:
                    fetch(item, cache)
        built = {}
        for package in packages:
            if package["name"] in reused:
                artifact = dependencies / reused[package["name"]].name
                shutil.copyfile(reused[package["name"]], artifact)
            else:
                target_output = output if package["name"] == args.package else dependencies
                artifact = build_package(package, development, cache, built, target_output, work)
            built[package["name"]] = (package, artifact)
        print(f"Compiled {len(packages) - len(reused)} package(s); reused {len(reused)} unchanged dependencies.", flush=True)
        # Verify this package alone, with only its runtime closure installed, so package jobs fail independently.
        runtime, pending = {}, [args.package]
        while pending:
            name = pending.pop()
            if name not in runtime:
                runtime[name] = built[name][0]
                pending.extend(runtime[name]["dependencies"])
        target = work / "target"
        target.mkdir()
        kernel = work / "target-bzimage"
        prepare_image(BASELINE["image"], BASELINE["version"], cache, target, kernel=kernel)
        for name in runtime:
            install_payload(built[name][1], target)
        audit_runtime(target)
        kernel_smoke(target, kernel, list(runtime.values()), work, work / "kernel-smoke.log")
    print(f"Built and smoke-tested {args.package} with its runtime closure; no full-catalog compatibility or publication claim.")


# Raise to deliberately recompile every package, e.g. after a builder change that alters package contents.
# Ordinary edits to the build scripts do not rebuild anything (like a Debian binNMU, a rebuild is a decision).
REBUILD_GENERATION = 1


def build_keys(packages):
    """Identity of the inputs that make a package's bytes; equal keys allow carrying a binary over."""
    shared = hashlib.sha256(str(REBUILD_GENERATION).encode())
    for path in ("build/baseline.json", "build/locks/toolchain.json", "build/unraid-build-root/bootstrap.py"):
        shared.update(digest(REPO / path).encode())
    inventory = recipe_inventory(REPO / "build/recipes")
    keys = {}
    for package in build_order(packages):
        directory = Path(package["recipe"]).parent.name + "/"
        identity = [shared.hexdigest(), {key: value for key, value in package.items() if key != "smoke"},
                    {name: entry for name, entry in inventory.items() if name.startswith(directory)},
                    sorted(keys[name] for name in package["dependencies"] + package["build_dependencies"])]
        keys[package["name"]] = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return keys


def carry_plan(packages, previous_keys, previous_catalog):
    """Split catalog packages into those to compile and those reusable from a published release,
    and choose each package's BUILD number."""
    keys = build_keys(packages)
    published = {record["tool"]: record for record in previous_catalog.get("packages", [])}
    plan = {"build": [], "reuse": [], "builds": {}}
    for package in packages:
        if not package.get("catalog", True):
            continue  # build-only helpers are compiled inside each dependent's own job
        name = package["name"]
        record = published.get(name) or {}
        previous = re.fullmatch(rf"nerdtools-{re.escape(name)}-(.+)-x86_64-([1-9][0-9]*)_NT73\.txz", str(record.get("file", "")))
        same_version = previous and previous.group(1) == package["version"].replace("-", "_")
        if (same_version and previous_keys.get(name) == keys[name]
                and SHA256.fullmatch(str(record.get("sha256", "")))):
            plan["builds"][name] = int(previous.group(2))
            plan["reuse"].append({"name": name, "file": record["file"], "sha256": record["sha256"]})
        else:
            plan["builds"][name] = int(previous.group(2)) + 1 if same_version else 1
            plan["build"].append(name)
    return plan


def checked_artifacts(packages, directory):
    expected = {artifact_name(package): package for package in packages}
    entries = list(directory.iterdir())
    required = {name for name, package in expected.items() if package.get("catalog", True)}
    names = {entry.name for entry in entries}
    if (len(expected) != len(packages) or not required <= names <= set(expected)
            or any(entry.is_symlink() or not entry.is_file() for entry in entries)):
        raise ValueError("Incomplete or unexpected package artifact set")
    return {expected[entry.name]["name"]: entry for entry in entries}

CANDIDATE_IDENTITY_FIELDS = ("repository", "pr", "head", "tree", "workflow", "run", "attempt", "artifact_name")
CANDIDATE_MANIFEST_FIELDS = frozenset((
    "schema", *CANDIDATE_IDENTITY_FIELDS, "baseline_version", "baseline_image_sha256",
    "toolchain_sha256", "rebuild_generation", "source_date_epoch", "packages"))
CANDIDATE_PACKAGE_FIELDS = frozenset(("name", "build", "build_key", "file", "sha256", "size"))


def _candidate_identity(identity):
    if not isinstance(identity, dict) or set(identity) != set(CANDIDATE_IDENTITY_FIELDS):
        raise ValueError("Candidate identity has an invalid schema")
    repository, workflow = identity["repository"], identity["workflow"]
    if not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid candidate repository")
    if not isinstance(workflow, str) or not workflow or "\0" in workflow:
        raise ValueError("Invalid candidate workflow")
    for field in ("pr", "run", "attempt"):
        if type(identity[field]) is not int or identity[field] < 1:
            raise ValueError(f"Invalid candidate {field}")
    for field in ("head", "tree"):
        if not isinstance(identity[field], str) or not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", identity[field]):
            raise ValueError(f"Invalid candidate {field} SHA")
    if not isinstance(identity["artifact_name"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", identity["artifact_name"]):
        raise ValueError("Invalid candidate artifact name")
    return {field: identity[field] for field in CANDIDATE_IDENTITY_FIELDS}


def _candidate_plan(packages, plan):
    if (not isinstance(packages, list) or not isinstance(plan, dict)
            or set(plan) != {"build", "reuse", "builds"}):
        raise ValueError("Invalid candidate build plan")
    catalog = {}
    all_names = set()
    for package in packages:
        if (not isinstance(package, dict) or not isinstance(package.get("name"), str)
                or not NAME.fullmatch(package["name"]) or not isinstance(package.get("version"), str)
                or not NAME.fullmatch(package["version"])):
            raise ValueError("Invalid candidate package definition")
        name = package["name"]
        if name in all_names:
            raise ValueError(f"Duplicate candidate package: {name}")
        all_names.add(name)
        if package.get("catalog", True):
            catalog[name] = package

    build = plan["build"]
    reuse = plan["reuse"]
    builds = plan["builds"]
    if (not isinstance(build, list) or any(not isinstance(name, str) for name in build)
            or len(set(build)) != len(build) or not isinstance(reuse, list) or not isinstance(builds, dict)):
        raise ValueError("Invalid candidate build plan")
    reuse_names, reuse_files = set(), set()
    for record in reuse:
        if not isinstance(record, dict) or set(record) != {"name", "file", "sha256"}:
            raise ValueError("Invalid candidate reuse plan")
        name, filename, sha256 = record["name"], record["file"], record["sha256"]
        if (not isinstance(name, str) or not isinstance(filename, str)
                or not re.fullmatch(r"[A-Za-z0-9_.+-]+\.txz", filename)
                or not isinstance(sha256, str) or not SHA256.fullmatch(sha256)
                or name in reuse_names or filename in reuse_files):
            raise ValueError("Invalid candidate reuse plan")
        reuse_names.add(name)
        reuse_files.add(filename)

    build_names = set(build)
    if (not build_names or build_names & reuse_names
            or build_names | reuse_names != set(catalog)
            or set(builds) != set(catalog)
            or any(type(number) is not int or number < 1 for number in builds.values())):
        raise ValueError("Candidate plan does not partition the current catalog")
    for record in reuse:
        if record["file"] != artifact_name(catalog[record["name"]], builds[record["name"]]):
            raise ValueError("Candidate reuse package does not match the current build")
    expected_files = {}
    for name in sorted(build_names):
        filename = artifact_name(catalog[name], builds[name])
        if filename in expected_files.values() or filename in reuse_files:
            raise ValueError("Candidate and reuse package files overlap")
        expected_files[name] = filename
    return catalog, sorted(build_names), dict(sorted(builds.items())), expected_files


def _candidate_file_hashes(package_dir, filenames):
    try:
        directory_fd = os.open(package_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise ValueError("Candidate package directory is not a real directory") from error
    try:
        if set(os.listdir(directory_fd)) != set(filenames):
            raise ValueError("Candidate package files do not match the plan")
        hashes = {}
        for filename in sorted(filenames):
            try:
                file_fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                  dir_fd=directory_fd)
            except OSError as error:
                raise ValueError(f"Candidate package is not a regular file: {filename}") from error
            with os.fdopen(file_fd, "rb") as archive:
                info = os.fstat(archive.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError(f"Candidate package is not a regular file: {filename}")
                if info.st_size < 1:
                    raise ValueError(f"Candidate package is empty: {filename}")
                sha256 = hashlib.file_digest(archive, "sha256").hexdigest()
                final = os.fstat(archive.fileno())
                if not stat.S_ISREG(final.st_mode) or final.st_nlink != 1 or final.st_size != info.st_size:
                    raise ValueError(f"Candidate package changed while reading: {filename}")
                hashes[filename] = {"sha256": sha256, "size": final.st_size}
        return hashes
    except OSError as error:
        raise ValueError("Could not read candidate package files") from error
    finally:
        os.close(directory_fd)


def _candidate_inputs(packages, plan, package_dir):
    _, build_names, builds, filenames = _candidate_plan(packages, plan)
    keys = build_keys(packages)
    if not isinstance(keys, dict):
        raise ValueError("Invalid package build keys")
    for name in build_names:
        if not isinstance(keys.get(name), str) or not SHA256.fullmatch(keys[name]):
            raise ValueError(f"Invalid candidate build key: {name}")
    hashes = _candidate_file_hashes(package_dir, set(filenames.values()))
    return build_names, builds, filenames, keys, hashes


def candidate_manifest(packages, plan, package_dir, identity):
    """Seal the exact changed package set with its reuse keys and byte identities."""
    identity = _candidate_identity(identity)
    names, builds, filenames, keys, hashes = _candidate_inputs(packages, plan, package_dir)
    records = []
    for name in names:
        filename = filenames[name]
        records.append({"name": name, "build": builds[name], "build_key": keys[name],
                        "file": filename, **hashes[filename]})
    return {"schema": 1, **identity, "baseline_version": BASELINE["version"],
            "baseline_image_sha256": BASELINE["image"]["sha256"],
            "toolchain_sha256": digest(REPO / "build/locks/toolchain.json"),
            "rebuild_generation": REBUILD_GENERATION, "source_date_epoch": int(EPOCH),
            "packages": records}


def write_candidate_manifest(path, manifest):
    Path(path).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def verify_candidate_bundle(packages, plan, package_dir, manifest, expected):
    """Verify a complete candidate against the current plan and return workflow outputs."""
    expected = _candidate_identity(expected)
    if not isinstance(manifest, dict) or set(manifest) != CANDIDATE_MANIFEST_FIELDS:
        raise ValueError("Candidate manifest has an invalid schema")
    identity = _candidate_identity({field: manifest[field] for field in CANDIDATE_IDENTITY_FIELDS})
    if type(manifest["schema"]) is not int or manifest["schema"] != 1 or identity != expected:
        raise ValueError("Candidate identity mismatch")
    if (manifest["baseline_version"] != BASELINE["version"]
            or manifest["baseline_image_sha256"] != BASELINE["image"]["sha256"]
            or manifest["toolchain_sha256"] != digest(REPO / "build/locks/toolchain.json")
            or type(manifest["rebuild_generation"]) is not int or manifest["rebuild_generation"] != REBUILD_GENERATION
            or type(manifest["source_date_epoch"]) is not int or manifest["source_date_epoch"] < 0):
        raise ValueError("Candidate build environment mismatch")

    names, builds, filenames, keys, hashes = _candidate_inputs(packages, plan, package_dir)
    records = manifest["packages"]
    if (not isinstance(records, list) or len(records) != len(names)
            or any(not isinstance(record, dict) for record in records)
            or [record.get("name") for record in records] != names):
        raise ValueError("Candidate package set does not match the current plan")
    for record, name in zip(records, names):
        if (not isinstance(record, dict) or set(record) != CANDIDATE_PACKAGE_FIELDS
                or type(record["build"]) is not int or record["build"] != builds[name]
                or not isinstance(record["build_key"], str) or record["build_key"] != keys[name]
                or not isinstance(record["file"], str) or record["file"] != filenames[name]
                or not isinstance(record["sha256"], str) or not SHA256.fullmatch(record["sha256"])
                or type(record["size"]) is not int or record["size"] < 1):
            raise ValueError(f"Invalid candidate package record: {name}")
        actual = hashes[filenames[name]]
        if record["sha256"] != actual["sha256"] or record["size"] != actual["size"]:
            raise ValueError(f"Candidate package digest mismatch: {name}")
    for name in names:
        inspect_package(Path(package_dir) / filenames[name], name)
    return {"builds": builds, "files": sorted(filenames.values())}


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_json(path):
    return json.loads(Path(path).read_text(), object_pairs_hook=_json_object)


def assemble_candidate_artifacts(packages, expected_data, candidate_bundle, reuse_dir, destination):
    """Reverify candidate bytes, then combine them with exact checksummed reuse artifacts."""
    if not isinstance(expected_data, dict) or set(expected_data) != {"identity", "plan"}:
        raise ValueError("Expected candidate JSON must contain identity and plan")
    candidate_bundle = Path(candidate_bundle)
    manifest_path = candidate_bundle / "candidate-manifest.json"
    package_dir = candidate_bundle / "packages"
    if (candidate_bundle.is_symlink() or not candidate_bundle.is_dir()
            or {entry.name for entry in candidate_bundle.iterdir()} != {"candidate-manifest.json", "packages"}
            or manifest_path.is_symlink() or not manifest_path.is_file()):
        raise ValueError("Candidate artifact has an invalid layout")
    manifest = _read_json(manifest_path)
    result = verify_candidate_bundle(packages, expected_data["plan"], package_dir,
                                     manifest, expected_data["identity"])
    catalog, build_names, builds, candidate_filenames = _candidate_plan(packages, expected_data["plan"])
    reuse = expected_data["plan"]["reuse"]
    reuse_filenames = {record["file"] for record in reuse}
    reuse_hashes = _candidate_file_hashes(reuse_dir, reuse_filenames)
    for record in reuse:
        if reuse_hashes[record["file"]]["sha256"] != record["sha256"]:
            raise ValueError(f"Previous-release package digest mismatch: {record['file']}")

    expected_files = set(candidate_filenames.values()) | reuse_filenames
    if len(expected_files) != len(packages) - sum(not package.get("catalog", True) for package in packages):
        raise ValueError("Candidate and reuse packages do not cover the current catalog")
    destination = Path(destination)
    if destination.exists():
        if destination.is_symlink() or not destination.is_dir() or any(destination.iterdir()):
            raise ValueError("Candidate package destination must be an empty real directory")
    else:
        destination.mkdir(parents=True)
    manifest_records = {record["name"]: record for record in manifest["packages"]}
    sources = [(package_dir / candidate_filenames[name], candidate_filenames[name],
                manifest_records[name]["sha256"]) for name in build_names]
    sources.extend((Path(reuse_dir) / record["file"], record["file"], record["sha256"])
                   for record in reuse)
    for source, filename, sha256 in sources:
        target = destination / filename
        if target.exists() or target.is_symlink():
            raise ValueError(f"Refusing to overwrite package artifact: {filename}")
        shutil.copyfile(source, target)
        if digest(target) != sha256:
            raise ValueError(f"Package artifact changed while assembling: {filename}")
    if {entry.name for entry in destination.iterdir()} != expected_files:
        raise ValueError("Candidate and reuse packages do not form the complete catalog")
    for name in sorted(catalog):
        inspect_package(destination / artifact_name(catalog[name], builds[name]), name)
    return {"identity": expected_data["identity"], "manifest_sha256": digest(manifest_path),
            "builds": result["builds"], "files": sorted(expected_files)}


def _read_candidate_expected(path):
    expected = _read_json(path)
    if not isinstance(expected, dict) or set(expected) != {"identity", "plan"}:
        raise ValueError("Expected candidate JSON must contain identity and plan")
    return expected


def build(args):
    if os.geteuid() != 0 or sys.platform != "linux" or os.uname().machine != "x86_64":
        raise ValueError("Builds require a disposable Linux x86_64 CI runner, running as root")
    validate_target(args.unraid_version, args.unraid_url, args.unraid_sha256)
    if args.publish and version_key(args.unraid_version) < version_key(BASELINE["version"]):
        raise ValueError("Targets older than the build baseline cannot be published")
    fam = family(args.unraid_version)
    packages = load_packages()
    blockers = publication_blockers(packages)
    if args.publish and blockers:
        raise ValueError("Source redistribution permission remains unverified: " + ", ".join(blockers))
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Output directory must be empty")
    candidate_origin = None
    if getattr(args, "candidate_artifacts", None) is not None:
        if (args.package_artifacts is None or args.reuse_artifacts is None
                or args.candidate_expected_json is None):
            raise ValueError("Candidate builds require candidate, reuse, and expected artifacts")
        candidate_origin = assemble_candidate_artifacts(
            packages, _read_candidate_expected(args.candidate_expected_json), args.candidate_artifacts,
            args.reuse_artifacts, args.package_artifacts)
        shutil.copyfile(Path(args.candidate_artifacts) / "candidate-manifest.json",
                        output / "candidate-manifest.json")
    elif (getattr(args, "candidate_expected_json", None) is not None
          or getattr(args, "reuse_artifacts", None) is not None):
        raise ValueError("Candidate build options must be supplied together")
    args.work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nerdtools-", dir=args.work) as temporary:
        work = Path(temporary)
        work.chmod(0o711)
        lock = REPO / "build/locks/toolchain.json"
        if bool(args.baseline_root) != bool(args.development_root):
            raise ValueError("Both prepared root paths are required together")
        if args.baseline_root:
            baseline, development, cache = prepared_roots(args.baseline_root, args.development_root)
        else:
            cache = work / "inputs"
            baseline = work / "baseline"
            baseline.mkdir()
            prepare_image(BASELINE["image"], BASELINE["version"], cache, baseline, BASELINE["bzroot_sha256"])
            development = work / "development"
            clone_root(baseline, development)
            prepare_toolchain(development, cache, lock)
        require_slocate_group(baseline)
        require_provided(baseline)
        if args.source_bundle:
            seed_sources(packages, args.source_bundle, cache)
        for package in packages:
            for item in package["inputs"]:
                fetch(item, cache)
        built = {}
        records = []
        build_only = work / "build-only"
        build_only.mkdir()
        artifacts = checked_artifacts(packages, args.package_artifacts) if args.package_artifacts else None
        for package in packages:
            if artifacts is not None and package["name"] not in artifacts:
                continue  # build-only helper, not needed once its dependents are built
            target_output = output if package.get("catalog", True) else build_only
            if artifacts is None:
                artifact = build_package(package, development, cache, built, target_output, work)
            else:
                artifact = target_output / artifacts[package["name"]].name
                shutil.copyfile(artifacts[package["name"]], artifact)
            built[package["name"]] = (package, artifact)
            if package.get("catalog", True):
                record = asset_record(package, artifact)
                record["commands"] = package_commands(artifact)
                records.append(record)
        target = work / "target"
        target.mkdir()
        kernel = work / "target-bzimage"
        prepare_image({"url": args.unraid_url, "sha256": args.unraid_sha256, "file": f"unRAIDServer-{args.unraid_version}-x86_64.zip"}, args.unraid_version, cache, target, kernel=kernel)
        require_slocate_group(target)
        require_provided(target)
        for package in packages:
            if package.get("catalog", True):
                install_payload(built[package["name"]][1], target)
        checked = audit_runtime(target)
        if not checked:
            raise ValueError("No native ELF files were produced")
        actual_kernel = kernel_smoke(target, kernel, packages, work, output / f"kernel-smoke-{fam}.log")
        if args.publish:
            source_bundle(packages, cache, output / f"sources-{fam}.tar.xz", work)
        # Enough to reproduce: packages compiled here use this commit's date and BUILD numbers;
        # carried-over packages are byte-identical to, and documented by, the previous release.
        provenance = {"repository": os.environ.get("GITHUB_REPOSITORY", "local"), "commit": os.environ.get("GITHUB_SHA", "local"),
                      "source_date_epoch": int(EPOCH), "builds": {p["name"]: int(BUILDS.get(p["name"], 1)) for p in packages},
                      "carried_over_from": os.environ.get("NERDTOOLS_PREVIOUS_RELEASE") or None,
                      "baseline": BASELINE, "packages": packages,
                      "toolchain": json.loads((REPO / "build/locks/toolchain.json").read_text())}
        if candidate_origin:
            provenance["candidate_identity"] = candidate_origin["identity"]
            provenance["candidate_manifest_sha256"] = candidate_origin["manifest_sha256"]
        (output / f"provenance-{fam}.json").write_text(json.dumps(provenance, indent=2) + "\n")
        report = {"status": "passed", "target_version": args.unraid_version, "target_sha256": args.unraid_sha256,
                  "channel": args.channel, "baseline": BASELINE["version"], "elf_files_checked": checked,
                  "packages_checked": len(records), "publish_requested": args.publish,
                  "native_tools_checked": sum(not record.get("internal", False) for record in records),
                  "provided_tools_checked": len(BASELINE["provided_packages"]),
                  "target_kernel": actual_kernel,
                  "publication_blockers": blockers,
                  "scope": "Userspace ELF dependency/version audit and declared nonmutating smoke commands run as root, as on Unraid, booted under the exact target kernel in a VM without disks, NIC or host mounts. No physical hardware or daemon integration certification."}
        (output / f"compatibility-{fam}.json").write_text(json.dumps(report, indent=2) + "\n")
        (output / f"catalog-{fam}.json").write_text(json.dumps(catalog(records, args.unraid_version), indent=2) + "\n")
        (output / "build-keys.json").write_text(json.dumps(build_keys(packages), indent=2, sort_keys=True) + "\n")
        lines = [f"{digest(path)}  {path.name}" for path in sorted(output.iterdir()) if path.is_file()]
        (output / "SHA256SUMS").write_text("\n".join(lines) + "\n")
        asset_limits(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "build-one", "validate", "pin-recipes", "sandbox-check",
                                            "plan", "keys", "candidate-create", "candidate-verify"))
    parser.add_argument("--previous", type=Path, help="Directory holding a published release's build-keys.json and catalog (plan only)")
    parser.add_argument("--package", help="Package to build with its transitive dependencies (build-one only)")
    parser.add_argument("--unraid-version", default=BASELINE["version"])
    parser.add_argument("--unraid-url", default=BASELINE["image"]["url"])
    parser.add_argument("--unraid-sha256", default=BASELINE["image"]["sha256"])
    parser.add_argument("--channel", choices=("stable", "next"), default="stable")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--baseline-root", type=Path)
    parser.add_argument("--development-root", type=Path)
    parser.add_argument("--source-bundle", type=Path)
    parser.add_argument("--package-artifacts", type=Path, help="Same-run normalized package artifacts")
    parser.add_argument("--reuse", type=Path, help="Verified unchanged dependency packages from the previous release (build-one only)")
    parser.add_argument("--candidate-artifacts", type=Path, help="Same-run verified candidate artifact bundle (build only)")
    parser.add_argument("--candidate-expected-json", type=Path, help="Current plan and candidate identity (build only)")
    parser.add_argument("--reuse-artifacts", type=Path, help="Checksum-verified previous-release packages (candidate build only)")
    parser.add_argument("--work", type=Path, default=Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())))
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--expected-json", type=Path)
    parser.add_argument("--repository")
    parser.add_argument("--pr", type=int)
    parser.add_argument("--head")
    parser.add_argument("--tree")
    parser.add_argument("--workflow")
    parser.add_argument("--run", type=int)
    parser.add_argument("--attempt", type=int)
    parser.add_argument("--artifact-name")
    args = parser.parse_args()
    candidate_fields = (args.plan, args.manifest, args.expected_json, args.repository, args.pr,
                        args.head, args.tree, args.workflow, args.run, args.attempt, args.artifact_name)
    if args.command not in ("candidate-create", "candidate-verify") and any(value is not None for value in candidate_fields):
        parser.error("Candidate options require candidate-create or candidate-verify")
    candidate_build_fields = (args.candidate_artifacts, args.candidate_expected_json, args.reuse_artifacts)
    if any(value is not None for value in candidate_build_fields):
        if args.command != "build":
            parser.error("Candidate build artifacts require the full-catalog build gate")
        if any(value is None for value in candidate_build_fields) or args.package_artifacts is None:
            parser.error("Candidate builds require package, candidate, reuse, and expected artifacts")
    if args.package and args.command != "build-one":
        parser.error("--package is only valid with build-one; full-catalog builds cannot be narrowed")
    if args.package_artifacts and args.command not in ("build", "candidate-create", "candidate-verify"):
        parser.error("--package-artifacts requires the full-catalog or candidate command")
    if args.command == "candidate-create":
        required = (args.package_artifacts, args.plan, args.output, args.repository, args.pr, args.head,
                    args.tree, args.workflow, args.run, args.attempt, args.artifact_name)
        if any(value is None for value in required) or args.manifest or args.expected_json or args.publish:
            parser.error("candidate-create requires package artifacts, plan, output and complete identity; publication is not allowed")
        identity = {field: getattr(args, field) for field in CANDIDATE_IDENTITY_FIELDS}
        identity["artifact_name"] = args.artifact_name
        write_candidate_manifest(args.output, candidate_manifest(load_packages(), _read_json(args.plan),
                                                                  args.package_artifacts, identity))
        return
    if args.command == "candidate-verify":
        if (args.package_artifacts is None or args.manifest is None or args.expected_json is None
                or any(value is not None for value in (args.plan, args.output, args.repository, args.pr,
                                                       args.head, args.tree, args.workflow, args.run,
                                                       args.attempt, args.artifact_name)) or args.publish):
            parser.error("candidate-verify requires package artifacts, manifest and expected JSON only")
        expected_data = _read_candidate_expected(args.expected_json)
        result = verify_candidate_bundle(load_packages(), expected_data["plan"], args.package_artifacts,
                                         _read_json(args.manifest), expected_data["identity"])
        print(json.dumps(result, sort_keys=True))
        return
    if args.output is None and args.command in ("build", "build-one"):
        args.output = Path("dist")
    if args.package_artifacts and args.command != "build":
        parser.error("--package-artifacts requires the full-catalog build gate")
    if args.command == "build-one":
        build_one(args)
        return
    if args.command == "pin-recipes":
        (REPO / "build/recipe-files.json").write_text(json.dumps(recipe_inventory(REPO / "build/recipes"), indent=2, sort_keys=True) + "\n")
        return
    if args.command == "sandbox-check":
        sandbox_check(args)
        return
    if args.command == "keys":
        print(json.dumps(build_keys(load_packages()), indent=2, sort_keys=True))
        return
    if args.command == "plan":
        keys, catalogs = args.previous / "build-keys.json", sorted(args.previous.glob("catalog-*.json"))
        usable = keys.is_file() and len(catalogs) == 1
        print(json.dumps(carry_plan(load_packages(), json.loads(keys.read_text()) if usable else {},
                                    json.loads(catalogs[0].read_text()) if usable else {})))
        return
    if args.command == "validate":
        load_packages()
        print("Locked recipes, dependency order and full catalog coverage are valid.")
    else:
        build(args)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError, tarfile.TarError, zipfile.BadZipFile) as error:
        sys.exit(f"Build refused: {error}")
