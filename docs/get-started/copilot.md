# Real Copilot runs

`--runtime copilot` replaces the fake runtime with real model calls through the
GitHub Copilot CLI.

!!! danger "This costs money"

    Every stage of a run is a separate Copilot invocation: triage, refiner,
    optional researcher, planner, implementer, tester and reviewer — plus one
    more per repair attempt. A single run is several model calls. There is no
    spend estimate and no dry-run preview of cost.

    `--runtime fake` is the default on every command precisely so nothing can
    spend money by accident.

## Prerequisites

```bash
factory doctor --runtime copilot
```

You need the `copilot` CLI on `PATH` and already authenticated. `doctor` only
runs `copilot --version`; it makes no paid call.

## Run it

```bash
uv run factory run \
  --repo ~/projects/example \
  --title "Reject empty customer names" \
  --description "Return HTTP 400 for empty or whitespace-only names." \
  --config ~/my-factory.yaml \
  --runtime copilot
```

Nothing else changes. Same states, same artifacts, same gates.

## Which model runs which stage

Model choice is configuration, not code. The packaged defaults:

| Role | Model | Reasoning |
| --- | --- | --- |
| Triage | `claude-sonnet-5` | medium |
| Refiner | `claude-opus-5` | high |
| Researcher | `gpt-5.6-sol` | high |
| Planner | `claude-opus-5` | high |
| Worker L0 | `mai-code-1.1-flash` | medium |
| Worker L1 | `claude-sonnet-5` | medium |
| Worker L2 | `claude-opus-5` | high |
| Worker L3 | `claude-opus-5` | high |
| Tester | `claude-sonnet-5` | high |
| Reviewer | `gpt-5.6-sol` | high |

Triage assigns a complexity level, `L0` to `L3`, and that selects the worker
model. Cheap mechanical work gets a cheap model.

Configuration rejects a reviewer whose model family matches any worker's. The
final review always comes from a different family than the code that produced
the change. See [Configuration](../reference/configuration.md#models).

## What the agent is allowed to do

Each role gets a permission profile:

- **Read-only roles** (triage, refiner, researcher, planner, tester, reviewer):
  `glob`, `grep`, `view`.
- **Implementer:** `glob`, `grep`, `view`, `create`, `edit`, `bash`.

The `copilot` process is started with the workspace as its working directory
and with remote features, MCP servers, auto-update, interactive prompts and
temp-directory access disabled.

The child environment is scrubbed: GitHub credential variables such as
`GH_TOKEN`, `GITHUB_TOKEN`, `GH_ENTERPRISE_TOKEN` and `GIT_ASKPASS` are removed
before the process starts. Agents never receive GitHub credentials. Only
controller-owned code passes a token to `gh`, and only for pull request and CI
operations you explicitly enabled.

## Contract

Each role must return exactly one valid typed artifact. The runtime parses the
Copilot JSON output and validates it against the Pydantic model for that role.

Malformed or missing output is an explicit agent failure. It is never treated
as a silent pass, and it never lets a stage skip its gate. Failures consume the
run's bounded retry budget like any other failure.

## Cost and usage reporting

Token usage and cost are recorded only when the runtime actually reports them.
No runtime reports them today, so those fields stay unknown. They are never
defaulted to zero and never reconstructed from a price table.

If you need spend numbers, get them from your GitHub Copilot billing, not from
`factory status`.

## Sensible practice

- Start on a scratch repository, not on something you care about.
- Set `repository.commands` first so verification has real checks to run.
  See [Configure a repository](../guides/configure-repository.md).
- Keep `pull_request.enabled: false` until you trust the output.
- Read `patch.diff` before you push anything.

## Next

- [Configure a repository](../guides/configure-repository.md)
- [Safety and trust boundaries](../reference/safety.md)
