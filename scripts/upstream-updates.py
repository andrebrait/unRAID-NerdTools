#!/usr/bin/env python3
"""Maintain one reviewed upstream recipe PR; never approve, merge, or publish it here."""
import argparse
import hashlib
import html
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
from urllib.parse import quote
import zlib
import tempfile
import tarfile
import zipfile

BRANCH = 'automation/upstream-recipes'
TITLE = 'Update upstream recipes'
BOT = 'github-actions[bot]'
LOCKS = ('build/locks/slackware.json', 'build/locks/slackbuilds.json')
STATE = '<!-- upstream-recipes-state\n'
MAX_REPAIR_ATTEMPTS = 3
ISSUE_STATE = '<!-- upstream-update-issue\n'
ISSUE_STATUS_START = '<!-- upstream-update-status -->'
ISSUE_STATUS_END = '<!-- /upstream-update-status -->'
ISSUE_STATUSES = {'queued', 'assessing', 'building', 'awaiting-review', 'blocked', 'rejected'}
ISSUE_FIELDS = ('name', 'upstream', 'source', 'url')
_UNSET = object()
NAME = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9+_.-]*\Z')

CANDIDATE_STATUS_CONTEXT = 'nerdtools/candidate-build'
CANDIDATE_STATE_KEYS = (
    'head_sha', 'run_id', 'run_attempt', 'package_artifact_id',
    'package_artifact_name', 'manifest_artifact_id', 'manifest_artifact_name',
    'manifest_sha256', 'build_url',
)
MAX_MANIFEST_ARCHIVE_SIZE = 2 * 1024 * 1024
MAX_MANIFEST_SIZE = 1024 * 1024


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

    def pages(self, path, field=None):
        pages = json.loads(run(['gh', 'api', '--paginate', '--slurp', f'{self.root}/{path}']))
        if field is None:
            if any(not isinstance(page, list) for page in pages):
                raise ValueError('Paginated GitHub response must contain arrays')
            return [item for page in pages for item in page]
        if any(not isinstance(page, dict) or not isinstance(page.get(field), list) for page in pages):
            raise ValueError(f'Paginated GitHub response has no {field} array')
        return [item for page in pages for item in page[field]]

    def download_artifact(self, artifact_id, limit):
        process = subprocess.Popen(
            ['gh', 'api', '-H', 'Accept: application/vnd.github+json',
             f'{self.root}/actions/artifacts/{artifact_id}/zip'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        data = bytearray()
        try:
            while len(data) <= limit:
                chunk = process.stdout.read(min(65536, limit + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > limit:
                process.kill()
                process.wait()
                raise ValueError('Manifest artifact download exceeds the size limit')
            if process.wait() != 0:
                raise ValueError('Could not download manifest artifact')
            return bytes(data)
        finally:
            process.stdout.close()

    def dispatch(self, workflow, ref='main', inputs=None):
        self.api(f'actions/workflows/{workflow}/dispatches', {'ref': ref, 'inputs': inputs or {}})


def read_candidate_manifest_archive(archive):
    if not isinstance(archive, bytes) or not archive or len(archive) > MAX_MANIFEST_ARCHIVE_SIZE:
        raise ValueError('Manifest artifact archive exceeds the size limit')
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as source:
            members = source.infolist()
            if len(members) != 1:
                raise ValueError('Manifest artifact must contain exactly one file')
            member = members[0]
            mode = member.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            if (member.filename != 'candidate-manifest.json' or member.is_dir()
                    or file_type not in (0, stat.S_IFREG) or member.flag_bits & 1
                    or member.file_size < 1 or member.file_size > MAX_MANIFEST_SIZE
                    or member.compress_size > MAX_MANIFEST_ARCHIVE_SIZE):
                raise ValueError('Manifest artifact contains an unsafe file')
            with source.open(member) as stream:
                data = stream.read(MAX_MANIFEST_SIZE + 1)
            if len(data) != member.file_size or len(data) > MAX_MANIFEST_SIZE:
                raise ValueError('Manifest artifact extraction exceeds the size limit')
            return data
    except (OSError, RuntimeError, NotImplementedError, EOFError, zlib.error,
            zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise ValueError('Invalid manifest artifact archive') from error


def clear_candidate_state(state):
    for key in CANDIDATE_STATE_KEYS:
        state.pop(key, None)


def set_candidate_status(gh, pr, sha, state):
    descriptions = {
        'pending': 'Waiting for exact-head catalog and candidate artifacts.',
        'success': 'Exact-head catalog and candidate manifest verified.',
        'failure': 'Catalog build or candidate artifact validation failed.',
    }
    if state not in descriptions or sha != pr.get('head', {}).get('sha'):
        raise ValueError('Candidate status must target the current PR head')
    gh.api(f'statuses/{sha}', {
        'context': CANDIDATE_STATUS_CONTEXT, 'state': state,
        'description': descriptions[state], 'target_url': pr['html_url'],
    })


def candidate_artifact_state(gh, completed, pr):
    run_id, attempt = completed.get('id'), completed.get('run_attempt')
    head = completed.get('head_sha')
    completed_repo = completed.get('head_repository')
    if (type(run_id) is not int or run_id < 1 or type(attempt) is not int or attempt < 1
            or head != pr.get('head', {}).get('sha')
            or completed.get('head_branch') != BRANCH
            or completed.get('event') != 'workflow_dispatch'
            or not isinstance(completed_repo, dict)
            or completed_repo.get('full_name') != gh.repository):
        raise ValueError('Workflow run identity does not match the PR')
    run = gh.api(f'actions/runs/{run_id}')
    run_repo = run.get('head_repository') if isinstance(run, dict) else None
    run_path = run.get('path') if isinstance(run, dict) else None
    if (not isinstance(run, dict) or type(run.get('id')) is not int or run['id'] != run_id
            or type(run.get('run_attempt')) is not int or run['run_attempt'] != attempt
            or run.get('head_sha') != head or run.get('head_branch') != BRANCH
            or run.get('event') != 'workflow_dispatch' or run.get('status') != 'completed'
            or run.get('conclusion') != 'success' or not isinstance(run_repo, dict)
            or run_repo.get('full_name') != gh.repository or not isinstance(run_path, str)
            or run_path.split('@', 1)[0] != '.github/workflows/build-packages.yml'):
        raise ValueError('GitHub workflow run identity is incomplete or mismatched')
    package_name = f'candidate-packages-{run_id}-{attempt}'
    manifest_name = f'candidate-manifest-{run_id}-{attempt}'
    artifacts = gh.pages(f'actions/runs/{run_id}/artifacts?per_page=100', field='artifacts')
    selected = {}
    for artifact in artifacts:
        name = artifact.get('name') if isinstance(artifact, dict) else None
        if name not in (package_name, manifest_name):
            continue
        workflow_run = artifact.get('workflow_run')
        artifact_id = artifact.get('id')
        if (name in selected or type(artifact_id) is not int or artifact_id < 1
                or artifact.get('expired') is not False or not isinstance(workflow_run, dict)
                or type(workflow_run.get('id')) is not int or workflow_run['id'] != run_id
                or workflow_run.get('head_sha') != head
                or type(artifact.get('size_in_bytes')) is not int
                or artifact['size_in_bytes'] < 1):
            raise ValueError('Candidate artifact is expired, duplicated, or from another run')
        selected[name] = artifact
    if set(selected) != {package_name, manifest_name}:
        raise ValueError('Exact package and manifest artifact set is incomplete')
    package = selected[package_name]
    manifest_artifact = selected[manifest_name]
    if manifest_artifact['size_in_bytes'] > MAX_MANIFEST_ARCHIVE_SIZE:
        raise ValueError('Manifest artifact archive exceeds the size limit')
    commit = gh.api(f'commits/{head}')
    tree = (commit.get('commit') or {}).get('tree', {}).get('sha') if isinstance(commit, dict) else None
    if (not isinstance(tree, str)
            or not re.fullmatch(r'(?:[a-f0-9]{40}|[a-f0-9]{64})', tree)):
        raise ValueError('Candidate head has no valid commit tree')
    manifest_bytes = read_candidate_manifest_archive(
        gh.download_artifact(manifest_artifact['id'], MAX_MANIFEST_ARCHIVE_SIZE))
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError('Candidate manifest is not valid JSON') from error
    expected = {
        'repository': gh.repository, 'pr': pr['number'], 'head': head, 'tree': tree,
        'workflow': 'build-packages.yml', 'run': run_id, 'attempt': attempt,
        'artifact_name': package_name,
    }
    if (not isinstance(manifest, dict) or type(manifest.get('schema')) is not int
            or manifest['schema'] != 1
            or any(manifest.get(key) != value for key, value in expected.items())
            or not isinstance(manifest.get('packages'), list) or not manifest['packages']):
        raise ValueError('Candidate manifest identity does not match the build')
    build_url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com').rstrip('/')}/{gh.repository}/actions/runs/{run_id}"
    return {
        'head_sha': head, 'run_id': run_id, 'run_attempt': attempt,
        'package_artifact_id': package['id'], 'package_artifact_name': package_name,
        'manifest_artifact_id': manifest_artifact['id'],
        'manifest_artifact_name': manifest_name,
        'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
        'build_url': build_url,
    }


def candidate_state_matches(state, run, pr):
    run_id, attempt = run.get('id'), run.get('run_attempt')
    return (type(run_id) is int and type(attempt) is int
            and state.get('head_sha') == pr.get('head', {}).get('sha')
            and state.get('run_id') == run_id and state.get('run_attempt') == attempt
            and type(state.get('package_artifact_id')) is int and state['package_artifact_id'] > 0
            and state.get('package_artifact_name') == f'candidate-packages-{run_id}-{attempt}'
            and type(state.get('manifest_artifact_id')) is int and state['manifest_artifact_id'] > 0
            and state.get('manifest_artifact_name') == f'candidate-manifest-{run_id}-{attempt}'
            and isinstance(state.get('manifest_sha256'), str)
            and re.fullmatch(r'[a-f0-9]{64}', state['manifest_sha256'])
            and isinstance(state.get('build_url'), str) and bool(state['build_url']))


def update_candidate_build(gh, pr, state, run):
    clear_candidate_state(state)
    run_id = run.get('id')
    build_url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com').rstrip('/')}"
                 f"/{gh.repository}/actions/runs/{run_id}") if type(run_id) is int else None
    if build_url:
        state['build_url'] = build_url
    if run.get('status') != 'completed':
        result = 'pending'
    elif run.get('conclusion') != 'success':
        result = 'failure'
    else:
        try:
            state.update(candidate_artifact_state(gh, run, pr))
            result = 'success'
        except (ValueError, TypeError, KeyError, OSError, subprocess.CalledProcessError) as error:
            state['build_url'] = build_url
            result = 'failure'
    set_candidate_status(gh, pr, pr['head']['sha'], result)
    gh.api(f"pulls/{pr['number']}", {'body': pr_body(state)}, method='PATCH')
    return result



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

def candidate_identity(candidate):
    identity = {key: candidate.get(key) for key in ISSUE_FIELDS}
    if any(not isinstance(value, str) or not value for value in identity.values()):
        raise ValueError('Invalid upstream candidate identity')
    return identity


def read_issue_state(issue):
    if (issue.get('user') or {}).get('login') != BOT:
        return None
    body = issue.get('body') or ''
    if ISSUE_STATE not in body:
        return None
    if body.count(ISSUE_STATE) != 1:
        raise ValueError('Upstream issue has ambiguous controller state')
    payload = body.split(ISSUE_STATE, 1)[1].split('\n-->', 1)[0]
    try:
        state = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ValueError('Upstream issue controller state is invalid') from error
    if (not isinstance(state, dict)
            or set(state) != {'candidate', 'attempts', 'status', 'decision', 'failure', 'run_url', 'build_url'}
            or not isinstance(state['candidate'], dict)
            or set(state['candidate']) != set(ISSUE_FIELDS)
            or any(not isinstance(value, str) or not value for value in state['candidate'].values())
            or isinstance(state['attempts'], bool) or not isinstance(state['attempts'], int)
            or not 0 <= state['attempts'] <= MAX_REPAIR_ATTEMPTS
            or not isinstance(state['status'], str) or state['status'] not in ISSUE_STATUSES
            or state['decision'] not in (None, 'accept', 'reject')
            or any(state[key] is not None and not isinstance(state[key], str)
                   for key in ('failure', 'run_url', 'build_url'))):
        raise ValueError('Upstream issue controller state has an invalid schema')
    return state


def issue_body(issue, state):
    body = issue.get('body') or ''
    if ISSUE_STATE in body:
        body = body.rsplit(ISSUE_STATE, 1)[0].rstrip()
    lines = [
        ISSUE_STATUS_START,
        f"**Status:** {state['status']} ({state['attempts']}/{MAX_REPAIR_ATTEMPTS} agent sessions)",
    ]
    if state.get('run_url'):
        lines.append(f"[Workflow run]({state['run_url']})")
    if state.get('build_url'):
        lines.append(f"[Catalog build]({state['build_url']})")
    if state.get('failure'):
        lines.append(f"**Latest failure:** {html.escape(state['failure'], quote=False).replace('@', '&#64;').replace('#', '&#35;')}")
    lines.append(ISSUE_STATUS_END)
    status_block = '\n'.join(lines)
    if ISSUE_STATUS_START in body:
        before, rest = body.split(ISSUE_STATUS_START, 1)
        _, after = rest.split(ISSUE_STATUS_END, 1)
        body = before.rstrip() + '\n\n' + status_block + after
    else:
        body = body.rstrip() + '\n\n' + status_block
    stored = json.dumps(state, sort_keys=True).replace('<', '\\u003c').replace('>', '\\u003e')
    stored = stored.replace('@', '\\u0040').replace('#', '\\u0023')
    return body.rstrip() + '\n\n' + ISSUE_STATE + stored + '\n-->'


def issue_title(candidate):
    return f"Upstream update: {candidate['name']} {candidate['ours']} -> {candidate['upstream']}"


def issue_description(candidate):
    return (f"## Upstream update: {candidate['name']}\n\n"
            f"- Current version: `{candidate['ours']}`\n"
            f"- Proposed version: `{candidate['upstream']}`\n"
            f"- Source: [{candidate['source']}]({candidate['url']})\n\n"
            "This candidate is tracked independently from the reviewed batch PR.")


def issue_matches_candidate(issue, candidate):
    if (issue.get('pull_request') or (issue.get('user') or {}).get('login') != BOT):
        return False
    try:
        state = read_issue_state(issue)
    except ValueError:
        state = None
    identity = candidate_identity(candidate)
    if state:
        return state['candidate'] == identity
    title = issue.get('title') or ''
    title_prefix = f"Upstream update: {candidate['name']} "
    title_suffix = f" -> {candidate['upstream']}"
    return (title.startswith(title_prefix) and title.endswith(title_suffix)
            and len(title) > len(title_prefix) + len(title_suffix)
            and identity['url'] in (issue.get('body') or ''))


def candidate_record(candidate, issue, state, repository):
    return {'candidate': candidate_identity(candidate), 'number': issue['number'],
            'issue_url': issue.get('html_url') or
                         f"https://github.com/{repository}/issues/{issue['number']}",
            **state}


def sync_candidate_issues(gh, candidates, run_url=None, prior_records=()):
    existing = gh.pages('issues?state=all&per_page=100')
    records = []
    unique = {tuple(candidate_identity(item).values()): item for item in candidates}
    prior = [item for item in prior_records if isinstance(item, dict)]
    for key in sorted(unique):
        candidate = unique[key]
        matches = [item for item in existing if issue_matches_candidate(item, candidate)]
        closed_matches = [item for item in matches if item.get('state') == 'closed']
        issue = min(closed_matches or matches, key=lambda item: item['number']) if matches else None
        prior_record = next((item for item in prior
                             if item.get('candidate') == candidate_identity(candidate)), None)
        if issue is None and prior_record:
            records.append({**prior_record, 'candidate': candidate_identity(candidate)})
            continue
        if issue is None:
            state = {'candidate': candidate_identity(candidate), 'attempts': 0, 'status': 'queued',
                     'decision': None, 'failure': None, 'run_url': run_url, 'build_url': None}
            draft = {'title': issue_title(candidate), 'labels': ['upstream-update'], 'body': issue_body(
                {'body': issue_description(candidate)}, state)}
            issue = gh.api('issues', draft, method='POST')
        else:
            try:
                state = read_issue_state(issue)
            except ValueError:
                state = None
            if state is None:
                state = {'candidate': candidate_identity(candidate), 'attempts': 0,
                         'status': 'blocked' if ISSUE_STATE in (issue.get('body') or '') else 'queued',
                         'decision': None,
                         'failure': ('Issue metadata is invalid; maintainer review required'
                                     if ISSUE_STATE in (issue.get('body') or '') else None),
                         'run_url': run_url, 'build_url': None}
            if issue.get('state') == 'closed':
                state['status'] = 'blocked'
            if issue.get('state') != 'closed':
                labels = {item.get('name') if isinstance(item, dict) else item
                          for item in issue.get('labels', [])}
                update = {'body': issue_body(issue, state)}
                if 'upstream-update' not in labels:
                    update['labels'] = sorted((labels - {None}) | {'upstream-update'})
                if update['body'] != (issue.get('body') or '') or 'labels' in update:
                    issue = gh.api(f"issues/{issue['number']}", update, method='PATCH')
        record = candidate_record(candidate, issue, state, gh.repository)
        if issue.get('state') == 'closed':
            record['status'] = 'closed'
        records.append(record)
    return records


def set_issue_status(gh, record, status, attempts=None, decision=_UNSET, failure=_UNSET,
                     run_url=_UNSET, build_url=_UNSET, expected=None):
    if status not in ISSUE_STATUSES:
        raise ValueError('Invalid upstream issue status')
    number = record.get('number')
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise ValueError('Invalid upstream issue number')
    issue = gh.api(f"issues/{number}")
    state = read_issue_state(issue) if expected is not None else None
    if expected is not None and (
            not state or state['candidate'] != expected.get('candidate')
            or state['status'] != expected.get('status')
            or state['attempts'] != expected.get('attempts')
            or state['decision'] != expected.get('decision')):
        return record
    if issue.get('state') == 'closed':
        record['status'] = 'closed'
        return record
    if state is None:
        state = read_issue_state(issue)
    if not state or state['candidate'] != record['candidate']:
        raise ValueError('Upstream issue state changed or is untrusted')
    state['status'] = status
    if attempts is not None:
        state['attempts'] = attempts
    if failure is not _UNSET:
        state['failure'] = failure
    if decision is not _UNSET:
        if decision not in (None, 'accept', 'reject'):
            raise ValueError('Invalid upstream issue decision')
        state['decision'] = decision
    if run_url is not _UNSET:
        state['run_url'] = run_url
    if build_url is not _UNSET:
        state['build_url'] = build_url
    if (isinstance(state['attempts'], bool) or not isinstance(state['attempts'], int)
            or not 0 <= state['attempts'] <= MAX_REPAIR_ATTEMPTS):
        raise ValueError('Upstream issue repair budget is invalid')
    body = issue_body(issue, state)
    if body != (issue.get('body') or ''):
        issue = gh.api(f"issues/{issue['number']}", {'body': body}, method='PATCH')
    record.update(state)
    record['issue_url'] = issue.get('html_url') or record.get('issue_url')
    return record


def workflow_run_url(repository):
    if os.environ.get('GITHUB_RUN_URL'):
        return os.environ['GITHUB_RUN_URL']
    if os.environ.get('GITHUB_RUN_ID'):
        return f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repository}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    return None

def issue_record_for(records, candidate):
    identity = candidate_identity(candidate)
    return next((item for item in records if item.get('candidate') == identity), None)


def all_candidates_decided(state):
    candidates = state.get('candidates', [])
    records = state.get('candidate_issues', [])
    return bool(candidates) and all(
        (issue_record_for(records, candidate) or {}).get('status') in ('closed', 'rejected')
        for candidate in candidates)


def candidate_failures(records, candidates):
    failures = {}
    for candidate in candidates:
        record = issue_record_for(records, candidate)
        if record and isinstance(record.get('failure'), str) and record['failure']:
            failures[candidate['name']] = record['failure']
    return failures


def update_candidate_issues(gh, records, candidates, status, **changes):
    for candidate in candidates:
        record = issue_record_for(records, candidate)
        if record and record.get('status') != 'closed':
            set_issue_status(gh, record, status, **changes)


def verify_issue_attempts(gh, records, candidates):
    for candidate in candidates:
        record = issue_record_for(records, candidate)
        if not record:
            continue
        number = record.get('number')
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise ValueError('Invalid upstream issue number')
        issue = gh.api(f"issues/{number}")
        current = read_issue_state(issue)
        unchanged_closed = (
            record.get('status') == 'closed' and issue.get('state') == 'closed' and current
            and current['candidate'] == record['candidate']
            and current['attempts'] == record['attempts']
            and current['decision'] == record.get('decision'))
        unchanged_open = (
            issue.get('state') == 'open' and current
            and current['candidate'] == record['candidate']
            and current['attempts'] == record['attempts']
            and current['status'] == record['status']
            and current['decision'] == record.get('decision'))
        if not (unchanged_closed or unchanged_open):
            raise ValueError('Upstream issue changed during agent work; refusing stale update')



def feedback_actionable(item, source='mixed', bots=(), allow_bot_summary=False):
    path = item.get('path')
    line = item.get('line') or item.get('original_line')
    inline = (isinstance(path, str) and bool(path) and isinstance(line, int)
              and not isinstance(line, bool) and line > 0)
    state = item.get('state')
    user = item.get('user') or {}
    human_command = (user.get('type') != 'Bot' and user.get('login') not in set(bots) | {BOT}
                     and re.match(r'(?is)^/upstream repair\s+\S',
                                  (item.get('body') or '').strip()) is not None)
    bot_summary = (allow_bot_summary and state == 'COMMENTED' and user.get('type') == 'Bot'
                   and user.get('login') in bots
                   and not re.search(r'(?i)\b(no issues?|no findings?|all checks passed)\b',
                                     item.get('body') or ''))
    if source == 'inline':
        return inline
    if source == 'review':
        return state == 'CHANGES_REQUESTED' or bot_summary
    if source == 'comment':
        return not inline and human_command
    return inline or state == 'CHANGES_REQUESTED' or human_command or bot_summary


def current_feedback(evidence, sha, committed_at, bots=(), source='mixed', allow_bot_summary=False):
    result = []
    for item in evidence:
        body = (item.get('body') or '').strip()
        if (not trusted_author(item, bots) or not body or item.get('state') in ('APPROVED', 'DISMISSED')
                or re.fullmatch(r'(?i)(lgtm|looks good|thanks|thank you|approved)[.! ]*', body)
                or 'usage limit' in body.lower()
                or not feedback_actionable(item, source, bots, allow_bot_summary)):
            continue
        commit = item.get('commit_id') or item.get('original_commit_id')
        if commit != sha and (commit or item.get('created_at', '') < committed_at):
            continue
        result.append({'id': item['id'], 'user': item['user']['login'], 'body': body[:20000],
                       'path': item.get('path'), 'line': item.get('line') or item.get('original_line'),
                       'state': item.get('state')})
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

def merge_retry_report(state, assessed, targets):
    target_names = {item['name'] for item in targets}
    frozen_names = [item['name'] for item in state.get('candidates', [])]
    statuses = {(record.get('candidate') or {}).get('name'): record.get('status')
                for record in state.get('candidate_issues', [])
                if isinstance(record, dict) and isinstance(record.get('candidate'), dict)}
    retained = {}
    for item in state.get('report', {}).get('recipes', []):
        if (not isinstance(item, dict) or set(item) != {'name', 'decision', 'reason'}
                or item.get('name') not in frozen_names
                or item.get('decision') not in ('accept', 'reject')
                or not isinstance(item.get('reason'), str) or not item['reason'].strip()):
            raise ValueError('Stored automation report is invalid')
        if item['name'] in retained:
            raise ValueError('Stored automation report has duplicate recipes')
        if item['name'] not in target_names:
            retained[item['name']] = item
    for name, status in statuses.items():
        if name in frozen_names and name not in target_names and status in ('rejected', 'closed'):
            retained[name] = {'name': name, 'decision': 'reject',
                              'reason': 'Candidate issue was rejected or closed; discard its changes.'}
    retained.update((item['name'], item) for item in assessed['recipes'])
    return {'summary': assessed['summary'],
            'recipes': [retained[name] for name in frozen_names if name in retained]}


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
    deleted = [relative for relative in paths
               if not (workspace / relative).is_file() or (workspace / relative).is_symlink()]
    for relative in sorted(deleted, key=lambda value: len(PurePosixPath(value).parts), reverse=True):
        destination = repo / relative
        if destination.is_dir() and not destination.is_symlink():
            destination.rmdir()
        else:
            destination.unlink(missing_ok=True)
    for relative in (item for item in paths if item not in deleted):
        source, destination = workspace / relative, repo / relative
        if destination.is_dir() and not destination.is_symlink():
            try:
                destination.rmdir()
            except OSError as error:
                raise ValueError(f'Cannot replace non-empty directory: {relative}') from error
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
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


def accepted_issue_numbers(repo, candidates, report, records):
    candidate_by_identity = {tuple(candidate_identity(item).values()): item for item in candidates}
    decisions = {item['name']: item['decision'] for item in report.get('recipes', [])}
    versions = {item['name']: item['version'] for lock in LOCKS
                for item in json.loads((repo / lock).read_text())}
    numbers = set()
    for record in records:
        candidate = candidate_by_identity.get(tuple((record.get('candidate') or {}).get(key)
                                                       for key in ISSUE_FIELDS))
        number = record.get('number')
        decision = decisions.get(candidate['name'], record.get('decision')) if candidate else None
        if (candidate and record.get('status') != 'closed' and isinstance(number, int)
                and not isinstance(number, bool) and number > 0
                and decision == 'accept' and versions.get(candidate['name']) == candidate['upstream']):
            numbers.add(number)
    return sorted(numbers)


def issue_reference(record):
    number = record.get('number')
    url = record.get('issue_url')
    if (not isinstance(number, int) or isinstance(number, bool) or number < 1
            or not isinstance(url, str)
            or not re.fullmatch(r'https://github\.com/[\w.-]+/[\w.-]+/issues/\d+', url)):
        return f"Issue {number}" if isinstance(number, int) and not isinstance(number, bool) else 'Issue'
    return f"[Issue {number}]({url})"



def pr_body(state):
    report = state.get('report', {'summary': 'Awaiting assessment', 'recipes': []})
    evidence = state.get('build_url')
    reviews = state.get('review_status', 'Bot review requests await successful catalog verification.')

    def safe(text):
        return html.escape(str(text), quote=False).replace('@', '&#64;').replace('#', '&#35;')

    def link(label, url):
        return f"[{label}]({html.escape(url, quote=True)})" if isinstance(url, str) and url.startswith('https://') else ''

    for limit in (240, 120, 60, 30):
        compact = {'summary': report['summary'][:limit * 4], 'recipes': [
            {**item, 'reason': item['reason'][:limit]} for item in report['recipes']]}
        candidate_issues = [{**item, 'failure': (item.get('failure') or '')[:limit]}
                            for item in state.get('candidate_issues', [])]
        stored = json.dumps({**state, 'report': compact, 'candidate_issues': candidate_issues}, sort_keys=True)
        stored = stored.replace('<', '\\u003c').replace('>', '\\u003e')
        stored = stored.replace('@', '\\u0040').replace('#', '\\u0023')
        decisions = '\n'.join(f"- **{safe(item['name'])}**: {item['decision']} — {safe(item['reason'])}"
                              for item in compact['recipes'])
        lifecycle = []
        for item in candidate_issues:
            candidate = item.get('candidate') or {}
            links = [issue_reference(item)]
            for key, label in (('run_url', 'Workflow run'), ('build_url', 'Catalog build')):
                target = link(label, item.get(key))
                if target:
                    links.append(target)
            failure = f" — Latest failure: {safe(item['failure'])}" if item.get('failure') else ''
            lifecycle.append(
                f"- **{safe(candidate.get('name', 'candidate'))}** — {safe(item.get('status', 'queued'))} "
                f"({item.get('attempts', 0)}/{MAX_REPAIR_ATTEMPTS}) — {'; '.join(links)}{failure}")
        run_link = link('Workflow run', state.get('run_url'))
        build_link = link('Catalog build', evidence)
        overview_links = ' · '.join(item for item in (run_link, build_link) if item)
        closure_numbers = sorted({number for number in state.get('issue_closures', [])
                                  if isinstance(number, int) and not isinstance(number, bool) and number > 0})
        closures = '\n'.join(f"Fixes #{number}" for number in closure_numbers)
        lifecycle_block = '\n'.join(lifecycle) or safe(state.get('status', 'assessing'))
        body = (f"## Upstream recipe batch\n\n{safe(compact['summary'])}\n\n{decisions}\n\n"
                f"## Candidate lifecycle\n\n{lifecycle_block}\n\n"
                f"## Acceptance\n\n{build_link or 'Not yet built; no compatibility claim.'}\n\n"
                f"## Reviews\n\n{safe(reviews)}\n\n{overview_links}\n\n"
                'Human approval is required before merge. This automation never approves or merges its PR. '
                'Publication follows approved merge and the default-branch compatibility gates.\n\n'
                f"Agent sessions this batch: {state.get('attempts', 0)}. Per-candidate budget: {MAX_REPAIR_ATTEMPTS}.\n\n"
                f"{closures}\n\n" + STATE + stored + '\n-->')
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

def recover_orphaned_publication(repo, gh, records, discovered):
    candidates = [candidate for candidate in discovered
                  if (record := issue_record_for(records, candidate))
                  and record.get('status') == 'building' and record.get('decision') == 'accept']
    if not candidates:
        return False
    previous = branch_lease(gh, None)
    if not previous:
        for candidate in candidates:
            record = issue_record_for(records, candidate)
            set_issue_status(
                gh, record, 'queued',
                failure=record.get('failure')
                or 'Publication failed before a recoverable branch was pushed; retrying assessment.')
        return False
    comparison = gh.api(f'compare/main...{quote(previous, safe="")}')
    commits = comparison.get('commits', [])
    files = comparison.get('files', [])
    if (not commits or len(commits) != comparison.get('total_commits') or len(files) >= 300
            or any((item.get('author') or {}).get('login') != BOT for item in commits)):
        raise ValueError('Leased automation branch is not a complete bot-authored update')
    check_paths([item['filename'] for item in files])
    closures = {int(number) for commit in commits
                for number in re.findall(
                    r'(?im)^Fixes #(\d+)\s*$',
                    ((commit.get('commit') or {}).get('message') or ''))}
    verify_issue_attempts(gh, records, candidates)
    current_records = [issue_record_for(records, candidate) for candidate in candidates]
    issues = {record['number'] for record in current_records
              if (record and record.get('status') == 'building'
                  and record.get('decision') == 'accept'
                  and isinstance(record.get('number'), int)
                  and not isinstance(record.get('number'), bool))}
    if len(issues) != len(candidates) or issues != closures:
        raise ValueError('Recovered candidate issue changed or closed during publication recovery')
    refs = gh.api(f'git/matching-refs/heads/{BRANCH}')
    current = next((item['object']['sha'] for item in refs
                    if item['ref'] == f'refs/heads/{BRANCH}'), '')
    if current != previous:
        raise ValueError('Automation branch changed during publication recovery')
    verify_issue_attempts(gh, records, candidates)
    report = {'summary': 'Recovered previously assessed changes after publication failed.',
              'recipes': [{'name': candidate['name'], 'decision': 'accept',
                           'reason': 'Accepted and validated before branch publication.'}
                          for candidate in candidates]}
    state = {'attempts': max(issue_record_for(records, item)['attempts'] for item in candidates),
             'batch': hashlib.sha256(json.dumps(candidates, sort_keys=True).encode()).hexdigest(),
             'candidates': candidates, 'candidate_issues': records, 'report': report,
             'issue_closures': sorted(issues), 'feedback_digest': '', 'reviewed_head': '',
             'status': 'building'}
    try:
        pr = gh.api('pulls', {'title': TITLE, 'head': BRANCH, 'base': 'main',
                              'draft': False, 'body': pr_body(state)})
        if (not trusted_pr(pr, gh.repository) or pr.get('state') != 'open'
                or pr.get('head', {}).get('sha') != previous):
            raise ValueError('Recovered PR does not match the leased automation branch')
        dispatch_catalog(repo, gh, pr, state)
    except Exception as error:
        detail = str(getattr(error, 'output', None) or error)[-20000:]
        for candidate in candidates:
            record = issue_record_for(records, candidate)
            if record.get('status') != 'closed':
                try:
                    set_issue_status(gh, record, 'building', failure=detail, expected=record)
                except Exception:
                    pass
        raise
    summary(f"Recovered assessed branch as batch PR {pr['html_url']}.")
    return True


def prepare(repo, gh, event, output, evaluation=False, package=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'plan.json').write_text(json.dumps({'needed': False}) + '\n')
    needed_output(False)
    if package and not evaluation:
        raise ValueError('--package requires --evaluation')
    requests = [] if evaluation else gh.api(
        f'pulls?state=open&head={quote(gh.repository.split("/")[0] + ":" + BRANCH)}&per_page=100')
    if len(requests) > 1:
        raise ValueError('Multiple automation PRs; refusing ambiguous ownership')
    pr = requests[0] if requests else None
    if pr and not trusted_pr(pr, gh.repository):
        raise ValueError('Refusing untrusted automation PR')
    workflow_run_event = not evaluation and os.environ.get('GITHUB_EVENT_NAME') == 'workflow_run'
    state = read_state(pr) if pr else {
        'attempts': 0, 'candidates': [], 'candidate_issues': [],
        'report': {'summary': '', 'recipes': []}}
    if workflow_run_event:
        completed = event.get('workflow_run') or {}
        head_repository = completed.get('head_repository')
        if (not pr or completed.get('head_sha') != pr['head']['sha']
                or completed.get('head_branch') != BRANCH
                or completed.get('event') != 'workflow_dispatch'
                or not isinstance(head_repository, dict)
                or head_repository.get('full_name') != gh.repository
                or type(completed.get('id')) is not int or completed['id'] < 1
                or type(completed.get('run_attempt')) is not int or completed['run_attempt'] < 1):
            summary('Ignoring stale or untrusted build completion.')
            return False
        candidate_result = update_candidate_build(gh, pr, state, completed)
        if completed.get('status') != 'completed':
            return False
        if completed.get('conclusion') == 'success' and candidate_result != 'success':
            summary('Candidate artifact verification failed; the PR remains unready.')
            return False
    discovered = json.loads(run([sys.executable, 'scripts/check-upstream-versions.py'], repo))
    if package:
        discovered = [item for item in discovered if item['name'] == package]
    discovered = sorted(discovered, key=lambda item: tuple(candidate_identity(item).values()))

    run_url = workflow_run_url(gh.repository)
    records = []
    if not evaluation:
        frozen = state.get('candidates', []) if pr else []
        queue = {tuple(candidate_identity(item).values()): item for item in discovered + frozen}
        records = sync_candidate_issues(
            gh, list(queue.values()), run_url, state.get('candidate_issues', []))
        if pr:
            old_records = state.get('candidate_issues', [])
            for candidate in frozen:
                record = issue_record_for(records, candidate)
                if (record and not issue_record_for(old_records, candidate)
                        and state.get('attempts', 0) and record.get('attempts', 0) == 0):
                    set_issue_status(gh, record, record['status'],
                                     attempts=min(state['attempts'], MAX_REPAIR_ATTEMPTS))
            state['batch'] = state.get('batch') or hashlib.sha256(
                json.dumps(frozen, sort_keys=True).encode()).hexdigest()
            candidates = frozen
            new_batch = False
        else:
            if recover_orphaned_publication(repo, gh, records, discovered):
                return False
            candidates = []
            for candidate in discovered:
                record = issue_record_for(records, candidate)
                if not record:
                    continue
                if record.get('attempts', 0) >= MAX_REPAIR_ATTEMPTS:
                    if record.get('status') not in ('closed', 'rejected', 'blocked'):
                        set_issue_status(gh, record, 'blocked',
                                         failure='Agent session budget exhausted; maintainer intervention required.')
                    continue
                if record.get('status') in ('queued', 'assessing'):
                    candidates.append(candidate)
            state.update(batch=hashlib.sha256(json.dumps(candidates, sort_keys=True).encode()).hexdigest(),
                         attempts=0, candidates=candidates, feedback_digest='', reviewed_head='')
            new_batch = True
        state['candidate_issues'] = records
        if run_url:
            state['run_url'] = run_url
    else:
        candidates = discovered
        new_batch = True
        state.update(batch=hashlib.sha256(json.dumps(candidates, sort_keys=True).encode()).hexdigest(),
                     candidates=candidates)
    if pr:
        active = []
        for candidate in candidates:
            record = issue_record_for(records, candidate)
            if not record or record.get('status') in ('closed', 'rejected', 'blocked'):
                continue
            active.append(candidate)
        candidates = active
        retry_candidates = state.get('retry_candidates')
        if retry_candidates:
            retry_identities = {tuple(item.get(key) for key in ISSUE_FIELDS)
                                for item in retry_candidates if isinstance(item, dict)}
            candidates = [item for item in candidates
                          if tuple(candidate_identity(item).values()) in retry_identities]
        state['candidate_issues'] = records
    if not candidates:
        if pr and all_candidates_decided(state):
            delete_branch(repo, gh, pr['head']['sha'])
            gh.api(f"pulls/{pr['number']}", {'state': 'closed', 'body': pr_body(state)}, method='PATCH')
            summary(f"Closed empty automation PR {pr['number']}; rejected issues remain open.")
            return False
        if pr:
            gh.api(f"pulls/{pr['number']}", {'body': pr_body(state)}, method='PATCH')
        summary('No eligible upstream candidates. Rejected, closed, blocked, or exhausted issues remain unchanged.')
        return False
    if pr:
        gh.api(f"pulls/{pr['number']}", {'body': pr_body(state)}, method='PATCH')
    reviews, inline, comments = evidence(gh, pr['number']) if pr else ([], [], [])
    build = None
    feedback = []
    if pr:
        baseline = json.loads((repo / 'build/baseline.json').read_text())
        title = f"Unraid compatibility {baseline['version']} {baseline['image']['sha256']} publish=false"
        runs = gh.api(f'actions/workflows/build-packages.yml/runs?branch={quote(BRANCH)}&event=workflow_dispatch&per_page=100')['workflow_runs']
        build = latest_build([item for item in runs if item.get('display_title') == title], pr['head']['sha'])
        if workflow_run_event and build is None:
            build = event['workflow_run']
        callback_build = (workflow_run_event and build
                         and build.get('id') == event['workflow_run'].get('id')
                         and build.get('run_attempt') == event['workflow_run'].get('run_attempt'))
        if build and not callback_build and not candidate_state_matches(state, build, pr):
            candidate_result = update_candidate_build(gh, pr, state, build)
            if (build.get('status') == 'completed' and build.get('conclusion') == 'success'
                    and candidate_result != 'success'):
                summary('Candidate artifact verification failed; the PR remains unready.')
                return False
        committed_at = gh.api(f"commits/{pr['head']['sha']}")['commit']['committer']['date']
        bots = available_bots(os.environ.get('UPSTREAM_REVIEW_BOTS', ''), reviews)
        inline_feedback = current_feedback(inline, pr['head']['sha'], committed_at, bots, 'inline')
        feedback = (inline_feedback
                    + current_feedback(reviews, pr['head']['sha'], committed_at, bots, 'review',
                                       allow_bot_summary=not inline_feedback)
                    + current_feedback(comments, pr['head']['sha'], committed_at, bots, 'comment'))
        if build and build['status'] != 'completed':
            state['build_url'] = build['html_url']
            update_candidate_issues(gh, records, candidates, 'building', build_url=build['html_url'])
            gh.api(f"pulls/{pr['number']}", {'body': pr_body(state)}, method='PATCH')
            summary(f"Batch build is still running: {build['html_url']}")
            return False
        if (build and build['conclusion'] == 'success'
                and candidate_state_matches(state, build, pr)
                and not state.get('validation_error') and not state.get('force_repair')):
            state['build_url'] = build['html_url']
            update_candidate_issues(gh, records, candidates, 'awaiting-review',
                                   failure=None, build_url=build['html_url'])
            if pr.get('draft'):
                run(['gh', 'pr', 'ready', str(pr['number']), '--repo', gh.repository])
            request_reviews(gh, pr, state, reviews)
            gh.api(f"pulls/{pr['number']}", {'body': pr_body(state)}, method='PATCH')
            digest = hashlib.sha256(json.dumps(feedback, sort_keys=True).encode()).hexdigest()
            if not feedback or state.get('feedback_digest') == digest:
                summary(f"Exact-head catalog gates passed; waiting for reviews and human approval: {pr['html_url']}")
                return False
        elif build and build['conclusion'] != 'success':
            state['build_url'] = build['html_url']
            update_candidate_issues(gh, records, candidates, 'building',
                                   failure='Catalog build failed; no compatibility claim.',
                                   build_url=build['html_url'])
        if build is None and not new_batch and not state.get('validation_error'):
            update_candidate_issues(gh, records, candidates, 'building')
            state['candidate_issues'] = records
            dispatch_catalog(repo, gh, pr, state)
            summary('Dispatched the missing exact-head catalog build.')
            return False
    digest = hashlib.sha256(json.dumps(feedback, sort_keys=True).encode()).hexdigest()
    included = []
    for candidate in candidates:
        record = issue_record_for(records, candidate) if not evaluation else None
        if record and (record.get('status') in ('closed', 'rejected', 'blocked')
                       or record.get('attempts', 0) >= MAX_REPAIR_ATTEMPTS):
            if (record.get('status') not in ('closed', 'rejected', 'blocked')
                    and record.get('attempts', 0) >= MAX_REPAIR_ATTEMPTS):
                set_issue_status(gh, record, 'blocked',
                                 failure='Agent session budget exhausted; maintainer intervention required.')
            continue
        included.append(candidate)
    if not included:
        if pr:
            state['candidate_issues'] = records
            gh.api(f"pulls/{pr['number']}", {'body': pr_body(state)}, method='PATCH')
        summary('Repair budget exhausted or maintainer decisions block all frozen candidates.')
        return False
    for candidate in included:
        if evaluation:
            continue
        record = issue_record_for(records, candidate)
        changes = {'attempts': record['attempts'] + 1, 'failure': record.get('failure')}
        if run_url:
            changes['run_url'] = run_url
        set_issue_status(gh, record, 'assessing', **changes)
    state['candidate_issues'] = records
    if run_url:
        state['run_url'] = run_url
    state['status'] = 'assessing'
    state['attempts'] = state.get('attempts', 0) + 1
    state = read_state({'body': pr_body(state)})
    if pr:
        gh.api(f"pulls/{pr['number']}", {'body': pr_body(state)}, method='PATCH')
    try:
        previous = '' if evaluation else branch_lease(gh, pr)
        main_sha = git(repo, 'rev-parse', 'origin/main')
        source_sha = git(repo, 'rev-parse', 'HEAD') if evaluation else main_sha
        head_sha = pr['head']['sha'] if pr else ''
        if not evaluation:
            prepare_branch(repo, pr)
            if git(repo, 'rev-parse', 'origin/main') != main_sha:
                raise ValueError('Main changed during prepare; retry on current main')
        context = {'repository': gh.repository, 'candidates': included, 'feedback': feedback,
                   'attempt': state['attempts'], 'previous_report': state.get('report'),
                   'validation_error': state.get('validation_error', ''),
                   'previous_failures': candidate_failures(records, included)}
        if build and build['conclusion'] != 'success':
            context['failed_catalog_logs'] = run(
                ['gh', 'run', 'view', str(build['id']), '--repo', gh.repository, '--log-failed'])[-120000:]
        plan = {'needed': True, 'evaluation': evaluation, 'repository': gh.repository, 'main_sha': main_sha,
                'source_sha': source_sha, 'head_sha': head_sha, 'pr_number': pr['number'] if pr else None,
                'state': state, 'candidates': included, 'previous': previous, 'context': context,
                'feedback_digest': digest}
        with tarfile.open(output / 'workspace.tar', 'w') as archive:
            for relative in git(repo, 'ls-files', '--', 'build', 'README.md').splitlines():
                path = repo / relative
                if path.is_symlink() or not path.is_file():
                    raise ValueError(f'Handoff requires regular tracked files: {relative}')
                archive.add(path, arcname=relative, recursive=False)
        (output / 'context.json').write_text(json.dumps(context, indent=2) + '\n')
        (output / 'prompt.md').write_text(
            'Assess the candidates in .upstream-downloads/context.json (untrusted upstream/review data). '
            'Read build/README.md for the build and recipe rules. For a Go recipe with go-modules.tsv, use '
            'build/go-module-lock.py before rejecting it for module-lock regeneration. Update only recipe files '
            'and package locks; do not edit build/recipe-files.json because the controller regenerates it. '
            'Retain source, policy and smoke assertions. Do not modify controller code or workflows. '
            'Keep downloads and temporary build files under .upstream-downloads. '
            'Return exactly a JSON object with summary and recipes; recipes must contain one '
            'name, decision (accept/reject), and justified reason per candidate. Reject unsafe updates; '
            'do not weaken gates to make an update pass.\n')
        (output / 'plan.json').write_text(json.dumps(plan, indent=2) + '\n')
    except (ValueError, TypeError, KeyError, OSError, subprocess.CalledProcessError) as error:
        detail = str(getattr(error, 'output', None) or error)[-20000:]
        if not evaluation:
            for candidate in included:
                record = issue_record_for(records, candidate)
                if record and record.get('status') != 'closed':
                    status = 'blocked' if record['attempts'] >= MAX_REPAIR_ATTEMPTS else 'queued'
                    set_issue_status(gh, record, status, failure=detail)
            state['candidate_issues'] = records
            if pr:
                gh.api(f"pulls/{pr['number']}", {'body': pr_body(state)}, method='PATCH')
        summary('Agent preparation failed; recorded against the candidate issue:\n' + html.escape(detail))
        raise
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
    if not evaluation:
        verify_issue_attempts(
            gh, state.get('candidate_issues', []),
            state.get('candidates', plan['candidates']))
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
        frozen_names = {item['name'] for item in state.get('candidates', [])}
        target_names = {item['name'] for item in plan['candidates']}
        if not evaluation and target_names < frozen_names:
            report = merge_retry_report(state, report, plan['candidates'])
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
        if not evaluation and not dry_run:
            records = state.get('candidate_issues', [])
            for candidate in plan['candidates']:
                record = issue_record_for(records, candidate)
                if record and record.get('status') != 'closed':
                    status = 'blocked' if record['attempts'] >= MAX_REPAIR_ATTEMPTS else 'queued'
                    set_issue_status(gh, record, status, failure=detail)
            state['candidate_issues'] = records
            if pr:
                gh.api(f"pulls/{pr['number']}", {'body': pr_body(state)}, method='PATCH')
        summary('Recipe validation failed; no publication performed:\n' + html.escape(detail))
        raise
    finally:
        shutil.rmtree(workspace)
    state.pop('validation_error', None)
    state.update(report=report, feedback_digest=plan['feedback_digest'])
    state.pop('force_repair', None)
    state.pop('retry_candidates', None)
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
    records = state.get('candidate_issues', [])
    decisions = {item['name']: item['decision'] for item in report['recipes']}
    versions = {item['name']: item['version'] for lock in LOCKS
                for item in json.loads((repo / lock).read_text())}
    for candidate in plan['candidates']:
        record = issue_record_for(records, candidate)
        if not record:
            continue
        if decisions.get(candidate['name']) == 'reject':
            set_issue_status(gh, record, 'rejected', decision='reject', failure=None, build_url=None)
        elif (decisions.get(candidate['name']) == 'accept'
              and versions.get(candidate['name']) == candidate['upstream']):
            set_issue_status(gh, record, 'building', decision='accept', failure=None, build_url=None)
        else:
            set_issue_status(gh, record, 'blocked', decision=None,
                             failure='Accepted target version is absent after validation; maintainer review required.',
                             build_url=None)
    state['candidate_issues'] = records
    state['issue_closures'] = accepted_issue_numbers(
        repo, state.get('candidates', plan['candidates']), report, records)
    if paths:
        git(repo, 'add', '--', *paths)
    git(repo, 'reset', '--soft', 'origin/main')
    consolidated_paths = git(repo, 'diff', '--cached', '--name-only', 'origin/main').splitlines()
    check_paths(consolidated_paths, generated=True)
    changed = bool(consolidated_paths)
    if changed:
        commit = ['git', '-c', 'user.name=github-actions[bot]', '-c',
                  'user.email=41898282+github-actions[bot]@users.noreply.github.com',
                  'commit', '-m', 'Update assessed upstream recipes']
        if state['issue_closures']:
            commit += ['-m', '\n'.join(f"Fixes #{number}" for number in state['issue_closures'])]
        run(commit, repo)
    sha = git(repo, 'rev-parse', 'HEAD')
    if pr:
        current = gh.api(f"pulls/{pr['number']}")
        if current['head']['sha'] != previous or current['state'] != 'open' or current.get('merged'):
            raise ValueError('PR changed or closed during agent work; refusing to overwrite it')
    if not pr and not changed:
        summary(pr_body(state))
        return
    if pr and not changed:
        delete_branch(repo, gh, previous)
        gh.api(f"pulls/{pr['number']}", {'state': 'closed', 'body': pr_body(state)}, method='PATCH')
        summary(f"Closed empty automation PR {pr['number']}; rejected issues remain open.")
        return
    try:
        if changed or sha != previous:
            push(repo, gh, previous)
            clear_candidate_state(state)
            state.pop('reviewed_head', None)
        if pr:
            pr = gh.api(f"pulls/{pr['number']}", {'body': pr_body(state)}, method='PATCH')
        else:
            pr = gh.api('pulls', {'title': TITLE, 'head': BRANCH, 'base': 'main',
                                  'draft': False, 'body': pr_body(state)})
        if changed or state.get('dispatched_head') != sha:
            dispatch_catalog(repo, gh, pr, state)
    except Exception as error:
        detail = str(getattr(error, 'output', None) or error)[-20000:]
        for candidate in plan['candidates']:
            record = issue_record_for(records, candidate)
            if record and record.get('status') == 'building':
                try:
                    set_issue_status(gh, record, 'building', failure=detail, expected=record)
                except Exception:
                    pass
        state['candidate_issues'] = records
        raise
    summary(f"Batch PR: {pr['html_url']}. Catalog verification pending; human approval required.")


def dispatch_catalog(repo, gh, pr, state):
    baseline = json.loads((repo / 'build/baseline.json').read_text())
    clear_candidate_state(state)
    set_candidate_status(gh, pr, pr['head']['sha'], 'pending')
    try:
        gh.dispatch('build-packages.yml', BRANCH, {'unraid_version': baseline['version'],
                    'unraid_sha256': baseline['image']['sha256'], 'unraid_url': baseline['image']['url'],
                    'channel': 'stable', 'publish': 'false'})
    except Exception:
        set_candidate_status(gh, pr, pr['head']['sha'], 'failure')
        raise
    state['dispatched_head'] = pr['head']['sha']
    gh.api(f"pulls/{pr['number']}", {'body': pr_body(state)}, method='PATCH')


def delete_branch(repo, gh, previous):
    git(repo, '-c', 'credential.helper=!gh auth git-credential',
        'push', f'--force-with-lease=refs/heads/{BRANCH}:{previous}',
        f'https://github.com/{gh.repository}.git', f':refs/heads/{BRANCH}')


def event_pr(event):
    if event.get('pull_request'):
        return event['pull_request']['number']
    if event.get('issue', {}).get('pull_request'):
        return event['issue']['number']
    return None


def current_writer(gh, sender):
    login = sender.get('login')
    if sender.get('type') != 'User' or not isinstance(login, str) or login == BOT:
        return False
    try:
        permission = gh.api(f"collaborators/{quote(login, safe='')}/permission")
    except subprocess.CalledProcessError:
        return False
    return (permission or {}).get('permission') in ('write', 'maintain', 'admin')


def retry_issue(gh, number, run_url=None):
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        return False
    issue = gh.api(f'issues/{number}')
    if issue.get('state') != 'open' or issue.get('pull_request'):
        return False
    try:
        metadata = read_issue_state(issue)
    except ValueError:
        return False
    if not metadata:
        return False
    record = candidate_record(metadata['candidate'], issue, metadata, gh.repository)
    active = None
    requests = gh.api(
        f'pulls?state=open&head={quote(gh.repository.split("/")[0] + ":" + BRANCH)}&per_page=100')
    if len(requests) > 1:
        return False
    if requests and trusted_pr(requests[0], gh.repository):
        pr = requests[0]
        try:
            pr_state = read_state(pr)
        except ValueError:
            return False
        identity = tuple(candidate_identity(metadata['candidate']).values())
        frozen = {tuple(candidate_identity(item).values()) for item in pr_state.get('candidates', [])}
        index = next((index for index, item in enumerate(pr_state.get('candidate_issues', []))
                      if item.get('number') == number and item.get('candidate') == metadata['candidate']), None)
        if identity in frozen and index is not None:
            active = (pr, pr_state, index)
    set_issue_status(gh, record, 'queued', attempts=0, decision=None, failure=None,
                     run_url=run_url, build_url=None)
    if record.get('status') == 'closed':
        return False
    if active:
        pr, pr_state, index = active
        pr_state['candidate_issues'][index].update(record)
        report = pr_state.get('report', {})
        report['recipes'] = [item for item in report.get('recipes', [])
                             if item.get('name') != metadata['candidate']['name']]
        report['summary'] = 'Awaiting maintainer-requested reassessment'
        pr_state.update(attempts=0, feedback_digest='', issue_closures=[
            item for item in pr_state.get('issue_closures', []) if item != number], reviewed_head='',
            force_repair=True, retry_candidates=[metadata['candidate']])
        clear_candidate_state(pr_state)
        pr_state.pop('validation_error', None)
        gh.api(f"pulls/{pr['number']}", {'body': pr_body(pr_state)}, method='PATCH')
        set_candidate_status(gh, pr, pr['head']['sha'], 'pending')
    return True


def retry_pr_batch(gh, number, run_url=None):
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        return False
    pr = gh.api(f'pulls/{number}')
    if not trusted_pr(pr, gh.repository) or pr.get('state') != 'open':
        return False
    state = read_state(pr)
    frozen = {tuple(candidate_identity(item).values()) for item in state.get('candidates', [])}
    reset = []
    for record in state.get('candidate_issues', []):
        if tuple((record.get('candidate') or {}).get(key) for key in ISSUE_FIELDS) not in frozen:
            continue
        issue_number = record.get('number')
        if not isinstance(issue_number, int) or isinstance(issue_number, bool) or issue_number < 1:
            continue
        issue = gh.api(f"issues/{issue_number}")
        if issue.get('state') != 'open':
            record['status'] = 'closed'
            continue
        try:
            metadata = read_issue_state(issue)
        except ValueError:
            continue
        if not metadata or metadata['candidate'] != record.get('candidate'):
            continue
        set_issue_status(gh, record, 'queued', attempts=0, decision=None, failure=None,
                         run_url=run_url, build_url=None)
        if record.get('status') != 'closed':
            reset.append(record)
    if not reset:
        return False
    state.update(attempts=0, report={'summary': 'Awaiting maintainer-requested reassessment', 'recipes': []},
                 feedback_digest='', issue_closures=[], reviewed_head='', force_repair=True,
                 retry_candidates=[item['candidate'] for item in reset])
    clear_candidate_state(state)
    gh.api(f"pulls/{number}", {'body': pr_body(state)}, method='PATCH')
    set_candidate_status(gh, pr, pr['head']['sha'], 'pending')
    return True


def feedback(gh, event):
    sender = event.get('sender') or {}
    item = event.get('comment') or event.get('review') or {}
    if event.get('comment') and (item.get('body') or '').strip() == '/upstream retry':
        if not current_writer(gh, sender):
            return
        issue = event.get('issue') or {}
        if issue.get('pull_request'):
            reset = retry_pr_batch(gh, issue.get('number'), workflow_run_url(gh.repository))
        else:
            reset = retry_issue(gh, issue.get('number'), workflow_run_url(gh.repository))
        if reset:
            gh.dispatch('upstream-versions.yml')
            summary('Queued a maintainer-requested upstream reassessment.')
        return
    number = event_pr(event)
    if not number or sender.get('login') == BOT:
        return
    pr = gh.api(f'pulls/{number}')
    if not trusted_pr(pr, gh.repository) or pr['state'] != 'open':
        return
    known = available_bots(os.environ.get('UPSTREAM_REVIEW_BOTS', ''),
                           gh.pages(f'pulls/{number}/reviews?per_page=100'))
    source = 'review' if event.get('review') else (
        'inline' if item.get('path') else 'comment')
    evidence = {**item, 'user': sender}
    if not trusted_author(evidence, known) or not feedback_actionable(evidence, source, known):
        return
    if item.get('commit_id') and item['commit_id'] != pr['head']['sha']:
        return
    gh.dispatch('upstream-versions.yml')
    summary(f'Relayed actionable review feedback for automation PR {number} to main.')

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
