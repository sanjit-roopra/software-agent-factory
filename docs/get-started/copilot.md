# Real Copilot runs

`--runtime copilot` replaces the fake runtime with real model calls through the
GitHub Copilot CLI.

!!! danger "This costs money"

    Every stage of a run is a separate Copilot invocation: triage, refiner,
    optional researcher, planner, implementer, tester and reviewer — plus one
    more per repair attempt. With the packaged configuration, the enabled
    post-green polish adds a second Implementer invocation. A single run is
    several model calls. There is no spend estimate and no dry-run preview of
    cost.

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

Use `--model-profile economy` to select the packaged lower-cost routing table.
The option is also available on `project`, `start`, `doctor`, `skill refresh`
and `service install`.

Before triage, the controller profiles the prepared worktree without shell,
network or imports and persists `repository-profile.json`. This scan itself
does not call Copilot.

## Which model runs which stage

Model choice is configuration, not code. The packaged defaults:

| Role | Model | Reasoning | Context |
| --- | --- | --- | --- |
| Triage | `gpt-5.6-terra` | medium | default |
| Refiner | `gpt-5.5` | high | default |
| Researcher | `claude-opus-5` | high | default |
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

## Repository skills

There is no fixed, built-in skill catalog. The deterministic profile records
technologies, test tools, package managers (`uv`, `pip`, `poetry`, `npm`,
`pnpm`, `yarn`, `bun`), version files, exact dependency declarations from
`pyproject.toml`, `requirements*.txt` and `package.json`, and two fingerprints
— nothing more.

Guidance for the bounded polish attempt comes from two files kept under the
factory's data directory, in repository-scoped storage keyed by the repository
and its `dependency_fingerprint`. Nothing is written into your repository, and
the factory never loads guidance from it.

- The **generated skill** describes the repository, not the task. The
  configured Researcher (`Claude Opus 5` in the default profile) produces it from the
  normalized profile and the configured source lists only — no changed
  filenames, source code, README content, task prose or diff — with web access
  limited to `polish.official_documentation_origins` (authoritative for version
  claims) and the exact, commit-pinned `polish.practice_reference_urls`
  (generic heuristics only). Later runs reuse it; a paid research call happens
  only when the current dependency fingerprint has no generated skill yet.
- The **overlay** is yours: a repository-level `repository-skill-overlay.yaml`
  holding house rules as prose. The factory never creates, rewrites or deletes
  it, and it survives dependency changes.

Both reach only that attempt's Implementer, Tester and Reviewer — never before
the initial green baseline — and both are advisory prompt context. They do not
grant tools, change model routing, add commands, alter workflow states, spend
retry budget or waive gates, and the target repository cannot provide plugins.

If the research, its validation, or the profile check fails, the factory
records a warning and skips polish. Your already-verified change still ships.

`factory skill path`, `factory skill validate` and `factory skill refresh`
manage this explicitly; see
[Repository skills and overlays](../guides/repository-skills.md).

## What the agent is allowed to do

Each role gets a permission profile:

- **Read-only roles** (triage, refiner, researcher, planner, tester, reviewer):
  `glob`, `grep`, `view`.
- **Implementer:** `glob`, `grep`, `view`, `create`, `edit`, `bash`.

The one exception is the Researcher's skill-generation call, made only when the
repository's current dependency fingerprint has no generated guidance: it gets
only `web_fetch`, restricted to `polish.official_documentation_origins` and
`polish.practice_reference_urls`, with no `glob`/`grep`/`view`/edit access, no
repository custom instructions, and the run directory rather than the worktree
as its working directory.

The optional polish uses the same Implementer permission profile and worker
routing. It introduces no separate role.

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

The runtime passes `--context` and `--usage-output-file` explicitly to Copilot.
Run and project artifacts persist reported input, output, reasoning and cache
tokens, timing, nano-AIU and premium-request cost. `factory status` and the
local dashboard derive summaries from those records.

Missing or malformed usage stays unknown. The factory does not convert raw
premium-request cost or nano-AIU to AI Credits or USD, so GitHub billing
remains authoritative for spend.

## Sensible practice

- Start on a scratch repository, not on something you care about.
- Set `repository.commands` first so verification has real checks to run.
  See [Configure a repository](../guides/configure-repository.md).
- Keep `pull_request.enabled: false` until you trust the output.
- Read `patch.diff` before you push anything.

## Next

- [Configure a repository](../guides/configure-repository.md)
- [Safety and trust boundaries](../reference/safety.md)
