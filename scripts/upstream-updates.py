#!/usr/bin/env python3
"""Maintain one reviewed upstream recipe PR; never approve, merge, or publish it here."""
import argparse
import hashlib
import html
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
from urllib.parse import quote
import tempfile
import tarfile

BRANCH = 'automation/upstream-recipes'
TITLE = 'Update upstream recipes'
BOT = 'github-actions[bot]'
LOCKS = ('build/locks/slackware.json', 'build/locks/slackbuilds.json')
STATE = '<!-- upstream-recipes-state\n'
MAX_REPAIR_ATTEMPTS = 3
NAME = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9+_.-]*\Z')


def run(args, repo=None, input=None):
    return subprocess.check_output(args, cwd=repo, input=input, text=True, stderr=subprocess.STDOUT)


def git(repo, *args):
    return run(['git', *args], repo).strip()


class GitHub:
    def __init__(self, repository):
        if not re.fullmatch(r'[\w.-]+/[\w.-]+', repository):
            raise ValueError('Invalid GH_REPO')
        self.repository = repository
        self.root = f'repos/{repository}'

    def api(self, path, data=None, method=None):
        args = ['gh', 'api', f'{self.root}/{path}']
        if method:
            args += ['--method', method]
        if data is not None:
            args += ['--input', '-']
        result = run(args, input=json.dumps(data) if data is not None else None)
        return json.loads(result) if result.strip() else None

    def pages(self, path):
        pages = json.loads(run(['gh', 'api', '--paginate', '--slurp', f'{self.root}/{path}']))
        return [item for page in pages for item in page]

    def dispatch(self, workflow, ref='main', inputs=None):
        self.api(f'actions/workflows/{workflow}/dispatches', {'ref': ref, 'inputs': inputs or {}})


def trusted_pr(pr, repository):
    return (pr.get('head', {}).get('ref') == BRANCH
            and pr.get('head', {}).get('repo', {}).get('full_name') == repository
            and pr.get('base', {}).get('ref') == 'main'
            and pr.get('base', {}).get('repo', {}).get('full_name') == repository
            and pr.get('user', {}).get('login') == BOT)


def human_approved(pr, reviews):
    if not pr.get('merged'):
        return False
    latest = {}
    for review in sorted(reviews, key=lambda item: item['id']):
        user = review.get('user') or {}
        if (user.get('type') == 'User' and review.get('author_association') in ('OWNER', 'MEMBER', 'COLLABORATOR')
                and review.get('state') in ('APPROVED', 'CHANGES_REQUESTED', 'DISMISSED')):
            latest[user.get('login')] = review
    current = [item for item in latest.values() if item.get('commit_id') == pr['head']['sha']]
    return (any(item['state'] == 'APPROVED' for item in current)
            and not any(item['state'] == 'CHANGES_REQUESTED' for item in current))


def latest_build(runs, sha):
    matching = [item for item in runs if item.get('head_sha') == sha and item.get('event') == 'workflow_dispatch']
    return max(matching, key=lambda item: (item['id'], item.get('run_attempt', 1)), default=None)


def available_bots(config, evidence):
    bots = {item['user']['login'] for item in evidence
            if (item.get('user') or {}).get('type') == 'Bot'}
    bots.update(item.strip() for item in config.split(',') if item.strip())
    return sorted(bot for bot in bots if bot != BOT and 'copilot' not in bot.lower()
                  and re.fullmatch(r'[\w-]+(?:\[bot\])?', bot))


def trusted_author(item, bots=()):
    user = item.get('user') or {}
    return (user.get('login') not in (None, BOT) and
            (user['login'] in bots or item.get('author_association') in ('OWNER', 'MEMBER', 'COLLABORATOR')))


def current_feedback(evidence, sha, committed_at, bots=()):
    result = []
    for item in evidence:
        body = (item.get('body') or '').strip()
        if (not trusted_author(item, bots) or not body or item.get('state') in ('APPROVED', 'DISMISSED')
                or re.fullmatch(r'(?i)(lgtm|looks good|thanks|thank you|approved)[.! ]*', body)
                or 'usage limit' in body.lower()):
            continue
        commit = item.get('commit_id') or item.get('original_commit_id')
        if commit != sha and (commit or item.get('created_at', '') < committed_at):
            continue
        result.append({'id': item['id'], 'user': item['user']['login'], 'body': body[:20000],
                       'path': item.get('path'), 'line': item.get('line'), 'state': item.get('state')})
    return result


def validate_report(report, candidates):
    if not isinstance(report, dict) or set(report) != {'summary', 'recipes'}:
        raise ValueError('Agent report must contain exactly summary and recipes')
    expected = {item['name'] for item in candidates}
    recipes = report.get('recipes', [])
    if (not isinstance(report.get('summary'), str) or not isinstance(recipes, list)
            or any(not isinstance(item, dict) or set(item) != {'name', 'decision', 'reason'} for item in recipes)
            or len(recipes) != len(expected) or {item.get('name') for item in recipes} != expected
            or any(item.get('decision') not in ('accept', 'reject')
                   or not isinstance(item.get('reason'), str) or not item['reason'].strip() for item in recipes)):
        raise ValueError('Agent must give one justified accept/reject decision per candidate')


def check_paths(paths, generated=False):
    for path in paths:
        parts = PurePosixPath(path).parts
        safe = (path in (*LOCKS, 'build/recipe-files.json')
                or (len(parts) >= 4 and parts[:2] == ('build', 'recipes') and NAME.fullmatch(parts[2])
                    and all(part not in ('.', '..') and not part.startswith('.') for part in parts)))
        if generated:
            safe = safe or path == '.github/workflows/build-packages.yml' or bool(
                re.fullmatch(r'\.github/workflows/package-[a-zA-Z0-9][a-zA-Z0-9+_.-]*\.yml', path)
                and path != '.github/workflows/package-build.yml')
        if not safe:
            raise ValueError(f'Forbidden automation change: {path}')


def changed_paths(repo, base=None):
    tracked = git(repo, 'diff', '--name-only', '--no-renames', *( [base] if base else [] )).splitlines()
    untracked = git(repo, 'ls-files', '--others', '--exclude-standard').splitlines()
    return sorted(set(tracked + untracked))


def workspace_path(repo, value):
    path = Path(value)
    roots = [Path(tempfile.gettempdir()).resolve()]
    if os.environ.get('RUNNER_TEMP'):
        roots.append(Path(os.environ['RUNNER_TEMP']).resolve())
    if (not path.is_absolute() or path.is_symlink() or not path.is_dir()
            or path.resolve() == repo.resolve() or repo.resolve() in path.resolve().parents
            or path.resolve() in repo.resolve().parents
            or not any(root in path.resolve().parents for root in roots)):
        raise ValueError('Agent workspace must be an external temporary directory')
    return path


def validate_workspace(repo, workspace, report, candidates):
    """Validate the entire untrusted export before copying any model bytes."""
    tracked = set(git(repo, 'ls-files', '--', 'build', 'README.md').splitlines())
    files = {}
    for folder, dirs, names in os.walk(workspace, followlinks=False):
        relative = Path(folder).relative_to(workspace)
        for name in list(dirs) + names:
            path = Path(folder) / name
            rel = (relative / name).as_posix()
            if rel == '.upstream-downloads' and path.is_dir() and not path.is_symlink():
                dirs.remove(name)
                continue
            if name == '.git':
                raise ValueError(f'Forbidden workspace file: {rel}')
            if path.is_symlink():
                original = repo / rel
                if (rel not in tracked or not original.is_symlink()
                        or os.readlink(path) != os.readlink(original)
                        or (path.lstat().st_mode & 0o111) != (original.lstat().st_mode & 0o111)):
                    raise ValueError(f'Changed workspace symlink: {rel}')
                files[rel] = path
            elif path.is_file():
                if path.stat().st_size > 16 * 1024 * 1024:
                    raise ValueError(f'Workspace file exceeds 16 MiB size limit: {rel}')
                files[rel] = path
            elif name in names:
                raise ValueError(f'Non-regular workspace file: {rel}')
    changed = sorted(path for path in tracked | files.keys()
                     if path not in files or
                     (not files[path].is_symlink() and (
                         (repo / path).is_symlink() or not (repo / path).is_file()
                         or files[path].read_bytes() != (repo / path).read_bytes()
                         or (files[path].stat().st_mode & 0o111) != ((repo / path).stat().st_mode & 0o111))))
    # This generated inventory is trusted only after the controller rebuilds it.
    changed = [path for path in changed if path != 'build/recipe-files.json']
    check_paths(changed, generated=False)
    if any((repo / path).is_symlink() for path in changed):
        raise ValueError('Tracked symlinks must remain unchanged')
    before, after = {}, {}
    for lock in LOCKS:
        old = json.loads((repo / lock).read_text())
        new = json.loads((workspace / lock).read_text())
        if not isinstance(new, list) or any(not isinstance(item, dict) for item in new):
            raise ValueError('Invalid package lock')
        old_names = {item['name'] for item in old}
        names = [item.get('name') for item in new]
        if len(names) != len(set(names)) or not old_names <= set(names):
            raise ValueError('Existing package names and lock placement are immutable')
        before.update((item['name'], item) for item in old)
        for item in new:
            if item['name'] in after or not isinstance(item['name'], str) or not NAME.fullmatch(item['name']):
                raise ValueError('Invalid or duplicate package name')
            after[item['name']] = item
    mutable = {'version', 'provenance', 'inputs', 'dependencies', 'build_dependencies', 'smoke', 'recipe_sha256'}
    for name, item in after.items():
        if name in before:
            old = before[name]
            if {key: value for key, value in old.items() if key not in mutable} != {
                    key: value for key, value in item.items() if key not in mutable}:
                raise ValueError(f'Immutable package policy changed: {name}')
            if item.get('provenance', {}).get('kind') != old.get('provenance', {}).get('kind'):
                raise ValueError(f'Source kind changed: {name}')
            version = str(old.get('version', ''))
            pattern = re.compile(r'(?<![A-Za-z0-9_.])' + re.escape(version) + r'(?![A-Za-z0-9_.])')
            smoke = [[pattern.sub(lambda _: str(item['version']), arg) for arg in command]
                     for command in old.get('smoke', [])]
            if item.get('smoke', []) not in (old.get('smoke', []), smoke):
                raise ValueError(f'Original smoke assertions must be retained: {name}')
        else:
            if (not (item.get('internal') is True or item.get('catalog') is False)
                    or item.get('provenance', {}).get('kind') != 'source'
                    or not item.get('provenance', {}).get('revision') or not item.get('recipe')
                    or not item.get('inputs') or any(not re.fullmatch(r'[0-9a-f]{64}', source.get('sha256', ''))
                                                        for source in item['inputs'])):
                raise ValueError(f'New dependency must be fully pinned internal source: {name}')
    versions = {item['name']: item['upstream'] for item in candidates}
    for decision in report['recipes']:
        if decision['decision'] == 'accept' and after[decision['name']]['version'] != versions[decision['name']]:
            raise ValueError('Accepted version differs from discovered upstream')
    accepted = {item['name'] for item in report['recipes'] if item['decision'] == 'accept'}
    permitted = dependency_closure(after, accepted)
    for relative in changed:
        parts = PurePosixPath(relative).parts
        if parts[:2] == ('build', 'recipes') and parts[2] not in permitted:
            raise ValueError(f'Recipe change is outside the accepted dependency closure: {relative}')
    for name, item in after.items():
        recipe = item.get('recipe')
        if name not in before or name in accepted or recipe in changed:
            check_paths([recipe])
            path = workspace / recipe
            if (not path.is_file() or path.is_symlink()
                    or any(parent.is_symlink() for parent in path.parents if parent != workspace)):
                raise ValueError(f'Recipe must be a regular workspace file: {recipe}')
    return changed


def import_workspace(repo, workspace, paths, report):
    before = {item['name']: item for lock in LOCKS for item in json.loads((repo / lock).read_text())}
    for relative in paths:
        source, destination = workspace / relative, repo / relative
        if source.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        else:
            destination.unlink()
    accepted = {item['name'] for item in report['recipes'] if item['decision'] == 'accept'}
    for lock in LOCKS:
        text = (repo / lock).read_text()
        for start, end, item in reversed(entry_spans(text)):
            original = dict(item)
            if item['name'] not in before or item['name'] in accepted or item.get('recipe') in paths:
                item['recipe_sha256'] = hashlib.sha256((repo / item['recipe']).read_bytes()).hexdigest()
            elif 'recipe_sha256' in before[item['name']]:
                item['recipe_sha256'] = before[item['name']]['recipe_sha256']
            else:
                item.pop('recipe_sha256', None)
            if item != original:
                text = text[:start] + json.dumps(item, indent=2) + text[end:]
        (repo / lock).write_text(text)


def entry_spans(text):
    decoder, spans = json.JSONDecoder(), []
    offset = text.index('[') + 1
    while True:
        while offset < len(text) and (text[offset].isspace() or text[offset] == ','):
            offset += 1
        if text[offset] == ']':
            return spans
        value, end = decoder.raw_decode(text, offset)
        spans.append((offset, end, value))
        offset = end


def dependency_closure(packages, names):
    found, todo = set(), list(names)
    while todo:
        name = todo.pop()
        if name not in found:
            found.add(name)
            package = packages.get(name, {})
            todo.extend(package.get('dependencies', []) + package.get('build_dependencies', []))
    return found


def manual_workflow_updates(repo, base, report):
    before = {item['name']: item for lock in LOCKS for item in json.loads(git(repo, 'show', f'{base}:{lock}'))}
    after = {item['name']: item for lock in LOCKS for item in json.loads((repo / lock).read_text())}
    changed = {name for name, item in after.items() if name not in before or any(
        set(item.get(key, [])) != set(before[name].get(key, [])) for key in ('dependencies', 'build_dependencies'))}
    if not changed:
        return
    generator = load_script(Path(__file__).with_name('package-change-workflows.py'))
    canonical = generator.catalog_jobs(before)
    for item in report['recipes']:
        affected = dependency_closure(after, [item['name']]) & changed
        if item['decision'] != 'accept' or not affected:
            continue
        trial = before | {name: after[name] for name in affected}
        if trial.keys() == before.keys() and generator.catalog_jobs(trial) == canonical:
            continue
        item.update(decision='reject', reason='Requires manual dependency/workflow changes: GITHUB_TOKEN cannot push workflow files.')


def load_script(path):
    spec = importlib.util.spec_from_file_location(path.stem.replace('-', '_'), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def restore_rejected(repo, base, report):
    """Return rejected updates and their unused dependencies to main, across repair sessions."""
    rejected = {item['name'] for item in report['recipes'] if item['decision'] == 'reject'}
    reported = {item['name'] for item in report['recipes']}
    texts = {lock: run(['git', 'show', f'{base}:{lock}'], repo) for lock in LOCKS}
    working = {lock: (repo / lock).read_text() for lock in LOCKS}
    original = {lock: json.loads(text) for lock, text in texts.items()}
    current = {lock: json.loads(text) for lock, text in working.items()}
    baseline = {item['name']: item for entries in original.values() for item in entries}
    entries = {item['name']: item for values in current.values() for item in values}
    restored = entries | {name: baseline[name] for name in rejected}
    accepted = [item['name'] for item in report['recipes'] if item['decision'] == 'accept']
    keep = dependency_closure(restored, accepted)
    restore = rejected | (((dependency_closure(entries, rejected) | dependency_closure(baseline, rejected)) - keep) & baseline.keys())
    restore.update(name for name in baseline.keys() & entries.keys() if name not in reported and name not in keep
                   and entries[name] != baseline[name])
    discard = entries.keys() - baseline.keys() - keep
    if any(not NAME.fullmatch(name) for name in restore | discard):
        raise ValueError('Invalid rejected package name')
    for lock, values in current.items():
        updated = [baseline[item['name']] if item['name'] in restore else item
                   for item in values if item['name'] not in discard]
        if values != updated:
            if updated == original[lock]:
                rendered = texts[lock]
            else:
                old = {item['name']: texts[lock][start:end] for start, end, item in entry_spans(texts[lock])}
                blocks = entry_spans(working[lock])
                pieces = [working[lock][:blocks[0][0]]]
                kept = 0
                for index, (start, end, item) in enumerate(blocks):
                    if item['name'] in discard:
                        continue
                    if kept:
                        pieces.append(working[lock][blocks[index - 1][1]:start])
                    pieces.append(old[item['name']] if item['name'] in restore else working[lock][start:end])
                    kept += 1
                pieces.append(working[lock][blocks[-1][1]:])
                rendered = ''.join(pieces)
            (repo / lock).write_text(rendered)
    for name in restore | discard:
        folder = repo / 'build/recipes' / name
        if folder.is_symlink():
            folder.unlink()
        elif folder.exists():
            shutil.rmtree(folder)
        if name in restore:
            git(repo, 'restore', '--source', base, '--worktree', '--', f'build/recipes/{name}')


def read_state(pr):
    body = pr.get('body') or ''
    if STATE not in body:
        raise ValueError('Automation PR has no controller state')
    return json.loads(body.rsplit(STATE, 1)[1].split('\n-->', 1)[0])


def pr_body(state):
    report = state.get('report', {'summary': 'Awaiting assessment', 'recipes': []})
    evidence = state.get('build_url', 'Not yet built; no compatibility claim.')
    reviews = state.get('review_status', 'Bot review requests await successful catalog verification.')
    def safe(text):
        return html.escape(text, quote=False).replace('@', '&#64;')
    for limit in (240, 120, 60, 30):
        compact = {'summary': report['summary'][:limit * 4], 'recipes': [
            {**item, 'reason': item['reason'][:limit]} for item in report['recipes']]}
        stored = json.dumps({**state, 'report': compact}, sort_keys=True)
        stored = stored.replace('<', '\\u003c').replace('>', '\\u003e').replace('@', '\\u0040')
        decisions = '\n'.join(f"- **{safe(item['name'])}**: {item['decision']} — {safe(item['reason'])}"
                              for item in compact['recipes'])
        body = (f"## Upstream recipe batch\n\n{safe(compact['summary'])}\n\n{decisions}\n\n"
                f"## Acceptance\n\n{evidence}\n\n## Reviews\n\n{reviews}\n\n"
                'Human approval is required before merge. This automation never approves or merges its PR. '
                'Publication follows approved merge and the default-branch compatibility gates.\n\n'
                f"Agent sessions: {state.get('attempts', 0)}/{MAX_REPAIR_ATTEMPTS}.\n\n"
                + STATE + stored + '\n-->')
        if len(body) <= 65000:
            return body
    raise ValueError('Batch metadata exceeds GitHub PR body limit')


def summary(text):
    print(text)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
            stream.write(text + '\n')


def evidence(gh, number):
    reviews = gh.pages(f'pulls/{number}/reviews?per_page=100')
    inline = gh.pages(f'pulls/{number}/comments?per_page=100')
    comments = gh.pages(f'issues/{number}/comments?per_page=100')
    return reviews, inline, comments


def request_reviews(gh, pr, state, reviews):
    sha, number = pr['head']['sha'], pr['number']
    if state.get('reviewed_head') == sha:
        return
    bots = available_bots(os.environ.get('UPSTREAM_REVIEW_BOTS', ''), reviews)
    missing = {'coderabbitai[bot]', 'chatgpt-codex-connector[bot]'} - set(bots)
    state['review_status'] = ('Requested/available integrations: ' + (', '.join(bots) or 'none') + '. '
                              + ('Unverified, not requested: ' + ', '.join(sorted(missing)) + '. ' if missing else '')
                              + 'A request is not evidence of completed review.')
    if missing:
        summary('Review integrations unverified (not requested): ' + ', '.join(sorted(missing)))
    existing = gh.pages(f'issues/{number}/comments?per_page=100')
    for bot in bots:
        # Usage-limit replies are evidence, not a reason to keep tagging Codex.
        if 'codex' in bot and any('usage limit' in (item.get('body') or '').lower()
                                  for item in reviews + existing if (item.get('user') or {}).get('login') == bot):
            continue
        mention = 'codex' if bot == 'chatgpt-codex-connector[bot]' else bot.removesuffix('[bot]')
        marker = f'<!-- upstream-review:{sha}:{bot} -->'
        if not any(marker in (item.get('body') or '') for item in existing):
            gh.api(f'issues/{number}/comments', {'body': f'@{mention} review\n\n{marker}'})
    state['reviewed_head'] = sha


def check_generated(repo, base):
    """Generated package jobs may change; the surrounding workflow must not."""
    path = '.github/workflows/build-packages.yml'
    def handwritten(text):
        start = text.index('  # BEGIN generated package jobs')
        end = text.index('  # END generated package jobs')
        return text[:start] + text[end:]
    if handwritten(run(['git', 'show', f'{base}:{path}'], repo)) != handwritten((repo / path).read_text()):
        raise ValueError('Automation changed handwritten catalog workflow')
    run([sys.executable, 'scripts/package-change-workflows.py', '--check'], repo)


def prepare_branch(repo, pr):
    git(repo, 'fetch', 'origin', 'main', BRANCH) if pr else git(repo, 'fetch', 'origin', 'main')
    if pr:
        remote = git(repo, 'rev-parse', f'origin/{BRANCH}')
        if remote != pr['head']['sha']:
            raise ValueError('PR head changed during checkout; retry against the new head')
        base = git(repo, 'merge-base', 'origin/main', remote)
        check_paths(git(repo, 'diff', '--name-only', base, remote).splitlines(), generated=True)
        git(repo, 'checkout', '-B', BRANCH, remote)
        check_generated(repo, base)
        git(repo, '-c', 'user.name=NerdTools updater', '-c', 'user.email=41898282+github-actions[bot]@users.noreply.github.com',
            'merge', '--no-edit', 'origin/main')
    else:
        git(repo, 'checkout', '-B', BRANCH, 'origin/main')
    check_paths(changed_paths(repo, 'origin/main'), generated=True)


def push(repo, gh, previous):
    # The credential helper reads GH_TOKEN; tokens never appear in command arguments or remote URLs.
    git(repo, '-c', 'credential.helper=!gh auth git-credential',
        'push', f'--force-with-lease=refs/heads/{BRANCH}:{previous}',
        f'https://github.com/{gh.repository}.git', f'HEAD:refs/heads/{BRANCH}')


def branch_lease(gh, pr):
    if pr:
        return pr['head']['sha']
    refs = gh.api(f'git/matching-refs/heads/{BRANCH}')
    existing = next((item['object']['sha'] for item in refs if item['ref'] == f'refs/heads/{BRANCH}'), '')
    if existing:
        closed = gh.api(f'pulls?state=closed&head={quote(gh.repository.split("/")[0] + ":" + BRANCH)}&per_page=100')
        matching = [item for item in closed if trusted_pr(item, gh.repository) and item['head']['sha'] == existing]
        if matching:
            if not any(item.get('merged_at') for item in matching):
                raise ValueError('Automation PR was closed without merge; manual intervention required')
        else:
            comparison = gh.api(f'compare/main...{quote(BRANCH, safe="")}')
            commits = comparison.get('commits', [])
            if (not commits or len(commits) != comparison.get('total_commits') or len(comparison.get('files', [])) >= 300
                    or any((item.get('author') or {}).get('login') != BOT for item in commits)):
                raise ValueError('Existing branch is not an orphaned automation push; refusing to overwrite it')
            check_paths([item['filename'] for item in comparison['files']])
    return existing


def prepare(repo, gh, event, output, evaluation=False, package=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'plan.json').write_text(json.dumps({'needed': False}) + '\n')
    needed_output(False)
    if package and not evaluation:
        raise ValueError('--package requires --evaluation')
    requests = [] if evaluation else gh.api(f'pulls?state=open&head={quote(gh.repository.split("/")[0] + ":" + BRANCH)}&per_page=100')
    if len(requests) > 1:
        raise ValueError('Multiple automation PRs; refusing ambiguous ownership')
    pr = requests[0] if requests else None
    if pr and not trusted_pr(pr, gh.repository):
        raise ValueError('Refusing untrusted automation PR')
    if not evaluation and os.environ.get('GITHUB_EVENT_NAME') == 'workflow_run':
        completed = event['workflow_run']
        if not pr or completed['head_sha'] != pr['head']['sha']:
            summary('Ignoring stale-head build completion.')
            return False
    # Discovery always uses main, not the already-updated batch lock versions.
    candidates = json.loads(run([sys.executable, 'scripts/check-upstream-versions.py'], repo))
    if package:
        candidates = [item for item in candidates if item['name'] == package]
    state = read_state(pr) if pr else {'attempts': 0, 'candidates': [], 'report': {'summary': '', 'recipes': []}}
    if not candidates:
        summary('No newer upstream recipes. No issue or PR created.')
        return False
    batch = hashlib.sha256(json.dumps(candidates, sort_keys=True).encode()).hexdigest()
    new_batch = state.get('batch') != batch
    if new_batch:
        state.update(batch=batch, attempts=0, candidates=candidates, feedback_digest='', reviewed_head='')
    reviews, inline, comments = evidence(gh, pr['number']) if pr else ([], [], [])
    build = None
    feedback = []
    if pr:
        baseline = json.loads((repo / 'build/baseline.json').read_text())
        title = f"Unraid compatibility {baseline['version']} {baseline['image']['sha256']} publish=false"
        runs = gh.api(f'actions/workflows/build-packages.yml/runs?branch={quote(BRANCH)}&event=workflow_dispatch&per_page=100')['workflow_runs']
        build = latest_build([item for item in runs if item.get('display_title') == title], pr['head']['sha'])
        committed_at = gh.api(f"commits/{pr['head']['sha']}")['commit']['committer']['date']
        bots = available_bots(os.environ.get('UPSTREAM_REVIEW_BOTS', ''), reviews)
        feedback = current_feedback(reviews + inline + comments, pr['head']['sha'], committed_at, bots)
        if build and build['status'] != 'completed':
            summary(f"Batch build is still running: {build['html_url']}")
            return False
        if build and build['conclusion'] == 'success' and not new_batch and not state.get('validation_error'):
            state['build_url'] = build['html_url']
            if pr.get('draft'):
                run(['gh', 'pr', 'ready', str(pr['number']), '--repo', gh.repository])
            request_reviews(gh, pr, state, reviews)
            gh.api(f"pulls/{pr['number']}", {'body': pr_body(state)}, method='PATCH')
            digest = hashlib.sha256(json.dumps(feedback, sort_keys=True).encode()).hexdigest()
            if not feedback or state.get('feedback_digest') == digest:
                summary(f"Exact-head catalog gates passed; waiting for reviews and human approval: {pr['html_url']}")
                return False
        elif build and build['conclusion'] != 'success':
            state['build_url'] = f"FAILED: {build['html_url']} — no compatibility claim."
        if build is None and not new_batch and not state.get('validation_error'):
            dispatch_catalog(repo, gh, pr, state)
            summary('Dispatched the missing exact-head catalog build.')
            return False
    digest = hashlib.sha256(json.dumps(feedback, sort_keys=True).encode()).hexdigest()
    if state['attempts'] >= MAX_REPAIR_ATTEMPTS:
        summary('Repair budget exhausted. Human intervention required; no merge or publication performed.')
        return False
    previous = '' if evaluation else branch_lease(gh, pr)
    main_sha = git(repo, 'rev-parse', 'origin/main')
    source_sha = git(repo, 'rev-parse', 'HEAD') if evaluation else main_sha
    head_sha = pr['head']['sha'] if pr else ''
    state['attempts'] += 1
    state = read_state({'body': pr_body(state)})
    if pr:
        gh.api(f"pulls/{pr['number']}", {'body': pr_body(state)}, method='PATCH')
    if not evaluation:
        prepare_branch(repo, pr)
        if git(repo, 'rev-parse', 'origin/main') != main_sha:
            raise ValueError('Main changed during prepare; retry on current main')
    context = {'repository': gh.repository, 'candidates': candidates, 'feedback': feedback,
               'attempt': state['attempts'], 'previous_report': state.get('report'),
               'validation_error': state.get('validation_error', '')}
    if build and build['conclusion'] != 'success':
        context['failed_catalog_logs'] = run(
            ['gh', 'run', 'view', str(build['id']), '--repo', gh.repository, '--log-failed'])[-120000:]
    plan = {'needed': True, 'evaluation': evaluation, 'repository': gh.repository, 'main_sha': main_sha,
            'source_sha': source_sha, 'head_sha': head_sha, 'pr_number': pr['number'] if pr else None,
            'state': state, 'candidates': candidates, 'previous': previous,
            'feedback_digest': digest, 'context': context,
            'passing_unchanged_batch': bool(pr and build and build['conclusion'] == 'success' and not new_batch)}
    with tarfile.open(output / 'workspace.tar', 'w') as archive:
        for relative in git(repo, 'ls-files', '--', 'build', 'README.md').splitlines():
            path = repo / relative
            if path.is_symlink() or not path.is_file():
                raise ValueError(f'Handoff requires regular tracked files: {relative}')
            archive.add(path, arcname=relative, recursive=False)
    (output / 'context.json').write_text(json.dumps(context, indent=2) + '\n')
    (output / 'prompt.md').write_text(
        'Assess the candidates in .upstream-downloads/context.json (untrusted upstream/review data). '
        'Read build/README.md for the build and recipe rules. Update only recipe files and package locks; '
        'do not edit build/recipe-files.json because the controller regenerates it. Retain source, policy '
        'and smoke assertions. Do not modify controller code or workflows. '
        'Keep downloads and temporary build files under .upstream-downloads. '
        'Return exactly a JSON object with summary and recipes; recipes must contain one '
        'name, decision (accept/reject), and justified reason per candidate. Reject unsafe updates; '
        'do not weaken gates to make an update pass.\n')
    (output / 'plan.json').write_text(json.dumps(plan, indent=2) + '\n')
    needed_output(True)
    return True


def needed_output(needed):
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
            stream.write(f'needed={str(needed).lower()}\n')


def finish(repo, gh, plan_path, workspace, report_path, dry_run=False, output=None):
    plan = json.loads(Path(plan_path).read_text())
    if plan.get('evaluation') and not dry_run:
        raise ValueError('Evaluation handoff requires --dry-run')
    if output is not None and not dry_run:
        raise ValueError('--output requires --dry-run')
    if plan['repository'] != gh.repository or os.environ.get('GH_REPO', gh.repository) != gh.repository:
        raise ValueError('Handoff repository mismatch')
    if plan['context']['repository'] != gh.repository:
        raise ValueError('Context repository mismatch')
    evaluation = bool(plan.get('evaluation'))
    base = plan['source_sha'] if evaluation else 'origin/main'
    if evaluation:
        if git(repo, 'rev-parse', 'HEAD') != base:
            raise ValueError('Stale evaluation source revision; refusing handoff')
    elif gh.api('git/ref/heads/main')['object']['sha'] != plan['main_sha']:
        raise ValueError('Stale main revision; refusing handoff')
    pr = gh.api(f"pulls/{plan['pr_number']}") if plan['pr_number'] else None
    if pr:
        if (not trusted_pr(pr, gh.repository) or pr['head']['sha'] != plan['head_sha']
                or pr['state'] != 'open' or pr.get('merged')):
            raise ValueError('PR changed or closed during agent work; refusing to overwrite it')
        if read_state(pr) != plan['state']:
            raise ValueError('PR state changed during agent work')
    elif not plan.get('evaluation'):
        requests = gh.api(f'pulls?state=open&head={quote(gh.repository.split("/")[0] + ":" + BRANCH)}&per_page=100')
        if requests:
            raise ValueError('PR changed or closed during agent work; a new PR already exists')
    state = plan['state']
    previous = plan['previous']
    if not plan.get('evaluation') and branch_lease(gh, pr) != previous:
        raise ValueError('Branch lease changed during agent work')
    workspace = workspace_path(repo, workspace)
    try:
        if evaluation:
            if git(repo, 'rev-parse', 'HEAD') != base:
                raise ValueError('Stale evaluation source revision before import')
        else:
            prepare_branch(repo, pr)
            if git(repo, 'rev-parse', 'origin/main') != plan['main_sha']:
                raise ValueError('Stale main revision after fetch')
        report = json.loads(Path(report_path).read_text())
        validate_report(report, plan['candidates'])
        paths = validate_workspace(repo, workspace, report, plan['candidates'])
        import_workspace(repo, workspace, paths, report)
        manual_workflow_updates(repo, base, report)
        restore_rejected(repo, base, report)
        check_paths(changed_paths(repo), generated=True)
        run([sys.executable, 'build/native.py', 'pin-recipes'], repo)
        run([sys.executable, 'scripts/package-change-workflows.py'], repo)
        check_generated(repo, base)
        run([sys.executable, 'build/native.py', 'validate'], repo)
        paths = changed_paths(repo)
        check_paths(paths, generated=True)
        if any(path.startswith('.github/workflows/') for path in changed_paths(repo, base)):
            raise ValueError('Workflow changes require manual handling with GITHUB_TOKEN')
    except (ValueError, TypeError, KeyError, OSError, subprocess.CalledProcessError) as error:
        detail = str(getattr(error, 'output', None) or error)[-20000:]
        state['validation_error'] = detail
        if pr and not dry_run:
            gh.api(f"pulls/{pr['number']}", {'body': pr_body(state)}, method='PATCH')
        summary('Recipe validation failed; no publication performed:\n' + html.escape(detail))
        raise
    finally:
        shutil.rmtree(workspace)
    state.pop('validation_error', None)
    state.update(report=report, feedback_digest=plan['feedback_digest'])
    if dry_run:
        if output is not None:
            output = Path(output)
            resolved = output.resolve()
            if (resolved == repo.resolve() or repo.resolve() in resolved.parents
                    or resolved in repo.resolve().parents):
                raise ValueError('Dry-run output must be outside the checkout')
            output.mkdir(parents=True, exist_ok=True)
            workspace_path(repo, output)
            if paths:
                git(repo, 'add', '--', *paths)
            (output / 'changes.patch').write_text(run(
                ['git', 'diff', '--cached', '--binary', '--no-ext-diff'], repo))
            (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        summary('Dry-run native validation passed; no PR, push, catalog dispatch or release performed.')
        return
    changed = bool(paths)
    if changed:
        git(repo, 'add', '--', *paths)
        git(repo, '-c', 'user.name=github-actions[bot]', '-c',
            'user.email=41898282+github-actions[bot]@users.noreply.github.com',
            'commit', '-m', 'Update assessed upstream recipes')
    elif plan['passing_unchanged_batch']:
        state['attempts'] -= 1
    sha = git(repo, 'rev-parse', 'HEAD')
    if pr:
        current = gh.api(f"pulls/{pr['number']}")
        if current['head']['sha'] != previous or current['state'] != 'open' or current.get('merged'):
            raise ValueError('PR changed or closed during agent work; refusing to overwrite it')
    if not pr and not changed:
        summary(pr_body(state))
        return
    if changed or sha != previous:
        if pr and not pr.get('draft'):
            node = pr['node_id']
            run(['gh', 'api', 'graphql', '-f', 'query=mutation($id:ID!){convertPullRequestToDraft(input:{pullRequestId:$id}){pullRequest{id}}}', '-f', f'id={node}'])
        push(repo, gh, previous)
        state.pop('build_url', None)
        state.pop('reviewed_head', None)
    if pr:
        pr = gh.api(f"pulls/{pr['number']}", {'body': pr_body(state)}, method='PATCH')
    else:
        pr = gh.api('pulls', {'title': TITLE, 'head': BRANCH, 'base': 'main', 'draft': True, 'body': pr_body(state)})
    if changed or state.get('dispatched_head') != sha:
        dispatch_catalog(repo, gh, pr, state)
    summary(f"Batch PR: {pr['html_url']}. Catalog verification pending; human approval required.")


def dispatch_catalog(repo, gh, pr, state):
    baseline = json.loads((repo / 'build/baseline.json').read_text())
    gh.dispatch('build-packages.yml', BRANCH, {'unraid_version': baseline['version'],
                'unraid_sha256': baseline['image']['sha256'], 'unraid_url': baseline['image']['url'],
                'channel': 'stable', 'publish': 'false'})
    state['dispatched_head'] = pr['head']['sha']
    gh.api(f"pulls/{pr['number']}", {'body': pr_body(state)}, method='PATCH')


def event_pr(event):
    if event.get('pull_request'):
        return event['pull_request']['number']
    if event.get('issue', {}).get('pull_request'):
        return event['issue']['number']
    return None


def feedback(gh, event):
    number = event_pr(event)
    if not number or event.get('sender', {}).get('login') == BOT:
        return
    pr = gh.api(f'pulls/{number}')
    if not trusted_pr(pr, gh.repository) or pr['state'] != 'open':
        return
    sender = event.get('sender') or {}
    item = event.get('review') or event.get('comment') or {}
    known = available_bots(os.environ.get('UPSTREAM_REVIEW_BOTS', ''), gh.pages(f'pulls/{number}/reviews?per_page=100'))
    if not trusted_author({**item, 'user': sender}, known):
        return
    if item.get('commit_id') and item['commit_id'] != pr['head']['sha']:
        return
    gh.dispatch('upstream-versions.yml')
    summary(f'Relayed review feedback for automation PR {number} to main.')


def require_approval(gh, pr):
    reviews = gh.pages(f"pulls/{pr['number']}/reviews?per_page=100")
    humans = {item['user']['login'] for item in reviews if (item.get('user') or {}).get('type') == 'User'}
    writers = {login for login in humans if gh.api(
        f"collaborators/{quote(login, safe='')}/permission").get('permission') in ('write', 'maintain', 'admin')}
    if not human_approved(pr, [item for item in reviews if item['user']['login'] in writers]):
        raise ValueError('Publication requires human approval of the merged PR head')


def attested(gh, pr):
    merge = pr.get('merge_commit_sha')
    if not merge or not (pr.get('merged') or pr.get('merged_at')):
        return False
    return any(status.get('context') == 'nerdtools/human-approval'
               and status.get('state') == 'success'
               and status.get('creator', {}).get('login') == BOT
               and status.get('description') == f"Approved head {pr['head']['sha']}"
               and status.get('target_url') == pr['html_url']
               for status in gh.pages(f'commits/{merge}/statuses?per_page=100'))


def authorize(gh, event):
    sha = os.environ['GITHUB_SHA']
    requests = gh.pages(f'pulls?state=closed&head={quote(gh.repository.split("/")[0] + ":" + BRANCH)}&per_page=100')
    for pr in requests:
        if not trusted_pr(pr, gh.repository) or not pr.get('merged_at'):
            continue
        comparison = gh.api(f"compare/{pr['merge_commit_sha']}...{sha}")
        if comparison['status'] in ('ahead', 'identical'):
            if not attested(gh, pr):
                raise ValueError('Publication requires durable human approval of the merged PR head')
    summary('All merged automation batches in this source revision have human approval.')


def release(gh, event):
    number = event_pr(event)
    if not number:
        return
    pr = gh.api(f'pulls/{number}')
    if not trusted_pr(pr, gh.repository):
        return
    if not attested(gh, pr):
        require_approval(gh, pr)
        if not pr.get('merge_commit_sha'):
            raise ValueError('Merged PR has no merge commit SHA')
        gh.api(f"statuses/{pr['merge_commit_sha']}", {
            'context': 'nerdtools/human-approval', 'state': 'success',
            'description': f"Approved head {pr['head']['sha']}", 'target_url': pr['html_url']})
    gh.dispatch('unraid-releases.yml')
    summary(f'Approved merge of PR {number}: dispatched default-branch compatibility and release discovery.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare_parser = commands.add_parser('prepare')
    prepare_parser.add_argument('--output', type=Path, required=True)
    prepare_parser.add_argument('--evaluation', action='store_true')
    prepare_parser.add_argument('--package')
    finish_parser = commands.add_parser('finish')
    for name in ('plan', 'workspace', 'report'):
        finish_parser.add_argument('--' + name, type=Path, required=True)
    finish_parser.add_argument('--dry-run', action='store_true')
    finish_parser.add_argument('--output', type=Path)
    for name in ('feedback', 'release', 'authorize'):
        commands.add_parser(name)
    args = parser.parse_args()
    gh = GitHub(os.environ['GH_REPO'])
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text()) if os.environ.get('GITHUB_EVENT_PATH') else {}
    repo = Path(__file__).resolve().parents[1]
    if args.command == 'prepare':
        prepare(repo, gh, event, args.output, evaluation=args.evaluation, package=args.package)
    elif args.command == 'finish':
        finish(repo, gh, args.plan, args.workspace, args.report, dry_run=args.dry_run, output=args.output)
    else:
        globals()[args.command](gh, event)


if __name__ == '__main__':
    main()
