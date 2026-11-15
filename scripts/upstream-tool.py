#!/usr/bin/env python3
"""Trusted transport and Docker adapter; never execute model commands on the host."""
import contextlib
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import signal
import subprocess
import sys
import time
import urllib.request
import uuid

MAX_DOWNLOAD = 512 * 2**20
MAX_TOTAL = 2 * 2**30
SECRET = re.compile(r'(^\.(git|omp|ssh|aws|netrc|npmrc|pypirc)$|^auth\.json$|^\.env|\.(pem|key|p12|pfx|keystore)$|^id_(rsa|dsa|ecdsa|ed25519)|(^|[-_.])(token|secret|credential)s?([-_.]|$))', re.I)


def clean_env():
    return {'PATH': '/usr/local/bin:/usr/bin:/bin', 'LANG': 'C.UTF-8'}


def destination(work, name):
    if not isinstance(name, str) or not name or '\\' in name or '\0' in name:
        raise ValueError('Unsafe download path')
    parts = PurePosixPath(name).parts
    if not parts or PurePosixPath(name).is_absolute() or any(p in ('.', '..') or SECRET.search(p) for p in parts):
        raise ValueError('Unsafe download path')
    target = work
    for part in parts:
        target = target / part
        if target.is_symlink():
            raise ValueError('Symlink download path')
    return target


def docker_args(config, command):
    return ['docker', 'run', '--rm', '--name', config['container'], '--read-only',
            '--network', 'none', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--pids-limit', '128', '--memory', '1g', '--cpus', '2', '--tmpfs', '/tmp:rw,nosuid,nodev,size=128m',
            '--user', f'{os.getuid()}:{os.getgid()}', '--mount', f"type=bind,src={config['workspace']},dst=/work",
            '--workdir', '/work', config['image'], 'sh', '-c', command]


def remove_container(config):
    subprocess.run(['docker', 'rm', '-f', config['container']], env=clean_env(),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)


def workspace(config, command):
    end = min(config['deadline'], time.monotonic() + 60)
    process = subprocess.Popen(docker_args(config, command), env=clean_env(),
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    output = bytearray()
    def abort(*_):
        raise TimeoutError('Workspace command aborted')
    previous = {s: signal.signal(s, abort) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                if time.monotonic() >= end:
                    raise TimeoutError('Workspace command exceeded its deadline')
                for key, _ in selector.select(min(0.2, end - time.monotonic())):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        output.extend(chunk[:max(0, 65536 - len(output))])
        return {'exit_code': process.wait(timeout=max(0.01, end - time.monotonic())),
                'output': output.decode('utf-8', 'replace'), 'output_limit': 65536}
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)
        try:
            remove_container(config)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()


@contextlib.contextmanager
def transfer_deadline(deadline):
    def expired(*_):
        raise TimeoutError('Download deadline exceeded')
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, max(0.001, deadline - time.monotonic()))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def fetch_source(config, url, path=None, mode='text', offset=0):
    spec = importlib.util.spec_from_file_location('bootstrap', config['bootstrap'])
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)
    bootstrap.https_url(url)
    work = Path(config['workspace'])
    if mode not in ('text', 'archive') or offset < 0:
        raise ValueError('Invalid fetch mode or offset')
    target = destination(work, path or '.upstream-downloads/' + uuid.uuid4().hex)
    total_file = Path(config['control']) / 'download-total'
    total = int(total_file.read_text()) if total_file.exists() else 0
    if total >= MAX_TOTAL:
        raise ValueError('Download byte limit exhausted')
    target.parent.mkdir(parents=True, exist_ok=True)
    # Tools run serially and containers are removed before this host-side write.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    digest, size, text = hashlib.sha256(), 0, bytearray()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), bootstrap.PinnedHTTPSHandler(), bootstrap.HTTPSRedirect())
    try:
        with transfer_deadline(config['deadline']), os.fdopen(os.open(target, flags, 0o644), 'wb') as stream:
            with opener.open(urllib.request.Request(url, headers={'User-Agent': 'NerdTools-upstream'}),
                             timeout=min(30, max(0.01, config['deadline'] - time.monotonic()))) as response:
                declared = response.headers.get('Content-Length')
                if declared is not None and 'chunked' in response.headers.get('Transfer-Encoding', '').lower():
                    raise ValueError('Ambiguous download framing')
                if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                    raise ValueError('Encoded downloads are not supported')
                if declared is not None and (not declared.isdigit() or int(declared) > min(MAX_DOWNLOAD, MAX_TOTAL - total)):
                    raise ValueError('Download byte limit exceeded')
                while True:
                    if time.monotonic() >= config['deadline']:
                        raise TimeoutError('Download deadline exceeded')
                    if declared is not None and size == int(declared):
                        break
                    remaining = min(MAX_DOWNLOAD - size, MAX_TOTAL - total)
                    if remaining <= 0:
                        raise ValueError('Download reached its byte limit before a verified end')
                    amount = min(65536, remaining, int(declared) - size if declared is not None else 65536)
                    try:
                        chunk = response.read1(amount)
                    except http.client.IncompleteRead as error:
                        total_file.write_text(str(total + len(error.partial)))
                        raise ValueError('Incomplete download') from error
                    if not chunk:
                        break
                    size += len(chunk)
                    total += len(chunk)
                    total_file.write_text(str(total))
                    digest.update(chunk)
                    stream.write(chunk)
                    if mode == 'text' and size > offset and len(text) < 65536:
                        start = max(0, offset - (size - len(chunk)))
                        text.extend(chunk[start:start + 65536 - len(text)])
                if declared is not None and size != int(declared):
                    raise ValueError('Incomplete download')
    except BaseException:
        # Never unlink a pre-existing file when exclusive creation failed.
        if 'stream' in locals():
            target.unlink(missing_ok=True)
        raise
    result = {'path': str(target.relative_to(work)), 'sha256': digest.hexdigest(), 'bytes': size}
    if mode == 'text':
        result.update(text=text.decode('utf-8', 'replace'), next_offset=offset + len(text) if offset + len(text) < size else None)
    return result


def finish(config, report):
    recipes = report.get('recipes', [])
    if (set(report) != {'summary', 'recipes'} or not isinstance(report['summary'], str)
            or not isinstance(recipes, list) or len(recipes) != len(config['candidates'])
            or any(not isinstance(r, dict) or set(r) != {'name', 'decision', 'reason'}
                   or not isinstance(r.get('name'), str) or r.get('decision') not in ('accept', 'reject')
                   or not isinstance(r.get('reason'), str) or not r['reason'].strip() for r in recipes)
            or sorted(r['name'] for r in recipes) != sorted(config['candidates'])):
        raise ValueError('Finish requires exactly one justified decision per candidate')
    with (Path(config['control']) / 'report.json').open('x') as stream:
        json.dump(report, stream)
    return {'finished': True}


def main():
    config = json.loads(Path(sys.argv[1]).read_text())
    if time.monotonic() >= config['deadline']:
        raise TimeoutError('Agent deadline exceeded')
    control = Path(config['control'])
    if (control / 'report.json').exists():
        raise ValueError('Session is already finished')
    request = json.load(sys.stdin)
    (control / 'touched').touch()
    tools = {'workspace': workspace, 'fetch_source': fetch_source, 'finish': finish}
    name = request['name']
    result = tools[name](config, **request['params']) if name != 'finish' else finish(config, request['params'])
    print(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'error': str(error)}))
        sys.exit(1)
