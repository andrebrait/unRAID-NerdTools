"""Bubblewrap isolation with no host-root identity or initial-namespace capabilities."""
from contextlib import contextmanager
import os
from pathlib import Path
import stat

from bootstrap import run

OFFSET = 100000
COUNT = 65536


def map_id(value, reverse):
    result = value - OFFSET if reverse else value
    if not 0 <= result < COUNT:
        raise ValueError(f"Filesystem identity outside sandbox mapping: {value}")
    return result if reverse else result + OFFSET


def shift_ownership(root, reverse):
    seen = set()
    for directory, dirs, files in os.walk(root, followlinks=False):
        paths = [Path(directory), *(Path(directory) / name for name in files)]
        paths.extend(Path(directory) / name for name in dirs if (Path(directory) / name).is_symlink())
        for path in paths:
            info = path.lstat()
            identity = (info.st_dev, info.st_ino)
            if identity in seen:
                continue
            seen.add(identity)
            os.lchown(path, map_id(info.st_uid, reverse), map_id(info.st_gid, reverse))
            if not stat.S_ISLNK(info.st_mode):
                os.chmod(path, stat.S_IMODE(info.st_mode))


@contextmanager
def mapped_root(root):
    if root.is_symlink():
        raise ValueError("Sandbox root cannot be a symlink")
    shift_ownership(root, False)
    try:
        yield
    finally:
        shift_ownership(root, True)

def run_sandbox(root, command, env, cwd="/", timeout=3600, capture=False, read_only=False):
    # The privileged mapper runs before any recipe; Bubblewrap removes the host tree.
    args = ["unshare", "--user", f"--map-users={OFFSET},0,{COUNT}", f"--map-groups={OFFSET},0,{COUNT}",
            "--setgroups=allow", "--setuid=0", "--setgid=0", "bwrap",
            "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-net", "--unshare-ipc",
            "--unshare-uts", "--unshare-cgroup-try", "--cap-drop", "ALL"]
    for capability in ("CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "SETUID", "SETGID"):
        args.extend(["--cap-add", "CAP_" + capability])
    args.extend(["--ro-bind" if read_only else "--bind", str(root), "/", "--proc", "/proc", "--dev", "/dev", "--chdir", cwd, "--clearenv"])
    for key, value in env.items():
        args.extend(["--setenv", key, value])
    args.extend(["/bin/bash", "-c", 'umask 022; exec "$@"', "_", *command])
    return run(args, timeout=timeout, capture_output=capture, text=capture, extra_groups=[])
