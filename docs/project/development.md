# Development

## Set up

You need Python 3.13 or newer, [uv](https://docs.astral.sh/uv/) and Git.

```bash
git clone https://github.com/sanjit-roopra/software-agent-factory.git
cd software-agent-factory
uv sync --locked --group dev
uv run factory --version
```

## Read first

Before changing orchestration code, read these in order:

1. [`AGENTS.md`](https://github.com/sanjit-roopra/software-agent-factory/blob/main/AGENTS.md)
2. [Architecture](../architecture.md)
3. [Symphony alignment](../symphony-alignment.md)
4. [`PLAN.md`](https://github.com/sanjit-roopra/software-agent-factory/blob/main/PLAN.md)

`AGENTS.md` is not advisory. It lists what V1 may not introduce and what agents
may not control.

## Local checks

These are the same gates CI runs.

```bash
uv run --no-sync ruff format --check .
uv run --no-sync ruff check .
uv run --no-sync mypy src/software_agent_factory scripts/release
uv run --no-sync pytest -q --cov=software_agent_factory --cov-branch
uv run --no-sync mkdocs build --strict
```

Packaging checks, if you touched anything that ships:

```bash
uv build --no-sources
uv run --no-sync twine check dist/*
uv run --no-sync check-wheel-contents dist/*.whl
uv run --no-sync pip-audit --skip-editable
```

## Dependency groups

`uv sync --group <name>` installs a subset. CI syncs the narrowest group each
job needs.

| Group | Contents |
| --- | --- |
| `quality` | ruff, mypy, type stubs |
| `test` | pytest and coverage |
| `docs` | mkdocs-material |
| `distribution` | twine, check-wheel-contents |
| `native` | pyinstaller |
| `security` | pip-audit |
| `dev` | all of the above |

## Working on the docs

The site is MkDocs Material. Sources are in `docs/`, navigation is in
`mkdocs.yml`.

```bash
uv sync --locked --no-default-groups --group docs
uv run --no-sync mkdocs serve       # http://127.0.0.1:8000
uv run --no-sync mkdocs build --strict
```

`--strict` turns warnings into errors, including broken internal links. CI runs
it, so run it before pushing.

Deliberate constraints on the docs site: no Node, no framework, no docs
versioning, no generated Python API reference, no analytics, and no
social-card or image-processing dependencies. `mkdocs.yml` stays valid for
plain `mkdocs`.

## Testing rules

Unit and integration tests must not make paid model calls, and must not reach
GitHub. The whole suite runs offline.

External boundaries have fake implementations. `FakeAgentRuntime` is the one
that matters: it exists so retries, escalation, failures and state transitions
can be tested deterministically. Fake agents are test doubles, not production
architecture.

Branch coverage is enforced at a 90% floor on Python 3.13.

Use `--data-dir` to point a test or a manual experiment at an isolated
directory instead of `~/.software-factory`.

## What CI runs

On every pull request:

- **quality** — `ruff format --check`, `ruff check`, `mypy`
- **tests** — Python 3.13 with a 90% branch-coverage floor, and Python 3.14
- **package** — build the wheel and sdist, validate them, and smoke-install both
  in clean virtualenvs
- **docs** — `mkdocs build --strict`
- **macos-arm64** and **macos-x86_64** — native packaging jobs on pushes to
  `main` and manual workflow runs
- **ci-gate** — requires all portable checks to have passed

Security workflows run dependency review on pull requests, CodeQL, and a weekly
audit of the locked environment. Scheduled workflows also test the next Python
prerelease.

No CI job holds secrets, and no CI job makes a paid model call.

Dependabot maintains uv dependencies and pinned GitHub Actions, grouping minor
and patch updates and leaving major upgrades in separate pull requests.

## Contributing

Full guidelines are in
[`CONTRIBUTING.md`](https://github.com/sanjit-roopra/software-agent-factory/blob/main/CONTRIBUTING.md).
The short version:

- Search existing issues first. Open an issue before a large or architectural
  change.
- Keep each pull request focused on one problem.
- Add tests for behaviour changes; never add paid model calls to tests.
- Update the docs when commands, configuration or behaviour change.
- Keep integrations disabled by default. Keep retries and external calls
  bounded.
- Never let an agent change workflow state directly.
- Conventional Commit prefixes (`feat:`, `fix:`, `docs:`, `chore:`) are
  preferred; they make release notes readable.

Contributions are licensed under Apache-2.0.

Project decisions and maintainer roles are in
[`GOVERNANCE.md`](https://github.com/sanjit-roopra/software-agent-factory/blob/main/GOVERNANCE.md).
Report vulnerabilities through the
[security policy](https://github.com/sanjit-roopra/software-agent-factory/security/policy),
not a public issue.

## Recording decisions

Substantive architectural choices go in [Decisions](../decisions.md) as a new
numbered ADR. If implementation shows the architecture is wrong, stop, describe
the problem, propose the smallest correction, update the architecture docs, and
then implement. Do not silently redesign the system.
