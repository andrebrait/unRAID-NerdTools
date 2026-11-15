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

    def test_feedback_ignores_own_comments_and_reviews_of_old_heads(self):
        evidence = [
            {'id': 1, 'body': 'fix current bug', 'commit_id': 'new', 'user': {'login': 'coderabbitai[bot]'}},
            {'id': 2, 'body': 'old bug', 'commit_id': 'old', 'user': {'login': 'coderabbitai[bot]'}},
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
        with tempfile.TemporaryDirectory() as folder, patch.object(
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
                self.updates, 'run', return_value=json.dumps([{'name': 'sshfs', 'upstream': '2'}])), patch.object(
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

class GitHubFixture:
    repository = 'owner/tools'

    def __init__(self, pr, reviews=()):
        self.pr = pr
        self.reviews = list(reviews)
        self.dispatched = []
        self.edits = []
        self.statuses = []

    def api(self, path, data=None, method=None):
        if path == 'collaborators/maintainer/permission':
            return {'permission': 'admin'}
        if path == 'pulls/12':
            if data:
                self.edits.append(data)
            return self.pr
        if path == 'statuses/merge12' and data:
            self.statuses.append(data | {'creator': {'login': 'github-actions[bot]'}})
            return self.statuses[-1]
        raise AssertionError(f'Unexpected API request: {path}')

    def pages(self, path):
        if path == 'pulls/12/reviews?per_page=100':
            return self.reviews
        if path == 'commits/merge12/statuses?per_page=100':
            return self.statuses
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
                 'review': {'commit_id': 'a' * 40}}
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
                    if path.startswith('git/matching-refs'):
                        return []
                    if path == 'pulls' and data:
                        self.pr = data | ControllerTests.pr(self) | {
                            'draft': data['draft'], 'body': data['body'],
                            'html_url': 'https://example.invalid/pr/12', 'node_id': 'node12'}
                        self.pr['head']['sha'] = git('rev-parse', 'HEAD')
                        return self.pr
                    if path.startswith('actions/workflows/build-packages.yml/runs?'):
                        return {'workflow_runs': self.builds}
                    if path.startswith('commits/'):
                        return {'commit': {'committer': {'date': '2026-11-14T00:00:00Z'}}}
                    if path == 'pulls/12' and data:
                        self.pr.update(data)
                    if path == 'pulls/12' and data is None and self.close_on_get:
                        self.pr.update(state='closed', merged=True)
                    return super().api(path, data, method)

                def pages(self, path):
                    if path.startswith(('pulls/12/', 'issues/12/')):
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
                return real_run(args, repo, input)
            with patch.object(updates, 'run', side_effect=boundary), patch.object(
                    updates, 'push', side_effect=lambda repo, service, previous: git(
                        'push', 'origin', 'HEAD:refs/heads/automation/upstream-recipes')), patch.dict(
                            'os.environ', {'GITHUB_EVENT_NAME': 'schedule', 'UPSTREAM_REVIEW_BOTS': ''}):
                output = Path(folder) / 'handoff'
                self.assertTrue(updates.prepare(root, gh, {}, output))
                plan = json.loads((output / 'plan.json').read_text())
                self.assertEqual(plan['head_sha'], '')
                self.assertEqual(plan['state']['attempts'], 1)
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
                self.assertIn('Unverified, not requested', gh.pr['body'])
                saved_body = gh.pr['body']
                retry_state = updates.read_state(gh.pr)
                retry_state['validation_error'] = 'native source smoke failed'
                gh.pr['body'] = updates.pr_body(retry_state)
                self.assertTrue(updates.prepare(root, gh, {}, output))
                self.assertEqual(json.loads((output / 'context.json').read_text())['validation_error'],
                                 'native source smoke failed')
                self.assertEqual(updates.read_state(gh.pr)['attempts'], 2)
                gh.pr['body'] = saved_body
                git('checkout', '-q', 'main')
                state = updates.read_state(gh.pr)
                state['attempts'] = 3
                gh.pr['body'] = updates.pr_body(state)
                gh.builds[0]['conclusion'] = 'failure'
                self.assertFalse(updates.prepare(root, gh, {}, output))
                self.assertEqual(len(gh.dispatched), 1)
                self.assertEqual(git('branch', '--show-current'), 'main')
                self.assertEqual(updates.read_state(gh.pr)['attempts'], 3)
                candidate['upstream'] = '99.1'
                gh.builds[0]['conclusion'] = 'success'
                gh.close_on_get = True
                self.assertTrue(updates.prepare(root, gh, {}, output))
                with self.assertRaisesRegex(ValueError, 'PR changed or closed'):
                    updates.finish(root, gh, output / 'plan.json', workspace, report)
                self.assertEqual(git('rev-parse', 'refs/remotes/origin/automation/upstream-recipes'), sha)


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
