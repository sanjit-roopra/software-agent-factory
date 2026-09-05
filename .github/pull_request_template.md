## What changed

Describe the user-visible result.

## Why

Link the issue or explain the problem.

## Checks

- [ ] Tests cover the change.
- [ ] Documentation is updated when behavior or configuration changed.
- [ ] `uv run --no-sync ruff format --check .`
- [ ] `uv run --no-sync ruff check .`
- [ ] `uv run --no-sync mypy src/software_agent_factory scripts/release`
- [ ] `uv run --no-sync pytest -q --cov=software_agent_factory --cov-branch`
- [ ] `uv run --no-sync mkdocs build --strict`

## Safety

- [ ] No secrets, credentials, or private repository content are included.
- [ ] External calls and retries remain bounded.
- [ ] Agents do not change workflow state directly.
- [ ] The default configuration remains offline and uses the fake runtime.
