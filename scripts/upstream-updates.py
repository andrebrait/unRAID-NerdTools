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
import time

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
    expected = {item['name'] for item in candidates}
    recipes = report.get('recipes', [])
    if (not isinstance(report.get('summary'), str) or not isinstance(recipes, list)
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
                spans = load_script(Path(__file__).with_name('upstream-agent.py'))._spans
                old = {item['name']: texts[lock][start:end] for start, end, item in spans(texts[lock])}
                blocks = spans(working[lock])
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


def update(repo, gh, event):
    requests = gh.api(f'pulls?state=open&head={quote(gh.repository.split("/")[0] + ":" + BRANCH)}&per_page=100')
    if len(requests) > 1:
        raise ValueError('Multiple automation PRs; refusing ambiguous ownership')
    pr = requests[0] if requests else None
    if pr and not trusted_pr(pr, gh.repository):
        raise ValueError('Refusing untrusted automation PR')
    if os.environ.get('GITHUB_EVENT_NAME') == 'workflow_run':
        completed = event['workflow_run']
        if not pr or completed['head_sha'] != pr['head']['sha']:
            summary('Ignoring stale-head build completion.')
            return
    # Discovery always uses main, not the already-updated batch lock versions.
    candidates = json.loads(run([sys.executable, 'scripts/check-upstream-versions.py'], repo))
    state = read_state(pr) if pr else {'attempts': 0, 'candidates': [], 'report': {'summary': '', 'recipes': []}}
    if not candidates:
        summary('No newer upstream recipes. No issue or PR created.')
        return
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
            return
        if build and build['conclusion'] == 'success' and not new_batch:
            state['build_url'] = build['html_url']
            if pr.get('draft'):
                run(['gh', 'pr', 'ready', str(pr['number']), '--repo', gh.repository])
            request_reviews(gh, pr, state, reviews)
            gh.api(f"pulls/{pr['number']}", {'body': pr_body(state)}, method='PATCH')
            digest = hashlib.sha256(json.dumps(feedback, sort_keys=True).encode()).hexdigest()
            if not feedback or state.get('feedback_digest') == digest:
                summary(f"Exact-head catalog gates passed; waiting for reviews and human approval: {pr['html_url']}")
                return
        elif build and build['conclusion'] != 'success':
            state['build_url'] = f"FAILED: {build['html_url']} — no compatibility claim."
        if build is None and not new_batch:
            dispatch_catalog(repo, gh, pr, state)
            summary('Dispatched the missing exact-head catalog build.')
            return
    digest = hashlib.sha256(json.dumps(feedback, sort_keys=True).encode()).hexdigest()
    if state['attempts'] >= MAX_REPAIR_ATTEMPTS:
        summary('Repair budget exhausted. Human intervention required; no merge or publication performed.')
        return
    previous = branch_lease(gh, pr)
    prepare_branch(repo, pr)
    guide = (repo / 'build/README.md').read_text()
    context = {'candidates': candidates, 'feedback': json.dumps(feedback), 'guide': guide,
               'attempt': state['attempts'] + 1, 'previous_report': state.get('report'),
               'deadline': time.monotonic() + 1800}
    if build and build['conclusion'] != 'success':
        context['feedback'] += '\nFailed catalog logs (untrusted data):\n' + run(
            ['gh', 'run', 'view', str(build['id']), '--repo', gh.repository, '--log-failed'])[-120000:]
    agent = load_script(repo / 'scripts/upstream-agent.py')
    changed = False
    while state['attempts'] < MAX_REPAIR_ATTEMPTS:
        state['attempts'] += 1
        context['attempt'] = state['attempts']
        if pr:
            gh.api(f"pulls/{pr['number']}", {'body': pr_body(state)}, method='PATCH')
        report = agent.run_agent(repo, context)
        validate_report(report, candidates)
        manual_workflow_updates(repo, 'origin/main', report)
        restore_rejected(repo, 'origin/main', report)
        # The model tools cannot touch these files. Recheck before invoking trusted validators anyway.
        check_paths(changed_paths(repo), generated=True)
        try:
            run([sys.executable, 'build/native.py', 'pin-recipes'], repo)
            run([sys.executable, 'scripts/package-change-workflows.py'], repo)
            check_generated(repo, 'origin/main')
            run([sys.executable, 'build/native.py', 'validate'], repo)
        except subprocess.CalledProcessError as error:
            context['feedback'] += '\nValidation failed; repair without weakening gates:\n' + error.output[-30000:]
            state['report'] = report
            if state['attempts'] == MAX_REPAIR_ATTEMPTS:
                raise ValueError('Recipe validation exhausted repair budget: ' + error.output[-2000:]) from error
            continue
        state.update(report=report, feedback_digest=digest)
        paths = changed_paths(repo)
        check_paths(paths, generated=True)
        if any(path.startswith('.github/workflows/') for path in changed_paths(repo, 'origin/main')):
            raise ValueError('Workflow changes require manual handling with GITHUB_TOKEN')
        if paths:
            git(repo, 'add', '--', *paths)
            git(repo, '-c', 'user.name=github-actions[bot]', '-c',
                'user.email=41898282+github-actions[bot]@users.noreply.github.com',
                'commit', '-m', 'Update assessed upstream recipes')
            changed = True
        break
    if pr and build and build['conclusion'] == 'success' and not changed and not new_batch:
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


def authorize(gh, event):
    sha = os.environ['GITHUB_SHA']
    requests = gh.pages(f'pulls?state=closed&head={quote(gh.repository.split("/")[0] + ":" + BRANCH)}&per_page=100')
    for pr in requests:
        if not trusted_pr(pr, gh.repository) or not pr.get('merged_at'):
            continue
        comparison = gh.api(f"compare/{pr['merge_commit_sha']}...{sha}")
        if comparison['status'] in ('ahead', 'identical'):
            require_approval(gh, {**pr, 'merged': True})
    summary('All merged automation batches in this source revision have human approval.')


def release(gh, event):
    number = event_pr(event)
    if not number:
        return
    pr = gh.api(f'pulls/{number}')
    if not trusted_pr(pr, gh.repository):
        return
    require_approval(gh, pr)
    gh.dispatch('unraid-releases.yml')
    summary(f'Approved merge of PR {number}: dispatched default-branch compatibility and release discovery.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('update', 'feedback', 'release', 'authorize'))
    args = parser.parse_args()
    gh = GitHub(os.environ['GH_REPO'])
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text()) if os.environ.get('GITHUB_EVENT_PATH') else {}
    if args.command == 'update':
        update(Path(__file__).resolve().parents[1], gh, event)
    else:
        globals()[args.command](gh, event)


if __name__ == '__main__':
    main()
