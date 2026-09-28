---
name: pr
description: >-
  Run this project's pre-PR quality gate (ruff format, ruff check, mypy,
  pytest with coverage, Simple English docs check, code review) and then
  create or update a pull request. Use when the user says "create a PR",
  "open a PR", "submit for review", or "I'm done with this change".
argument-hint: "[--skip-review] [--draft] [--base <branch>]"
user-invocable: true
allowed-tools: Read, Glob, Grep, Bash(git *), Bash(gh *), Bash(uv *), Skill(dev-team:code-review *)
---

# Pull Request (software-agent-factory)

Role: orchestrator. Enforce the quality gate, then open the PR. Do not
bypass failing gates. Be concise: report gate results and the PR URL.

## Arguments

Arguments: $ARGUMENTS

- `--skip-review`: skip the `/dev-team:code-review` step (not recommended)
- `--draft`: create a draft PR
- `--base <branch>`: target branch (default `main`)

## Steps

### 1. Pre-flight

- Current branch is not `main`.
- Working tree is clean or all changes are staged for the commit.
- `gh auth status` succeeds.

### 2. Quality gate (stop on first failure)

Run sequentially from the repository root:

```bash
uv sync --locked --group dev
uv run --no-sync ruff format --check .
uv run --no-sync ruff check --output-format=github .
uv run --no-sync mypy src/software_agent_factory scripts/docs scripts/release
uv run --no-sync pytest -q --cov --cov-report=term-missing
```

If `README.md` or anything under `docs/` changed:

```bash
uv run --no-sync python scripts/docs/check_simple_english.py
```

Coverage floor is 90% (`[tool.coverage.report] fail_under`). A failing
floor is a failing gate.

### 3. Code review

Unless `--skip-review`: invoke `/dev-team:code-review` on the diff against
the base branch. Blocking findings must be fixed before continuing.

### 4. Commit and push

- Commit with a conventional, descriptive message. Keep normal prose in the
  message body (the concision rule does not apply to commit messages).
- `git push -u origin HEAD`.

### 5. Create or update the PR

```bash
gh pr create --base <base> [--draft] --title "<title>" --body "<body>"
```

If a PR for the branch already exists, update it with `gh pr edit` instead.

PR body sections: Summary, Changes, Verification (the exact gate commands
run and their results), Notes for reviewers. End the body with:

```
🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

### 6. Report

Print the gate results table and the PR URL. Nothing else.
