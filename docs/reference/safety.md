# Safety and trust boundaries

The design rule is one sentence: **LLMs provide intelligence, the factory
provides authority.**

## What agents may do

- Understand a work item, refine requirements, research, plan.
- Edit source code and write tests inside their assigned Git worktree.
- Run commands (implementer only).
- Review a change and diagnose a failure.

## What agents may not do

- Transition workflow state.
- Grant themselves a retry.
- Choose which model runs.
- Pass or waive a quality gate.
- Claim a task, or take a task from another run.
- Push, merge, or change branch protection.
- Deploy anything.
- See production credentials.
- Decide whether their own output is accepted.

Every item on that list is deterministic Python. A prompt cannot change it,
because a prompt is not what enforces it.

## Off by default

| Feature | Default |
| --- | --- |
| Agent runtime | `fake` (no model calls, no cost) |
| `pull_request.enabled` | `false` |
| `ci.enabled` | `false` |
| `merge.enabled` | `false` |
| `scheduler.enabled` | `false` |
| Dashboard | not running |
| launchd service | not installed |

With those defaults, `factory run` performs no network access and makes no paid
model call. The test suite runs entirely offline: it never calls a model and
never reaches GitHub.

The packaged configuration enables `polish`, but the fake runtime still makes
no model or network call. Legacy configurations that omit the section default
it to disabled.

## Network access

Nothing in the factory contacts the network unless you turned something on.

| Trigger | Talks to |
| --- | --- |
| `--runtime copilot` | GitHub Copilot, through the `copilot` CLI. Paid. |
| `pull_request.enabled` | GitHub, through `gh`. |
| `ci.enabled` | GitHub, through `gh`. |
| `merge.enabled` | GitHub, through `gh`, with a separate repository and check allowlist. |
| `scheduler.enabled` | GitHub Issues, through `gh`. |
| An eligible `polish.enabled` attempt with no stored guidance for the repository's current dependency fingerprint, or `factory skill refresh --runtime copilot` | The configured Researcher fetches only `polish.official_documentation_origins` and the exact, commit-pinned `polish.practice_reference_urls` to generate a `RepositorySkill`. `web_fetch` is its only tool for that call, and it runs outside the worktree. A run that reuses stored guidance fetches nothing. |
| Your own `repository.commands` | Whatever they contact. `uv sync` hits a package index. |

There is no external analytics, crash reporting or telemetry exporter. When
Copilot reports invocation usage, the factory persists a bounded typed record
locally with the run, project plan or standalone skill refresh. Raw prompts,
tool output and usage files are not retained. Logs and telemetry stay in the
data directory.

## Money

`--runtime copilot` is the only thing that spends. It is never the default, on
any command.

Repository guidance is researched once per repository and dependency
fingerprint, then reused, so a normal run spends nothing on it. Reuse is not a
cross-process lock. Two concurrent first runs for the same missing fingerprint
can each make an initial call and one retry after failure. Invalid output
includes the exact bounded rejection reason. Atomic no-clobber publication
keeps one winner. Both runs revalidate it. The race costs at most one extra
sequence (two calls) and changes nothing else.

Two extra bounds exist for the daemon:

- `factory service install` defaults to `--runtime fake`, so an
  installed-and-forgotten service cannot spend.
- `scheduler.max_runs_per_day` (default `20`) caps claims per UTC day,
  independently of concurrency.

Token usage and cost are reported only if the runtime returns them. The Copilot
runtime requests and persists its usage-output data. Missing or malformed
fields stay unknown and are never defaulted to zero. Persisted telemetry remains
in raw runtime units. The dashboard can derive an AI usage value in USD from
nano-AIU for display, but it is not necessarily the invoice charge. Use GitHub
Copilot billing for authoritative spend.

## Credentials

- The `copilot` child process starts with GitHub credential variables removed:
  `GH_TOKEN`, `GITHUB_TOKEN`, `GH_ENTERPRISE_TOKEN`, `GITHUB_ENTERPRISE_TOKEN`,
  `GITHUB_PAT`, `GIT_ASKPASS` and the Actions token variables. Agents never
  receive GitHub credentials.
- Only controller code passes a token to `gh`. It uses the subprocess
  environment, never as a command-line argument where it lands in the process
  list.
- Repository commands run with an environment allowlist: `PATH`, `HOME`, `LANG`,
  `TERM` and whatever you named in `env_passthrough`. Nothing else is inherited,
  and commands use a non-login shell so profile files cannot reintroduce filtered
  variables.
- Captured command output and structured logs are redacted for token-shaped
  strings before they are written.

## Repository safety

- Every work item runs in its own Git worktree under the data directory. The
  source checkout is not modified in place.
- After preparing the worktree, capability detection reads only
  repository-local paths and allowlisted bounded manifests. It uses no shell,
  network or imports, and does not load repository-defined skills. There is
  no fixed skill catalog.
- Repository guidance is stored under `factory.data_dir`, in repository-scoped
  storage keyed by the repository and its dependency fingerprint. Generated
  skills and your `repository-skill-overlay.yaml` are never written into the
  target repository or its worktree, and guidance is never auto-loaded from the
  target repository.
- Workspace paths are sanitized and must stay inside the workspace root. Cleanup
  refuses a path outside it.
- A short-lived exclusive lock prevents two processes owning the same work item.
- No command is run through a shell. Everything is an argument list.

## Git and publishing

- Branch names must start with `repository.branch_prefix` and cannot be the
  base branch.
- The remote host must be in `pull_request.allowed_hosts`.
- The changed-file count must be within `repository.max_changed_files`.
- No changed file can match `repository.protected_file_patterns`.
- Scope drift is re-checked at the pull request boundary.
- Publication binds both the immutable reviewed tree and the exact allowed
  parent. The controller records its commit before push. Resume uses that
  receipt rather than accepting arbitrary commit history with the same tree.
- Pull requests are drafts by default.
- The factory never force-pushes. Automatic merging requires explicit policy,
  named green checks on the reviewed head, a matching repository and target,
  and confirmed merge evidence. It never bypasses branch protection.
- Exact, human-authorized dependency/CI files can pass sensitive-scope checks
  only when also explicitly named in the plan. This never exempts protected
  files, migration/infrastructure changes, risk approval or ordinary scope limits.

## Quality gates

Deterministic evidence comes first. Lint, type checks, tests, build, the changed
file list and the Git diff are computed by the factory, not reported by an agent.

The tester and reviewer receive the controller-derived diff, the changed files
and the deterministic results. They never see the implementer's own summary of
what it did. The implementer's success claim is not a gate.

The reviewer's model family must differ from every worker's. Configuration
enforces this.

Reviewer repair scope is controller-owned. Typed findings have stable ids and
source locations. Every open finding needs an explicit disposition, and the
controller derives repair approval from those dispositions rather than trusting
the model's approval flag. Repair regressions remain blocking. One batch of
late findings can be adopted. Later drip-fed findings are advisory.

Review loops are bounded separately from implementation retries. The default
allows three logical reviews. After that, the controller can continue an
`R0`/`R1` run with at most five correctness or compatibility findings. It
persists `review-acceptance.json`, binds it to the exact tree, and discloses the
debt in the PR. Security, scope, repair-regression and high-risk findings are
never accepted automatically.

Broken required checks cannot reach the tester or reviewer at all.

Repository guidance is advisory prompt context only. There is no fixed built-in
catalog and no repository-provided plugin system. Guidance consists of two
artifacts: a `RepositorySkill` generated by the configured Researcher, and an
optional human-written overlay. It reaches only the post-green polish attempt's
Implementer, Tester and Reviewer, never the initial attempt.

Generated guidance is repository-wide and reusable. It is stored under
`factory.data_dir`, keyed by the repository and `dependency_fingerprint`. It is
never overwritten, and never expires on a timer. Reuse is not trust. Schema,
agreement with the profile, and cited sources are revalidated on every load. A
corrupted or hand-edited generated file is rejected like a bad generation. The
file is left as written, polish is skipped, and the warning points at `factory
skill refresh`.

The repository key comes from the canonical local Git common directory. All
linked worktrees of one checkout share a directory. No remote URL is involved.
Moving or re-cloning a repository selects a new key: guidance at the old path
is neither followed nor deleted.

The research call behind it is deliberately blind and web-only. It runs in the
run directory instead of the worktree. Its only tool is `web_fetch`. It runs
without repository custom instructions, and sees only the normalized profile
and configured source lists. It never sees changed filenames, source code,
README content, task prose, or the diff. Fetched pages are treated as untrusted
data. Embedded instructions are ignored. Official documentation is authoritative
for version claims. Curated practice references (pinned to an immutable commit)
contribute generic heuristics only. They never supply version claims,
commands, tools or orchestration.

The controller validates the skill deterministically.
The skill must contain the profile `dependency_fingerprint`.
Each target must match a profiled dependency declaration and evidence path.
Detected `python`, `pytest`, `react`, `react-dom`, `vite`, and `vitest`
dependencies need official provenance.
Each source must match `polish.official_documentation_origins` or an exact
`polish.practice_reference_urls` entry.

The overlay is human-owned and deliberately weaker. A repository-level
`repository-skill-overlay.yaml` carries guidance prose only, with `mode:
extend|replace` plus optional simplify and polish blocks. It cannot declare
targets, sources, versions or fingerprints, so it can never smuggle in a
version claim or a source. The factory never creates, rewrites, normalizes,
refreshes or deletes it. An invalid overlay is preserved untouched, reported as
a warning, and ignored for that run, while valid generated guidance still
applies.

Every run snapshots what it used (the effective skill, the overlay as read
when valid, and the guidance provenance) before any agent sees it. A run
stays explainable and a mid-run edit affects later runs only.

None of this can break an already-green run. Failed profiling or research
records a profile warning. A rejected skill, invalid overlay, or stale guidance
also records a warning. The factory skips or disables polish instead of
failing or escalating. Guidance cannot
grant tools, alter models, change workflow states, waive gates, add commands,
spend retry budget, widen permissions, change dependencies or widen scope.
`factory skill refresh` is the only command that writes guidance, it writes
generated files only, and the dashboard has no skill or overlay write path.

## Bounded everything

There is no unlimited retry loop anywhere.

| Budget | Default | Bounds |
| --- | --- | --- |
| `retries.same_model_attempts` | `2` | Per-stage same-model attempt limit for implementation routing and supported typed-output correction. |
| `retries.max_total_attempts` | `6` | Implementation attempts per run. |
| `polish.enabled` | `true` packaged, `false` if omitted | At most one post-green implementation attempt. |
| `review.max_rounds` | `3` | Absolute logical review rounds. Eligible low-risk findings can be accepted, otherwise the run stops for a human. |
| `review.max_accepted_findings` | `5` | Maximum findings in one controller acceptance. |
| `review.accepted_risks` | `[R0, R1]` | Risk levels eligible for bounded acceptance. |
| `review.blocked_categories` | `[SECURITY, SCOPE]` | Finding categories that always require resolution or a human. |
| Reviewer late-finding adoption | `1` round | One repair review can add a batch of previously missed blockers. |
| Reviewer path/finding stall guard | `3` reviews | Triggers bounded acceptance evaluation or stops with `review-impasse.json`. |
| Reviewer blocker-replacement guard | `2` reviews | Stops consecutive complete blocker replacement cycles. |
| `scope_drift.max_replans` | `1` | Replans after scope drift. |
| `ci.repair_attempts` | `3` | CI repair cycles. |
| `ci.max_wait_seconds` | `1800` | CI polling. |
| `factory.agent_timeout_seconds` | `900` | One agent invocation. |
| `repository.command_timeout_seconds` | `900` | One repository command. |
| `scheduler.max_concurrent_tasks` | `1` | Concurrent runs, max `2`. |
| `scheduler.max_runs_per_day` | `20` | Claims per UTC day. |
| `--max-scanned-runs` | `1000` | Run files parsed per `status` call or dashboard request. |

Budgets are persisted on the run. A restart does not reset them.

Polish uses the implementation budget. It runs only when one recovery attempt
remains, never runs during CI repair, and is always verified again. It can make
no edits. No `POLISHING` state or `POLISHER` role exists.

## Recovery is conservative

A persisted, non-terminal run left behind by a dead process is transitioned to
`NEEDS_HUMAN` through the controller. It is never auto-resumed. No paid retry is
spent, the budget is untouched, and the workspace and artifacts stay on disk.

The explicit project `--resume` path is a narrow exception: it reconciles
persisted task identities and safe PR delivery checkpoints under the original
policy. It does not replay ambiguous in-flight implementation, reopen an
exhausted terminal run or reset retry budgets.

`factory status` reports stale locks, orphaned worktrees and abandoned runs as
findings. Repairing one is an explicit operator action.

## The dashboard

The only exception to the V1 ban on web UIs, and deliberately a viewer rather
than a control plane.

- Started only by `factory dashboard`. It is the only command that opens a
  socket.
- Binds `127.0.0.1` only. Not configurable.
- `GET` only.
- Token generated per process, printed once, never logged.
- Renders the run list, run detail, workflow state, attempt history and derived
  metrics. Never command logs, diffs, prompts or raw artifacts.
- Data minimization is applied twice. The detail provider builds a typed object
  with summary fields and metadata (never failure reasons, agent reasoning, or
  raw artifacts). The request handler then allowlists the fields it renders. A
  future provider mistake still cannot leak content.
- Cannot approve, retry, cancel or reconfigure anything.
- Python standard library only. No framework, no npm, no bundler, no build step.

## The service

- macOS only, per-user, opt-in.
- Exactly one plist under `~/Library/LaunchAgents`. Nothing under `/Library`. No
  root `LaunchDaemon`.
- Installed only by `factory service install`. Never when you extract an
  archive, run the factory, or upgrade it.
- Refuses unless the configuration enables the scheduler, and refuses if
  `factory doctor` reports any error.
- Defaults to `--runtime fake`.
- `KeepAlive` is `Crashed`-only, so no exit code can produce a restart loop.
- Uninstall removes the plist and leaves all history on disk.

## Release artifacts

Release archives are unsigned or ad-hoc signed. Apple Developer ID signing and
notarization are deferred. macOS quarantines a downloaded archive until you
clear the attribute yourself.

The release workflow refuses to replace an existing release. GitHub release
immutability is also enabled for new releases. Existing releases from `v0.3.0`
onward report `immutable=true`. Older historical releases still report
`immutable=false`.

Verify `SHA256SUMS` before you extract anything.

See [Releases](../project/releases.md).

## What does not exist

The factory deliberately excludes several features. It does not implement
unrestricted autonomous merge, deployment, a staging tier, or remote workers.
It also omits sandboxes, hosted services, control planes, and semantic memory.
