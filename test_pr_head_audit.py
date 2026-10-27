import io
import json
import unittest
from unittest.mock import patch

import pr_head_audit as audit


class FixtureClient(audit.Client):
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, path):
        self.calls.append(path)
        value = self.responses[path]
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value()
        return value, ''


def fixture():
    root = '/repos/upstream/project'
    sha = 'a' * 40
    pr = {'head': {'sha': sha}, 'html_url': 'https://github.com/upstream/project/pull/7',
          'state': 'open', 'merged': False, 'mergeable': None, 'mergeable_state': 'unknown'}
    responses = {
        root + '/pulls/7': pr,
        f'{root}/commits/{sha}/check-runs?per_page=100&filter=latest': {'check_runs': []},
        f'{root}/commits/{sha}/statuses?per_page=100': [],
        f'{root}/actions/runs?head_sha={sha}&per_page=100': {'workflow_runs': []},
    }
    return FixtureClient(responses), root, sha


class ClassificationTests(unittest.TestCase):
    def test_empty_is_not_success(self):
        self.assertEqual(audit.ci_state([], [], [], True), 'no_checks')

    def test_incomplete_is_never_green(self):
        self.assertEqual(audit.ci_state([], [{'state': 'success'}], [], False), 'unknown')

    def test_pending(self):
        self.assertEqual(audit.ci_state([{'status': 'in_progress'}], [], [], True), 'pending')

    def test_unknown_conclusion(self):
        self.assertEqual(audit.ci_state([{'status': 'completed', 'conclusion': 'new_value'}], [], [], True), 'unknown')

    def test_only_skipped(self):
        self.assertEqual(audit.ci_state([{'status': 'completed', 'conclusion': 'skipped'}], [], [], True), 'skipped_only')

    def test_legacy_error(self):
        self.assertEqual(audit.ci_state([], [{'state': 'error'}], [], True), 'failed_observed')

    def test_success(self):
        self.assertEqual(audit.ci_state([], [{'state': 'success'}], [], True), 'passed_observed')

    def test_action_required_not_test_failure(self):
        c, root, sha = fixture()
        c.responses[f'{root}/actions/runs?head_sha={sha}&per_page=100'] = {'workflow_runs': [
            {'id': 10, 'head_sha': sha, 'workflow_id': 1, 'status': 'completed', 'conclusion': 'action_required'}]}
        c.responses[root + '/actions/runs/10/jobs?per_page=100'] = {'jobs': []}
        r = audit.audit_pr(c, 'upstream/project', 7)
        self.assertEqual(r['ci_state'], 'action_required')
        self.assertEqual(r['workflows'][0]['jobs_count'], 0)

    def test_missing_jobs_is_not_zero_jobs(self):
        c, root, sha = fixture()
        c.responses[f'{root}/actions/runs?head_sha={sha}&per_page=100'] = {'workflow_runs': [
            {'id': 10, 'head_sha': sha, 'workflow_id': 1, 'status': 'completed', 'conclusion': 'action_required'}]}
        c.responses[root + '/actions/runs/10/jobs?per_page=100'] = audit.AuditError('HTTP 403')
        r = audit.audit_pr(c, 'upstream/project', 7)
        self.assertEqual(r['ci_state'], 'unknown')
        self.assertIsNone(r['workflows'][0]['jobs_count'])

    def test_ignore_stale_sha(self):
        c, root, sha = fixture()
        c.responses[f'{root}/commits/{sha}/check-runs?per_page=100&filter=latest'] = {'check_runs': [
            {'id': 1, 'name': 'unit', 'head_sha': 'old', 'status': 'completed', 'conclusion': 'success'}]}
        self.assertEqual(audit.audit_pr(c, 'upstream/project', 7)['ci_state'], 'unknown')

    def test_head_changes_during_audit(self):
        c, root, sha = fixture()
        pr = c.responses[root + '/pulls/7']
        values = iter([pr, {**pr, 'head': {'sha': 'b' * 40}}])
        c.responses[root + '/pulls/7'] = lambda: (next(values), '')
        r = audit.audit_pr(c, 'upstream/project', 7)
        self.assertFalse(r['complete'])
        self.assertEqual(r['ci_state'], 'unknown')

    def test_legacy_latest_context_wins(self):
        c, root, sha = fixture()
        c.responses[f'{root}/commits/{sha}/statuses?per_page=100'] = [
            {'id': 2, 'context': 'unit', 'state': 'success'}, {'id': 1, 'context': 'unit', 'state': 'failure'}]
        self.assertEqual(audit.audit_pr(c, 'upstream/project', 7)['ci_state'], 'passed_observed')

    def test_closed_only_records_status(self):
        c, root, _ = fixture()
        c.responses[root + '/pulls/7']['state'] = 'closed'
        r = audit.audit_pr(c, 'upstream/project', 7)
        self.assertEqual(r['ci_state'], 'closed_not_audited')
        self.assertEqual(len(c.calls), 1)


class PaginationTests(unittest.TestCase):
    def test_follows_next_not_last(self):
        c = audit.Client()
        with patch.object(c, 'get', side_effect=[({'items': [1]}, '<https://api.github.com/p?page=2>; rel="next", <https://api.github.com/p?page=2>; rel="last"'), ({'items': [2]}, '')]):
            self.assertEqual(c.pages('/p', 'items'), [1, 2])

    def test_foreign_link_rejected(self):
        c = audit.Client()
        with patch.object(c, 'get', return_value=([], '<https://evil.example/page>; rel="next"')):
            with self.assertRaises(audit.AuditError): c.pages('/p')

    def test_loop_rejected(self):
        c = audit.Client()
        with patch.object(c, 'get', return_value=([], '<https://api.github.com/p>; rel="next"')):
            with self.assertRaises(audit.AuditError): c.pages('/p')

    def test_incomplete_search_rejected(self):
        c = FixtureClient({'/p': {'items': [], 'incomplete_results': True}})
        with self.assertRaises(audit.AuditError): c.pages('/p', 'items')

    def test_search_cap_rejected(self):
        c = FixtureClient({'/p': {'items': [], 'total_count': 1001}})
        with self.assertRaises(audit.AuditError): c.pages('/p', 'items')

    def test_bad_response_rejected(self):
        with self.assertRaises(audit.AuditError): FixtureClient({'/p': {}}).pages('/p', 'items')


class AuthorTests(unittest.TestCase):
    def test_target_owner_not_source_fork_and_dedup(self):
        c = audit.Client()
        items = [{'repository_url': 'https://api.github.com/repos/USER/own', 'number': 1},
                 {'repository_url': 'https://api.github.com/repos/upstream/project', 'number': 7}]
        with patch.object(c, 'pages', return_value=items + [items[1]]), patch.object(audit, 'audit_pr', return_value={'complete': True}) as pr:
            r = audit.audit_author(c, 'user')
            pr.assert_called_once_with(c, 'upstream/project', 7)
            self.assertEqual(len(r['pull_requests']), 1)

    def test_invalid_author_rejected(self):
        with self.assertRaises(audit.AuditError): audit.audit_author(audit.Client(), 'name is:closed')

    def test_error_json_exit_two(self):
        out = io.StringIO()
        with patch.object(audit, 'audit_author', side_effect=audit.AuditError('HTTP 403')), patch('sys.stdout', out):
            self.assertEqual(audit.main(['user']), 2)
        self.assertFalse(json.loads(out.getvalue())['complete'])


if __name__ == '__main__':
    unittest.main()
