# Contributing

Thanks for helping improve Software Agent Factory.

## Before you start

- Search the existing issues.
- Open an issue before a large or architectural change.
- Keep each pull request focused on one problem.
- Do not add paid model calls to tests.

Read these files before changing orchestration code:

1. `AGENTS.md`
2. `docs/architecture.md`
3. `docs/symphony-alignment.md`
4. `PLAN.md`

## Set up the project

You need Python 3.13 or newer, `uv`, and Git.

```bash
git clone https://github.com/sanjit-roopra/software-agent-factory.git
cd software-agent-factory
uv sync --locked --group dev
```

Run the checks:

```bash
uv run --no-sync ruff format --check .
uv run --no-sync ruff check .
uv run --no-sync mypy src/software_agent_factory scripts/release
uv run --no-sync pytest -q --cov=software_agent_factory --cov-branch
uv run --no-sync mkdocs build --strict
```

## Make a change

- Follow the existing architecture and naming.
- Add tests for behavior changes.
- Update the docs when commands, configuration, or behavior change.
- Keep integrations disabled by default.
- Keep retries and external calls bounded.
- Never let an agent change workflow state directly.

Run `uv run factory doctor` when a change affects installation or runtime
requirements.

## Commit and pull request

Use a clear commit message. Conventional Commit prefixes such as `feat:`,
`fix:`, `docs:`, and `chore:` are preferred because they make release notes
easier to read.

By submitting a contribution, you agree that it is licensed under the
Apache License 2.0.

The pull request template lists the required checks and safety questions.

## Review

Maintainers may ask for a smaller scope or more tests. A model does not approve
its own work, so changes to factory behavior need deterministic checks and
independent review.

See [GOVERNANCE.md](GOVERNANCE.md) for project decisions and maintainer roles.
