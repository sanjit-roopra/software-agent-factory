# Software Agent Factory

[![CI](https://github.com/sanjit-roopra/software-agent-factory/actions/workflows/ci.yml/badge.svg)](https://github.com/sanjit-roopra/software-agent-factory/actions/workflows/ci.yml)
[![Docs](https://github.com/sanjit-roopra/software-agent-factory/actions/workflows/docs.yml/badge.svg)](https://sanjit-roopra.github.io/software-agent-factory/)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue)](https://www.python.org/downloads/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

A local-first autonomous software engineering factory. It takes one work item
from triage to a reviewed change using specialized agents — triage, refinement,
optional research, planning, implementation, verification and review — each with
its own model.

Orchestration, Git worktrees, tests, builds and persisted state stay on your
machine. The real agent runtime calls GitHub Copilot. GitHub automation is
separate and opt-in.

The design rule is that LLMs provide intelligence and deterministic code
provides authority. Agents return typed artifacts. They do not control workflow
state, retry budgets, model routing, quality gates or merging.

**[Documentation](https://sanjit-roopra.github.io/software-agent-factory/)**

## Safety defaults

Nothing costs money or touches the network unless you turn it on.

| Default | Value |
| --- | --- |
| Agent runtime | `fake` — deterministic, offline, free |
| `pull_request.enabled` | `false` |
| `ci.enabled` | `false` |
| `scheduler.enabled` | `false` |
| Dashboard | not running |
| launchd service | not installed |

The factory never force-pushes, never merges and never deploys. Every retry is
bounded. The test suite runs entirely offline: it never calls a model and never
reaches GitHub.

## Install

Requires Python 3.13+, [uv](https://docs.astral.sh/uv/) and `git`.

```bash
git clone https://github.com/sanjit-roopra/software-agent-factory.git
cd software-agent-factory
uv sync --locked --group dev
uv run factory --version
```

Released macOS archives and wheels are on the
[releases page](https://github.com/sanjit-roopra/software-agent-factory/releases).
Verify `SHA256SUMS` before extracting, and clear the macOS Gatekeeper quarantine
flag — archives are unsigned. See
[Install](https://sanjit-roopra.github.io/software-agent-factory/get-started/install/).

## Five minutes

Run one work item through the whole pipeline. This makes no network calls and
costs nothing.

```bash
uv run factory run \
  --repo ~/projects/example \
  --title "Reject empty customer names" \
  --description "Return HTTP 400 for empty or whitespace-only names." \
  --config config/factory.example.yaml \
  --data-dir ./.factory-demo
```

```text
run id: run-9bb36bbbdf114f53bd9599a103122976
state: PR_READY
workspace: ./.factory-demo/workspaces/WI-c769695fc242
changed files: FACTORY_NOTES.md
```

The run moved through `CREATED → TRIAGING → REFINING → [RESEARCHING] → PLANNING
→ IMPLEMENTING → VERIFYING → REVIEWING → PR_READY`, in an isolated Git worktree,
persisting a typed artifact per stage.

Inspect it:

```bash
uv run factory runs   --data-dir ./.factory-demo
uv run factory show   RUN_ID --data-dir ./.factory-demo
uv run factory status --data-dir ./.factory-demo
```

Add `--runtime copilot` for real agents. That costs money and is never the
default.

Full walkthrough:
[First offline run](https://sanjit-roopra.github.io/software-agent-factory/get-started/first-run/).

## Commands

| Command | Purpose |
| --- | --- |
| `factory run` | Run one work item through the workflow. |
| `factory start` | Poll a GitHub Issues backlog and dispatch work (opt-in). |
| `factory runs` / `show` | List and inspect persisted runs. |
| `factory doctor` | Check prerequisites for your configuration. |
| `factory status` | Derived run metrics and health, read-only. |
| `factory skill` | Inspect, validate or refresh repository guidance and your overlay. |
| `factory dashboard` | Loopback-only, token-protected, read-only viewer. |
| `factory service` | Install or remove the opt-in macOS launchd agent. |

See the
[CLI reference](https://sanjit-roopra.github.io/software-agent-factory/reference/cli/).

## Platform and status

Early, and supervised use only. It works end to end, and CI, packaging and the
release process are real.

- **Packaged builds:** macOS, native arm64 and native x86_64. No `universal2`.
- **Supported platform:** macOS with Python 3.13+. Other platforms are not
  tested or supported.
- **External tools:** `git` always; `gh` only for the GitHub integrations;
  `copilot` only for `--runtime copilot`.
- **Implemented:** phases 0–14, plus 15.0, 15.1, 15.2, 15.5 and 15.11.
- **Deferred:** staging, deployment, Docker and Kubernetes sandboxes, remote
  workers, Postgres, Temporal, non-GitHub trackers.

See the
[roadmap](https://sanjit-roopra.github.io/software-agent-factory/project/roadmap/).

## Documentation

- [Get started](https://sanjit-roopra.github.io/software-agent-factory/get-started/)
- [How it works](https://sanjit-roopra.github.io/software-agent-factory/concepts/how-it-works/)
- [Safety and trust boundaries](https://sanjit-roopra.github.io/software-agent-factory/reference/safety/)
- [Configuration reference](https://sanjit-roopra.github.io/software-agent-factory/reference/configuration/)
- [Architecture](docs/architecture.md) ·
  [Symphony alignment](docs/symphony-alignment.md) ·
  [Decisions](docs/decisions.md)
- [`AGENTS.md`](AGENTS.md) and [`PLAN.md`](PLAN.md) — the rules for changing this
  repository, and the phased implementation plan.

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md). Local checks:

```bash
uv run --no-sync ruff format --check .
uv run --no-sync ruff check .
uv run --no-sync mypy src/software_agent_factory scripts/release
uv run --no-sync pytest -q --cov=software_agent_factory --cov-branch
uv run --no-sync mkdocs build --strict
```

Do not add paid model calls to tests.

Also see [GOVERNANCE.md](GOVERNANCE.md), [SUPPORT.md](SUPPORT.md) and the
[code of conduct](CODE_OF_CONDUCT.md).

## Security

Report vulnerabilities through the
[security policy](https://github.com/sanjit-roopra/software-agent-factory/security/policy),
not a public issue. See [SECURITY.md](SECURITY.md).

## License

[Apache-2.0](LICENSE).
