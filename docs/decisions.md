# Architecture Decisions

## ADR-023: Enforce concise controlled writing

All factory-authored prose uses one controller-owned writing policy. This
includes agent artifacts, agent prompts, generated issues, pull requests and
commit messages.

The factory includes a reviewed subset of the SimpleEnglish v2.0.2 linter at
revision `61ee200efbd423050aab982eed94226229891ae0`. The MIT license and source
notice ship with every package. The factory uses only local deterministic
checks. It does not run upstream plugins, hooks or benchmark tools.

The runtime policy checks sentence length, field word limits, filler terms,
semicolons, em dashes, and Latin abbreviations. It does not ban uncertainty
words such as `may` or `might`. Review and research must keep calibrated
uncertainty.

Repository documentation uses the pinned SimpleEnglish skill in Strict mode.
A local gate checks `README.md` and every Markdown file in `docs/`.
The gate also checks contractions, modal words, selected perfect tenses, and
selected comma-plus-`-ing` clauses. It applies a 20-word limit to procedural sentences.
It applies a 25-word limit to descriptive sentences.

CI, Pages deployment, and release validation run the documentation gate.
`AGENTS.md` requires future documentation changes to use the same skill and gate.

The controller rejects invalid model prose and gives one bounded correction
prompt. It never silently rewrites an artifact. Publication text fails before
Git or GitHub mutation.

Human input, code, identifiers, paths, commands, URLs, quoted errors and raw
command output remain exact. Generated pull request text uses the refined
specification instead of repeating the original work item description.

These checks apply ASD-STE100 principles. They cannot validate the full
standard or its controlled dictionary, so the factory does not claim formal
compliance.

## ADR-022: Opt-in autonomous project delivery

The explicitly requested delivery boundary is reviewed, CI-green code merged
into the configured target branch, not a local integration branch or an open
PR. This supersedes the original blanket deferral of autonomous merging only
for the opt-in controller-owned delivery path.

The existing `WorkflowController` still owns every child run, local quality
gate, independent review, PR publication and bounded CI repair. A controller
merge adapter verifies repository and target allowlists. It checks the
reviewed head revision, required checks, and merge eligibility before
requesting a merge. It never bypasses branch protection, uses administrator
privileges, force-pushes or deploys. Completion requires persisted evidence
that the PR actually merged.

Branch push gets one bounded retry for transient Git transport or remote
backend failures. Before retrying, the controller reads the exact target
branch tip and accepts a lost response only when that tip is the expected
commit. Authentication, authorization, policy and non-fast-forward failures
are not retried.

PR creation separately gets one bounded retry for transient GitHub transport
failures. Before retrying, the controller searches for the exact repository,
head, base and run marker. This recovers a PR that GitHub created before the
response was lost and prevents duplicate publication.

Every PR revision (such as each CI repair) must pass the configured
independent Reviewer or the controller's bounded review-acceptance policy. A
controller acceptance is a separate typed artifact, never a rewritten Reviewer
approval. It is limited to configured low-risk findings, bound to the exact
reviewed tree, and disclosed in the PR. When opt-in automatic merge is enabled,
an eligible accepted-with-findings revision can merge after required CI passes.
This path does not wait for a human to read the disclosure. The controller
refuses to publish or merge a different head. Security, scope and
repair-regression findings cannot be accepted. This model review does not
impersonate a GitHub user review or bypass repository rules requiring
additional human approvals.

Planner, Tester and Reviewer schema failures get bounded same-model correction
with the exact validation error. Tester and Reviewer correction calls do not
spend implementation attempts. Reviewer repair attempts receive the current
work item and earlier blocking findings, and deterministic verification must
leave the Git tree unchanged before independent review starts.

Remote project delivery executes tasks serially against the freshly fetched
target branch. This is the smallest sufficient way to make sure that every task
includes its merged predecessors and avoids inventing a merge-queue scheduler.
Local-only project execution retains its existing bounded wave concurrency.

Project recovery reconciles immutable plans, persisted child run identifiers,
Git worktrees and GitHub delivery evidence before dispatch. It reuses delivery
checkpoints and retry budgets rather than creating duplicate PRs or resetting
attempts. Ambiguous in-flight implementation or conflicting workspace state
stops with a recorded reason instead of guessing or discarding changes.

Human configuration can authorize exact repository-relative dependency and CI
files, but only when those same files are explicitly named in the task plan.
Protected files, risk approval, scope limits, verification and independent
review remain authoritative. No agent can grant itself an exemption.

Review approval is bound to an immutable Git tree. Publication verifies both
the staged tree and the committed tree before pushing. Delivery starts from
the exact fetched target, not an ahead local checkout, and the original
repository/host identity remains fixed throughout the run.

Tree approval alone does not authorize intermediate history. The controller
creates a commit from the approved tree and allowed parent. It persists the
commit receipt, advances the branch, and pushes that SHA. Recovery can publish
only the recorded commit, never an arbitrary current `HEAD`. Implementers are
also denied direct `git commit` access as defense in depth.

Configured required checks must also be enforced server-side by the target's
active protection policy, so a rerun cannot race the final merge. The merge
adapter uses a synchronous expected-head merge API, not a CLI operation that
can silently enable auto-merge or enqueue work. Unsupported queues and
unenforceable policies fail closed before mutation.

Classic PR bypass allowances for users, teams and apps must all be explicitly
empty. A PR still reporting `REVIEW_REQUIRED` cannot merge, even when the
factory credential has authority to exercise a repository bypass.

All new capabilities are disabled by default. Merging implementation code
does not authorize running a migration, production credential access, or
software deployment.

## ADR-001: Build one small executable vertical slice

Phase 1 combines the original fake-workflow and Git-worktree milestones.

Reason:
- a workflow without a repository boundary proves too little
- adding workspaces later forces the runtime and controller APIs to change
- one synchronous path is easier to understand and test

The slice keeps the intended stages, typed artifacts, deterministic routing,
bounded repair, filesystem persistence and independent fake review.

## ADR-002: Keep authority and evidence deterministic

Only `WorkflowController` changes run state.

Agents return typed outcomes but do not transition runs. The controller derives
changed files and `patch.diff` from Git. This includes newly created files.
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

The controller therefore finalizes it explicitly with `finalize_pr_ready`.
This operation stamps `completed_at`.
`workflow.is_run_finished` is the single predicate that distinguishes the two
completion conditions. The scheduler uses that predicate instead of comparing
states directly.

Transitions also clear a stale `completed_at`/`failure_reason` whenever a run
becomes active again, so a repaired run never carries a completion timestamp
from an earlier cycle.

## ADR-007: Two separate, persisted retry budgets

`AttemptBudget.IMPLEMENTATION` covers worktree-editing attempts: implementer
failures, deterministic verification failures and reviewer rejections consume
`retries.max_total_attempts`.

`AttemptBudget.CI_REPAIR` is a separate budget bounded by `ci.repair_attempts`.
It also hard-caps how many times a PR can be updated, so a CI loop cannot push
forever.

Both implementation attempt numbers are derived from persisted
`FactoryRun.attempt_records`, never from a local counter. A restarted process
therefore cannot widen a budget. A scope replan after successful deterministic
verification updates only the `ExecutionPlan`, re-assesses the existing green
diff, and proceeds without rerunning the Implementer. These metadata-only
replans are bounded independently by `scope_drift.max_replans` and persisted in
`FactoryRun.scope_replans`. Legacy scope-triggered attempt records remain
counted during recovery.

## ADR-008: Lock contention is not a persisted failure

If another run owns a work item's workspace, `WorkflowController.run`
returns a non-persisted `FAILED` outcome. It explains the work item is active,
and writes nothing.

Persisting a junk `FAILED` run pollutes the store, counts against nothing,
and later forces reconciliation to explain a run that never did work. Since
no workspace is prepared and no artifact is written, there is nothing to
corrupt or recover.

## ADR-009: Research runs and does not escalate

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
deterministic report. Neither ever receives the implementer's `ChangeSet`
summary.

## ADR-011: Conservative scheduler recovery

A persisted, non-terminal run found at startup is escalated to `NEEDS_HUMAN`
through `WorkflowController.recover_abandoned_run` rather than auto-resumed.

Auto-resuming spends a paid attempt on a run whose true state cannot be
established cheaply. Escalating preserves every artifact and the workspace,
consumes no budget, and leaves a human in control. The scheduler itself still
never mutates run state.

## ADR-012: Worktree administration is serialized per source repository

`git worktree add` and `git worktree prune` both rewrite repository-global
administrative metadata. With `scheduler.max_concurrent_tasks = 2`, two runs
can prepare workspaces simultaneously. The `prepare()` sequence runs under a
per-source-repo `flock` in Git's common directory. The lock is therefore shared
even when factory processes use different data directories. Per-work-item
workspace locks remain separate and are what prevent duplicate active work
within one factory data directory.

## ADR-013: Tracked work is dispatched at most once

The generic `Scheduler` prevents *concurrent* duplicates and otherwise assumes
a tracker withdraws an item once work starts. GitHub Issues do not withdraw
items. An issue stays open and keeps its `agent-ready` label, while the factory
holds no write access.

Without an additional rule, the tick after a run reaches
`DONE`/`NEEDS_HUMAN`/`FAILED` dispatches the same issue again under a new
`FactoryRun` with an empty `attempt_records` list. This causes an unbounded loop
of paid work that mints a fresh retry budget every cycle and defeats
ADR-003/ADR-007.

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

Every other sub-phase (staging 15.3, deployment 15.4, Docker 15.6,
remote workers 15.7, Postgres 15.8, Temporal 15.9, Jira 15.10, and
Kubernetes 15.12) stays deferred. Nothing in the open sub-phases can depend
on a deferred one, and no deferred item is unblocked by proximity.

The selection is operational, not architectural: it makes the existing factory
installable, observable and inspectable on one MacBook. It does not widen what
the factory is allowed to do autonomously.

## ADR-015: CD means publishing release artifacts, never deploying

"Continuous delivery" in this project stops at a published GitHub Release. A
version tag builds artifacts and attaches them. Nothing installs, restarts,
promotes or self-updates, and there is no mutable pointer a client follows
automatically. Autonomous deployment stays banned by `AGENTS.md`.

The release workflow checks whether the tag's release already exists and
refuses to replace its artifacts. GitHub release immutability is also enabled
for new releases. Existing releases from `v0.3.0` onward report
`immutable=true`. Older historical releases remain mutable through the
platform. `SHA256SUMS` and `build-info.json` remain required consumer checks for
the downloaded bytes and their build provenance.

Two native macOS builds are produced (arm64 on `macos-15` and x86_64 on
`macos-15-intel`) as separate PyInstaller `onedir` archives. `universal2`
is rejected. It requires universal wheels for every native dependency, produces
a larger artifact, and turns packaging problems into total build failures.
Building each slice natively on its own runner keeps failures isolated and
diagnosable.

The release contains a wheel, sdist, `SHA256SUMS`, and `build-info.json`.
These record tag, commit, runner image, Python, and PyInstaller facts to trace
build provenance.

Artifacts are unsigned or ad-hoc signed. Developer ID signing and notarization
are deferred. They need a paid account and secrets in CI, which are not
justified for this tool. The consequence is that Gatekeeper will quarantine a
downloaded archive, so release notes must say so plainly and document the
manual step. Silence here looks like a broken build.

A frozen artifact is not self-sufficient. It bundles Python and the factory,
but `git` must exist on `PATH`. In addition, `gh` is required only for enabled
GitHub features. The `copilot` executable is required only for `--runtime
copilot`. Preflight therefore validates prerequisites for *enabled* features,
so the default offline run does not demand tools it will never call.

## ADR-016: The local dashboard is a bounded exception to the V1 ban

`AGENTS.md` bans a web dashboard in V1. One narrow exception is granted.
Inspecting runs, states, attempts, and metrics by reading JSON files is worse
than viewing a page.

The exception holds only within these boundaries:
- loopback bind, explicit start command, disabled by default
- read-only: `GET` only, no route mutates runs, workspaces or configuration
- token protected, token generated per start and never logged
- Python standard library only: no framework, no npm, no bundler, and no build step
- no command logs and no diffs rendered, because repository content and
  near-secrets can leak into a browser in those places. Data minimization is
  applied twice. The detail view uses an allowlisted typed model. The request
  handler allowlists fields again before responding

The ban itself is unchanged for everything else. This tool is a local viewer,
not a control plane. It cannot approve or retry runs, cannot enable
integrations, and has no multi-user concept. If a change requires a write path,
a framework, or a network listener, that requires a new ADR.

## ADR-017: Health and metrics are derived, never accumulated

Persisted run artifacts remain the single source of truth. Health and metrics
are pure functions over the run store, computed on demand.

No counter store, no time-series database and no separate metrics file is
introduced. A derived view cannot drift from the runs it describes, can be
recomputed after any crash, and is trivially testable against a fixture store.
Health and metrics are strictly read-only: they never repair a lock, prune a
worktree or transition a run. They report those as findings for an operator.

Cost is deliberately not fabricated. Token usage and cost appear only when the
runtime actually reported them. Otherwise the value is unknown, never zero and
never inferred from a hard-coded price table. A confidently wrong spend number
is worse than no number. Copilot invocations request the CLI's experimental
usage-output file and persist typed `InvocationRecord` telemetry. Raw
premium-request cost and nano-AIU remain separate persisted units. The
dashboard can derive an AI usage value in USD from nano-AIU for display. It uses
GitHub's fixed AI Credit conversion. It is labeled as usage value rather than
invoice spend because included or pooled credits can cover it. `AttemptRecord`
remains the implementer retry ledger and links to its invocation rather than
being overloaded with every agent call.

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
snapshot. Otherwise the service fails to find git, gh, or copilot, which looks
like a factory bug. The installation command also refuses unsuitable
configuration. The configuration must enable the scheduler, and `factory doctor`
must report no problems. A service that cannot work is worse than no service.

Logging goes to the factory's bounded rotating structured log under the
configured data directory. Launchd's stdout/stderr are pointed at `/dev/null`
precisely because launchd never rotates what it captures. `KeepAlive` is
`Crashed`-only, so no exit code (including the configuration-error code 2)
can create a restart loop. Uninstall unloads the agent and removes the plist
while leaving runs and workspaces untouched.

## ADR-019: Repository capabilities are deterministic profiling plus on-demand skill research

*Supersedes the original ADR-019, which selected advisory skills from a fixed,
versioned built-in catalog. That catalog is removed.*

*Superseded in part by [ADR-021](#adr-021-repository-guidance-is-two-artifacts-generated-and-reusable-plus-a-human-overlay):
generated guidance is repository-wide, reusable and stored outside the target
repository, and a separate human overlay exists. The profiling, sandbox rules,
and validation decisions below still stand. The "regenerated fresh for every
eligible run, no cross-run cache" part does not.*

Repository awareness is a controller-owned scan, not an agent discovery step.
After the worktree is prepared and before `TRIAGING`, the factory walks
repository-local paths and reads a small allowlist of bounded manifests. It
does not run a shell command, import target code, contact the network or
trust repository-provided instructions.

The resulting versioned `RepositoryProfile` is persisted as
`repository-profile.json`. It records technologies, tools, package managers,
markers, warnings, and version files. It also records declarations, ecosystems,
versions, manifests, and dependency groups. Declarations are parsed from
`pyproject.toml`, `requirements.txt`, and `package.json`. These include PEP
621 tables, Poetry tables, and package manager declarations. `setup.cfg` and
`tox.ini` contribute Python and pytest evidence only. Exact versions are
resolved from `uv.lock`, `package-lock.json` and `pnpm-lock.yaml` when
unambiguous, and an ambiguous resolution records a warning rather than a
version. `poetry.lock`, `yarn.lock`, `bun.lock`/`bun.lockb`, `Pipfile.lock` and
`pylock.toml` identify their package manager where applicable and are
fingerprinted as `version_files` without claiming exact graph parsing.

The profile carries two distinct SHA-256 fingerprints. `dependency_fingerprint`
is semantic: it digests technologies, test tools, package managers and the
normalized dependency declarations, and it is the identity that binds a
generated skill. `manifest_fingerprint` is provenance: it digests the content
of the version files, so reformatting a manifest changes it without
invalidating guidance that is still correct.

There is no fixed skill catalog. When `polish.enabled` is true, the controller
re-profiles the post-implementation worktree after verification. If needed, it
transitions through `RESEARCHING` to invoke the Researcher for guidance.
Invalid typed output or provenance receives its exact bounded rejection reason
in one retry. Infrastructure failure receives one ordinary retry. A second
failure safely skips polish.

That call is bounded and web-only. It runs in the run directory instead of the
worktree. Its only tool is `web_fetch`. It sees only the normalized profile,
configured URL lists, and generation rules. It never sees changed filenames,
source code, README content, task prose or the diff.
`polish.official_documentation_origins` (official documentation,
migration guides, release notes) is authoritative for every version claim.
Curated `polish.practice_reference_urls` are pinned to commit `52cc5efd`. They
contribute generic quality heuristics only, synthesized rather than copied.
They never supply version claims, commands, tools or orchestration. Fetched
pages are untrusted data.

It returns one typed `RepositorySkill` with the `dependency_fingerprint`,
bounded targets, and HTTPS sources. It includes simplify and polish guidance,
and uncertainties. The type itself refuses a skill with neither an official
source nor an explicit uncertainty, and refuses an official source that claims
only generic applicability.

The controller validates deterministically. It rejects fingerprint mismatches,
unprofiled targets, or evidence paths outside the profile. It also rejects
missing official provenance for frameworks or unallowlisted sources.

The run is green before polish. A failed re-profile, rejected skill, or stale
skill records a profile warning. The factory skips polish and proceeds to
review. The skill reaches only the polish Implementer, Tester and Reviewer,
never before the initial green baseline, and is regenerated fresh for every
eligible run. There is no cross-run cache or plugin system. The context is
advisory: it changes no tools, models, workflow states, quality gates,
commands, permissions or routing.
reaches only the polish Implementer, Tester and Reviewer, never before the
initial green baseline, and is regenerated fresh for every eligible run. There
is no cross-run cache or plugin system. The context is advisory: it changes no
tools, models, workflow states, quality gates, commands, permissions or
routing.

## ADR-020: Post-green polish is one bounded implementation attempt, informed by on-demand skill research

*Superseded in part by [ADR-021](#adr-021-repository-guidance-is-two-artifacts-generated-and-reusable-plus-a-human-overlay).
Polish applies reusable guidance and human overlays. Research runs only when
the fingerprint has no guidance. The bounded, simplify-then-polish shape
stands.*

When `polish.enabled` is true, verification schedules skill research and one
`IMPLEMENTER` pass with `AttemptTrigger.POLISH`. It applies `RepositorySkill`:
simplify first, then version-specific polish second. It uses existing worker
routing and implementation budgets. It can make no edits. Full deterministic
verification and scope assessment run again before review.

Polish never runs during CI repair and is scheduled only when one later
implementation attempt remains available to recover from a regression. It
introduces no `POLISHING` state and no `POLISHER` role. The temporary
`VERIFYING → RESEARCHING → IMPLEMENTING → VERIFYING` sequence remains
authoritative and visible in persisted attempt records. The generated skill is
provided only to that attempt's Implementer, Tester and Reviewer and is
regenerated fresh for every eligible run.

Because polish is an improvement on an already-verified change, its failure
modes are non-fatal by design. An unverifiable or stale skill is discarded with
a recorded warning. The run continues on its existing green path rather than
failing or escalating.

The configuration model defaults omitted legacy `polish` sections to disabled
for compatibility. The packaged default and example enable it, so their normal
fake run records an initial implementation attempt and one polish attempt.

## ADR-021: Repository guidance is two artifacts: generated and reusable, plus a human overlay

Repository guidance has two producers with different trust levels. It uses two
separate artifacts rather than one shared file.

**Generated guidance is repository-wide and reusable.** It describes the
repository, not the task, so the Researcher receives the normalized
`RepositoryProfile` and the configured source lists only. It never sees changed
filenames, source code, README content, task prose or the diff. Generated
skills are stored under `factory.data_dir` in repository-scoped storage keyed
by the canonical local repository identity and the profile's
`dependency_fingerprint`. Storage follows the template
`<data_dir>/repository-skills/v1/<repository-key>/...`. Guidance is never
stored in or loaded from target repositories. Target code cannot inject guidance
into the factory, and the factory does not mutate target checkouts.

A run reuses the generated skill matching the current fingerprint and makes no
research call. Generation runs only when a fingerprint has no generated skill.
Existing files are never overwritten. A dependency change selects a new file.
There is no TTL, because time does not invalidate guidance. A dependency change
invalidates guidance. Reuse is not trust. Schema, profile agreement, and
sources are revalidated on every load. A corrupted file is rejected, left on
disk, and reported with a warning pointing to `factory skill refresh`.

This bounds research per fingerprint, not per process, and that choice is
deliberate. Two concurrent first runs for the same missing fingerprint can each
run one sequence: an initial Researcher call and one retry after failure.
Publication is atomic and no-clobber, so one result is kept, the loser loads
the winner, and both revalidate it in full before use. Cross-process
serialization needs extra locking machinery and risks stalls. To save at
most one sequence (two calls), the race is accepted. It cannot corrupt storage,
produce competing files, change which guidance is used, or affect the overlay.

Repository identity is the local Git common directory. All linked worktrees of
a checkout share one skill directory. No remote URL is consulted. Two clones of
the same remote are legitimately different local repositories with different
profiles. The visible consequence is that moving or re-cloning a repository
selects a new key with no guidance. That is preferred over guessing identity
from a remote. The factory neither follows moved repositories nor deletes
orphaned guidance. Operators can copy guidance directories or recreate them
deliberately. Use `factory skill path` to find paths.

**Human customization is a separate overlay.** A repository-level
`repository-skill-overlay.yaml` lives in the same repository-scoped storage,
outside the target repository. It carries guidance prose only, with
`mode: extend|replace` plus optional `simplify` and `polish` guidance blocks.
It has no targets, sources, versions or fingerprints. Version-specific claims
stay the Researcher's job, grounded in official documentation. The overlay is
where house rules live. Because it is unbound to a dependency state, it survives
dependency changes and keeps applying when a new generated file is selected.

The factory never creates, rewrites, normalizes, refreshes or deletes the
overlay. It is a human's file, and a tool that reformats or regenerates it
destroys intent and discourages its use. An invalid overlay is preserved
exactly as written, recorded as a warning, and ignored for that run. Valid
generated guidance can still apply, so one YAML mistake does not silently drop
all guidance.

**Explicit commands, no hidden writes.** The command `factory skill path`
discovers file paths. The command `factory skill validate` validates current
files. Both are read-only. `factory skill refresh --repo PATH
[--runtime fake|copilot]` refreshes generated guidance only and never touches
the overlay. The read-only dashboard gains no skill or overlay write path
(ADR-016 stands).

**Runs snapshot what they used.** Before agents consume guidance, each run
stores immutable snapshots of effective skills, overlays, and metadata. This
records loaded files, fingerprints, and skip reasons. Runs remain explainable
even though guidance files are editable. Mid-run edits affect later runs only.

**Nothing about this grants authority.** Guidance remains advisory prompt text
for one post-green attempt. Full deterministic verification runs again
afterwards. Guidance cannot change tools, models, states, budgets, permissions,
gates, or scope. Failure to load or validate guidance skips polish with a
warning instead of failing green runs.
