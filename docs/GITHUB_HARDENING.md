# GitHub Hardening and Main Branch Protection

This repository cannot enforce GitHub branch protection from git files alone.
Protection must be configured in repository settings (UI/API).

## Recommended Main Branch Rules

Apply these rules to `main`:

- Require pull request before merging
- Require at least 1 approving review
- Dismiss stale reviews when new commits are pushed
- Require conversation resolution before merge
- Require status checks to pass before merge
  - `test (3.10)`
  - `test (3.11)`
  - `test (3.12)`
  - `test (3.13)`
- Require branches to be up to date before merging
- Restrict who can push to `main` (optional, recommended for teams)
- Do not allow force pushes
- Do not allow deletions

## Configure in GitHub UI

1. Go to `Settings -> Branches`.
2. Add branch protection rule for `main`.
3. Enable the options listed above.

## Configure via GitHub CLI (Ruleset API)

Run from your machine after `gh auth login`:

```bash
gh api \
  -X POST \
  repos/vinayak3022/codeSpine/rulesets \
  -f name='main-protection' \
  -f target='branch' \
  -f enforcement='active' \
  -f conditions='{"ref_name":{"include":["~DEFAULT_BRANCH"],"exclude":[]}}' \
  -f rules='[
    {"type":"pull_request","parameters":{"required_approving_review_count":1,"dismiss_stale_reviews_on_push":true,"require_code_owner_review":true,"require_last_push_approval":false,"required_review_thread_resolution":true}},
    {"type":"required_status_checks","parameters":{"strict_required_status_checks_policy":true,"required_status_checks":[{"context":"test (3.10)"},{"context":"test (3.11)"},{"context":"test (3.12)"},{"context":"test (3.13)"}]}},
    {"type":"non_fast_forward"},
    {"type":"deletion"}
  ]'
```

If your status-check names differ, update them with exact names from Actions.

## Optional Extra Hardening

- Enable Dependabot alerts and security updates.
- Enable secret scanning and push protection.
- Require signed commits.
- Protect tags for release branches.
