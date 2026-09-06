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

Before triage, the controller profiles the prepared worktree without shell,
network or imports and persists `repository-profile.json`. This scan itself
does not call Copilot.

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
  configured Researcher (`GPT-5.6 Sol` by default) produces it from the
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
