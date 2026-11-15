#!/usr/bin/env python3
"""Notify maintainers about terminal automation workflow outcomes."""
import hashlib
import html
import importlib.util
import json
import os
from pathlib import Path
import re
from urllib.parse import quote

WORKFLOWS = {
    'Update upstream recipes': 'upstream-versions.yml',
    'Release merged upstream recipes': 'upstream-release.yml',
    'Discover Unraid releases': 'unraid-releases.yml',
    'Build native packages': 'build-packages.yml',
}
BOT = 'github-actions[bot]'
BRANCH = 'automation/upstream-recipes'


def safe_text(value):
    return html.escape(str(value or '').replace('@', '@\u200b'), quote=False)


def trusted_pr(pr, repository, merged=False):
    return ((not merged or (pr.get('state') == 'closed' and pr.get('merged') and pr.get('merged_at')))
            and pr.get('head', {}).get('ref') == BRANCH
            and pr.get('head', {}).get('repo', {}).get('full_name') == repository
            and pr.get('base', {}).get('ref') == 'main'
            and pr.get('base', {}).get('repo', {}).get('full_name') == repository
            and pr.get('user', {}).get('login') == BOT)


def marker(kind, identity):
    return f'<!-- upstream-notify: {kind}:{identity} -->'


def comment(gh, number, body, identity):
    if any(item.get('user', {}).get('login') == BOT and identity in (item.get('body') or '')
           for item in gh.pages(f'issues/{number}/comments?per_page=100')):
        return False
    gh.api(f'issues/{number}/comments', {'body': body}, method='POST')
    return True


def failure_issue(gh, run, target):
    source = run.get('_verified_source_sha') or run.get('source_head_sha') or run['head_sha']
    identity = f'{source}:{run["name"]}:{target}'
    tag = marker('failure', identity)
    issues = gh.pages('issues?state=all&per_page=100')
    existing = next((issue for issue in issues if issue.get('user', {}).get('login') == BOT
                     and tag in (issue.get('body') or '')), None)
    return identity, tag, existing


def run_order(run):
    return int(run['id']), int(run.get('run_attempt', 1))


def result_marker(run, result):
    run_id, attempt = run_order(run)
    return marker('result', f'{run_id}:{attempt}:{result}')


def latest_result(body):
    matches = re.findall(r'<!-- upstream-notify: result:(\d+):(\d+):(failure|success) -->', body or '')
    return max(((int(run_id), int(attempt), result) for run_id, attempt, result in matches),
               default=(0, 0, ''))


def success_context(run, target):
    source = run.get('_verified_source_sha') or run.get('source_head_sha') or run['head_sha']
    identity = f'{source}:{run["name"]}:{target}'
    return source, 'nerdtools/upstream-success-' + hashlib.sha256(identity.encode()).hexdigest()[:24]


def latest_success(gh, run, target):
    source, context = success_context(run, target)
    orders = []
    for status in gh.pages(f'commits/{source}/statuses?per_page=100'):
        match = re.fullmatch(r'Successful run (\d+):(\d+)', status.get('description') or '')
        if (status.get('context') == context and status.get('state') == 'success'
                and status.get('creator', {}).get('login') == BOT and match):
            orders.append((int(match.group(1)), int(match.group(2))))
    return max(orders, default=(0, 0))


def record_success(gh, run, target):
    source, context = success_context(run, target)
    if run_order(run) > latest_success(gh, run, target):
        run_id, attempt = run_order(run)
        gh.api(f'statuses/{source}', {
            'context': context, 'state': 'success',
            'description': f'Successful run {run_id}:{attempt}',
            'target_url': run['html_url'],
        }, method='POST')


def stale_failure(gh, run, target):
    return latest_success(gh, run, target) >= run_order(run)


def terminal(gh, run, success, target):
    if success:
        record_success(gh, run, target)
    _, tag, issue = failure_issue(gh, run, target)
    current = run_order(run)
    latest_id, latest_attempt, _ = latest_result((issue or {}).get('body'))
    if current < (latest_id, latest_attempt):
        return False
    if success:
        if issue and (issue.get('state') == 'open' or current > (latest_id, latest_attempt)):
            body = (issue.get('body') or '') + '\n' + result_marker(run, 'success')
            gh.api(f"issues/{issue['number']}", {'state': 'closed', 'body': body}, method='PATCH')
        return False
    if stale_failure(gh, run, target):
        return False
    if issue:
        previous = latest_result(issue.get('body'))
        if previous[:2] == current:
            return False
        body = (issue.get('body') or '') + '\n' + result_marker(run, 'failure')
        body += f'\n\nFailure run: {run["html_url"]}'
        data = {'body': body}
        if issue.get('state') == 'closed':
            data['state'] = 'open'
        gh.api(f"issues/{issue['number']}", data, method='PATCH')
        if stale_failure(gh, run, target):
            gh.api(f"issues/{issue['number']}", {'state': 'closed'}, method='PATCH')
            return False
        return True
    title = f"Automation failure: {run['name']} ({run['head_sha'][:12]})"
    body = (f'{tag}\n{result_marker(run, "failure")}\n\n'
            f'Workflow **{safe_text(run["name"])}** failed for `{run["head_sha"]}`. '
            f'Target: {safe_text(target)}.\n\n[Workflow run]({run["html_url"]})\n\n'
            'This tracks automation failure separately from upstream recipe issues.')
    created = gh.api('issues', {'title': title, 'body': body}, method='POST')
    if stale_failure(gh, run, target):
        if isinstance(created, dict) and isinstance(created.get('number'), int):
            gh.api(f"issues/{created['number']}", {'state': 'closed'}, method='PATCH')
        return False
    return True


def relevant_pr(gh, source_sha):
    candidates = gh.pages(f'pulls?state=closed&head={quote(gh.repository.split("/")[0] + ":" + BRANCH)}&per_page=100')
    for candidate in sorted(candidates, key=lambda item: item.get('merged_at') or '', reverse=True):
        if (not candidate.get('merged_at') or candidate.get('head', {}).get('ref') != BRANCH
                or candidate.get('head', {}).get('repo', {}).get('full_name') != gh.repository
                or candidate.get('base', {}).get('ref') != 'main'
                or candidate.get('base', {}).get('repo', {}).get('full_name') != gh.repository
                or candidate.get('user', {}).get('login') != BOT):
            continue
        pr = gh.api(f"pulls/{candidate['number']}")
        if not trusted_pr(pr, gh.repository, merged=True) or not pr.get('merge_commit_sha'):
            continue
        merge_sha = pr['merge_commit_sha']
        if merge_sha == source_sha:
            return pr
        comparison = gh.api(f'compare/{quote(merge_sha, safe="")}...{quote(source_sha, safe="")}')
        if comparison.get('status') == 'ahead':
            return pr
    return None
def process(event, gh):
    if event.get('repository', {}).get('full_name') != gh.repository:
        return False
    run = event.get('workflow_run') or {}
    name = run.get('name')
    if name not in WORKFLOWS or run.get('path') != f'.github/workflows/{WORKFLOWS[name]}':
        return False
    if (run.get('repository', {}).get('full_name') != gh.repository
            or run.get('head_repository', {}).get('full_name') != gh.repository):
        return False
    expected_event = 'pull_request' if name == 'Release merged upstream recipes' else None
    if name == 'Build native packages':
        expected_event = 'workflow_dispatch'
    elif name == 'Update upstream recipes':
        if run.get('event') not in ('schedule', 'workflow_dispatch', 'workflow_run'):
            return False
    elif name == 'Discover Unraid releases':
        if run.get('event') not in ('schedule', 'workflow_dispatch'):
            return False
    if expected_event and run.get('event') != expected_event:
        return False
    if name == 'Update upstream recipes' and run.get('display_title') != 'Upstream recipe update':
        return False
    if run.get('event') == 'pull_request':
        verified = []
        for reference in run.get('pull_requests') or []:
            number = reference.get('number')
            if isinstance(number, int):
                pr = gh.api(f'pulls/{number}')
                if trusted_pr(pr, gh.repository, merged=True) and pr.get('merge_commit_sha'):
                    verified.append(pr)
        if not verified:
            return False
        run['_verified_pr'] = verified[0]
        run['_verified_source_sha'] = verified[0]['merge_commit_sha']
    elif run.get('head_branch') != 'main':
        return False
    success = run.get('conclusion') == 'success'
    if name == 'Discover Unraid releases' and success:
        terminal(gh, run, True, run.get('source_head_sha') or run.get('head_sha'))
        return False
    if name == 'Build native packages':
        if (run.get('event') != 'workflow_dispatch' or run.get('head_branch') != 'main'
                or not re.fullmatch(r'Unraid compatibility [0-9][A-Za-z0-9.+_-]* [0-9a-f]{64} publish=true',
                                    run.get('display_title', ''))):
            return False
        if not success and stale_failure(gh, run, run['display_title']):
            return False
        jobs = gh.pages(
            f'actions/runs/{run["id"]}/attempts/{run.get("run_attempt", 1)}/jobs?per_page=100',
            field='jobs')
        published = any(job.get('name') == 'publish' and job.get('conclusion') == 'success' for job in jobs)
        if success and not published:
            note = 'Build checks succeeded; publication was skipped, so no release is reported.'
        elif published:
            note = 'Publication succeeded.'
        else:
            note = 'The build workflow failed; publication was not confirmed.'
        result = terminal(gh, run, True, run['display_title']) if success and published else (
            terminal(gh, run, False, run['display_title']) if not success else False)
        pr = relevant_pr(gh, run['head_sha'])
        if pr and success:
            run_identity = f'{run["id"]}:{run.get("run_attempt", 1)}'
            tag = marker('run', run_identity)
            result = comment(gh, pr['number'],
                             f'{tag}\n\n{note} Workflow run: {run["html_url"]}', tag) or result
        return result
    if name == 'Update upstream recipes':
        if success:
            return terminal(gh, run, True, run['head_sha'])
        if run.get('event') == 'workflow_run' and run.get('head_branch') != 'main':
            return False
        return terminal(gh, run, False, run['head_sha'])
    source = run.get('_verified_source_sha') or run.get('source_head_sha') or run.get('head_sha')
    return terminal(gh, run, success, source)


def main():
    spec = importlib.util.spec_from_file_location('upstream_updates', Path(__file__).with_name('upstream-updates.py'))
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)
    gh = controller.GitHub(os.environ['GH_REPO'])
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    process(event, gh)


if __name__ == '__main__':
    main()
