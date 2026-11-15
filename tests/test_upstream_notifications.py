"""Pure fixtures for trusted terminal workflow notifications."""
import importlib.util
from pathlib import Path
import re
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/upstream-notify.py'
spec = importlib.util.spec_from_file_location('upstream_notify', SCRIPT)
notify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notify)

REPO = 'owner/tools'
SHA = 'a' * 40


def pr(number=12, state='open', merged=False, head_sha=SHA):
    return {'number': number, 'state': state, 'merged': merged, 'merged_at': 'now' if merged else None,
            'merge_commit_sha': 'c' * 40 if merged else None,
            'head': {'ref': 'automation/upstream-recipes', 'sha': head_sha,
                     'repo': {'full_name': REPO}},
            'base': {'ref': 'main', 'repo': {'full_name': REPO}},
            'user': {'login': 'github-actions[bot]', 'type': 'Bot'},
            'html_url': f'https://github.com/{REPO}/pull/{number}'}


class FakeGitHub:
    repository = REPO

    def __init__(self):
        self.pull_requests = []
        self.issues = []
        self.comments = {}
        self.comparisons = {}
        self.calls = []
        self.jobs = []
        self.statuses = []
        self.page_calls = []

    def pages(self, path, field=None):
        self.page_calls.append((path, field))
        if path.startswith('actions/runs/'):
            return self.jobs if field == 'jobs' else self.jobs
        if path.startswith('pulls?'):
            return [{key: value for key, value in pr.items() if key != 'merged'} for pr in self.pull_requests]
        if path.startswith('commits/') and path.endswith('/statuses?per_page=100'):
            return self.statuses
        if path.startswith('issues?'):
            return self.issues
        if path.endswith('/comments?per_page=100'):
            return self.comments.get(int(path.split('/')[1]), [])
        return []

    def api(self, path, data=None, method=None):
        self.calls.append((path, data, method))
        if method == 'POST' and path.startswith('statuses/'):
            self.statuses.append({**data, 'creator': {'login': 'github-actions[bot]'}})
            return self.statuses[-1]
        if path.startswith('pulls/') and path[6:].isdigit():
            return next((pr for pr in self.pull_requests if pr['number'] == int(path[6:])), {})
        if path.startswith('compare/'):
            return self.comparisons.get(path.split('compare/', 1)[1], {'status': 'diverged'})
        if method == 'POST' and path == 'issues':
            issue = {'number': 900, 'state': 'open', **data,
                     'user': {'login': 'github-actions[bot]'}}
            self.issues.append(issue)
            return issue
        if method == 'POST' and path.startswith('issues/') and path.endswith('/comments'):
            number = int(path.split('/')[1])
            self.comments.setdefault(number, []).append({'body': data['body'], 'user': {'login': 'github-actions[bot]'}})
            return {}
        if method == 'PATCH' and path.startswith('issues/'):
            number = int(path.split('/')[1])
            issue = next((item for item in self.issues if item['number'] == number), None)
            if issue:
                issue.update(data)
            return {}
        return {}


def event(workflow='Update upstream recipes', conclusion='failure', **kw):
    run = {'name': workflow, 'path': f'.github/workflows/{kw.pop("path", "upstream-versions.yml")}',
           'event': 'schedule', 'head_branch': 'main', 'head_sha': SHA, 'conclusion': conclusion,
           'id': 123, 'run_attempt': 1, 'display_title': 'Upstream recipe update',
           'html_url': 'https://github.com/owner/tools/actions/runs/123',
           'repository': {'full_name': REPO}, 'head_repository': {'full_name': REPO},
           'pull_requests': [], **kw}
    return {'repository': {'full_name': REPO}, 'workflow_run': run}


class NotificationTests(unittest.TestCase):
    def test_rejects_untrusted_repository_workflow_sources_and_evaluations(self):
        for e in (
            event(repository={'full_name': 'attacker/tools'}),
            event(path='foreign.yml'),
            event(event='pull_request'),
            event(head_branch='attacker'),
            event(display_title='Upstream recipe evaluation'),
        ):
            gh = FakeGitHub()
            self.assertFalse(notify.process(e, gh))
            self.assertEqual(gh.calls, [])

    def test_updater_failure_uses_canonical_issue_not_racy_pr_comment(self):
        gh = FakeGitHub()
        gh.pull_requests = [pr()]
        e = event()
        self.assertTrue(notify.process(e, gh))
        self.assertFalse(notify.process(e, gh))
        self.assertEqual(len(gh.issues), 1)
        self.assertEqual(gh.issues[0]['state'], 'open')
        self.assertEqual(gh.comments, {})

    def test_failure_before_discovery_still_notifies_and_matching_success_resolves(self):
        gh = FakeGitHub()
        self.assertTrue(notify.process(event(), gh))
        self.assertEqual(len(gh.issues), 1)
        self.assertEqual(gh.issues[0]['state'], 'open')
        notify.process(event(conclusion='success', id=124), gh)
        self.assertEqual(gh.issues[0]['state'], 'closed')

    def test_delayed_failures_cannot_override_newer_success(self):
        for newer, older in (
                (event(conclusion='success', id=124), event(id=123)),
                (event(conclusion='success', run_attempt=2), event(run_attempt=1))):
            gh = FakeGitHub()
            gh.pull_requests = [pr()]
            self.assertFalse(notify.process(newer, gh))
            self.assertFalse(notify.process(older, gh))
            self.assertEqual(gh.issues, [])
            self.assertEqual(gh.comments, {})
            self.assertEqual(len(gh.statuses), 1)

    def test_success_checkpoint_arriving_during_failure_creation_closes_stale_issue(self):
        older = event(id=123)['workflow_run']
        newer = dict(older, id=124, conclusion='success')

        class RacingGitHub(FakeGitHub):
            def api(self, path, data=None, method=None):
                result = super().api(path, data, method)
                if path == 'issues' and method == 'POST':
                    _, context = notify.success_context(newer, SHA)
                    self.statuses.append({
                        'context': context, 'state': 'success',
                        'description': 'Successful run 124:1',
                        'creator': {'login': 'github-actions[bot]'},
                    })
                return result

        gh = RacingGitHub()
        self.assertFalse(notify.terminal(gh, older, False, SHA))
        self.assertEqual(gh.issues[0]['state'], 'closed')

    def test_manual_update_is_not_rejected_for_skipped_eval_job(self):
        gh = FakeGitHub()
        gh.pull_requests = [pr()]
        gh.jobs = [{'name': 'native-evaluation', 'conclusion': 'skipped'}]
        e = event(event='workflow_dispatch')
        self.assertTrue(notify.process(e, gh))
        self.assertEqual(len(gh.issues), 1)
        self.assertEqual(gh.comments, {})

    def test_untrusted_comment_marker_does_not_suppress_bot_comment(self):
        gh = FakeGitHub()
        gh.comments[12] = [{'body': '<!-- upstream-notify: run:123:1 -->',
                            'user': {'login': 'attacker'}}]
        self.assertTrue(notify.comment(gh, 12, 'trusted comment', '<!-- upstream-notify: run:123:1 -->'))
        self.assertEqual(len(gh.comments[12]), 2)

    def test_failed_release_creates_separate_issue_and_does_not_close_recipe_issue(self):
        gh = FakeGitHub()
        recipe = {'number': 4, 'state': 'open', 'title': 'Upstream update: sshfs 3.7.7 -> 3.7.8',
                  'body': 'https://github.com/libfuse/sshfs/releases/tag/3.7.8',
                  'user': {'login': 'github-actions[bot]', 'type': 'Bot'}, 'labels': [{'name': 'upstream-update'}]}
        gh.issues = [recipe]
        merged = pr(state='closed', merged=True)
        gh.pull_requests = [merged]
        e = event('Discover Unraid releases', source_head_sha=SHA)
        e['workflow_run'].update(path='.github/workflows/unraid-releases.yml', event='schedule')
        self.assertTrue(notify.process(e, gh))
        self.assertFalse(any(method == 'PATCH' and path == 'issues/4' for path, _, method in gh.calls))
        self.assertFalse(any('labels' in data for path, data, method in gh.calls if path == 'issues'))
        self.assertTrue(any(path == 'issues' and method == 'POST' for path, _, method in gh.calls))

    def test_newer_failure_reopens_tracking_issue_but_older_run_cannot(self):
        gh = FakeGitHub()
        run = event('Discover Unraid releases')['workflow_run']
        target = SHA
        self.assertTrue(notify.terminal(gh, run, False, target))
        failure = dict(run, id=124)
        self.assertFalse(notify.terminal(gh, failure, True, target))
        later = dict(run, id=125)
        self.assertTrue(notify.terminal(gh, later, False, target))
        self.assertEqual(gh.issues[0]['state'], 'open')
        self.assertFalse(notify.terminal(gh, failure, False, target))

    def test_later_success_while_closed_prevents_stale_failure_reopening(self):
        gh = FakeGitHub()
        run = event('Discover Unraid releases')['workflow_run']
        notify.terminal(gh, run, False, SHA)
        notify.terminal(gh, dict(run, id=125), True, SHA)
        notify.terminal(gh, dict(run, id=127), True, SHA)
        notify.terminal(gh, dict(run, id=126), False, SHA)
        self.assertEqual(gh.issues[0]['state'], 'closed')

    def test_release_notification_rejects_wrong_trigger(self):
        gh = FakeGitHub()
        e = event('Release merged upstream recipes', path='upstream-release.yml', event='workflow_dispatch')
        self.assertFalse(notify.process(e, gh))
        self.assertEqual(gh.calls, [])

    def test_merged_release_uses_verified_pr_and_merge_commit_not_branch_head(self):
        gh = FakeGitHub()
        merged = pr(state='closed', merged=True, head_sha=SHA)
        gh.pull_requests = [merged]
        e = event('Release merged upstream recipes')
        e['workflow_run'].update(path='.github/workflows/upstream-release.yml', event='pull_request',
                                 pull_requests=[{'number': 12}], head_sha=merged['merge_commit_sha'])
        self.assertTrue(notify.process(e, gh))
        self.assertTrue(any(path == 'pulls/12' for path, _, _ in gh.calls))
        self.assertEqual(len(gh.issues), 1)
        self.assertEqual(gh.comments, {})
        self.assertFalse(any(path.startswith('compare/') for path, _, _ in gh.calls))

    def test_discovery_ancestry_compares_merge_commit_not_pr_head(self):
        gh = FakeGitHub()
        gh.pull_requests = [pr(state='closed', merged=True, head_sha=SHA)]
        merge_sha, source_sha = 'c' * 40, 'd' * 40
        gh.comparisons[f'{merge_sha}...{source_sha}'] = {'status': 'ahead'}
        self.assertEqual(notify.relevant_pr(gh, source_sha)['number'], 12)
        self.assertTrue(any(path == f'compare/{merge_sha}...{source_sha}' for path, _, _ in gh.calls))

    def test_matching_successful_retry_closes_failure_issue(self):
        gh = FakeGitHub()
        failure = {'number': 88, 'state': 'open',
                   'body': f'<!-- upstream-notify: failure:{SHA}:Discover Unraid releases:{SHA} -->',
                   'labels': [{'name': 'automation-failure'}], 'user': {'login': 'github-actions[bot]'}}
        gh.issues = [failure]
        e = event('Discover Unraid releases', 'success', source_head_sha=SHA)
        e['workflow_run'].update(path='.github/workflows/unraid-releases.yml', event='schedule')
        notify.process(e, gh)
        self.assertTrue(any(path == 'issues/88' and method == 'PATCH' and data['state'] == 'closed'
                            for path, data, method in gh.calls))

    def test_build_success_requires_exact_publish_title_and_successful_publish_job(self):
        gh = FakeGitHub()
        e = event('Build native packages', 'success')
        e['workflow_run'].update(path='.github/workflows/build-packages.yml', event='workflow_dispatch',
                                 display_title=f'Unraid compatibility 7.3.4 {"b" * 64} publish=true',
                                 head_branch='main')
        gh.jobs = [{'name': 'publish', 'conclusion': 'skipped'}]
        self.assertFalse(notify.process(e, gh))
        self.assertEqual(gh.calls, [])
        merged = pr(state='closed', merged=True)
        gh.pull_requests = [merged]
        e['workflow_run']['head_sha'] = merged['merge_commit_sha']
        gh.jobs = [{'name': 'publish', 'conclusion': 'success'}]
        self.assertTrue(notify.process(e, gh))
        self.assertIn('Publication succeeded.', gh.comments[12][0]['body'])
        self.assertIn((
            'actions/runs/123/attempts/1/jobs?per_page=100', 'jobs'),
            gh.page_calls)

    def test_untrusted_title_is_escaped_and_never_interpreted_as_mention(self):
        text = notify.safe_text('@everyone <script>alert(1)</script>')
        self.assertNotIn('<script>', text)
        self.assertNotIn('@everyone', text)


if __name__ == '__main__':
    unittest.main()
