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

### External prerequisites

The factory bundles no toolchain. On `PATH` it needs:

```text
required always      git
required if enabled  gh        (pull_request.enabled / ci.enabled / scheduler.enabled)
required if chosen   copilot   (--runtime copilot)
```

`factory run` and `factory start` refuse to start with an explicit message and
exit code `2` when a tool they actually need is missing — never a traceback.
`factory doctor` explains every requirement for your configuration.

### Install from source

```bash
git clone https://github.com/<owner>/software-agent-factory.git
cd software-agent-factory
uv sync --locked --group dev
uv run --no-sync ruff format --check .
uv run --no-sync ruff check .
uv run --no-sync mypy src/software_agent_factory scripts/release
uv run --no-sync pytest -q --cov=software_agent_factory --cov-branch
uv run --no-sync pip-audit --skip-editable
uv build --no-sources
uv run --no-sync twine check dist/*
uv run --no-sync check-wheel-contents dist/*.whl
uv run factory --version
```

These are the same deterministic gates used by CI. Pull requests run formatting,
linting, strict type checking, Python 3.13/3.14 tests with a 90% branch-coverage
floor, package validation, locked dependency auditing, and CodeQL. Scheduled workflows
audit the complete locked environment and test the next Python prerelease.
Dependabot maintains both uv dependencies and pinned GitHub Actions, grouping
minor and patch updates while leaving major upgrades in separate pull requests.
Because this private repository does not have GitHub Advanced Security,
pull-request dependency review uses the same local locked-environment audit,
and CodeQL findings are enforced directly from generated SARIF. SARIF is
retained as a workflow artifact instead of uploaded to GitHub code scanning.

GitHub secret scanning with push protection and immutable releases are
repository settings rather than workflow files; enable them in the repository
security and release settings.

Every command below can be run either as `uv run factory ...` from a source
checkout or as `factory ...` from an installed wheel or an extracted macOS
archive.

### Install a released macOS archive

Releases are built by tag and attached to a GitHub Release: one native
`arm64` archive, one native `x86_64` archive (no `universal2`), a wheel, an
sdist, `SHA256SUMS` and `build-info.json`.

```bash
# 1. download the archive for your architecture plus SHA256SUMS, then verify
shasum -a 256 -c SHA256SUMS --ignore-missing

# 2. extract and move it somewhere permanent (not Downloads, not /tmp)
tar -xzf software-agent-factory-<version>-macos-arm64.tar.gz
mkdir -p ~/.local/opt
mv software-agent-factory ~/.local/opt/software-agent-factory

# 3. clear the Gatekeeper quarantine flag (see below), then run it
xattr -dr com.apple.quarantine ~/.local/opt/software-agent-factory
~/.local/opt/software-agent-factory/factory --version
~/.local/opt/software-agent-factory/factory doctor
```

Release artifacts are **unsigned or ad-hoc signed**. Apple Developer ID
signing and notarization are deferred, so macOS Gatekeeper quarantines a
downloaded archive and refuses to run it until the quarantine attribute is
removed with the `xattr` command above. Every archive ships an `INSTALL.txt`
repeating these steps.

Extracting an archive installs nothing, starts nothing and changes no system
state.

Alternatively, if you already have Python 3.13:

```bash
pip install software_agent_factory-<version>-py3-none-any.whl
factory --version
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

Two configured bounds apply: `scheduler.max_concurrent_tasks` limits how much
runs at once, and `scheduler.max_runs_per_day` (default 20, `null` to disable)
limits how much may be *claimed* per UTC calendar day. Both are reported at
startup, and a tick stopped by the daily ceiling says so instead of looking
like an empty backlog.

### Health check

```bash
uv run factory doctor
uv run factory doctor --json --config ~/my-factory.yaml
```

`doctor` reports the platform, whether this is a frozen or source build,
`launchctl`, `git`, configuration validity, the executables behind your
configured repository commands, and the writability of the data directory.
`gh` is required only when `pull_request.enabled`, `ci.enabled` or
`scheduler.enabled` is set; `copilot` only with `--runtime copilot`. It never
makes a paid model call — the only `copilot` interaction is a bounded
`copilot --version` probe. It exits nonzero if any check errored; warnings
alone do not fail it.

### Monitoring

```bash
uv run factory status
uv run factory status --json --limit 50 --offset 50
```

`status` is strictly read-only: it derives run counts, attempt tallies,
first-pass success, durations and operational health (stale runs, stale
workspace locks, orphaned workspaces) from persisted artifacts, and creates or
modifies nothing — not even the data directory. Staleness defaults to
`scheduler.stall_timeout_seconds`; override it with `--stale-after-seconds`.
`--max-scanned-runs` bounds the work one call may do, and a truncated or
partially unreadable scan is reported as `DEGRADED` rather than presented as a
complete picture.

Structured JSON logs are written by `run`, `start` and `dashboard` to
`<data_dir>/logs/factory.log`, rotated and size-bounded, with credentials
redacted. Nothing is exported anywhere.

### Read-only dashboard

```bash
uv run factory dashboard                 # http://127.0.0.1:8765/?token=...
uv run factory dashboard --port 0 --open-browser
```

`dashboard` blocks in the foreground and is the only thing that ever starts a
server: no other command opens a socket. It binds `127.0.0.1` and nothing
else, answers `GET` only, and requires a token generated for that process,
printed once as part of the URL and never written to a log. Pages show the run
list, run detail, workflow state, attempt history and the derived metrics —
never command logs, diffs, prompts or raw artifacts. Ctrl-C stops it.

It is built from the Python standard library: no framework, no npm, no
bundler, no build step.

### Background service (macOS, opt-in)

```bash
uv run factory service install \
  --repo ~/projects/example \
  --github-repo acme/example \
  --config ~/my-factory.yaml
uv run factory service status --json
uv run factory service uninstall
```

`service install` writes exactly one per-user LaunchAgent under
`~/Library/LaunchAgents` and nothing under `/Library`. It is macOS-only,
refuses unless the given configuration sets `scheduler.enabled`, refuses while
`factory doctor` reports any error, captures a `PATH` snapshot (launchd agents
get a minimal environment) and defaults to `--runtime fake`, so an
installed-but-forgotten agent cannot spend money. Use `--runtime copilot` to
opt in deliberately, `--executable` to point at a specific build, and
`--allow-source-dev` to install from a source checkout.

The fake runtime is a real dry run, not a preview: it persists completed runs,
and the scheduler will not automatically dispatch those same backlog items
again. Select `--runtime copilot` before polling real `agent-ready` issues, or
use a separate data directory for fake-runtime service testing.

A service is *never* installed as a side effect of extracting an archive,
running the factory or upgrading it. `service uninstall` unloads the agent and
removes the plist, leaving every run and workspace on disk.

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

### Phase 15: delivery and operations (selected sub-phases)

The requested Phase 15 sub-phases are implemented; the rest stay deferred:

- **15.0 Factory CI** — this repository's own lint and tests in GitHub Actions,
  plus native `macos-15` (arm64) and `macos-15-intel` (x86_64) packaging jobs,
  with no secrets and no paid model calls.
- **15.1 Tag-driven release** — a `v*` tag publishes an immutable GitHub
  Release: separate arm64 and x86_64 PyInstaller archives, a wheel, an sdist,
  `SHA256SUMS` and `build-info.json`. Continuous *delivery*, not deployment:
  nothing installs, updates or promotes itself, and re-running a tag refuses to
  replace published artifacts.
- **15.2 macOS packaging and a launchd service** — runnable archives that still
  require an external `git` (and `gh`/`copilot` only for enabled features), plus
  the opt-in, manually installed per-user LaunchAgent described above.
- **15.5 Local monitoring and health** — `factory doctor`, `factory status` and
  bounded structured logs under the data directory. No cloud observability, and
  no invented cost figures: token usage and cost are reported only if a runtime
  actually returns them, which none does today.
- **15.11 Read-only local dashboard** — the loopback-only, token-protected,
  `GET`-only viewer described above.

Release artifacts are unsigned or ad-hoc signed: Apple Developer ID signing and
notarization are deferred, so macOS Gatekeeper quarantines a downloaded archive
until it is cleared manually.

### Deferred (rest of Phase 15)

Staging, deployment/promotion, Jira, Postgres, Temporal, remote workers,
Docker/Kubernetes sandboxes and any hosted or multi-user service remain
explicitly *not* part of this build, and nothing in the codebase requires them.

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
network access and makes no paid model call. The dashboard is off unless
`factory dashboard` is running, and no launchd service exists unless someone
ran `factory service install`. The test suite runs entirely offline: it never
calls a model and never reaches GitHub.

## Documentation

Read:
- `AGENTS.md`
- `docs/architecture.md`
- `docs/symphony-alignment.md`
- `docs/decisions.md`
- `PLAN.md`

`PLAN.md` defines the implementation order and records which phases are done.

Phases 0-14 are implemented. Phase 15 is split into sub-phases with individual
statuses: 15.0, 15.1, 15.2, 15.5 and 15.11 are implemented; everything else,
including staging and deployment, stays deferred and must not be added without
a documented, demonstrated need.
