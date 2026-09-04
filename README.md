# Local Software Agent Factory

A local-first autonomous software engineering factory using specialized AI agents.

The long-term goal is:

```text
Backlog
   ↓
prioritize
   ↓
refine
   ↓
research
   ↓
plan
   ↓
implement
   ↓
test
   ↓
independent review
   ↓
pull request
   ↓
CI
   ↓
repair
   ↓
staging
   ↓
deployment
   ↓
validation
```

The first implementation is intentionally much smaller.

## V1 goal

From the MacBook:

```bash
factory run \
  --repo ~/projects/example \
  --title "Reject empty customer names" \
  --description "Return HTTP 400 for empty or whitespace-only names."
```

The factory should eventually produce a flow like:

```text
TRIAGE
Complexity: L1
Risk: R1

REFINE
Acceptance criteria generated

PLAN
Implementation steps generated

IMPLEMENT
Local Git worktree modified

VERIFY
lint ✓
tests ✓
build ✓

REVIEW
independent review ✓

RESULT
READY FOR PR
```

Pull request creation, CI observation and backlog polling are implemented but
strictly opt-in. Nothing is merged automatically, ever.

## Local architecture

Everything except model inference initially runs locally:

```text
                MacBook

          Factory Controller
                 │
      ┌──────────┼──────────┐
      │          │          │
   workflow    policy    model router
      │
      ▼
   workspace
      │
  Git worktree
      │
local commands/tests
      │
      ▼
 GitHub Copilot
      │
┌─────┼──────────────┐
▼     ▼              ▼
Opus  Sonnet         MAI
│
▼
GPT-5.6 Sol
```

Later:

```text
Local Factory
     ↓
   GitHub
     ↓
GitHub Actions
```

## Architectural inspiration

The orchestration model is based heavily on OpenAI Symphony.

We reuse the concepts of:
- reconciliation
- polling
- claiming before dispatch
- per-task workspaces
- bounded concurrency
- explicit retries
- local recovery
- controller-owned scheduling

See `docs/symphony-alignment.md`.

## Why multiple agents?

Different parts of software engineering benefit from different model characteristics.

For example:

```text
Research
  GPT-5.6 Sol

Architecture / Planning
  Claude Opus 5

Normal implementation
  Claude Sonnet 5

Mechanical implementation
  MAI-Code-1.1-Flash

Independent review
  GPT-5.6 Sol
```

The routing should eventually be optimized using our own measured success/cost data.

## Getting started

Development target:

```text
Python 3.13+
uv
```

Install dependencies (including dev tools):

```bash
uv sync --group dev
```

Run the test suite:

```bash
uv run pytest
```

Run the linter:

```bash
uv run ruff check .
```

### Demo

`factory run` drives one work item through the whole pipeline. It defaults to
the deterministic fake agent runtime, so this makes no network or model calls:

```bash
uv run factory run \
  --repo ~/projects/example \
  --title "Reject empty customer names" \
  --description "Return HTTP 400 for empty or whitespace-only names." \
  --config config/factory.example.yaml
```

This drives the work item through
`CREATED → TRIAGING → REFINING → [RESEARCHING] → PLANNING → IMPLEMENTING →
VERIFYING → REVIEWING → PR_READY` (or `NEEDS_HUMAN`/`FAILED`), creates an
isolated Git worktree under the configured data directory, and prints the run
id, final state, workspace path and controller-derived changed files. A nonzero
exit code means the run did not finish successfully.

List and inspect persisted runs:

```bash
uv run factory runs --config config/factory.example.yaml
uv run factory show RUN_ID --config config/factory.example.yaml
```

Use `--data-dir` to point any command at an isolated directory instead of the
configured one, which is how the test suite exercises the CLI without touching
`~/.software-factory`.

### Choosing a runtime

```bash
uv run factory run ... --runtime fake      # default, deterministic, free
uv run factory run ... --runtime copilot   # real Copilot calls (costs money)
```

`--runtime fake` is the default everywhere precisely so no command can spend
money by accident. `--runtime copilot` runs the `copilot` CLI per role with
constrained tool permissions, a credential-scrubbed environment and a strict
typed-artifact contract; malformed output is an explicit agent failure.

### Backlog daemon

```bash
uv run factory start \
  --repo ~/projects/example \
  --github-repo acme/example \
  --config ~/my-factory.yaml
```

`factory start` polls GitHub Issues labelled `agent-ready`, reconciles persisted
runs before dispatching, and runs up to `scheduler.max_concurrent_tasks` (1 or
2) work items concurrently in isolated worktrees. It refuses to run — and never
contacts GitHub — unless `scheduler.enabled` is true in configuration. Use
`--once` for a single bounded tick.

## What is implemented

Phases 0-14 of `PLAN.md` are implemented and integrated:

- **Typed SDLC artifacts** — `WorkItem → TriageResult → Specification →
  [ResearchReport] → ExecutionPlan → ChangeSet → VerificationReport →
  TestReport → ReviewReport → CIReport`, each persisted as versioned JSON with
  per-attempt snapshots under `attempts/NN/`.
- **One authoritative controller** — only `WorkflowController` transitions a
  run, against a transition table declared as data.
- **Real and fake agent runtimes** — `CopilotAgentRuntime` and
  `FakeAgentRuntime` behind one `AgentRuntime` protocol.
- **Optional research** — run at most once, only when triage asks for it.
- **Deterministic governance** — repository `install`/`verify`/`build` commands
  run with an environment allowlist, bounded/redacted output and durable
  per-command logs. Broken required checks cannot reach the tester or reviewer.
- **Independent quality gates** — the tester and reviewer see the
  controller-derived diff, changed files and deterministic results, never the
  implementer's own summary. The reviewer uses a different model family from
  every worker (enforced by config validation).
- **Scope-drift governance** — deterministic detection of unexpected modules,
  excessive file counts, dependency/migration/CI/infrastructure changes, with
  bounded replans and a re-check at the PR boundary.
- **Bounded repair** — one persisted implementation budget shared by
  implementer, verification, review and scope failures, plus a separate CI
  repair budget. Every repair attempt receives a small, explicit `RepairContext`.
- **Pull requests** (opt-in) — controller-owned commit/push/PR creation with
  push safety guards. Never force-pushes, never merges.
- **CI observation and repair** (opt-in) — bounded polling, normalized evidence
  and code repair only for `CODE_FAILURE`/`TEST_FAILURE`; everything else
  escalates to `NEEDS_HUMAN` with evidence.
- **Local backlog daemon** (opt-in) — GitHub Issues polling, reconciliation,
  reservation before dispatch, duplicate protection and concurrency up to two.

### Not implemented (Phase 15, optional)

Jira, dashboards, Postgres, Temporal, remote workers, Docker/Kubernetes
sandboxes, staging, deployment and production monitoring are explicitly *not*
part of this build and are not required by anything in the codebase.

### Safety defaults

Every integration is disabled in the packaged configuration:

```yaml
pull_request:
  enabled: false
ci:
  enabled: false
scheduler:
  enabled: false
```

With those defaults, and the default `--runtime fake`, the factory performs no
network access and makes no paid model call. The test suite runs entirely
offline: it never calls a model and never reaches GitHub.

## Documentation

Read:
- `AGENTS.md`
- `docs/architecture.md`
- `docs/symphony-alignment.md`
- `docs/decisions.md`
- `PLAN.md`

`PLAN.md` defines the implementation order and records which phases are done.

Phases 0-14 are implemented. Phase 15 is optional and deliberately not built;
do not add any of it without a documented, demonstrated need.
