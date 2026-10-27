#!/usr/bin/env python3
"""Read-only, exact-head CI evidence for an author's external pull requests."""
import argparse
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

API = 'https://api.github.com'


class AuditError(Exception):
    """An endpoint could not provide complete, trustworthy evidence."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AuditError('API redirect refused; check repository identity')


class Client:
    def __init__(self, token=None):
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())

    def get(self, path):
        if not path.startswith('/') or path.startswith('//'):
            raise AuditError('Invalid API path')
        headers = {'Accept': 'application/vnd.github+json',
                   'X-GitHub-Api-Version': '2022-11-28',
                   'User-Agent': 'pr-head-audit/0.1'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        try:
            with self.opener.open(urllib.request.Request(API + path, headers=headers), timeout=30) as response:
                return json.load(response), response.headers.get('Link', '')
        except urllib.error.HTTPError as exc:
            raise AuditError(f'HTTP {exc.code}; endpoint unavailable (not a passing check)') from None
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            raise AuditError('Network or JSON response error') from None

    def pages(self, path, key=None):
        """Follow GitHub Link pagination; refuse truncation and credential redirects."""
        result, seen, expected = [], set(), 0
        while path:
            if path in seen:
                raise AuditError('Pagination loop')
            seen.add(path)
            payload, links = self.get(path)
            rows = payload.get(key) if key and isinstance(payload, dict) else payload
            if not isinstance(rows, list):
                raise AuditError('Unexpected list response')
            if isinstance(payload, dict) and payload.get('incomplete_results'):
                raise AuditError('GitHub search reports incomplete results')
            if isinstance(payload, dict):
                expected = max(expected, payload.get('total_count', 0))
            result.extend(rows)
            if isinstance(payload, dict) and payload.get('total_count', 0) > 1000 and key in ('items', 'workflow_runs'):
                raise AuditError('GitHub filtered search exceeds 1000-result cap; narrow the query')
            path = None
            for link in links.split(','):
                match = re.search(r'<([^>]+)>;\s*rel="next"', link)
                if match:
                    url = urllib.parse.urlsplit(match.group(1))
                    if url.scheme != 'https' or url.netloc != 'api.github.com' or url.fragment:
                        raise AuditError('Untrusted pagination URL')
                    path = url.path + ('?' + url.query if url.query else '')
        if len(result) < expected:
            raise AuditError('Response omitted rows without a next page')
        return result


def latest(rows, key):
    """Keep the newest record for each status context / check identity."""
    chosen = {}
    for row in rows:
        identity = key(row)
        if identity not in chosen or row.get('id', 0) > chosen[identity].get('id', 0):
            chosen[identity] = row
    return list(chosen.values())


def ci_state(checks, statuses, runs, complete):
    if not complete:
        return 'unknown'
    conclusions = [r.get('conclusion') for r in checks + runs]
    if any(c in ('failure', 'timed_out', 'startup_failure') for c in conclusions) or any(s.get('state') in ('failure', 'error') for s in statuses):
        return 'failed_observed'
    if 'action_required' in conclusions:
        return 'action_required'
    if 'cancelled' in conclusions:
        return 'cancelled'
    if any(r.get('status') != 'completed' for r in checks + runs) or any(s.get('state') == 'pending' for s in statuses):
        return 'pending'
    if not checks and not statuses and not runs:
        return 'no_checks'
    if any(c not in ('success', 'neutral', 'skipped') for c in conclusions) or any(s.get('state') != 'success' for s in statuses):
        return 'unknown'
    if not any(c == 'success' for c in conclusions) and not statuses:
        return 'skipped_only'
    return 'passed_observed'


def audit_pr(client, repo, number):
    root = f'/repos/{repo}'
    pr, _ = client.get(f'{root}/pulls/{number}')
    head = pr['head']['sha']
    result = {'repository': repo, 'number': number, 'url': pr['html_url'],
              'head_sha': head, 'state': pr['state'], 'merged': bool(pr.get('merged')),
              'mergeable': pr.get('mergeable'), 'mergeable_state': pr.get('mergeable_state'),
              'complete': True, 'errors': {}, 'checks': [], 'statuses': [], 'workflows': []}
    if pr['state'] != 'open':
        result['ci_state'] = 'closed_not_audited'
        return result

    def fetch(name, path, key=None):
        try:
            return client.pages(path, key)
        except AuditError as exc:
            result['complete'] = False
            result['errors'][name] = str(exc)
            return []

    raw_checks = fetch('checks', f'{root}/commits/{head}/check-runs?per_page=100&filter=latest', 'check_runs')
    if any(c.get('head_sha') != head for c in raw_checks):
        result['complete'] = False
        result['errors']['checks_head'] = 'Check response contains a different or missing head SHA'
    checks = latest([c for c in raw_checks if c.get('head_sha') == head],
                    lambda c: (c.get('app', {}).get('id'), c.get('name')))
    statuses = latest(fetch('statuses', f'{root}/commits/{head}/statuses?per_page=100'), lambda s: s.get('context'))
    # Status endpoint is already commit-scoped; status objects do not carry a SHA.
    raw_runs = fetch('workflows', f'{root}/actions/runs?head_sha={head}&per_page=100', 'workflow_runs')
    if any(r.get('head_sha') != head for r in raw_runs):
        result['complete'] = False
        result['errors']['workflow_head'] = 'Workflow response contains a different or missing head SHA'
    runs = latest([r for r in raw_runs if r.get('head_sha') == head],
                  lambda r: (r.get('workflow_id'), r.get('event'), r.get('head_branch')))
    for run in runs:
        row = {k: run.get(k) for k in ('id', 'name', 'event', 'head_sha', 'status', 'conclusion', 'html_url', 'run_attempt')}
        if run.get('conclusion') == 'action_required':
            jobs = fetch(f"jobs:{run['id']}", f"{root}/actions/runs/{run['id']}/jobs?per_page=100", 'jobs')
            row['jobs_count'] = len(jobs) if f"jobs:{run['id']}" not in result['errors'] else None
            row['note'] = 'Requires action; zero jobs can indicate a fork approval gate, not a test failure. Confirm on GitHub.'
        result['workflows'].append(row)
    result['checks'] = [{k: c.get(k) for k in ('id', 'name', 'head_sha', 'status', 'conclusion', 'html_url')} for c in checks]
    result['statuses'] = [{k: s.get(k) for k in ('id', 'context', 'state')} for s in statuses]
    try:
        final, _ = client.get(f'{root}/pulls/{number}')
        if final['head']['sha'] != head or final['state'] != pr['state']:
            result['complete'] = False
            result['errors']['head_recheck'] = 'PR head or state changed during audit; rerun'
            result['observed_final_head_sha'] = final['head']['sha']
    except AuditError as exc:
        result['complete'] = False
        result['errors']['head_recheck'] = str(exc)
    result['action_required'] = any(r.get('conclusion') == 'action_required' for r in checks + runs)
    result['ci_state'] = ci_state(checks, statuses, runs, result['complete'])
    return result


def audit_author(client, author):
    if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})', author):
        raise AuditError('Invalid GitHub login')
    query = urllib.parse.urlencode({'q': f'is:pr is:open author:{author}', 'per_page': 100})
    items = client.pages('/search/issues?' + query, 'items')
    results, seen = [], set()
    for item in items:
        match = re.fullmatch(r'https://api\.github\.com/repos/([^/]+/[^/]+)', item.get('repository_url', ''))
        if not match:
            raise AuditError('Unexpected repository URL in search result')
        repo = match.group(1)
        if repo.split('/')[0].casefold() == author.casefold():
            continue
        identity = (repo.casefold(), item['number'])
        if identity in seen:
            continue
        seen.add(identity)
        try:
            results.append(audit_pr(client, repo, item['number']))
        except (AuditError, KeyError, TypeError) as exc:
            results.append({'repository': repo, 'number': item['number'], 'complete': False,
                            'ci_state': 'unknown', 'errors': {'pull_request': str(exc) if isinstance(exc, AuditError) else 'Malformed PR response'}})
    return {'schema_version': 1, 'author': author,
            'observed_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'scope': 'open PRs with external target owner; source fork owner is irrelevant',
            'search_count': len(items), 'complete': all(r['complete'] for r in results), 'pull_requests': results}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('author', help='GitHub author login (public repositories only without a token)')
    args = parser.parse_args(argv)
    try:
        report = audit_author(Client(os.environ.get('GITHUB_TOKEN')), args.author)
    except AuditError as exc:
        print(json.dumps({'complete': False, 'error': str(exc)}))
        return 2
    print(json.dumps(report, indent=2))
    if not report['complete']:
        return 2
    return 0 if all(p['ci_state'] == 'passed_observed' for p in report['pull_requests']) else 1


if __name__ == '__main__':
    sys.exit(main())
