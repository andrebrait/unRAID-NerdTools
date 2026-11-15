#!/usr/bin/env python3
"""Trusted, stdlib-only boundary around untrusted vendor workspaces."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile

MAX_FILE = 16 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
MAX_MEMBERS = 10000


def outside(path, workspace):
    original = Path(path).absolute()
    resolved = original.resolve()
    if resolved == workspace or workspace in resolved.parents:
        raise ValueError('trusted path must be outside workspace')
    return original


def source_path(name):
    path = PurePosixPath(name)
    if not name or path.is_absolute() or any(part in ('', '.', '..') for part in name.split('/')) or str(path) != name:
        raise ValueError('invalid source path')
    if name != 'README.md' and path.parts[0] != 'build':
        raise ValueError('unexpected source path')
    return path


def manifest_for(files):
    return {name: {'sha256': hashlib.sha256(data).hexdigest(), 'executable': bool(mode & 0o111)}
            for name, (data, mode) in sorted(files.items())}


def extract(archive, workspace, manifest):
    workspace_path = Path(workspace)
    if workspace_path.is_symlink():
        raise ValueError('workspace is a link')
    workspace = workspace_path.resolve()
    manifest = outside(manifest, workspace)
    if workspace.exists() and (not workspace.is_dir() or any(workspace.iterdir())):
        raise ValueError('workspace must be empty')
    files, directories, total = {}, set(), 0
    # Validate the complete archive before materializing even one member.
    with tarfile.open(archive, 'r:*') as source:
        for count, member in enumerate(source, 1):
            if count > MAX_MEMBERS:
                raise ValueError('too many archive members')
            name = member.name
            source_path(name)
            if name in files or name in directories:
                raise ValueError('duplicate archive member')
            if member.isdir():
                if name == 'README.md':
                    raise ValueError('README must be a file')
                directories.add(name)
                continue
            if not member.isfile() or member.size < 0 or member.size > MAX_FILE:
                raise ValueError('unsafe archive member')
            if name == 'build':
                raise ValueError('build must be a directory')
            total += member.size
            if total > MAX_TOTAL:
                raise ValueError('archive too large')
            with source.extractfile(member) as stream:
                data = stream.read(MAX_FILE + 1)
            if len(data) != member.size:
                raise ValueError('truncated archive member')
            files[name] = (data, 0o755 if member.mode & 0o111 else 0o644)
    for name in [*files, *directories]:
        if any(str(parent) in files for parent in PurePosixPath(name).parents if str(parent) != '.'):
            raise ValueError('file used as directory')
    workspace.mkdir(parents=True, exist_ok=True)
    for name in sorted(directories):
        (workspace / name).mkdir(parents=True, exist_ok=True)
    for name, (data, mode) in files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(manifest_for(files), sort_keys=True) + '\n')
    manifest.chmod(0o444)


def scan(workspace):
    files, total, count = {}, 0, 0

    def walk(directory, prefix=''):
        nonlocal total, count
        for name in sorted(os.listdir(directory)):
            count += 1
            if count > MAX_MEMBERS:
                raise ValueError('too many workspace entries')
            relative = prefix + name
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise ValueError('unsafe workspace entry')
            if relative == '.upstream-downloads' and stat.S_ISDIR(info.st_mode):
                continue
            source_path(relative)
            if stat.S_ISDIR(info.st_mode):
                if relative == 'README.md':
                    raise ValueError('README must be a file')
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                try:
                    walk(child, relative + '/')
                finally:
                    os.close(child)
                continue
            if relative == 'build' or info.st_size > MAX_FILE:
                raise ValueError('unsafe source file')
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, 'rb') as stream:
                current = os.fstat(stream.fileno())
                if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
                    raise ValueError('unsafe source file')
                data = stream.read(MAX_FILE + 1)
            total += len(data)
            if len(data) > MAX_FILE or total > MAX_TOTAL:
                raise ValueError('workspace too large')
            files[relative] = (data, 0o755 if current.st_mode & 0o111 else 0o644)

    fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        walk(fd)
    finally:
        os.close(fd)
    return files


def read_report(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('report is not a regular file')
        data = stream.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise ValueError('report too large')
    report = json.loads(data)
    if not isinstance(report, dict) or set(report) != {'summary', 'recipes'} or not isinstance(report['summary'], str) or not isinstance(report['recipes'], list) or not all(isinstance(recipe, dict) for recipe in report['recipes']):
        raise ValueError('invalid report shape')
    return data


def collect(workspace, manifest, report, output, outcome='success'):
    workspace_path = Path(workspace)
    workspace = workspace_path.absolute()
    resolved_workspace = workspace.resolve()
    manifest = outside(manifest, resolved_workspace)
    report = outside(report, resolved_workspace)
    output = outside(output, resolved_workspace)
    output.mkdir(parents=True, exist_ok=True)
    for name in ('workspace.tar', 'report.json', 'status.json'):
        (output / name).unlink(missing_ok=True)
    status = {'outcome': outcome, 'changed': True, 'unsafe': False}
    try:
        if workspace_path.is_symlink():
            raise ValueError('workspace is a link')
        baseline = json.loads(manifest.read_text())
        if not isinstance(baseline, dict):
            raise ValueError('invalid baseline')
        files = scan(workspace)
        status['changed'] = manifest_for(files) != baseline
        report_data = read_report(report) if outcome == 'success' else None
        # Archive captured bytes, never re-open model-controlled paths for tar.add().
        with tarfile.open(output / 'workspace.tar', 'w') as archive:
            for name, (data, mode) in sorted(files.items()):
                member = tarfile.TarInfo(name)
                member.size, member.mode = len(data), mode
                archive.addfile(member, io.BytesIO(data))
        if report_data is not None:
            (output / 'report.json').write_bytes(report_data)
    except (OSError, ValueError, tarfile.TarError):
        status['unsafe'] = True
        for name in ('workspace.tar', 'report.json'):
            (output / name).unlink(missing_ok=True)
    (output / 'status.json').write_text(json.dumps(status, sort_keys=True) + '\n')
    return status


TIMESTAMP = re.compile(r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z\s+(\{.*\})$')
PROVIDER = re.compile(r'\b(?:insufficient_quota|insufficient_credits|rate_limit_exceeded|billing_hard_limit_reached|server_error|overloaded_error|request_timeout|quota (?:exceeded|exhausted)|billing|credits? (?:exhausted|balance|limit)|rate[ _-]limit|too many requests|429|5\d\d|server error|internal server error|overloaded|overload|network (?:error|timeout)|connection (?:timed out|timeout|reset)|request timed out|request timeout)\b', re.I)
DENY = re.compile(r'(?:\b(?:build|tests?|safety|refusal|refused|schema|validation|sandbox|permission)\b|(?:build|test|safety|schema|validation|permission)_)', re.I)


def event_line(line):
    line = line.strip()
    if line.startswith('{'):
        raw = line
    else:
        fields = line.split('\t', 2)
        if len(fields) == 3 and fields[1] == 'Run Codex':
            match = TIMESTAMP.fullmatch(fields[2])
        elif len(fields) == 1:
            match = TIMESTAMP.fullmatch(line)
        else:
            return None
        if not match:
            return None
        raw = match.group(1)
    try:
        event = json.loads(raw)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


def fallback(log, status):
    try:
        status = json.loads(Path(status).read_text())
        if not isinstance(status, dict) or status.get('outcome') != 'failure' or status.get('changed') is not False or status.get('unsafe', False) is not False:
            return False
        eligible = False
        for line in Path(log).read_text().splitlines():
            event = event_line(line)
            if not event or event.get('type') not in ('error', 'turn.failed'):
                continue
            error = event.get('error')
            values = [event.get('message')]
            if isinstance(error, str):
                values.append(error)
            elif isinstance(error, dict):
                values.extend(error.get(key) for key in ('message', 'code', 'type'))
            message = ' '.join(value for value in values if isinstance(value, str))
            if DENY.search(message):
                return False
            eligible |= bool(PROVIDER.search(message))
        return eligible
    except (OSError, ValueError):
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    extract_parser = commands.add_parser('extract')
    for name in ('archive', 'workspace', 'manifest'):
        extract_parser.add_argument('--' + name, required=True, type=Path)
    collect_parser = commands.add_parser('collect')
    for name in ('workspace', 'manifest', 'report', 'output'):
        collect_parser.add_argument('--' + name, required=True, type=Path)
    collect_parser.add_argument('--outcome', choices=('success', 'failure'), default='success')
    fallback_parser = commands.add_parser('fallback')
    for name in ('log', 'status'):
        fallback_parser.add_argument('--' + name, required=True, type=Path)
    args = vars(parser.parse_args(argv))
    command = args.pop('command')
    if command == 'fallback':
        value = 'eligible=' + str(fallback(**args)).lower()
        print(value)
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                output.write(value + '\n')
    elif command == 'extract':
        extract(**args)
    else:
        collect(**args)


if __name__ == '__main__':
    main()
