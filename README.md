# PR Head Audit

A dependency-free, read-only Python CLI for **commit-pinned CI evidence across an author's external pull requests**. It helps distinguish “the checks I could observe passed” from “there were no checks”, “GitHub refused the endpoint”, and “the workflow requires action before jobs can run”.

## Run

Python 3.10+; no installation, SDK, Git checkout, or GitHub CLI required:

```sh
python pr_head_audit.py YOUR_GITHUB_LOGIN > audit.json
python -m unittest -v
```

Public API access works without a token, subject to GitHub's low anonymous rate limit. An **already-authorized** `GITHUB_TOKEN` environment variable is optional. The program never creates, requests, stores, or prints a token. Never put a token on the command line or commit your reports. Token permissions limit what can be observed; a denied endpoint remains unknown.

Exit codes: **0** all discovered external open PRs have passing observed evidence (also 0 for no matching PRs; inspect the list); **1** complete report with failed, pending, action-required, cancelled, skipped-only or absent checks; **2** incomplete/unavailable evidence. A 0 is **not permission to merge**.

## What it does

- Searches an author's open PRs, follows API pagination and rejects incomplete search/capped or truncated results.
- Filters using the **target repository owner**, case-insensitively. An upstream PR from the author's fork remains included.
- Pins check runs, legacy commit statuses and workflow runs to the observed head SHA. Keeps the newest check per app/name and status per context.
- Re-reads the PR after collection. A changed head/state makes the result incomplete, never green.
- Preserves endpoint errors independently so unavailable Actions data cannot masquerade as an empty successful result.
- Reports `action_required` and job count separately. Zero jobs **can** mean fork workflow approval is required; it is not proof of an approval gate or failed tests. Confirm on the linked GitHub page.
- If failure and action-required evidence coexist, reports `failed_observed` plus `action_required: true`, so the action gate cannot hide the failure.
- Never approves runs, posts comments, pushes commits, merges, deploys, changes permissions, or writes files itself.

Only GET requests to `https://api.github.com` are sent. Redirects and off-host pagination links are refused. Raw provider error bodies, status descriptions and external status URLs are intentionally omitted to reduce accidental leakage of provider credentials/authorization links. Reports can still contain private repository names or check names if your token can see them: **keep reports private unless reviewed**.

## Interpretation and limits

`passed_observed` means the fetched evidence passed, not that all branch-protection requirements are satisfied or that every desired test exists. Neutral/skipped checks do not count as executed tests. `failed_observed` describes CI/provider state, **not a diagnosis**: a deployment provider may fail due to authorization rather than code. Cancelled runs are reported separately. Review comments, approvals, required-check configuration, deployment environments, merge-queue commits and test logs are outside this tool's scope.

Workflow evidence is commit-scoped and may include multiple events for the same SHA; it is not a claim that every run belongs exclusively to this PR. A PR closed during discovery is reported as closed without CI maintenance. Search-index delay and changes between API calls mean the output is a timestamped observation, not an atomic snapshot. Search over 1,000 results is rejected rather than silently truncated. Large audits may hit rate limits; the tool does not retry indefinitely or modify rate-limit settings. GitHub Enterprise/custom hosts are not supported.

## Why not just `gh`?

Use [`gh pr checks`](https://cli.github.com/manual/gh_pr_checks) for interactive single-PR checks/watch and [`gh status`](https://cli.github.com/manual/gh_status) for your subscribed activity. This tool is a small complementary evidence exporter: author-wide external-target filtering, per-endpoint completeness, exact-head race checks, and zero-job action-required distinctions in one JSON report. It does not wrap `gh`, claim these capabilities are unique, or replace GitHub's merge controls.

Public API references: [check runs](https://docs.github.com/en/rest/checks/runs), [commit statuses](https://docs.github.com/en/rest/commits/statuses), [workflow runs](https://docs.github.com/en/rest/actions/workflow-runs), [pagination](https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api).

## Verification

The unittest suite uses original synthetic fixtures and fake transports: no credentials, private records, or live writes. It covers paging, truncated/foreign/looping links, stale-head checks, head races, denied endpoints, status supersession, fork-owner filtering, action-required workflows with zero/unavailable jobs, mixed failures and action gates, skipped-only states, and token-safe errors.

Development environment: Python 3.12.14. A live anonymous smoke on 2026-10-27 retrieved 5 open search results for GitHub's public `octocat` example account, excluded the self-owned target, then all 4 external PR detail requests returned HTTP 403. The CLI reported `complete: false`, unknown evidence and exit code 2; a successful full live audit is **not** claimed. Offline tests exercise the CLI and API semantics, not GitHub service availability. No GitHub Actions workflow is enabled by this repository; run the documented tests locally.

AI-assisted implementation and independently reviewed synthetic regression tests. MIT licensed; see LICENSE.
