# Real Copilot runs

`--runtime copilot` replaces the fake runtime with real model calls through the
GitHub Copilot CLI.

!!! danger "This costs money"

    Every stage of a run is a separate Copilot invocation: triage, planner,
    implementer, tester, and reviewer.
    Each repair attempt adds one invocation. With the packaged configuration,
    the enabled post-green polish adds a second Implementer invocation.
    A single run uses several model calls. The factory provides no spend
    estimate and no dry-run preview of cost.

    `--runtime fake` is the default on every command to prevent unexpected spending.

## Prerequisites

```bash
factory doctor --runtime copilot
```

You need the `copilot` CLI on `PATH` and authenticated.
`doctor` only runs `copilot --version`. It makes no paid model call.

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

Use `--model-profile economy` to select the packaged lower-cost routing table.
The option is also available on `project`, `start`, `doctor`, `skill refresh`
and `service install`.

Use `--model-profile security` for an expensive security-focused route:
GPT-6 Astra performs the independent Tester pass.
GPT-5.6 Sol remains the final Reviewer.
Astra stays out of the worker map.
Thus, a reviewer model family cannot review its own worker family.

Before triage, the controller profiles the prepared worktree without shell,
network or imports and persists `repository-profile.json`. This scan itself
does not call Copilot.

## Which model runs which stage

Model choice is configuration, not code. The packaged defaults:

| Role | Model | Reasoning | Context |
| --- | --- | --- | --- |
| Triage | `gpt-5.6-terra` | medium | default |
| Planner | `claude-opus-5` | high | default |
| Worker L0 | `mai-code-1.1-flash` | medium | default |
| Worker L1 | `gemini-3.8-flash` | high | default |
| Worker L2 | `claude-sonnet-5` | high | default |
| Worker L3 | `claude-opus-5` | high | default |
| Tester | `gemini-3.8-flash` | high | default |
| Reviewer | `gpt-5.6-sol` | high | default |

Triage assigns a complexity level, `L0` to `L3`, and that selects the worker
model. Cheap mechanical work gets a cheap model.

Configuration rejects a reviewer whose model family matches any worker's. The
final review always comes from a different family than the code that produced
the change. See [Configuration](../reference/configuration.md#models).

For current model prices, context and reasoning capabilities, benchmark
evidence, and role-specific tradeoffs, see
[Model selection, cost and benchmarks](../reference/model-selection.md).

## Polish guidance

There is no repository-provided skill plugin. The deterministic profile records
technologies, test tools, package managers (`uv`, `pip`, `poetry`, `npm`,
`pnpm`, `yarn`, `bun`), version files, exact dependency declarations from
`pyproject.toml`, `requirements*.txt` and `package.json`, and two fingerprints.
It records nothing more.

No model writes or selects guidance. The bounded polish attempt gets the fixed
`simplify` and `polish` templates of the factory and the review lenses for the
changed files. A stack lens for React, Vue or Angular applies only when the
repository declares one of its dependencies. The guidance is advisory prompt
context. It does not grant tools, change model routing, add commands, alter
workflow states, spend retry budget, or waive gates.

## What the agent is allowed to do

Each role gets a permission profile:

- Read-only roles (triage, planner, tester, reviewer):
  `glob`, `grep`, `view`.
- Implementer: `glob`, `grep`, `view`, `create`, `edit`, `bash`.

The optional polish uses the same Implementer permission profile and worker
routing. It introduces no separate role.

The factory starts the `copilot` process with the workspace as its working directory.
It disables remote features, MCP servers, auto-update, interactive prompts, and
temporary directory access.

The factory scrubs the child environment. It removes GitHub credential variables
such as `GH_TOKEN`, `GITHUB_TOKEN`, `GH_ENTERPRISE_TOKEN`, and `GIT_ASKPASS`
before the process starts. Agents never receive GitHub credentials. Only
controller-owned code passes a token to `gh`. It passes tokens only for pull
request and CI operations that you explicitly enabled.

## Contract

Each role must return exactly one valid typed artifact.
The runtime parses the final Copilot JSON output and ignores lifecycle events.
Then the runtime validates the artifact against the Pydantic model for that role.

Malformed or missing output is an explicit agent failure. It is never treated
as a silent pass, and it never lets a stage skip its gate.

Planner, Tester and Reviewer output-shape failures get a bounded same-model
correction sequence. The correction includes the exact validation error and
asks for one complete artifact. Tester and Reviewer corrections are recorded as
agent invocations but do not consume implementation attempts. Other failures,
or an exhausted correction limit, fail the stage.

## Cost and usage reporting

The runtime passes `--context` and `--usage-output-file` explicitly to Copilot.
Run and project artifacts persist reported input tokens, output tokens,
cache tokens, and reasoning tokens. They also record elapsed time, nano-AIU,
and premium-request cost. `factory status` and the local dashboard derive
summaries from those records.

Missing or malformed usage stays unknown. Persisted telemetry and
`factory status` keep the runtime-reported units unchanged.
When nano-AIU is available, the dashboard derives an AI usage value in USD.
It uses the GitHub conversion of one AI Credit to $0.01.
This value is not necessarily the invoice charge because included or pooled
credits can cover the cost. Premium-request cost remains a separate raw metric.
GitHub billing remains authoritative.

## Sensible practice

- Start on a scratch repository, not on production code.
- Set `repository.commands` first so that verification has real checks to run.
  Read [Configure a repository](../guides/configure-repository.md).
- Keep `pull_request.enabled: false` until you verify the output.
- Read `patch.diff` before you push anything.

## Next

- [Real pi runs](pi.md), the recommended runtime
- [Configure a repository](../guides/configure-repository.md)
- [Safety and trust boundaries](../reference/safety.md)
