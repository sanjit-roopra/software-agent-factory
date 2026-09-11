# Software Agent Factory

[![CI](https://github.com/sanjit-roopra/software-agent-factory/actions/workflows/ci.yml/badge.svg)](https://github.com/sanjit-roopra/software-agent-factory/actions/workflows/ci.yml)
[![Docs](https://github.com/sanjit-roopra/software-agent-factory/actions/workflows/docs.yml/badge.svg)](https://sanjit-roopra.github.io/software-agent-factory/)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue)](https://www.python.org/downloads/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

A local factory that turns a work item into a reviewed change. Specialized
agents triage, refine, research, plan, implement, test and review the work.

Git worktrees, commands and state stay on your machine. GitHub Copilot runs the
real agents. GitHub automation stays separate and opt-in.

LLMs provide intelligence. Deterministic code provides authority. Agents return
short typed artifacts. They do not control state, budgets, model routes, gates,
or merges.

All factory-authored prose uses a mandatory controlled writing policy. It uses
selected checks from [SimpleEnglish](https://github.com/AminBlg/SimpleEnglish)
and follows ASD-STE100 principles. The checks do not prove formal compliance.

**[Documentation](https://sanjit-roopra.github.io/software-agent-factory/)**

## Safety defaults

Nothing costs money or touches the network unless you enable it.

| Default | Value |
| --- | --- |
| Agent runtime | `fake` (deterministic, offline, free) |
| `pull_request.enabled` | `false` |
| `ci.enabled` | `false` |
| `merge.enabled` | `false` |
| `scheduler.enabled` | `false` |
| Dashboard | not running |
| launchd service | not installed |

The factory never force-pushes or deploys. Automatic merging is opt-in.
Reviewed pull requests in allowlisted repositories must pass required checks.
The factory never bypasses branch protection. Projects can deliver task
pull requests to the target branch through bounded CI repair.
Read [autonomous project delivery](docs/guides/projects.md#autonomous-delivery).
Every retry is bounded. The test suite runs offline.

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
Make sure that `SHA256SUMS` matches before you extract an archive.
Clear the macOS Gatekeeper quarantine flag because archives are unsigned.
Read [Install](https://sanjit-roopra.github.io/software-agent-factory/get-started/install/).

## Five minutes

Run one work item through the pipeline. This makes no network calls and
costs nothing.

```bash
uv run factory run \
  --repo ~/projects/example \
  --title "Reject empty customer names" \
  --description "Return HTTP 400 for empty or whitespace-only names." \
  --acceptance-criterion "Empty or whitespace-only names return HTTP 400." \
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
→ IMPLEMENTING → VERIFYING → REVIEWING → PR_READY`.
The run used an isolated Git worktree.
Each stage persisted a typed artifact.

Inspect it:

```bash
uv run factory runs   --data-dir ./.factory-demo
uv run factory show   RUN_ID --data-dir ./.factory-demo
uv run factory status --data-dir ./.factory-demo
```

Add `--runtime copilot` for real agents.
That option costs money and is not the default.

Read the full walkthrough:
[First offline run](https://sanjit-roopra.github.io/software-agent-factory/get-started/first-run/).

For a broader product or feature description, let the factory derive and
execute the smallest sufficient work breakdown:

```bash
uv run factory project \
  --repo ~/projects/example \
  --title "Build customer onboarding" \
  --description "Add signup, email verification, and the first-login flow." \
  --acceptance-criterion "A new customer can complete onboarding." \
  --runtime copilot
```

The project planner uses one work item unless a real boundary requires more.
The factory stores the plan, runs ready tasks, and combines their commits.
Then the factory runs repository verification on the combined branch.
Use `--github-repo OWNER/NAME` to create one issue for each task.
The factory does not add the `agent-ready` label.

## Commands

| Command | Purpose |
| --- | --- |
| `factory run` | Run one work item through the workflow. |
| `factory project` | Derive the smallest sufficient task graph and execute it. |
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

Use with supervision only.
The system works end to end. Packaging, CI, and the release process are real.

- Packaged builds: macOS, native arm64 and native x86_64. No `universal2`.
- Supported platform: macOS with Python 3.13+. Other platforms are not
  tested or supported.
- External tools: `git` is always required. `gh` is required only for GitHub
  integrations. `copilot` is required only for `--runtime copilot`.
- Implemented: phases 0 to 14, plus 15.0, 15.1, 15.2, 15.5, 15.11, 16, and 17.
- Deferred: staging, deployment, Docker and Kubernetes sandboxes, remote
  workers, Postgres, Temporal, and non-GitHub trackers.

See the
[roadmap](https://sanjit-roopra.github.io/software-agent-factory/project/roadmap/).

## Documentation

- [Get started](https://sanjit-roopra.github.io/software-agent-factory/get-started/)
- [How it works](https://sanjit-roopra.github.io/software-agent-factory/concepts/how-it-works/)
- [Safety and trust boundaries](https://sanjit-roopra.github.io/software-agent-factory/reference/safety/)
- [Configuration reference](https://sanjit-roopra.github.io/software-agent-factory/reference/configuration/)
- [Writing policy](docs/reference/writing-policy.md)
- [Architecture](docs/architecture.md) ·
  [Symphony alignment](docs/symphony-alignment.md) ·
  [Decisions](docs/decisions.md)
- [`AGENTS.md`](AGENTS.md) and [`PLAN.md`](PLAN.md): rules for changing this
  repository, and the phased implementation plan.

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md). Local checks:

```bash
uv run --no-sync ruff format --check .
uv run --no-sync ruff check .
uv run --no-sync mypy src/software_agent_factory scripts/docs scripts/release
uv run --no-sync pytest -q --cov=software_agent_factory --cov-branch
uv run --no-sync python scripts/docs/check_simple_english.py
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
