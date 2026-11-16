"""Offline regression coverage for the trusted upstream PR controller."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/upstream-updates.py'


def module():
    spec = importlib.util.spec_from_file_location('upstream_updates', SCRIPT)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class ControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.updates = module()

    def pr(self):
        return {'number': 12, 'state': 'open', 'merged': False,
                'head': {'ref': 'automation/upstream-recipes', 'sha': 'a' * 40,
                         'repo': {'full_name': 'owner/tools'}},
                'base': {'ref': 'main', 'repo': {'full_name': 'owner/tools'}},
                'merge_commit_sha': 'merge12', 'html_url': 'https://github.com/owner/tools/pull/12',
                'user': {'login': 'github-actions[bot]', 'type': 'Bot'}}

    def test_foreign_or_wrong_branch_pr_cannot_trigger_privileged_work(self):
        pr = self.pr()
        self.assertTrue(self.updates.trusted_pr(pr, 'owner/tools'))
        pr['head']['repo']['full_name'] = 'attacker/tools'
        self.assertFalse(self.updates.trusted_pr(pr, 'owner/tools'))
        pr = self.pr()
        pr['base']['ref'] = 'dev'
        self.assertFalse(self.updates.trusted_pr(pr, 'owner/tools'))
        pr = self.pr()
        pr['user']['login'] = 'attacker'
        self.assertFalse(self.updates.trusted_pr(pr, 'owner/tools'))

    def test_release_requires_current_head_human_approval_not_bot_or_stale_approval(self):
        pr = self.pr()
        pr.update(merged=True, state='closed')
        approval = {'user': {'login': 'maintainer', 'type': 'User'},
                    'state': 'APPROVED', 'commit_id': 'a' * 40,
                    'author_association': 'OWNER', 'id': 1}
        self.assertTrue(self.updates.human_approved(pr, [approval]))
        for change in ({'commit_id': 'b' * 40},
                       {'user': {'login': 'reviewbot', 'type': 'Bot'}},
                       {'author_association': 'CONTRIBUTOR'}):
            self.assertFalse(self.updates.human_approved(pr, [approval | change]))
        dismissal = approval | {'state': 'DISMISSED', 'id': 2}
        self.assertFalse(self.updates.human_approved(pr, [approval, dismissal]))
        comment = approval | {'state': 'COMMENTED', 'id': 2}
        self.assertTrue(self.updates.human_approved(pr, [approval, comment]))
        pr['merged'] = False
        self.assertFalse(self.updates.human_approved(pr, [approval]))

    def test_latest_exact_head_build_is_the_only_qualifying_evidence(self):
        runs = [
            {'id': 8, 'head_sha': 'old', 'event': 'workflow_dispatch', 'status': 'completed', 'conclusion': 'success'},
            {'id': 7, 'head_sha': 'new', 'event': 'workflow_dispatch', 'status': 'completed', 'conclusion': 'failure'},
            {'id': 9, 'head_sha': 'new', 'event': 'workflow_dispatch', 'status': 'in_progress', 'conclusion': None},
            {'id': 10, 'head_sha': 'new', 'event': 'pull_request', 'status': 'completed', 'conclusion': 'success'},
        ]
        self.assertEqual(self.updates.latest_build(runs, 'new')['id'], 9)
        self.assertIsNone(self.updates.latest_build(runs, 'unbuilt'))

    def test_review_requests_require_availability_and_never_include_copilot(self):
        evidence = [{'user': {'login': 'coderabbitai[bot]', 'type': 'Bot'}}]
        self.assertEqual(self.updates.available_bots('', evidence), ['coderabbitai[bot]'])
        self.assertEqual(self.updates.available_bots('chatgpt-codex-connector[bot],copilot-pull-request-reviewer[bot]', []),
                         ['chatgpt-codex-connector[bot]'])
        self.assertEqual(self.updates.available_bots('', []), [])

    def test_branch_deletion_requires_exact_previous_head(self):
        gh = GitHubFixture(None)
        with patch.object(self.updates, 'run', return_value='') as command:
            self.updates.delete_branch(Path('/repo'), gh, 'a' * 40)
        args = command.call_args.args[0]
        self.assertIn(
            '--force-with-lease=refs/heads/automation/upstream-recipes:' + 'a' * 40,
            args)
        self.assertEqual(args[-1], ':refs/heads/automation/upstream-recipes')

    def test_paginated_object_field_is_flattened_without_iterating_dict_keys(self):
        gh = self.updates.GitHub('owner/tools')
        payload = json.dumps([
            {'total_count': 1, 'jobs': [{'name': 'publish', 'conclusion': 'success'}]},
            {'total_count': 1, 'jobs': [{'name': 'verify', 'conclusion': 'success'}]},
        ])
        with patch.object(self.updates, 'run', return_value=payload):
            jobs = gh.pages('actions/runs/123/attempts/2/jobs?per_page=100', field='jobs')
        self.assertEqual([item['name'] for item in jobs], ['publish', 'verify'])

    def test_feedback_ignores_own_comments_and_reviews_of_old_heads(self):
        evidence = [
            {'id': 1, 'body': 'fix current bug', 'commit_id': 'new', 'path': 'build/recipes/vim/build',
             'line': 12, 'user': {'login': 'coderabbitai[bot]'}},
            {'id': 2, 'body': 'old bug', 'commit_id': 'old', 'path': 'build/recipes/vim/build',
             'line': 13, 'user': {'login': 'coderabbitai[bot]'}},
            {'id': 3, 'body': '@codex review', 'user': {'login': 'github-actions[bot]'}},
        ]
        feedback = self.updates.current_feedback(evidence, 'new', '2026-11-14T00:00:00Z', ['coderabbitai[bot]'])
        self.assertEqual([item['body'] for item in feedback], ['fix current bug'])

    def test_recipe_decisions_must_cover_the_batch_once(self):
        candidates = [{'name': 'vim'}, {'name': 'tmux'}]
        good = {'summary': 'Reviewed both', 'recipes': [
            {'name': 'vim', 'decision': 'accept', 'reason': 'Pinned source'},
            {'name': 'tmux', 'decision': 'reject', 'reason': 'License uncertain'}]}
        self.updates.validate_report(good, candidates)
        for recipes in (good['recipes'][:1], good['recipes'] + good['recipes'][:1],
                        [good['recipes'][0], {'name': 'tmux', 'decision': 'maybe', 'reason': 'uncertain'}]):
            with self.assertRaises(ValueError):
                self.updates.validate_report(good | {'recipes': recipes}, candidates)

    def test_agent_cannot_publish_native_code_or_secret_file_changes(self):
        for forbidden in ('build/native.py', '.github/workflows/upstream-versions.yml',
                          '.env', 'build/recipes/../../native.py', 'build/recipes/a/.git/config'):
            with self.subTest(path=forbidden), self.assertRaises(ValueError):
                self.updates.check_paths([forbidden])
        self.updates.check_paths(['build/recipes/vim/vim.SlackBuild', 'build/locks/slackware.json',
                                  'build/recipe-files.json'])


    def test_prepare_empty_discovery_never_prepares_or_launches_provider(self):
        updates = self.updates
        gh = GitHubFixture(None)
        gh.api = lambda path: []
        with tempfile.TemporaryDirectory() as folder, patch.dict(
                'os.environ', {'GITHUB_EVENT_NAME': 'schedule'}), patch.object(
                updates, 'run', return_value='[]') as run, patch.object(updates, 'prepare_branch') as branch:
            self.assertFalse(updates.prepare(Path(folder), gh, {}, Path(folder) / 'handoff'))
            self.assertEqual(run.call_args.args[0][1], 'scripts/check-upstream-versions.py')
            self.assertEqual(run.call_count, 1)
            branch.assert_not_called()
            self.assertEqual(json.loads((Path(folder) / 'handoff/plan.json').read_text()), {'needed': False})

    def test_report_rejects_non_schema_and_empty_reports(self):
        for report in ({}, [], {'summary': '', 'recipes': [], 'workspace': '/tmp/attack'},
                       {'summary': 'Update vim', 'recipes': [
                           {'name': 'vim', 'decision': 'accept', 'reason': 'Verified', 'extra': {'untrusted': True}}]}):
            with self.assertRaises(ValueError):
                self.updates.validate_report(report, [{'name': 'vim'}])

    def test_evaluation_filters_discovery_without_reading_or_editing_pr_state(self):
        gh = GitHubFixture(None)
        gh.api = lambda *args, **kwargs: self.fail('Evaluation must not access PR metadata')
        with tempfile.TemporaryDirectory() as folder, patch.object(
                self.updates, 'run', return_value=json.dumps([{
                    'name': 'sshfs', 'ours': '1', 'upstream': '2',
                    'source': 'Slackware-current', 'url': 'https://example.invalid/packages'}])), patch.object(
                self.updates, 'git', side_effect=lambda repo, *args: (
                    'source' if args == ('rev-parse', 'HEAD') else 'main' if args[0] == 'rev-parse' else '')), patch.object(
                self.updates, 'prepare_branch') as branch:
            output = Path(folder) / 'handoff'
            self.assertTrue(self.updates.prepare(Path(folder), gh, {}, output, evaluation=True, package='sshfs'))
            plan = json.loads((output / 'plan.json').read_text())
            self.assertTrue(plan['evaluation'])
            self.assertEqual(plan['source_sha'], 'source')
            branch.assert_not_called()
            self.assertEqual(plan['pr_number'], None)
            self.assertEqual([item['name'] for item in plan['candidates']], ['sshfs'])

class IssueLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.updates = module()

    def candidate(self, upstream='2.0', url='https://example.invalid/releases'):
        return {'name': 'vim', 'ours': '1.0', 'upstream': upstream,
                'source': 'Slackware-current', 'url': url}

    def test_exact_candidate_adopts_legacy_issue_and_distinct_version_gets_its_own(self):
        candidate = self.candidate()
        legacy = {'number': 7, 'state': 'open',
                  'title': 'Upstream update: vim 1.0 -> 2.0',
                  'body': 'Existing readable description\n\n' + candidate['url'],
                  'user': {'login': 'github-actions[bot]', 'type': 'Bot'}}
        gh = GitHubFixture(None)
        gh.issues.append(legacy)

        records = self.updates.sync_candidate_issues(gh, [candidate], run_url='https://example.invalid/run/1')

        self.assertEqual([item['number'] for item in records], [7])
        self.assertEqual(len(gh.issues), 1)
        self.assertIn('Existing readable description', gh.issues[0]['body'])
        self.assertEqual(self.updates.read_issue_state(gh.issues[0])['candidate'],
                         self.updates.candidate_identity(candidate))
        self.assertEqual(self.updates.read_issue_state(gh.issues[0])['status'], 'queued')
        self.assertEqual(gh.issues[0]['labels'], ['upstream-update'])

        self.updates.sync_candidate_issues(gh, [candidate], run_url='https://example.invalid/run/1')
        self.assertEqual(len(gh.issues), 1)
        drifted = candidate | {'ours': '1.5'}
        self.assertEqual(self.updates.sync_candidate_issues(gh, [drifted])[0]['number'], 7)
        self.assertEqual(len(gh.issues), 1)
        self.updates.sync_candidate_issues(gh, [self.candidate('3.0')])
        self.assertEqual(len(gh.issues), 2)
        self.assertEqual(gh.issues[1]['labels'], ['upstream-update'])

    def test_closed_exact_issue_is_never_reopened_or_recreated(self):
        candidate = self.candidate()
        closed = {'number': 8, 'state': 'closed',
                  'title': 'Upstream update: vim 1.0 -> 2.0',
                  'body': candidate['url'],
                  'user': {'login': 'github-actions[bot]', 'type': 'Bot'}}
        gh = GitHubFixture(None)
        gh.issues.append(closed)

        records = self.updates.sync_candidate_issues(gh, [candidate])

        self.assertEqual([item['number'] for item in records], [8])
        self.assertEqual(records[0]['status'], 'closed')
        self.assertEqual(len(gh.issues), 1)
        self.assertFalse(any(method == 'PATCH' for _, _, method in gh.calls))

    def test_issue_status_and_attempts_are_durable_and_render_links_and_failure(self):
        gh = GitHubFixture(None)
        record = self.updates.sync_candidate_issues(gh, [self.candidate()])[0]

        self.updates.set_issue_status(gh, record, 'assessing', attempts=1,
                                      run_url='https://example.invalid/run/2')
        self.updates.set_issue_status(gh, record, 'queued', failure='catalog validation failed',
                                      build_url='https://example.invalid/build/4')

        stored = self.updates.read_issue_state(gh.issues[0])
        self.assertEqual(stored['attempts'], 1)
        self.assertEqual(stored['status'], 'queued')
        self.assertEqual(stored['failure'], 'catalog validation failed')
        self.assertIn('https://example.invalid/run/2', gh.issues[0]['body'])
        self.assertIn('https://example.invalid/build/4', gh.issues[0]['body'])
        self.assertIn('catalog validation failed', gh.issues[0]['body'])

    def test_saved_no_pr_failure_is_returned_for_next_provider_context(self):
        candidate = self.candidate()
        gh = GitHubFixture(None)
        record = self.updates.sync_candidate_issues(gh, [candidate])[0]
        self.updates.set_issue_status(
            gh, record, 'queued', attempts=1, failure='source smoke drift')
        self.assertEqual(
            self.updates.candidate_failures([record], [candidate]),
            {'vim': 'source smoke drift'})

    def test_closure_refs_require_accepted_version_and_retain_prior_batch_accepts(self):
        candidate = self.candidate()
        retained = {'name': 'tmux', 'ours': '3.0', 'upstream': '4.0',
                    'source': 'SlackBuilds.org 15.0', 'url': 'https://example.invalid/tmux'}
        records = [{'candidate': candidate, 'number': 42},
                   {'candidate': self.updates.candidate_identity(retained), 'number': 43,
                    'status': 'blocked', 'decision': 'accept'}]
        with tempfile.TemporaryDirectory() as folder:
            locks = Path(folder) / 'build/locks'
            locks.mkdir(parents=True)
            (locks / 'slackware.json').write_text(json.dumps([{'name': 'vim', 'version': '2.0'}]))
            (locks / 'slackbuilds.json').write_text(json.dumps([{'name': 'tmux', 'version': '4.0'}]))
            report = {'recipes': [{'name': 'vim', 'decision': 'accept', 'reason': 'Pinned source'}]}
            candidates = [candidate, retained]
            self.assertEqual(self.updates.accepted_issue_numbers(
                Path(folder), candidates, report, records), [42, 43])
            (locks / 'slackware.json').write_text(json.dumps([{'name': 'vim', 'version': '3.0'}]))
            self.assertEqual(self.updates.accepted_issue_numbers(
                Path(folder), candidates, report, records), [43])
            report['recipes'][0]['decision'] = 'reject'
            (locks / 'slackware.json').write_text(json.dumps([{'name': 'vim', 'version': '2.0'}]))
            self.assertEqual(self.updates.accepted_issue_numbers(
                Path(folder), candidates, report, records), [43])

    def test_targeted_retry_retains_prior_non_target_decisions(self):
        vim = self.candidate()
        tmux = {'name': 'tmux', 'ours': '3.0', 'upstream': '4.0',
                'source': 'SlackBuilds.org 15.0', 'url': 'https://example.invalid/tmux'}
        state = {'candidates': [vim, tmux], 'report': {'summary': 'Prior batch', 'recipes': [
            {'name': 'vim', 'decision': 'accept', 'reason': 'Old vim assessment'},
            {'name': 'tmux', 'decision': 'accept', 'reason': 'Pinned tmux'}]}}
        assessed = {'summary': 'Retried vim', 'recipes': [
            {'name': 'vim', 'decision': 'reject', 'reason': 'New vim concern'}]}
        merged = self.updates.merge_retry_report(state, assessed, [vim])
        self.assertEqual(merged, {'summary': 'Retried vim', 'recipes': [
            {'name': 'vim', 'decision': 'reject', 'reason': 'New vim concern'},
            {'name': 'tmux', 'decision': 'accept', 'reason': 'Pinned tmux'}]})


class FeedbackFilteringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.updates = module()

    def test_only_actionable_current_head_review_inline_or_explicit_command_is_repair_input(self):
        evidence = [
            {'id': 1, 'body': 'Summary says tests failed', 'commit_id': 'head', 'state': 'COMMENTED',
             'user': {'login': 'reviewbot[bot]', 'type': 'Bot'}},
            {'id': 2, 'body': 'Fix ABI handling', 'commit_id': 'head', 'path': 'build/recipe',
             'line': 8, 'user': {'login': 'reviewbot[bot]', 'type': 'Bot'}},
            {'id': 3, 'body': 'Fix ABI handling', 'commit_id': 'head', 'state': 'CHANGES_REQUESTED',
             'user': {'login': 'reviewbot[bot]', 'type': 'Bot'}},
            {'id': 4, 'body': 'please change this', 'commit_id': 'head',
             'user': {'login': 'owner', 'type': 'User'}, 'author_association': 'OWNER'},
            {'id': 5, 'body': '/upstream repair retain the original smoke assertion',
             'commit_id': 'head', 'user': {'login': 'owner', 'type': 'User'},
             'author_association': 'OWNER'},
            {'id': 6, 'body': 'Fix ABI handling', 'commit_id': 'head', 'path': 'build/recipe',
             'line': 9, 'user': {'login': 'owner', 'type': 'User'}, 'author_association': 'OWNER'},
            {'id': 7, 'body': 'LGTM', 'commit_id': 'head', 'state': 'APPROVED',
             'user': {'login': 'owner', 'type': 'User'}, 'author_association': 'OWNER'},
        ]

        result = self.updates.current_feedback(
            evidence, 'head', '2026-11-14T00:00:00Z', ['reviewbot[bot]'])

        self.assertEqual([item['id'] for item in result], [2, 3, 5, 6])

    def test_model_closure_directive_is_not_executable_in_pr_body(self):
        body = self.updates.pr_body({
            'attempts': 1,
            'report': {'summary': 'Fixes #999', 'recipes': [
                {'name': 'vim', 'decision': 'accept', 'reason': 'Resolves #998'}]},
            'candidate_issues': [],
            'issue_closures': [42],
            'run_url': 'https://example.invalid/run/1',
        })

        self.assertIn('Fixes #42', body)
        self.assertNotIn('Fixes #999', body)
        self.assertNotIn('Resolves #998', body)


class GitHubFixture:
    repository = 'owner/tools'

    def __init__(self, pr, reviews=()):
        self.pr = pr
        self.reviews = list(reviews)
        self.dispatched = []
        self.edits = []
        self.statuses = []

        self.issues = []
        self.calls = []

    def api(self, path, data=None, method=None):
        self.calls.append((path, data, method))
        if path.startswith('collaborators/') and path.endswith('/permission'):
            return {'permission': 'admin'}
        if path.startswith('pulls?state=open&head='):
            return [self.pr] if self.pr and self.pr.get('state') == 'open' else []
        if path == 'issues' and method == 'POST':
            issue = {'number': max((item['number'] for item in self.issues), default=0) + 1,
                     'state': 'open', 'title': data['title'], 'body': data['body'],
                     'labels': data.get('labels', []),
                     'user': {'login': 'github-actions[bot]', 'type': 'Bot'}}
            self.issues.append(issue)
            return issue
        if path.startswith('issues/'):
            number = int(path.split('/')[1])
            issue = next((item for item in self.issues if item['number'] == number), None)
            if issue is None:
                raise AssertionError(f'Unexpected issue request: {path}')
            if data:
                issue.update(data)
            return issue
        if path == 'pulls/12':
            if data:
                self.edits.append(data)
                self.pr.update(data)
            return self.pr
        if path == 'statuses/merge12' and data:
            self.statuses.append(data | {'creator': {'login': 'github-actions[bot]'}})
            return self.statuses[-1]
        raise AssertionError(f'Unexpected API request: {path}')

    def pages(self, path):
        if path == 'issues?state=all&per_page=100':
            return self.issues
        if path == 'pulls/12/reviews?per_page=100':
            return self.reviews
        if path == 'commits/merge12/statuses?per_page=100':
            return self.statuses
        if path.endswith('/comments?per_page=100'):
            return []
        raise AssertionError(f'Unexpected pagination: {path}')

    def dispatch(self, workflow, ref='main', inputs=None):
        self.dispatched.append((workflow, ref, inputs))


class ControllerIntegrationTests(unittest.TestCase):
    pr = ControllerTests.pr

    @classmethod
    def setUpClass(cls):
        cls.updates = module()

    def test_approved_merge_dispatches_release_but_stale_approval_cannot(self):
        pr = self.pr() | {'merged': True, 'state': 'closed'}
        review = {'id': 1, 'user': {'login': 'maintainer', 'type': 'User'},
                  'state': 'APPROVED', 'commit_id': 'a' * 40, 'author_association': 'OWNER'}
        gh = GitHubFixture(pr, [review])
        self.updates.release(gh, {'pull_request': {'number': 12}})
        self.assertEqual(gh.dispatched, [('unraid-releases.yml', 'main', None)])
        gh = GitHubFixture(pr, [review | {'commit_id': 'old'}])
        with self.assertRaises(ValueError):
            self.updates.release(gh, {'pull_request': {'number': 12}})
        self.assertEqual(gh.dispatched, [])

    def test_review_feedback_dispatches_main_without_checking_out_pr_code(self):
        review = {'id': 1, 'user': {'login': 'coderabbitai[bot]', 'type': 'Bot'}}
        gh = GitHubFixture(self.pr(), [review])
        event = {'pull_request': {'number': 12},
                 'sender': {'login': 'coderabbitai[bot]', 'type': 'Bot'},
                 'review': {'commit_id': 'a' * 40, 'state': 'CHANGES_REQUESTED'}}
        self.updates.feedback(gh, event)
        self.assertEqual(gh.dispatched, [('upstream-versions.yml', 'main', None)])
        gh = GitHubFixture(self.pr(), [review])
        event['review']['commit_id'] = 'old'
        self.updates.feedback(gh, event)
        self.assertEqual(gh.dispatched, [])

    def test_catalog_dispatch_is_read_only_and_uses_exact_baseline_pins(self):
        gh = GitHubFixture(self.pr())
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'build').mkdir()
            (root / 'build/baseline.json').write_text(json.dumps({
                'version': '7.3.4', 'image': {'sha256': 'abc', 'url': 'https://example.invalid/image'}}))
            state = {}
            self.updates.dispatch_catalog(root, gh, self.pr(), state)
        self.assertEqual(gh.dispatched, [('build-packages.yml', 'automation/upstream-recipes', {
            'unraid_version': '7.3.4', 'unraid_sha256': 'abc', 'unraid_url': 'https://example.invalid/image',
            'channel': 'stable', 'publish': 'false'})])
        self.assertEqual(state['dispatched_head'], 'a' * 40)
        self.assertIn('upstream-recipes-state', gh.edits[0]['body'])


    def test_old_merged_batch_can_be_reused_but_unknown_branch_cannot(self):
        pr = self.pr() | {'merged_at': '2026-11-14T00:00:00Z'}
        gh = GitHubFixture(pr)
        original = gh.api
        def api(path, data=None, method=None):
            if path == 'git/matching-refs/heads/automation/upstream-recipes':
                return [{'ref': 'refs/heads/automation/upstream-recipes', 'object': {'sha': 'a' * 40}}]
            if path.startswith('pulls?state=closed'):
                return [pr]
            return original(path, data, method)
        gh.api = api
        self.assertEqual(self.updates.branch_lease(gh, None), 'a' * 40)
        pr['merged_at'] = None
        with self.assertRaises(ValueError):
            self.updates.branch_lease(gh, None)


class PartialReportFinishTests(unittest.TestCase):
    def test_ordinary_partial_plan_retains_live_decisions_and_accepts_unchanged_closed_issues(self):
        updates = module()
        vim = {'name': 'vim', 'ours': '1.0', 'upstream': '2.0',
               'source': 'Slackware-current', 'url': 'https://example.invalid/vim'}
        tmux = {'name': 'tmux', 'ours': '3.0', 'upstream': '4.0',
                'source': 'SlackBuilds.org 15.0', 'url': 'https://example.invalid/tmux'}
        zsh = {'name': 'zsh', 'ours': '5.0', 'upstream': '6.0',
               'source': 'Slackware-current', 'url': 'https://example.invalid/zsh'}
        nano = {'name': 'nano', 'ours': '7.0', 'upstream': '8.0',
                'source': 'Slackware-current', 'url': 'https://example.invalid/nano'}
        candidates = [vim, tmux, zsh, nano]
        gh = GitHubFixture(None)
        records = updates.sync_candidate_issues(gh, candidates)
        for candidate, status, attempts, decision in (
                (vim, 'assessing', 2, None), (tmux, 'building', 1, 'accept'),
                (zsh, 'rejected', 1, 'reject'), (nano, 'rejected', 1, 'reject')):
            record = updates.issue_record_for(records, candidate)
            updates.set_issue_status(gh, record, status, attempts=attempts, decision=decision)
        nano_record = updates.issue_record_for(records, nano)
        gh.issues[nano_record['number'] - 1]['state'] = 'closed'
        nano_record['status'] = 'closed'
        state = {
            'attempts': 2,
            'candidates': candidates,
            'candidate_issues': records,
            'report': {'summary': 'Prior batch', 'recipes': [
                {'name': name, 'decision': 'accept', 'reason': 'Prior validated assessment'}
                for name in ('vim', 'tmux', 'zsh', 'nano')]},
        }
        plan = {'repository': 'owner/tools', 'context': {'repository': 'owner/tools'},
                'evaluation': False, 'main_sha': 'main', 'pr_number': None,
                'head_sha': '', 'previous': '', 'feedback_digest': 'digest',
                'candidates': [vim], 'state': state}
        report = {'summary': 'Assessed vim', 'recipes': [
            {'name': 'vim', 'decision': 'reject', 'reason': 'Current assessment rejects vim'}]}

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repo, workspace = root / 'repo', root / 'workspace'
            repo.mkdir()
            workspace.mkdir()
            plan_path, report_path = root / 'plan.json', root / 'report.json'
            plan_path.write_text(json.dumps(plan))
            report_path.write_text(json.dumps(report))

            class Service(GitHubFixture):
                def __init__(self):
                    super().__init__(None)
                    self.issues = gh.issues

                def api(self, path, data=None, method=None):
                    if path == 'git/ref/heads/main':
                        return {'object': {'sha': 'main'}}
                    if path.startswith('pulls?state=open'):
                        return []
                    return super().api(path, data, method)

            validated, restored = [], []
            with patch.object(
                    updates, 'prepare_branch'), patch.object(
                    updates, 'branch_lease', return_value=''), patch.object(
                    updates, 'git', side_effect=lambda repo, *args: 'main' if args == ('rev-parse', 'origin/main') else ''), patch.object(
                    updates, 'validate_workspace', side_effect=lambda repo, workspace, value, candidates:
                        validated.append(value) or []), patch.object(
                    updates, 'import_workspace'), patch.object(
                    updates, 'manual_workflow_updates'), patch.object(
                    updates, 'restore_rejected', side_effect=lambda repo, base, value: restored.append(value)), patch.object(
                    updates, 'changed_paths', return_value=[]), patch.object(
                    updates, 'check_paths'), patch.object(
                    updates, 'run', return_value=''), patch.object(
                    updates, 'check_generated'):
                updates.finish(repo, Service(), plan_path, workspace, report_path, dry_run=True)

        self.assertEqual([item['name'] for item in validated[0]['recipes']], ['vim'])
        self.assertEqual(
            [(item['name'], item['decision']) for item in restored[0]['recipes']],
            [('vim', 'reject'), ('tmux', 'accept'), ('zsh', 'reject'), ('nano', 'reject')])


    def test_retry_during_failed_finish_pr_creation_is_not_overwritten(self):
        updates = module()
        candidate = {'name': 'vim', 'ours': '1.0', 'upstream': '2.0',
                     'source': 'Slackware-current', 'url': 'https://example.invalid/vim'}
        gh = GitHubFixture(None)
        records = updates.sync_candidate_issues(gh, [candidate])
        record = records[0]
        updates.set_issue_status(gh, record, 'building', attempts=1, decision='accept')
        state = {'attempts': 1, 'candidates': [candidate], 'candidate_issues': records,
                 'issue_closures': [], 'status': 'building'}

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repo, workspace = root / 'repo', root / 'workspace'
            (repo / 'build/locks').mkdir(parents=True)
            workspace.mkdir()
            (repo / 'build/locks/slackware.json').write_text(json.dumps([
                {'name': 'vim', 'version': '2.0'}]))
            (repo / 'build/locks/slackbuilds.json').write_text('[]')
            plan_path, report_path = root / 'plan.json', root / 'report.json'
            plan = {'repository': gh.repository, 'context': {'repository': gh.repository},
                    'evaluation': False, 'main_sha': 'main', 'pr_number': None,
                    'head_sha': '', 'previous': '', 'feedback_digest': 'digest',
                    'state': state, 'candidates': [candidate]}
            report = {'summary': 'Accepted vim', 'recipes': [
                {'name': 'vim', 'decision': 'accept', 'reason': 'Pinned upstream source'}]}
            plan_path.write_text(json.dumps(plan))
            report_path.write_text(json.dumps(report))
            original_api = gh.api

            def api(path, data=None, method=None):
                if path == 'git/ref/heads/main':
                    return {'object': {'sha': 'main'}}
                if path.startswith('pulls?state=open'):
                    return []
                if path == 'pulls':
                    retry_record = dict(record)
                    updates.set_issue_status(
                        gh, retry_record, 'queued', attempts=0, decision=None, failure=None)
                    raise RuntimeError('PR creation failed')
                return original_api(path, data, method)

            gh.api = api
            with patch.object(updates, 'branch_lease', return_value=''), patch.object(
                    updates, 'prepare_branch'), patch.object(
                    updates, 'git', side_effect=lambda repo, *args:
                        {'rev-parse': 'main', 'diff': 'build/locks/slackware.json'}.get(args[0], 'head')), patch.object(
                    updates, 'validate_workspace', return_value=['build/locks/slackware.json']), patch.object(
                    updates, 'import_workspace'), patch.object(
                    updates, 'manual_workflow_updates'), patch.object(
                    updates, 'restore_rejected'), patch.object(
                    updates, 'changed_paths', return_value=['build/locks/slackware.json']), patch.object(
                    updates, 'check_paths'), patch.object(
                    updates, 'check_generated'), patch.object(
                    updates, 'run', return_value=''), patch.object(
                    updates, 'push'):
                with self.assertRaisesRegex(RuntimeError, 'PR creation failed'):
                    updates.finish(repo, gh, plan_path, workspace, report_path)

        current = updates.read_issue_state(gh.issues[0])
        self.assertEqual((current['status'], current['attempts'], current['decision']),
                         ('queued', 0, None))

    def test_non_target_issue_change_or_close_invalidates_frozen_metadata(self):
        updates = module()
        vim = {'name': 'vim', 'ours': '1.0', 'upstream': '2.0',
               'source': 'Slackware-current', 'url': 'https://example.invalid/vim'}
        tmux = {'name': 'tmux', 'ours': '3.0', 'upstream': '4.0',
                'source': 'SlackBuilds.org 15.0', 'url': 'https://example.invalid/tmux'}
        candidates = [vim, tmux]

        for change in ('status', 'closed', 'decision', 'untrusted'):
            gh = GitHubFixture(None)
            records = updates.sync_candidate_issues(gh, candidates)
            for record in records:
                updates.set_issue_status(gh, record, 'building', attempts=1, decision='accept')
            frozen_records = [dict(record) for record in records]
            tmux_record = next(item for item in records if item['candidate']['name'] == 'tmux')
            issue = gh.issues[tmux_record['number'] - 1]
            if change == 'status':
                updates.set_issue_status(gh, tmux_record, 'queued', attempts=2)
            elif change == 'closed':
                issue['state'] = 'closed'
            elif change == 'decision':
                updates.set_issue_status(gh, tmux_record, 'building', decision='reject')
            else:
                issue['user']['login'] = 'untrusted-user'

            with self.assertRaisesRegex(ValueError, 'changed during agent work'):
                updates.verify_issue_attempts(gh, frozen_records, candidates)


class OrphanedPublicationRaceTests(unittest.TestCase):
    LEASE = 'a' * 40

    def setUp(self):
        self.updates = module()
        self.candidate = {'name': 'vim', 'ours': '1.0', 'upstream': '2.0',
                          'source': 'Slackware-current', 'url': 'https://example.invalid/vim'}
        self.gh = GitHubFixture(None)
        self.record = self.updates.sync_candidate_issues(self.gh, [self.candidate])[0]
        self.updates.set_issue_status(
            self.gh, self.record, 'building', attempts=1, decision='accept')

    def comparison(self):
        return {'head_commit': {'sha': self.LEASE}, 'total_commits': 1,
                'commits': [{'sha': self.LEASE, 'author': {'login': 'github-actions[bot]'},
                             'commit': {'message': f'Update assessed upstream recipes\n\nFixes #{self.record["number"]}'}}],
                'files': [{'filename': 'build/locks/slackware.json'}]}

    def test_branch_moved_after_lease_is_not_published(self):
        original_api = self.gh.api
        comparisons, ref_reads = [], []

        def api(path, data=None, method=None):
            if path.startswith('compare/main...'):
                comparisons.append(path)
                return self.comparison()
            if path.startswith('git/matching-refs/heads/'):
                ref_reads.append(path)
                return [{'ref': 'refs/heads/automation/upstream-recipes',
                         'object': {'sha': 'b' * 40}}]
            return original_api(path, data, method)

        self.gh.api = api
        with patch.object(self.updates, 'branch_lease', return_value=self.LEASE), self.assertRaisesRegex(
                ValueError, 'branch changed'):
            self.updates.recover_orphaned_publication(
                Path(tempfile.gettempdir()), self.gh, [self.record], [self.candidate])

        self.assertEqual(comparisons, [f'compare/main...{self.LEASE}'])
        self.assertEqual(len(ref_reads), 1)
        self.assertIsNone(self.gh.pr)
        self.assertEqual(self.gh.dispatched, [])

    def test_issue_closed_during_recovery_aborts_before_pr_creation(self):
        original_api = self.gh.api

        def api(path, data=None, method=None):
            if path.startswith('compare/main...'):
                return self.comparison()
            return original_api(path, data, method)

        self.gh.api = api
        self.gh.issues[0]['state'] = 'closed'
        with patch.object(self.updates, 'branch_lease', return_value=self.LEASE), self.assertRaisesRegex(
                ValueError, 'issue changed'):
            self.updates.recover_orphaned_publication(
                Path(tempfile.gettempdir()), self.gh, [self.record], [self.candidate])

        self.assertEqual(self.gh.issues[0]['state'], 'closed')
        self.assertEqual(self.record['status'], 'building')
        self.assertFalse(any(path == 'pulls' for path, _, _ in self.gh.calls))
        self.assertEqual(self.gh.dispatched, [])

    def test_concurrent_retry_is_not_overwritten_during_recovery(self):
        frozen = dict(self.record)
        self.updates.set_issue_status(
            self.gh, self.record, 'queued', attempts=0, decision=None, failure=None)
        original_api = self.gh.api

        def api(path, data=None, method=None):
            if path.startswith('compare/main...'):
                return self.comparison()
            return original_api(path, data, method)

        self.gh.api = api
        with patch.object(self.updates, 'branch_lease', return_value=self.LEASE), self.assertRaisesRegex(
                ValueError, 'issue changed'):
            self.updates.recover_orphaned_publication(
                Path(tempfile.gettempdir()), self.gh, [frozen], [self.candidate])
        current = self.updates.read_issue_state(self.gh.issues[0])
        self.assertEqual((current['status'], current['attempts'], current['decision']),
                         ('queued', 0, None))

    def test_retry_during_failed_pr_creation_is_not_overwritten(self):
        frozen = dict(self.record)
        original_api = self.gh.api

        def api(path, data=None, method=None):
            if path.startswith('compare/main...'):
                return self.comparison()
            if path.startswith('git/matching-refs/heads/'):
                return [{'ref': 'refs/heads/automation/upstream-recipes',
                         'object': {'sha': self.LEASE}}]
            if path == 'pulls':
                retry_record = dict(frozen)
                self.updates.set_issue_status(
                    self.gh, retry_record, 'queued', attempts=0, decision=None, failure=None)
                raise RuntimeError('PR creation failed')
            return original_api(path, data, method)

        self.gh.api = api
        with patch.object(self.updates, 'branch_lease', return_value=self.LEASE), self.assertRaisesRegex(
                RuntimeError, 'PR creation failed'):
            self.updates.recover_orphaned_publication(
                Path(tempfile.gettempdir()), self.gh, [frozen], [self.candidate])
        current = self.updates.read_issue_state(self.gh.issues[0])
        self.assertEqual((current['status'], current['attempts'], current['decision']),
                         ('queued', 0, None))

    def test_created_pr_must_still_point_to_leased_head(self):
        original_api = self.gh.api

        def api(path, data=None, method=None):
            if path == f'compare/main...{self.LEASE}':
                return self.comparison()
            if path.startswith('git/matching-refs/heads/'):
                return [{'ref': 'refs/heads/automation/upstream-recipes',
                         'object': {'sha': self.LEASE}}]
            if path == 'pulls' and data:
                created = ControllerTests.pr(self)
                created['head']['sha'] = 'b' * 40
                return data | created
            return original_api(path, data, method)

        self.gh.api = api
        with patch.object(self.updates, 'branch_lease', return_value=self.LEASE), self.assertRaisesRegex(
                ValueError, 'does not match the leased'):
            self.updates.recover_orphaned_publication(
                Path(tempfile.gettempdir()), self.gh, [self.record], [self.candidate])
        self.assertEqual(self.gh.dispatched, [])


class FinalAttemptTests(unittest.TestCase):
    def prepare_final_build(self, conclusion):
        updates = module()
        candidate = {'name': 'vim', 'ours': '1.0', 'upstream': '2.0',
                     'source': 'Slackware-current', 'url': 'https://example.invalid/vim'}
        pr = ControllerTests.pr(self) | {'draft': True}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'build').mkdir()
            baseline = {'version': '7.3.4', 'image': {
                'sha256': 'abc', 'url': 'https://example.invalid/image'}}
            (root / 'build/baseline.json').write_text(json.dumps(baseline))
            output = root / 'handoff'

            class Service(GitHubFixture):
                def api(self, path, data=None, method=None):
                    if path.startswith('actions/workflows/build-packages.yml/runs?'):
                        return {'workflow_runs': self.builds}
                    if path.startswith('commits/'):
                        return {'commit': {'committer': {'date': '2026-11-14T00:00:00Z'}}}
                    return super().api(path, data, method)

            gh = Service(pr)
            record = updates.sync_candidate_issues(gh, [candidate])[0]
            updates.set_issue_status(gh, record, 'building', attempts=3, decision='accept')
            state = {'attempts': 3, 'candidates': [candidate], 'candidate_issues': [record],
                     'report': {'summary': 'Assessed vim', 'recipes': [
                         {'name': 'vim', 'decision': 'accept', 'reason': 'Pinned source'}]}}
            pr['body'] = updates.pr_body(state)
            gh.builds = [{'id': 9, 'head_sha': pr['head']['sha'], 'event': 'workflow_dispatch',
                          'status': 'completed', 'conclusion': conclusion,
                          'html_url': 'https://example.invalid/build/9',
                          'display_title': f"Unraid compatibility {baseline['version']} "
                                           f"{baseline['image']['sha256']} publish=false"}]
            real_run = updates.run

            def boundary(args, repo=None, input=None):
                if len(args) > 1 and args[1] == 'scripts/check-upstream-versions.py':
                    return json.dumps([candidate])
                if args[:3] == ['gh', 'pr', 'ready']:
                    pr['draft'] = False
                    return ''
                return real_run(args, repo, input)

            with patch.object(updates, 'run', side_effect=boundary), patch.object(
                    updates, 'prepare_branch'), patch.object(
                    updates, 'branch_lease', return_value=pr['head']['sha']), patch.object(
                    updates, 'git', return_value='main'), patch.dict(
                    'os.environ', {'GITHUB_EVENT_NAME': 'schedule', 'UPSTREAM_REVIEW_BOTS': ''}):
                self.assertFalse(updates.prepare(root, gh, {}, output))
            return updates.read_issue_state(gh.issues[0]), pr, gh

    def test_successful_build_on_third_attempt_still_becomes_ready(self):
        issue, pr, gh = self.prepare_final_build('success')

        self.assertEqual((issue['attempts'], issue['status']), (3, 'awaiting-review'))
        self.assertFalse(pr['draft'])
        self.assertEqual(len(gh.dispatched), 0)

    def test_failed_build_on_third_attempt_blocks_without_another_session(self):
        issue, pr, gh = self.prepare_final_build('failure')

        self.assertEqual((issue['attempts'], issue['status']), (3, 'blocked'))
        self.assertTrue(pr['draft'])
        self.assertEqual(len(gh.dispatched), 0)


class BatchLifecycleTests(unittest.TestCase):
    def test_real_recipe_commit_push_dispatch_and_exact_head_ready_transition(self):
        updates = module()
        source = SCRIPT.parents[1]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'repo'
            root.mkdir()
            for directory in ('build', 'scripts', '.github'):
                shutil.copytree(source / directory, root / directory, ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copy(source / '.gitignore', root / '.gitignore')
            def git(*args):
                return subprocess.check_output(['git', '-C', str(root), *args], text=True, stderr=subprocess.STDOUT).strip()
            git('init', '-q', '-b', 'main')
            git('config', 'user.name', 'Test')
            git('config', 'user.email', 'test@example.invalid')
            # Discovery/GitHub are fixtures; handoff, validation and publication are real.
            git('add', '.')
            git('commit', '-qm', 'baseline')
            remote = Path(folder) / 'remote.git'
            subprocess.check_output(['git', 'init', '--bare', '-q', str(remote)])
            git('remote', 'add', 'origin', str(remote))
            git('push', '-q', 'origin', 'main')
            baseline = json.loads((root / 'build/baseline.json').read_text())
            candidate = {'name': 'vim', 'ours': '9.2.1252', 'upstream': '99.0',
                         'source': 'Slackware-current', 'url': 'https://example.invalid/versions'}

            class Service(GitHubFixture):
                def __init__(self):
                    super().__init__(None)
                    self.builds = []
                    self.close_on_get = False

                def api(self, path, data=None, method=None):
                    if path == 'git/ref/heads/main':
                        return {'object': {'sha': git('rev-parse', 'origin/main')}}
                    if path.startswith('pulls?state=open'):
                        return [self.pr] if self.pr else []
                    if path.startswith('pulls?state=closed'):
                        return [self.pr] if self.pr and self.pr.get('state') == 'closed' else []
                    if path.startswith('git/matching-refs'):
                        try:
                            sha = subprocess.check_output(
                                ['git', '--git-dir', str(remote), 'rev-parse',
                                 'refs/heads/automation/upstream-recipes'],
                                text=True, stderr=subprocess.DEVNULL).strip()
                        except subprocess.CalledProcessError:
                            return []
                        return [{'ref': 'refs/heads/automation/upstream-recipes', 'object': {'sha': sha}}]
                    if path.startswith('compare/main...'):
                        target = path.rsplit('...', 1)[1]
                        sha = target if len(target) == 40 else subprocess.check_output(
                            ['git', '--git-dir', str(remote), 'rev-parse',
                             'refs/heads/automation/upstream-recipes'], text=True).strip()
                        message = subprocess.check_output(
                            ['git', '--git-dir', str(remote), 'show', '-s', '--format=%B', sha], text=True)
                        files = subprocess.check_output(
                            ['git', '--git-dir', str(remote), 'diff', '--name-only', 'main', sha],
                            text=True).splitlines()
                        return {'head_commit': {'sha': sha},
                                'commits': [{'sha': sha, 'author': {'login': 'github-actions[bot]'},
                                             'commit': {'message': message}}],
                                'total_commits': 1, 'files': [{'filename': path} for path in files]}
                    if path == 'pulls' and data:
                        number = self.pr['number'] + 1 if self.pr else 12
                        pr = ControllerTests.pr(self)
                        pr['number'] = number
                        pr['head']['sha'] = git('rev-parse', 'HEAD')
                        self.pr = data | pr | {
                            'draft': data['draft'], 'body': data['body'],
                            'html_url': f'https://example.invalid/pr/{number}',
                            'node_id': f'node{number}'}
                        return self.pr
                    if path.startswith('actions/workflows/build-packages.yml/runs?'):
                        return {'workflow_runs': self.builds}
                    if path.startswith('commits/'):
                        return {'commit': {'committer': {'date': '2026-11-14T00:00:00Z'}}}
                    if (path.startswith('pulls/') and path.split('/')[1].isdigit()
                            and self.pr and int(path.split('/')[1]) == self.pr['number']):
                        if data:
                            self.pr.update(data)
                        if data is None and self.close_on_get:
                            self.pr.update(state='closed', merged=True)
                        return self.pr
                    if path == 'git/refs/heads/automation%2Fupstream-recipes' and method == 'DELETE':
                        git('push', 'origin', '--delete', 'automation/upstream-recipes')
                        return None
                    return super().api(path, data, method)

                def pages(self, path):
                    if path.startswith(('pulls/', 'issues/')):
                        return []
                    return super().pages(path)

            gh = Service()
            real_run = updates.run
            def boundary(args, repo=None, input=None):
                if len(args) > 1 and args[1] == 'scripts/check-upstream-versions.py':
                    return json.dumps([candidate])
                if args[:3] == ['gh', 'pr', 'ready']:
                    gh.pr['draft'] = False
                    return ''
                if args[:3] == ['gh', 'api', 'graphql']:
                    gh.pr['draft'] = True
                    return ''
                return real_run(args, repo, input)
            with patch.object(updates, 'run', side_effect=boundary), patch.object(
                    updates, 'push', side_effect=lambda repo, service, previous: git(
                        'push', '--force', 'origin', 'HEAD:refs/heads/automation/upstream-recipes')), patch.object(
                    updates, 'delete_branch') as delete, patch.dict(
                            'os.environ', {'GITHUB_EVENT_NAME': 'schedule', 'UPSTREAM_REVIEW_BOTS': ''}):
                output = Path(folder) / 'handoff'
                self.assertTrue(updates.prepare(root, gh, {}, output))
                plan = json.loads((output / 'plan.json').read_text())
                self.assertEqual(plan['head_sha'], '')
                self.assertEqual(plan['state']['attempts'], 1)
                self.assertEqual(len(gh.issues), 1)
                issue_state = updates.read_issue_state(gh.issues[0])
                self.assertEqual((issue_state['attempts'], issue_state['status']), (1, 'assessing'))
                self.assertNotIn('number', plan['context']['candidates'][0])
                self.assertNotIn('guide', json.loads((output / 'context.json').read_text()))
                self.assertIn('build/README.md', (output / 'prompt.md').read_text())
                import tarfile
                self.assertEqual(plan['context']['repository'], gh.repository)
                workspace = Path(folder) / 'workspace'
                workspace.mkdir()
                with tarfile.open(output / 'workspace.tar') as archive:
                    self.assertTrue(all(item.isfile() for item in archive.getmembers()))
                    archive.extractall(workspace, filter='data')
                lock = workspace / 'build/locks/slackware.json'
                entries = json.loads(lock.read_text())
                next(item for item in entries if item['name'] == 'vim')['version'] = '99.0'
                lock.write_text(json.dumps(entries, indent=2) + '\n')
                report = output / 'report.json'
                report.write_text(json.dumps({'summary': 'Reviewed source', 'recipes': [
                    {'name': 'vim', 'decision': 'accept', 'reason': 'Pinned licensed source'}]}))
                evaluation = Path(folder) / 'evaluation'
                updates.finish(root, gh, output / 'plan.json', workspace, report,
                               dry_run=True, output=evaluation)
                self.assertIsNone(gh.pr)
                self.assertEqual(gh.dispatched, [])
                self.assertIn('slackware.json', (evaluation / 'changes.patch').read_text())
                self.assertEqual(json.loads((evaluation / 'report.json').read_text())['recipes'][0]['decision'], 'accept')
                git('restore', '--staged', '.')
                git('restore', '--worktree', '.')
                workspace.mkdir()
                with tarfile.open(output / 'workspace.tar') as archive:
                    archive.extractall(workspace, filter='data')
                lock.write_text(json.dumps(entries, indent=2) + '\n')
                updates.finish(root, gh, output / 'plan.json', workspace, report)
                sha = git('rev-parse', 'HEAD')
                self.assertEqual(git('rev-parse', 'refs/remotes/origin/automation/upstream-recipes'), sha)
                self.assertTrue(gh.pr['draft'])
                self.assertIn(f"Fixes #{gh.issues[0]['number']}", gh.pr['body'])
                self.assertIn(f"Fixes #{gh.issues[0]['number']}", git('log', '-1', '--format=%B'))
                self.assertEqual(gh.dispatched[0][0:2], ('build-packages.yml', 'automation/upstream-recipes'))
                self.assertEqual(gh.dispatched[0][2]['publish'], 'false')
                entries = json.loads((root / 'build/locks/slackware.json').read_text())
                self.assertEqual(next(item['version'] for item in entries if item['name'] == 'vim'), '99.0')
                gh.builds = [{'id': 9, 'head_sha': sha, 'event': 'workflow_dispatch', 'status': 'completed',
                              'conclusion': 'success', 'html_url': 'https://example.invalid/build/9',
                              'display_title': f"Unraid compatibility {baseline['version']} {baseline['image']['sha256']} publish=false"}]
                git('checkout', '-q', 'main')
                gh.builds[0]['status'] = 'in_progress'
                self.assertFalse(updates.prepare(root, gh, {}, output))
                self.assertTrue(gh.pr['draft'])
                self.assertEqual(updates.read_state(gh.pr)['attempts'], 1)
                gh.builds[0]['status'] = 'completed'
                with patch.dict('os.environ', {'GITHUB_EVENT_NAME': 'workflow_run'}):
                    self.assertFalse(updates.prepare(root, gh, {'workflow_run': {'head_sha': 'stale'}}, output))
                self.assertFalse(updates.prepare(root, gh, {}, output))
                self.assertFalse(gh.pr['draft'])
                self.assertEqual(len(gh.dispatched), 1)
                self.assertIn('https://example.invalid/build/9', gh.pr['body'])
                issue_state = updates.read_issue_state(gh.issues[0])
                self.assertEqual(issue_state['status'], 'awaiting-review')
                self.assertIn('https://example.invalid/build/9', gh.issues[0]['body'])
                self.assertIn('Unverified, not requested', gh.pr['body'])
                saved_body = gh.pr['body']
                retry_state = updates.read_state(gh.pr)
                retry_state['validation_error'] = 'native source smoke failed'
                gh.pr['body'] = updates.pr_body(retry_state)
                self.assertTrue(updates.prepare(root, gh, {}, output))
                self.assertEqual(json.loads((output / 'context.json').read_text())['validation_error'],
                                 'native source smoke failed')
                self.assertEqual(updates.read_state(gh.pr)['attempts'], 2)
                stale_plan = Path(folder) / 'stale-plan.json'
                stale_plan.write_text((output / 'plan.json').read_text())
                gh.pr['body'] = saved_body
                git('checkout', '-q', 'main')
                record = updates.sync_candidate_issues(gh, [candidate])[0]
                updates.set_issue_status(gh, record, 'blocked', attempts=3, failure='Repair budget exhausted')
                state = updates.read_state(gh.pr)
                state['candidate_issues'] = [record]
                state['attempts'] = 3
                gh.pr['body'] = updates.pr_body(state)
                gh.builds[0]['conclusion'] = 'failure'
                self.assertFalse(updates.prepare(root, gh, {}, output))
                self.assertEqual(len(gh.dispatched), 1)
                self.assertEqual(git('branch', '--show-current'), 'main')
                self.assertEqual(updates.read_state(gh.pr)['attempts'], 3)
                candidate['upstream'] = '99.1'
                gh.builds[0]['conclusion'] = 'success'
                self.assertFalse(updates.prepare(root, gh, {}, output))
                frozen = updates.read_state(gh.pr)
                self.assertEqual([item['upstream'] for item in frozen['candidates']], ['99.0'])
                self.assertEqual(len(gh.issues), 2)
                self.assertEqual(updates.read_issue_state(gh.issues[1])['status'], 'queued')
                retry = {'issue': {'number': 12, 'pull_request': {'url': 'https://example.invalid/pr/12'}},
                         'comment': {'body': '/upstream retry'},
                         'sender': {'login': 'maintainer', 'type': 'User'}}
                updates.feedback(gh, retry)
                self.assertTrue(updates.prepare(root, gh, {}, output))
                plan = json.loads((output / 'plan.json').read_text())
                self.assertEqual([item['upstream'] for item in plan['candidates']], ['99.0'])
                workspace.mkdir()
                with tarfile.open(output / 'workspace.tar') as archive:
                    archive.extractall(workspace, filter='data')
                report.write_text(json.dumps({'summary': 'Rejected after reassessment', 'recipes': [
                    {'name': 'vim', 'decision': 'reject', 'reason': 'Maintainer requested a fresh review'}]}))
                delete.side_effect = OSError('transient branch deletion failure')
                with self.assertRaisesRegex(OSError, 'transient branch deletion'):
                    updates.finish(root, gh, output / 'plan.json', workspace, report)
                self.assertEqual(gh.pr['state'], 'open')
                delete.side_effect = lambda repo, service, previous: git(
                    'push', 'origin', '--delete', 'automation/upstream-recipes')
                self.assertFalse(updates.prepare(root, gh, {}, output))
                rejected = updates.read_issue_state(gh.issues[0])
                self.assertEqual((gh.issues[0]['state'], rejected['status'], rejected['decision']),
                                 ('open', 'rejected', 'reject'))
                self.assertNotIn(f"Fixes #{gh.issues[0]['number']}", gh.pr['body'])
                self.assertNotIn(f"Fixes #{gh.issues[0]['number']}",
                                 git('log', '-1', '--format=%B'))
                self.assertEqual(gh.pr['state'], 'closed')
                with self.assertRaises(subprocess.CalledProcessError):
                    subprocess.check_output(
                        ['git', '--git-dir', str(remote), 'rev-parse',
                         'refs/heads/automation/upstream-recipes'],
                        text=True, stderr=subprocess.STDOUT)
                gh.close_on_get = True
                with self.assertRaisesRegex(ValueError, 'PR changed or closed'):
                    updates.finish(root, gh, stale_plan, workspace, report)
                with self.assertRaises(subprocess.CalledProcessError):
                    subprocess.check_output(
                        ['git', '--git-dir', str(remote), 'rev-parse',
                         'refs/heads/automation/upstream-recipes'],
                        text=True, stderr=subprocess.STDOUT)
                gh.pr = None
                candidate['upstream'] = '99.2'
                self.assertTrue(updates.prepare(root, gh, {}, output))
                plan = json.loads((output / 'plan.json').read_text())
                self.assertEqual([item['upstream'] for item in plan['candidates']], ['99.2'])
                workspace = Path(folder) / 'workspace'

                def assess_workspace(version):
                    workspace.mkdir()
                    with tarfile.open(output / 'workspace.tar') as archive:
                        archive.extractall(workspace, filter='data')
                    lock = workspace / 'build/locks/slackware.json'
                    entries = json.loads(lock.read_text())
                    next(item for item in entries if item['name'] == 'vim')['version'] = version
                    lock.write_text(json.dumps(entries, indent=2) + '\n')
                    report.write_text(json.dumps({'summary': 'Reviewed source', 'recipes': [
                        {'name': 'vim', 'decision': 'accept', 'reason': 'Pinned licensed source'}]}))

                assess_workspace('99.2')
                with patch.object(updates, 'push', side_effect=OSError('transient push failure')):
                    with self.assertRaisesRegex(OSError, 'transient push failure'):
                        updates.finish(root, gh, output / 'plan.json', workspace, report)
                issue = updates.read_issue_state(gh.issues[-1])
                self.assertEqual((issue['attempts'], issue['status']), (1, 'building'))
                self.assertIn('transient push failure', issue['failure'])
                git('checkout', '-q', 'main')

                self.assertTrue(updates.prepare(root, gh, {}, output))
                issue = updates.read_issue_state(gh.issues[-1])
                self.assertEqual((issue['attempts'], issue['status']), (2, 'assessing'))
                plan = json.loads((output / 'plan.json').read_text())
                self.assertEqual([item['upstream'] for item in plan['candidates']], ['99.2'])
                assess_workspace('99.2')
                real_api = gh.api

                def fail_pr_creation(path, data=None, method=None):
                    if path == 'pulls' and data:
                        raise OSError('transient PR creation failure')
                    return real_api(path, data, method)

                with patch.object(gh, 'api', side_effect=fail_pr_creation):
                    with self.assertRaisesRegex(OSError, 'transient PR creation failure'):
                        updates.finish(root, gh, output / 'plan.json', workspace, report)
                issue = updates.read_issue_state(gh.issues[-1])
                self.assertEqual((issue['attempts'], issue['status']), (2, 'building'))
                self.assertIn('transient PR creation failure', issue['failure'])

                dispatch_count = len(gh.dispatched)
                self.assertFalse(updates.prepare(root, gh, {}, output))
                issue = updates.read_issue_state(gh.issues[-1])
                self.assertEqual((issue['attempts'], issue['status']), (2, 'building'))
                self.assertEqual(gh.pr['state'], 'open')
                self.assertEqual(len(gh.dispatched), dispatch_count + 1)


class RejectedRecipeTests(unittest.TestCase):
    def test_rejected_candidate_restores_recipe_and_lock_without_losing_accepted_changes(self):
        updates = module()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            def git(*args):
                return subprocess.check_output(['git', '-C', folder, *args], text=True)
            git('init', '-q')
            git('config', 'user.email', 'test@example.invalid')
            git('config', 'user.name', 'Test')
            (root / 'build/locks').mkdir(parents=True)
            entries = [{'name': 'vim', 'version': '1'}, {'name': 'tmux', 'version': '1'}]
            lock = root / 'build/locks/slackware.json'
            lock.write_text(json.dumps(entries))
            (root / 'build/locks/slackbuilds.json').write_text('[]')
            for name in ('vim', 'tmux'):
                (root / f'build/recipes/{name}').mkdir(parents=True)
                (root / f'build/recipes/{name}/recipe').write_text('original')
            git('add', '.')
            git('commit', '-qm', 'baseline')
            base = git('rev-parse', 'HEAD').strip()
            entries[0]['version'] = entries[1]['version'] = '2'
            entries[1]['dependencies'] = ['libnew']
            entries.append({'name': 'libnew', 'version': '1', 'internal': True})
            (root / 'build/recipes/libnew').mkdir()
            (root / 'build/recipes/libnew/recipe').write_text('left over from an earlier agent session')
            lock.write_text(json.dumps(entries))
            for name in ('vim', 'tmux'):
                (root / f'build/recipes/{name}/recipe').write_text('updated')
            (root / 'build/recipes/tmux/new-sidecar').write_text('unsafe')
            report = {'recipes': [{'name': 'vim', 'decision': 'accept'}, {'name': 'tmux', 'decision': 'reject'}]}
            updates.restore_rejected(root, base, report)
            self.assertEqual(json.loads(lock.read_text()), [{'name': 'vim', 'version': '2'}, {'name': 'tmux', 'version': '1'}])
            self.assertEqual((root / 'build/recipes/vim/recipe').read_text(), 'updated')
            self.assertEqual((root / 'build/recipes/tmux/recipe').read_text(), 'original')
            self.assertFalse((root / 'build/recipes/tmux/new-sidecar').exists())
            self.assertFalse((root / 'build/recipes/libnew').exists())


if __name__ == '__main__':
    unittest.main()
