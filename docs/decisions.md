# Architecture Decisions

## ADR-001: Build one small executable vertical slice

Phase 1 combines the original fake-workflow and Git-worktree milestones.

Reason:
- a workflow without a repository boundary proves too little
- adding workspaces later would force the runtime and controller APIs to change
- one synchronous path is easier to understand and test

The slice keeps the intended stages, typed artifacts, deterministic routing,
bounded repair, filesystem persistence and independent fake review.

## ADR-002: Keep authority and evidence deterministic

Only `WorkflowController` changes run state.

Agents return typed outcomes but do not transition runs. The controller derives
changed files and `patch.diff` from Git, including newly created files.
Verification command results are also controller-produced evidence.

## ADR-003: Use one repair budget

Every implementation or repair entry appends an attempt record and consumes one
global maximum. Verification and review failures share this budget.

This prevents alternating gate failures from bypassing bounded retry policy.

## ADR-004: Defer scheduler architecture

Phase 1 is a synchronous manual command with concurrency one.

Polling, reconciliation, tracker adapters, retry timers, activity heartbeats and
multi-task scheduling are deferred until `factory start`. A per-work-item
exclusive lock and subprocess timeouts provide the necessary local safety now.

## ADR-005: Treat Symphony as coordination inspiration

The project follows Symphony's control-loop and workspace principles, but is not
a conforming implementation. Copilot execution, finite persisted repair
budgets, typed SDLC artifacts, controller-owned Git/PR behavior and independent
quality gates are deliberate extensions.

## ADR-006: `PR_READY` is a completed endpoint, not a terminal state

Terminal states are `DONE`, `NEEDS_HUMAN` and `FAILED`.

`PR_READY` stays reachable for pull-request-enabled runs (it transitions to
`PR_CREATED`), so it cannot be terminal. But when `pull_request.enabled` is
false it *is* where the manual flow legitimately ends.

The controller therefore finalizes it explicitly (`finalize_pr_ready`) by
stamping `completed_at`, and `workflow.is_run_finished` is the single predicate
that distinguishes "the manual flow completed here" from "a PR-enabled run was
interrupted at the publishing boundary". The scheduler uses that predicate
rather than comparing states directly.

Transitions also clear a stale `completed_at`/`failure_reason` whenever a run
becomes active again, so a repaired run never carries a completion timestamp
from an earlier cycle.

## ADR-007: Two separate, persisted retry budgets

`AttemptBudget.IMPLEMENTATION` covers the whole pre-PR loop: implementer
failures, deterministic verification failures, reviewer rejections and
scope-drift replans all consume `retries.max_total_attempts`.

`AttemptBudget.CI_REPAIR` is a separate budget bounded by `ci.repair_attempts`.
It also hard-caps how many times a PR may be updated, so a CI loop cannot push
forever.

Both attempt numbers are derived from persisted `FactoryRun.attempt_records`,
never from a local counter. A restarted process therefore cannot widen a budget.
Scope replans are bounded independently by `scope_drift.max_replans`, counted
from persisted records whose `triggered_by` is `SCOPE`.

## ADR-008: Lock contention is not a persisted failure

If another live run already owns a work item's workspace, `WorkflowController.run`
returns a non-persisted `FAILED` outcome explaining that the work item is
already active, and writes nothing to the run store.

Persisting a junk `FAILED` run would pollute the store, count against nothing,
and later force reconciliation to explain a run that never did any work. Since
no workspace is prepared and no artifact is written, there is nothing to
corrupt or recover.

## ADR-009: Research runs; it does not escalate

Phase 1 escalated `needs_research=true` to `NEEDS_HUMAN` because no researcher
existed. The researcher now runs exactly once per run, its `ResearchReport` is
persisted, it is handed to the planner, and the run continues. Research is never
re-run, so a task cannot repeatedly pay for it.

## ADR-010: The independent tester returns a `TestReport`

Earlier wiring mapped the tester role onto `VerificationReport`. That conflated
a model's judgement with deterministic, factory-produced evidence, which
directly contradicts "a model does not approve its own work".

The tester now returns `TestReport` (advisory), while `VerificationReport`
remains exclusively controller-produced. Tester and reviewer receive the
authoritative diff, the controller-derived changed-file list and the
deterministic report; neither ever receives the implementer's `ChangeSet`
summary.

## ADR-011: Conservative scheduler recovery

A persisted, non-terminal run found at startup is escalated to `NEEDS_HUMAN`
through `WorkflowController.recover_abandoned_run` rather than auto-resumed.

Auto-resuming would spend a paid attempt on a run whose true state (workspace
contents, partially applied edits, an already-pushed commit) cannot be
established cheaply. Escalating preserves every artifact and the workspace,
consumes no budget, and leaves a human in control. The scheduler itself still
never mutates run state.

## ADR-012: Worktree administration is serialized per source repository

`git worktree add` and `git worktree prune` both rewrite repository-global
administrative metadata. With `scheduler.max_concurrent_tasks = 2` two runs can
prepare workspaces against the same repository simultaneously, so the whole
`prepare()` sequence is held under a per-source-repo `flock`. Per-work-item
workspace locks remain separate and are what prevent duplicate active work.

## ADR-013: Tracked work is dispatched at most once

The generic `Scheduler` prevents *concurrent* duplicates and otherwise assumes
a tracker withdraws an item once work starts. GitHub Issues do not: an issue
stays open and keeps its `agent-ready` label after a run finishes, and this
factory deliberately holds no write access to the backlog.

Without an additional rule, the tick after a run reached
`DONE`/`NEEDS_HUMAN`/`FAILED` would dispatch the same issue again under a new
`FactoryRun` with an empty `attempt_records` list — an unbounded loop of paid
work that mints a fresh retry budget every cycle, defeating ADR-003/ADR-007.

`service.AlreadyRunFilter` therefore makes any tracker item with a persisted
`FactoryRun` (finished or not) ineligible. Re-running is an explicit operator
action: archive or remove the previous run, or invoke
`factory run --work-item-id` by hand. This keeps the rule durable across
restarts without adding GitHub write permissions or a database.

## ADR-014: Phase 15 is opened selectively, not as a whole

Phase 15 was a single "later integrations" bucket. That made it impossible to
say yes to delivery work without appearing to say yes to Temporal, Postgres,
Kubernetes, Jira and autonomous deployment.

Phase 15 is therefore split into numbered sub-phases with independent statuses.
Exactly five are open: 15.0 factory CI, 15.1 tag-driven release, 15.2 macOS
packaging and the launchd service, 15.5 local monitoring/health and 15.11 the
read-only dashboard.

Every other sub-phase — staging (15.3), deployment (15.4), Docker (15.6),
remote workers (15.7), Postgres (15.8), Temporal (15.9), Jira (15.10),
Kubernetes (15.12) — stays deferred. Nothing in the open sub-phases may depend
on a deferred one, and no deferred item is unblocked by proximity.

The selection is operational, not architectural: it makes the existing factory
installable, observable and inspectable on one MacBook. It does not widen what
the factory is allowed to do autonomously.

## ADR-015: CD means publishing release artifacts, never deploying

"Continuous delivery" in this project stops at a published GitHub Release. A
version tag builds artifacts and attaches them. Nothing installs, restarts,
promotes or self-updates, and there is no mutable pointer a client follows
automatically. Autonomous deployment stays banned by `AGENTS.md`.

Releases are intended to be treated as write-once, but that is a workflow
convention, not a platform guarantee. The release workflow checks whether the
tag's release already exists and refuses to replace its artifacts. It does not
stop someone editing or deleting a release through the GitHub UI or API.

GitHub's own release immutability is a repository setting and is off by default;
the current releases report `immutable=false`. Enable immutable releases in the
repository settings before relying on platform enforcement. Until then,
`SHA256SUMS` and `build-info.json` are what let a consumer detect a swapped
artifact.

Two native macOS builds are produced — arm64 on `macos-15` and x86_64 on
`macos-15-intel` — as separate PyInstaller `onedir` archives. `universal2` is
rejected: it requires universal wheels for every native dependency, produces a
larger artifact for a single-user tool, and turns one architecture's packaging
problem into a total build failure. Building each slice natively on its own
runner keeps failures isolated and diagnosable.

The release also contains a wheel and an sdist for people who already have
Python, plus `SHA256SUMS` and a `build-info.json` recording tag, commit,
runner image, Python version, PyInstaller version and architecture, so any
downloaded artifact is traceable to a build.

Artifacts are unsigned or ad-hoc signed. Apple Developer ID signing and
notarization are explicitly deferred: they need a paid account and secrets in
CI, and neither is justified for a local-first single-user tool yet. The
consequence is that Gatekeeper will quarantine a downloaded archive, so release
notes must say so plainly and document the manual step. Silence here would look
like a broken build.

A frozen artifact is not self-sufficient. It bundles Python and the factory,
but `git` must exist on `PATH`; `gh` is required only when a GitHub-touching
feature is enabled — pull requests, CI observation, or the backlog daemon,
which polls GitHub Issues through `gh` — and `copilot` only for `--runtime
copilot`. Preflight therefore validates prerequisites for *enabled* features,
so the default offline run does not demand tools it will never call.

## ADR-016: The local dashboard is a bounded exception to the V1 ban

`AGENTS.md` bans a web dashboard in V1. One narrow exception is now explicitly
requested and granted, because inspecting runs, states, attempts and metrics by
reading JSON under `~/.software-factory` is genuinely worse than a page.

The exception holds only within these boundaries:
- loopback bind, explicit start command, disabled by default
- read-only: `GET` only, no route mutates runs, workspaces or configuration
- token protected, token generated per start and never logged
- Python standard library only — no framework, no npm, no bundler, no build
- no command logs and no diffs rendered, because those are the two places where
  repository content and near-secrets would leak into a browser. Minimization
  is applied twice and independently: the detail view is built from an
  allowlisted typed model (no `failure_reason`, no agent reasoning, no raw
  artifact), and the request handler allowlists again before responding

The ban itself is unchanged for everything else. This is a local viewer, not a
control plane: it cannot approve a run, cannot retry one, cannot enable an
integration and has no multi-user concept. If a change would need a write path,
a framework or a non-loopback listener, that is a new ADR, not a refactor.

## ADR-017: Health and metrics are derived, never accumulated

Persisted run artifacts remain the single source of truth. Health and metrics
are pure functions over the run store, computed on demand.

No counter store, no time-series database and no separate metrics file is
introduced. A derived view cannot drift from the runs it describes, can be
recomputed after any crash, and is trivially testable against a fixture store.
Health and metrics are strictly read-only: they never repair a lock, prune a
worktree or transition a run — they report those as findings for an operator.

Cost is deliberately not fabricated. Token usage and cost appear only when the
runtime actually reported them; otherwise the value is unknown, never zero and
never inferred from a hard-coded price table. A confidently wrong spend number
is worse than no number. In practice no runtime reports usage today and
`AttemptRecord` persists none, so no usage or cost figure is reported at all;
reporting one starts by adding a typed field to `AttemptRecord`.

Monitoring stays local: structured JSON logs bounded in size inside the data
directory, with the same credential redaction already applied to command
output. No exporter, no cloud backend, no telemetry leaves the machine.

## ADR-018: The launchd service is an opt-in user agent

Running `factory start` continuously is a `launchd` job, but a deliberately
timid one.

It is a per-user `LaunchAgent` under `~/Library/LaunchAgents`, installed only
by an explicit CLI command. It is never a root `LaunchDaemon`, never installed
by extracting an archive, and never installed as a side effect of running the
factory. A background process that can spend money and push branches must be an
explicit, reversible act.

The installed job defaults to `--runtime fake`, so an accidentally loaded agent
costs nothing until someone deliberately changes it. Because launchd gives
agents a minimal environment, the installer captures an explicit `PATH`
snapshot; otherwise the service would fail to find `git`, `gh` or `copilot` in
a way that looks like a factory bug. Install also refuses unless the given
configuration enables the scheduler and `factory doctor` is clean, because a
service that cannot work is worse than no service.

Logging goes to the factory's own bounded, rotating structured log under the
configured data directory; launchd's stdout/stderr are pointed at `/dev/null`
precisely because launchd never rotates what it captures. `KeepAlive` is
`Crashed`-only, so no exit code — including the configuration-error code 2 —
can create a restart loop. Uninstall unloads the agent and removes the plist
while leaving runs and workspaces untouched.

## ADR-019: Repository capabilities are deterministic profiling plus on-demand skill research

*Supersedes the original ADR-019, which selected advisory skills from a fixed,
versioned built-in catalog. That catalog is removed.*

Repository awareness is a controller-owned scan, not an agent discovery step.
After the worktree is prepared and before `TRIAGING`, the factory walks
repository-local paths and reads a small allowlist of bounded manifests. It
does not run a shell command, import target code, contact the network or
trust repository-provided instructions.

The resulting versioned `RepositoryProfile` is persisted as
`repository-profile.json`. It records technologies, test tools, package
managers, markers, warnings, `version_files`, and exact dependency evidence:
direct declarations with ecosystem, name, declared version, an optional exact
resolved version and resolution path, manifest path and dependency group.
Declarations are parsed from `pyproject.toml` (PEP 621 dependency tables,
`dependency-groups`, `requires-python` as the `python` runtime target, and the
Poetry dependency/dev-dependency/group tables), from
`requirements.txt`/`requirements-*.txt` (which also mark the `pip` package
manager) and from `package.json` (runtime, dev, peer, optional dependencies and
`packageManager`); `setup.cfg` and `tox.ini` contribute Python and pytest
evidence only. Exact versions are resolved from `uv.lock`, `package-lock.json`
and `pnpm-lock.yaml` when unambiguous, and an ambiguous resolution records a
warning rather than a version. `poetry.lock`, `yarn.lock`, `bun.lock`/
`bun.lockb`, `Pipfile.lock` and `pylock.toml` identify their package manager
where applicable and are fingerprinted as `version_files` without claiming
exact graph parsing.

The profile carries two distinct SHA-256 fingerprints. `dependency_fingerprint`
is semantic: it digests technologies, test tools, package managers and the
normalized dependency declarations, and it is the identity that binds a
generated skill. `manifest_fingerprint` is provenance: it digests the content
of the version files, so reformatting a manifest changes it without
invalidating guidance that is still correct.

There is no fixed skill catalog. When `polish.enabled` and the bounded polish
attempt is eligible, after the first successful deterministic verification the
controller re-profiles the post-implementation worktree (capturing any
dependency upgrades the task made), transitions through a temporary
`RESEARCHING` state, and invokes the configured Researcher (`GPT-5.6 Sol` by
default) with purpose `GENERATE_REPOSITORY_SKILL`, at most once per run.

That call is bounded and web-only. It runs in the run's own persistence
directory rather than the worktree, has `web_fetch` as its only tool, runs
without repository custom instructions, and sees only the normalized profile,
the controller-derived changed file paths, the configured URL lists and the
factory-owned generation rules — never source code, README content, task prose
or the diff. `polish.official_documentation_origins` (official documentation,
migration guides, release notes) is authoritative for every version claim. The
exact curated `polish.practice_reference_urls` — by default reviewed
general-practice notes from `bdfinst/agentic-dev-team`, pinned to commit
`52cc5efd1c445e71c55b956837c003911346d7e7` so the fetched text cannot change
under us — may contribute generic quality heuristics only, synthesized rather
than copied, and never version claims, commands, tools or orchestration. Fetched pages are untrusted data.

It returns one typed `RepositorySkill` (`repository-skill.json`) carrying the
profile's `dependency_fingerprint`, bounded targets, HTTPS official and
practice sources that each declare the detected dependencies they ground,
separate simplify and polish guidance, and uncertainties. The type itself
refuses a skill with neither an official source nor an explicit uncertainty,
and refuses an official source that claims only generic applicability.

The controller then validates deterministically and rejects a fingerprint
mismatch, a target that is not an exact profiled dependency declaration,
evidence paths outside the profile, a missing target or missing per-dependency
official provenance for a detected `python`, `pytest`, `react`, `react-dom`,
`vite` or `vitest` dependency, a source that claims applicability to an
undetected dependency, or a source outside the configured lists.

Rejection is a safe skip, not a failure. The run is already green when polish
is considered, so a failed re-profile, failed research, rejected skill or a
skill that becomes stale (the `dependency_fingerprint` changed after
generation) records the reason as a profile warning and skips or disables
polish, leaving the verified change to proceed to testing and review. The skill
reaches only the polish Implementer, Tester and Reviewer, never before the
initial green baseline, and is regenerated fresh for every eligible run — there
is no cross-run cache or plugin system. The context is advisory: it changes no
tools, models, workflow states, quality gates, commands, permissions or
routing.

## ADR-020: Post-green polish is one bounded implementation attempt, informed by on-demand skill research

When `polish.enabled` is true and the bounded polish attempt is eligible, the
first successful deterministic verification schedules the version-specific
skill research described in ADR-019, then exactly one more `IMPLEMENTER` pass
with `AttemptTrigger.POLISH` that applies the returned `RepositorySkill`:
simplify first, preserving tests, behavior, public interfaces, security and
error handling, then version-specific polish second, both inside that single
existing attempt. It uses the existing worker routing and implementation
budget, may correctly make no edits, and is always followed by the full
deterministic verification and scope assessment again before testing or review.

Polish never runs during CI repair and is scheduled only when one later
implementation attempt remains available to recover from a regression. It
introduces no `POLISHING` state and no `POLISHER` role; the temporary
`VERIFYING → RESEARCHING → IMPLEMENTING → VERIFYING` sequence remains
authoritative and visible in persisted attempt records. The generated skill is
provided only to that attempt's Implementer, Tester and Reviewer and is
regenerated fresh for every eligible run.

Because polish is an improvement on an already-verified change, its failure
modes are non-fatal by design. An unverifiable or stale skill is never silently
reused: it is discarded with a recorded warning and the run continues on its
existing green path rather than failing or escalating.

The configuration model defaults omitted legacy `polish` sections to disabled
for compatibility. The packaged default and example enable it, so their normal
fake run records an initial implementation attempt and one polish attempt.
