# Architecture Decisions

<<<<<<< HEAD
## ADR-038: The planner and the reviewer keep the change simple
=======
## ADR-039: Unattended mode lets gates before the pull request continue
>>>>>>> 4339b48 (feat(workflow): unattended mode lets gates before the pull request continue (ADR-039))

Status: accepted on 2026-10-04.

### Context

<<<<<<< HEAD
AI agents often build more than the task needs.
They add files, classes, layers, options and dependencies that no requirement asks for.
Most of this starts in the plan. The implementer and the reviewer then use the plan as the limit.
The implementer already has a simplify pass. The planner and the reviewer only had general rules.
A separate simplify step for the plan or the review adds a call and more complexity.

### Decision

- The planner chooses the simplest approach that meets the acceptance criteria.
- The planner adds a new file, class, layer, option or dependency only when an acceptance criterion needs it.
  The step names that criterion.
- The reviewer reports a new file, class, layer or option as a blocker when no acceptance criterion needs it.
  The finding uses the new category `SIMPLICITY` and names the simpler change.
- `SCOPE` is work outside the work item. `SIMPLICITY` is an extra part inside it.
- An unneeded new dependency stays a `SCOPE` finding. A person must still accept it.
- `SIMPLICITY` is not in the default `review.blocked_categories`.
  At the review limit, a low risk run accepts an open `SIMPLICITY` finding.
- There is no new agent call and no new workflow step.
- The scope drift check does not change. It still finds unplanned modules and dependency changes.

### Consequences

- A `SIMPLICITY` blocker goes to the normal repair loop. It does not ask a person.
- If the repair loop cannot remove the part, a low risk run still continues.
  The finding is recorded in the review acceptance.
- The rule uses acceptance criteria, so it cannot block work that the task asks for.
=======
The factory must run most of the time without a person.
Before this change, more than 40 places in the workflow can stop a run in `NEEDS_HUMAN`.
A stopped run waits until a person replies. With escalation off, it waits forever.
Only the risk approval gate had a setting to turn it off.

### Decision

- The new setting `factory.unattended` is `false` by default.
- When it is `true`, these gates let the run continue instead of stopping:
  - Risk approval. No risk level needs approval.
  - Manual triage. The run uses the full workflow.
  - An ineligible triage result. The run uses the full workflow.
  - Unresolved plan decisions. The implementer uses the plan as it is.
  - Sensitive scope, such as dependency, CI workflow or migration files.
  - A scope replan that makes no progress or uses its whole budget.
  - A review impasse or a review limit. The run accepts the open findings, for any risk and any category.
- An unattended run never ends in `FAILED` in place of a human stop.
- The run record keeps every accepted finding in the review acceptance.
- Gates after implementation, such as a used attempt budget or failed CI, are a later change.

### Consequences

- Runs before the pull request no longer wait for a person.
- Code with open review findings or sensitive changes can reach a pull request.
  A later change labels such pull requests so that a person can look at them later.
- With the setting off, the factory behaves as before.
>>>>>>> 4339b48 (feat(workflow): unattended mode lets gates before the pull request continue (ADR-039))

## ADR-037: Deterministic routing replaces the Jev classifier

Status: accepted on 2026-10-03.
This amends ADR-027.

### Context

ADR-027 added an external route classifier.
The classifier was Jev, from TypeSafe System One.
The factory sent sanitized task text to Jev over HTTPS when more than one route was legal.
Independent research shows that prompt-only routers are only a little better than a fixed table.
Issue text is a weak signal of difficulty for coding tasks.
The factory makes one route decision for each run.
So the speed and price of Jev give no real advantage.
Only the vendor reports the accuracy and calibration of Jev.
Jev also adds a network call, an API key and a prompt injection surface.

### Decision

- The factory does not call a route classifier. It makes no network call to choose a route.
- The safety floors decide which configured route options are legal. This does not change.
- When routing is enabled and more than one option is legal, the controller selects the lightest legal route.
- The order from lightest to heaviest is `SINGLE`, `CRITIQUE`, `FULL` and `MANUAL_TRIAGE`.
- When two legal options have the same route, the controller selects the first one in the configuration.
- The decision records the source `rule`, a confidence of 1.0 and no probabilities.
- The rules for disabled routing, no legal option and one legal option do not change.
- The ratchets do not change.
- The `routing` keys `api_url`, `model`, `api_key_env_var`, `timeout_seconds`, `min_confidence`, `min_probability`, `max_prompt_chars` and `max_response_bytes` are removed.
  An old configuration file can still have them. The factory ignores them.
- The packaged default still disables routing.

### Consequences

- Routing needs no API key and no network access.
- The same work item and configuration always give the same route.
- Old run records with the source `jev` still load. They keep their probabilities, model identifier and usage.
- The pi runtime still removes every `*_API_KEY` variable from the agent environment.

## ADR-036: Remove fast performance mode, the security profile and per-result writing checks

Status: accepted on 2026-10-03.
This amends ADR-023 and ADR-029.

### Context

The leanness plan removes options and checks that add little value.
The fast performance mode sent eligible `L0` and `L1` work with `R0` or `R1` risk to a cheaper Planner profile.
It also skipped the polish pass, and it fell back to the standard mode when the scope grew.
Adaptive routing already gives low-risk work a shorter path, and the next slice makes that path the light path.
`--model-profile economy` already selects cheaper models.
The packaged `security` model profile was almost the same as the default models.
Since ADR-029, the controller checked each agent result against the writing policy.
It only logged the findings and stored them in `writing_findings` on the invocation record.
No person or process used these findings.

### Decision

- The controller has no performance mode. The `--performance-mode` option is removed from all commands except `factory start`.
  `factory start` accepts and ignores it, so a service that was installed with it still starts.
- The `performance` configuration section is removed. An old configuration file can still have it. The factory ignores it.
- The `security` model profile is removed. A request for it fails like any unknown profile.
- The controller does not check agent results against the writing policy.
- The output contract of each role still states the word limit of each field.
- The controller still checks issue, pull request and commit text before it publishes it. A finding is a log warning.
- The routing ratchet still moves a run to `FULL_REVIEW` when the changed files include protected, manifest, version or sensitive files.

### Consequences

- The polish pass depends only on `polish.enabled`, the route and the attempt budget.
- Old run records load. The factory ignores the old performance mode fields and `writing_findings`.
- A service that an operator installed with `--performance-mode` keeps starting. The option has no effect.
- The run detail page does not show a performance mode.
- The offline benchmark no longer compares the standard mode with the fast mode.

## ADR-035: One planning call replaces the refiner and the researcher

Status: accepted on 2026-10-03.
This amends ADR-009, ADR-024, ADR-027 and ADR-033.

### Context

A `FULL` run called the Refiner and then the Planner. These were two sequential model calls.
When triage set `needs_research`, a Researcher call came between them.
The Researcher had the same read-only repository tools as the Planner.
So the Planner explored the same repository again.
The leanness plan removes model calls that do not add evidence.

### Decision

- A `FULL` run goes from `TRIAGING` to `PLANNING`. No new run enters `REFINING` or `RESEARCHING`.
- One Planner call returns a `PlanningResult`. It holds a `Specification` and an `ExecutionPlan`.
- The controller saves `specification.json` and `execution-plan.json` as before.
  The Implementer, Tester, Reviewer, scope checks and the dashboard see no change.
- The Planner prompt keeps the Refiner rules: separate facts, assumptions and unknowns.
  Acceptance criteria must be measurable. The Planner must not invent requirements.
- The Planner receives the work item and the triage result.
  A re-plan also receives the current specification.
- A re-plan for unresolved decisions (ADR-026) returns both parts again.
  A scope re-plan changes only the plan and keeps the specification.
- A risk approval reopens the run at `PLANNING`. An approval context written earlier names `REFINING`.
  The controller accepts that context and also reopens the run at `PLANNING`.
- The `models.refiner` and `models.researcher` keys are removed.
  An old configuration file can still have them in `models` or in a model profile. The factory ignores them.
- `TriageResult.needs_research` is not used. It has the default `false`, so old triage files load.
  Fast mode no longer falls back to standard mode for it.
- The `ResearchReport` artifact is removed. The factory ignores an old `research.json` file.
- The `REFINER` and `RESEARCHER` roles and the `REFINING` and `RESEARCHING` states stay, so old run records load.
  A resume of an old run in one of these states stops the run for a human.

### Consequences

- A `FULL` run makes one or two fewer model calls.
- The Tester and the Reviewer still get the acceptance criteria from the specification.
- Old runs, triage files and configuration files load.
- The Planner is the only role that explores the repository before the Implementer.
- A later change can give the Planner web access.
  That access must be read-only and allowlisted, and only the Planner can get it.
  The Implementer has shell and write access, so it must not get web access.
  `agent_capabilities.capability_for` is the one place that grants it.

## ADR-034: The factory sets up the target toolchain and repository skills

Status: accepted on 2026-10-03.
This supersedes ADR-019 in part and ADR-021 in part. ADR-020 stays.

Amendment on 2026-10-03 (slice C1): a setup run with an unclear stack records a note and adds nothing. It does not escalate.

Amendment on 2026-10-03 (Bun and Yarn): Yarn and Bun lockfiles are supported. Each one selects its own install, run and add commands.

Amendment on 2026-10-03 (slice E): lenses go into the reviewer prompt only. The polish attempt keeps its repository guidance.

Amendment on 2026-10-03 (fixed polish guidance):

- The polish attempt uses the fixed `simplify` and `polish` templates and the lenses selected for the changed files.
- The factory no longer generates repository skills with a model. No model writes or selects guidance.
- This supersedes the remaining skill generation parts of ADR-019 and ADR-021. The human overlay and `factory skill` are removed too.
- Stack lenses for React, Vue and Angular are selected from the declared dependencies of the repository.
  A stack lens applies only when the repository declares one of its dependencies and a changed file matches its scope.
- The `polish.official_documentation_origins` and `polish.practice_reference_urls` keys are ignored.

Amendment on 2026-10-03 (mutation gate):

- After the verify commands pass, the factory runs `mutmut` 3 on the changed Python source modules.
  It uses the package runner of the lane, such as `uv run --no-sync`.
- Without configuration, `mutmut` 3 mutates `lib/` or `src/`. The gate then runs only for changed files in that directory.
  A flat layout needs `source_paths` in `[tool.mutmut]` or in the `[mutmut]` section of `setup.cfg`. Without it, the gate skips and records why.
- The gate reads the result of each mutant from the `.meta` files that `mutmut` writes. It does not read command output, because the factory cuts long output.
- The gate runs only when the inventory finds `mutmut`, the lane has a package runner and at least one verify command ran.
  `repository.mutation_gate: false` turns it off.
- The gate adds one check to the verification report. The check lists the surviving mutants and each module of which the tests kill no mutant.
  The tester and the reviewer read the report.
- The gate is advisory. It never fails verification, because `mutmut` also makes equivalent mutants that no test can kill.
  A trial on 2026-10-03 showed this: `<` to `<=` in a clamp function returns the same value. The reviewer judges each survivor.
- The `dev-team` gate pins `mutmut<3`. The factory does not, because `mutmut` 2 fails on Python 3.13 and later.
- A missing tool, a crash, a timeout or an error in the gate itself skips the gate and records the reason in `mutation.json`.
  Every passed verification writes `mutation.json`, so no report from an older attempt stays.
- The gate always removes `mutants/`. If the repository already has `mutants/`, the gate skips and deletes nothing.

The factory gets lint, format and test commands only from the YAML configuration.
It installs no tools in the target repository.
A research call to a model generates skill guidance, and that guidance stays in the factory data directory.
So each new repository needs hand configuration, and a person who works on the repository without the factory gets no tools and no skills.

The `dev-team` plugin (bdfinst/agentic-dev-team, MIT) solves this with fixed tables, not research.
It detects the stack and makes a list of the tools that are already there.
It then installs only the missing tools and copies skills and review lenses for the stack.
The factory takes over this model, in five slices.

### Toolchain registry and inventory

- A Python registry holds the toolchain facts.
  Each lane is a language: Python and JavaScript/TypeScript first.
  Each lane has four slots: format, lint, typecheck and test.
  The verify commands need these four jobs as separate commands.
  Each slot has an ordered list of providers and one default provider.
  The first provider with evidence wins. The default is the provider to add when no provider has evidence.
  A slot can require a technology. For example, the JavaScript typecheck slot applies only to TypeScript.
- Each provider has detection rules: a dependency declaration or a root-level configuration file, table, section or key.
  The inventory does not probe executables, because a probe needs a shell or a process.
- The inventory runs with the repository profile. It uses the same limits: no shell, no network and no target code.
- If the profile evidence is cut short or degraded, the inventory is marked incomplete.
  A later slice must not add a default tool from an incomplete inventory.
- If a provider is already configured, the factory keeps it and installs nothing for that slot.
  For example, `black` and `flake8` fill the Python slots, and the factory does not add `ruff`.

### Verify commands come from the inventory

- If all `repository.commands` lists are empty, the factory makes commands from the inventory.
- If the YAML configuration has any repository command, the factory uses only the YAML commands.
- `repository.derive_commands: false` turns derivation off. It is on by default, because the factory runs automatically.
- The factory makes commands only for tools that the repository has. It never adds a default tool here.
- Each lane needs exactly one supported lockfile at the repository root: `uv.lock` or `poetry.lock` for Python, `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `bun.lock` or `bun.lockb` for JavaScript.
- A `package.json` script such as `lint` or `test` replaces the tool command for its slot.
- Each verify command is meant to check and not write. Flags such as `--no-fix` and `--ci` and the variable `CI=true` turn off fixes and snapshot writes.
- Before the agents start, the factory runs the install command and each verify command on the unchanged base commit.
  It keeps only the commands that pass there, so a check that already fails does not block every run.
  Each lane is checked on its own. One lane's failure does not reject the commands of another lane.
- The worktree must be clean and at its base commit before this check. If it is not, the factory runs nothing and deletes nothing.
- If a lane's commands change the Git tree, the factory discards the changes and rejects that lane's verify commands.
- `repository-commands.json` records the source, the commands, the rejected commands and the reasons. It never records command output.
- Run verification uses this plan.
- The routing safety floors still use only the YAML verify commands.
  A derived `package.json` script is a file that the agent can change, so it does not unlock the SINGLE or CRITIQUE route.
- Project integration verification still uses only the YAML commands.
- Autonomous merge (ADR-022) still needs explicit YAML verify commands.
- This check runs repository code before triage. That code includes install hooks and package scripts.
  Use `repository.derive_commands: false` for a repository that you do not trust.
  That switch also turns off the automatic setup check.

### Setup run

- `factory start` checks the repository when the source HEAD changes. It starts a setup run only for a plan that it did not propose before.
- No person answers a question. The setup run uses safe defaults.
- A fixed table selects the tools. No model selects them.
- For each slot without a provider, the setup run adds the default provider as a development dependency.
  It also adds the mutation tool for the stack: `mutmut` for Python, and Stryker with the runner for the test tool for JavaScript/TypeScript.
- The package manager of the lane adds the tools, so the manifest and the lockfile change together:
  `uv add --dev --no-sync`, `poetry add --group dev --lock`, `npm install --save-dev --package-lock-only --ignore-scripts`, `pnpm add --save-dev --lockfile-only --ignore-scripts`,
  `yarn add --dev --ignore-scripts` (Yarn 1), `yarn add --dev --mode=update-lockfile` (Yarn 2 and later) or `bun add --dev --lockfile-only --ignore-scripts`.
  These commands install nothing, except Yarn 1, which has no lockfile-only mode. The JavaScript commands run no package scripts.
  Yarn 2 and later skips scripts in this mode.
  Yarn 2 and later is found from a root `.yarnrc.yml`, a `packageManager` of `yarn@2` or later, or a `yarn.lock` with a `__metadata:` header.
  A Yarn 1 workspace root also gets `-W`.
  Python locking can run the project's build backend to read package metadata.
  A pnpm workspace root also gets `--workspace-root`.
- A lane is skipped when it has more than one lockfile at the root, such as `package-lock.json` and `yarn.lock`.
- A repository that declares or configures its mutation tool keeps it. Configuration counts: `[tool.mutmut]`, `[mutmut]` or a `stryker.config.*` file.
- The setup run works in a factory worktree at the source HEAD, on its own branch. It never changes the source checkout.
- It holds the work item lock. It refuses a worktree that is not clean at its base, so it never builds on a failed run.
- It writes `.factory/setup.json` without following a symbolic link.
- `.factory/setup.json` records the plan. After a setup, the inventory finds the new tools, so the same state does not cause a second setup run.
- The setup run adds nothing from an incomplete inventory, and nothing for a lane without exactly one root lockfile.
  It records a note instead. It does not guess.
- The setup run opens a pull request.
  Dependency changes are sensitive (`governance.py`), so the factory never merges a setup pull request. A person reviews it.
- The factory never installs host-level tools, such as `semgrep`, `trivy` or `gh`.
  It records a missing tool in the run. A lens that needs the tool is skipped.
- A failed setup run does not stop delivery runs. They use the YAML commands or the derived commands.
- `factory setup --repo PATH` runs a setup by hand. `--publish` also commits, pushes and opens the pull request.
- `factory start` checks the repository at each tick when `setup.enabled` and `pull_request.enabled` are on.
  It plans again only when the source HEAD changes. It plans from the checkout first, so most checks create no worktree.
  It does not open a second pull request for the commands and files that it already proposed.
  It records a failed or refused setup, or a failed publication, and does not try the same HEAD again.
  Only one process checks a repository at a time.
  Before it publishes, it checks that only the manifest, the lockfile, `.factory/setup.json` and the files that the plan lists changed.
  A setup problem is logged. It never stops the backlog.

### Repository skills in the target repository

The setup pull request also writes skills that a local agent can use without the factory.
The controller writes these files from fixed templates. No model writes them.

```text
AGENTS.md                                      shared base
CLAUDE.md -> AGENTS.md                         symbolic link
.agents/skills/<name>/SKILL.md                 canonical skill files
.claude/skills/<name> -> ../../.agents/skills/<name>   one link for each skill
.claude/agents/<stack>-quality.md              Claude Code only
```

- `.agents/skills` is the shared folder for GitHub Copilot, Cursor, OpenCode and Cline.
  Claude Code reads `.claude/skills`.
  The `skills` command (`npx skills add`) uses the same layout in its link mode, so later installs do not collide.
- If `AGENTS.md` exists, the factory changes only a block between factory markers. It does not change other text.
  Without markers, the factory adds the block at the end.
  The factory leaves `AGENTS.md` alone and records a note in three cases.
  The markers are not exactly one well-formed block, the line endings are mixed, or the file is not readable UTF-8 text.
  The factory keeps the line endings of the file.
- The factory skips a path that the repository ignores, because Git never commits it. It records a note.
- An add command can change only the manifest and the lockfile. Any other change stops the setup.
  The factory plans the files again after the add commands, and that plan decides the files of the pull request.
- The factory never replaces another file that exists, such as a skill, a link or `CLAUDE.md`.
- The block and the `pr-gate` skill name the install command and the checks of the repository,
  including the tools that the same setup adds.
- Windows checkouts need `core.symlinks=true`.
- The first skills are `pr-gate`, `simplify` and `polish`. The factory owns these templates.
  The Python review agent `python-quality` is adapted from `dev-team`, with attribution.
- A setup pull request can change these files. The path check before publication allows exactly the files that the plan lists.

### Review lenses

- A lens registry (`review_lenses.py`) holds each review lens with a scope and a short checklist.
  The scope is file patterns or `always`.
- A pure function selects the lenses that match the changed files that the controller derived. The `always` lenses come first.
  A repair review uses the same lenses, because the controller derives the files of the whole change.
- The checklists of the selected lenses go into the reviewer prompt.
  A backend-only change gets no user interface lens.
- The polish attempt keeps its repository guidance. It gets no lenses in this slice.
- No model selects lenses.

### Changes to earlier decisions

- ADR-019: this decision reverses the skill research part. A fixed catalog of skill templates returns.
  The profiling rules stay.
- ADR-021: this decision replaces the rule that the factory never writes guidance to target repositories.
  The factory now writes guidance and tools, but only through the setup pull request.
  It still makes no hidden writes. The human overlay stays.
- ADR-020: the polish attempt stays. It keeps its generated guidance for now. A later slice can replace that input with lens guidance.
- This decision does not change the factory runtimes. They do not load the repository skills.
  Copilot has no skill tool in its tool list, and pi runs with `--no-skills`.
  A later decision can change this.

Consequences:

- A new repository needs no YAML commands when its stack is in the registry.
- The setup pull request can be large. The factory can split it into a tools pull request and a skills pull request.
- The registry is code. A new tool or lane needs a factory release.
- Target repositories now contain files that the factory owns.
  A person can change them, and the factory then leaves them alone.

## ADR-033: The dashboard may request a resume

Status: accepted on 2026-10-01 for the data minimization part and the write path part.
This amends ADR-016. It extends ADR-024 and ADR-026. Amended by ADR-035.

### Data minimization

ADR-016 kept failure reasons out of the dashboard.
The operator then had to run `factory show` to learn why a run failed or what it needs.
Issue #80 asks the dashboard to show errors and how to continue.

The dashboard now shows failure reasons for the run, each attempt and each agent call.
It also shows the escalation text that a halted run needs: the approval scope and the decision questions.

- The sanitizer is the single place that redacts this text.
  The detail provider passes raw text, and the request handler sends only the redacted result.
- Redaction comes first. The sanitizer runs the shared secret patterns over the full text before any cut.
  The patterns live in `redaction.py`, a module that imports only `re`.
- A redacted reason longer than 500 characters is cut. The start and the end stay.
  A marker replaces the middle and names `factory show <run>` for the full text.
- The call's reasoning level, such as `high`, is shown. Agent reasoning text is not.
- The snapshot and health carry no failure reasons. The run list carries one reason for each stopped run, as the second amendment below says.
- Command logs, diffs, prompts and raw artifacts stay out, as ADR-016 says.

Amendment on 2026-10-02 (issue #88):

- The sanitizer also redacts the run title, the task titles and the model names.
  This applies to the run list, the run detail, the compare view and the projects view.
  These values are not cut. A value that is not text becomes `null`.
- The sanitizer also redacts the health text. This covers check messages, remediation text, degraded reasons and the report error.
- The shared secret patterns now live in one place, `redaction.py`.
  Other modules call `redact_secrets` from there.
- CI logs, check descriptions and GitHub comment bodies now get the full pattern set.
  Before, they matched GitHub token shapes only. They now also match assignment and header shapes, such as `API_KEY=...` and `Authorization: ...`.
  This is a behavior change. The team accepts it. Local verification output already got these patterns.
- `resume.py` no longer keeps its own credential patterns. It reads the same patterns through `contains_secret`.
- The factory refuses a plan answer that holds a redacted secret.
  A GitHub comment body is redacted before the factory parses it. Without this rule, an answer such as `Authorization: admins only` becomes `[REDACTED]` and passes.
  The dashboard path refuses the same answer.
- When the dashboard cannot read a request file, the log names the exception type only.
  The log does the same when the run detail provider or an action fails.
  The message can quote the field input, and that input can hold a plan answer.

Amendment on 2026-10-02 (dashboard overview):

- The run list shows why a run stopped. A run that failed or needs a person carries its failure reason.
- The sanitizer redacts and cuts this reason in the same way as the reason on the run detail. The cut is at 500 characters.
- The run list also carries the role and the model name of each call. The sanitizer redacts the model names.
- The run page marks the last call before a failure. The factory can reject the output of a call that reported success.
  The page marks the call as rejected when the failure reason names its role. Otherwise it says that the run failed after the call.
- Logs, diffs and prompts stay out.

Consequences:

- Secret patterns that miss a credential shape can now leak it to the browser as well as to logs.
  The dashboard is loopback only and token protected, so the risk stays on the operator's machine.
- `RunDetail` now holds raw reasons. Any other consumer must sanitize them first.

### Write path

ADR-016 made the dashboard read-only. That rule is replaced by two named write actions:

- Approve a risk approval (`RISK_APPROVAL`). The run reopens to `REFINING`, as in ADR-024.
- Answer plan decisions (`PLAN_DECISION`). The run reopens to `PLANNING`, as in ADR-026.

No other route can change a run, a workspace or the configuration.
Retry, cancel, reconfigure and any other write stay banned.
The dashboard cannot reopen a run itself.
It only asks the factory service to do it.

The write path has one writer:

- The dashboard never writes `run.json`. It never calls `save_run`.
- A dashboard action creates one request file in the run directory.
  The name holds the episode and the first 16 hex characters of the context fingerprint.
  The write is create-only, so a second approval of the same context is impossible.
  A changed context has a new fingerprint and so gets a new file.
- The factory service ingests the request.
  It checks the request again, writes the receipt, and reopens the run through `controller.reopen`.
  For plan answers it also writes the answers.
- The service is the only writer of `run.json` and the only writer of the request's `stale` status.
  A request fails the check if the reply window ended, the reopen limit is reached,
  the context changed or the run is no longer waiting.
  The service then marks it `stale` with a reason code.
  If a GitHub reply was accepted first, the request goes stale.
- The reply window is judged when a dashboard request was made.
  A GitHub reply is judged when the poller reads it, as before.
  So a full service or a spent daily quota can make a GitHub reply expire, but never a dashboard request.
- The reopen checks are the ones in ADR-024: reopen limit, quota, concurrency,
  approval context match, and delivery resume for R2 and R3 runs.
  A dashboard request does not need `remote_resume_enabled`.
- The service ingests requests even when GitHub escalation is off.
  If `factory start` is not running, the request waits for it.

Authority for a local approval:

- ADR-024 checks the GitHub comment author. A local approval has no author to check.
- The per-start dashboard token guards the HTTP route only.
  The token is random, per start, printed to stdout and never logged.
  The dashboard binds to `127.0.0.1`, so only a process on the operator's machine can reach the route.
- The factory service never checks the token.
  It trusts any well-formed request file in the run directory.
  The file name is `dashboard-approval-<episode>-<fingerprint prefix>.json`.
- So the real authority is write access to `<data_dir>/runs`.
  Anyone who can write there can approve a run.
  Anyone who can write `run.json` can already do the same.
- The control that stops an implementer agent from approving its own R2 or R3 risk is the workspace.
  Agents work in their own workspace, and the factory does not give them the data directory.
  This is not a sandbox. The code sets the working directory and does not block other paths.
- The receipt records the source `dashboard` and the login `dashboard-local`.
  It also records the time and the context fingerprint, and the service writes a log event.
- A `POST` also needs the token in a header, an exact `Origin` and a JSON body of at most 16 KB.
  Other write methods return `405`.
- The page link that `factory dashboard` prints holds the token in its query.
  The first request for that link gets the page with status `200`.
  The response also sets a session cookie.
  The page script then removes the query from the address bar and from the current history entry.
  It keeps the fragment. A reload then works from the cookie.
- The server does not redirect the link.
  A `303` leaves the first address in the history of Chrome and Firefox.
  A browser can also drop a `SameSite=Strict` cookie that arrives on a redirect after a click from another site.
  The first open then gets `401`.
- The removal is best effort.
  The page removes the token from the address bar and the current history entry.
  Some browsers can still keep the first address in their visit records until the dashboard restarts.
  The old token has no use after a restart.
- The response for the link is never cached, and it sends `Referrer-Policy: no-referrer`.
- The cookie is `HttpOnly`, `SameSite=Strict` and `Path=/`.
  It has no `Max-Age`, so it ends with the browser session.
  It has no `Secure` flag, because the server speaks plain `http` on loopback and a browser can drop a `Secure` cookie over `http`.
  Its name ends with the port, because a browser shares cookies between the ports of one host.
- Only the page route accepts the token in its query, and only for this first request.
  A `GET` for the page, an asset or the API needs the cookie or the token header.
  A token in the query of any other route never counts.
  A `POST` still needs the token header, so the cookie alone cannot write.
- A `GET` that has the token header uses the header alone.
  A wrong header gets `401`, even when the cookie is right.
  The cookie counts only when the header is absent.
  After a restart on the same port, a tab of the old start still sends its old token in the header.
  This rule stops that tab from working halfway.
- The page holds the token in a `<meta>` tag, and the script sends it in the header for a write.
  The server renders the page only for a request that holds the token.
  So the address bar, the current history entry and referrers hold no token.
  `HttpOnly` keeps the cookie from script, but script that runs in the page can read the `<meta>` tag.
  The content security policy allows only scripts from the dashboard itself.
- After a restart the server has a new token, so the old cookie gets `401`.
  The operator opens the new link from `factory dashboard`.
- The cookie is as strong as the token.
  Any program that holds the cookie can load the page and read the token from the `<meta>` tag.
  A browser counts all ports of `127.0.0.1` as one site, so it sends the cookie to every port.
  So another local web server on `127.0.0.1` that the browser visits can receive it and reuse it.
  The `Host` check and the `Origin` check on a write stop a page in a browser only.
  A program that is not a browser can send any `Host` and `Origin` header.
  The team accepts this risk: the trust boundary is anything that listens on `127.0.0.1`.
  That includes a forwarded port, such as `ssh -L`, a container port or an editor port forward.
  Do not forward a port to `127.0.0.1` from a host you do not trust while the dashboard runs.
  Anyone who can read the cookie store of the browser can also approve while that dashboard runs.
- The terminal still shows the link while it stays on screen.

Consequences:

- Anyone who can write the run directory can approve a run, with or without the token.
  The risk is the same as for anyone who can write `run.json`.
- Receipts and plan answers carry a `source` field. Models use `extra="forbid"`.
  The factory writes `source` only for dashboard receipts and answers. GitHub is the default and is left out.
  After a dashboard receipt exists, rolling back to code without `source` needs a hand edit of `run.json`.
  `plan-decision-answers.json` in the run directory can also hold `source: dashboard`. It needs the same edit.
  Older code ignores the `dashboard-approval-*.json` request files.
- The `created_at` of a request decides its reply window.
  Ingest marks a request stale as `expired` if it is older than its escalation or newer than the service clock.
  Back-dating a request needs write access to the data directory, which can already edit `run.json`.
  The slice 4 route stamps `created_at` from the server clock and never reads it from the request body.
- Slice 3 of issue #80 added the request file and the service ingest.
  Slice 4 added the HTTP routes for the two actions.
  Before slice 4, the dashboard was read-only and no route created a request.

See also ADR-016, ADR-024 and ADR-026.

## ADR-032: pi implementer shell commands match the Copilot deny list

Status: accepted on 2026-09-30. This amends ADR-031.

ADR-031 left the pi implementer with an unrestricted `bash` tool.
Pi has no approval layer, so the implementer was able to run `git commit`, `git push`, `gh`, `curl` or `wget`.
The Copilot runtime denies these commands.
The factory printed a startup warning and issue #70 tracked the gap.

The factory now gives pi the same limits as Copilot, and no more.

- The implementer loads the command filter, a pi extension that the factory owns (`command_filter.mjs`).
  The factory passes it with `-e`. Other extension discovery stays off.
- The command filter blocks each `bash` call that runs `git commit`, `git push`, any `gh` command, `curl` or `wget`.
  It finds them in compound commands, in `sh -c` and `bash -c` bodies, after `env` and `command` prefixes and after git global options.
- The agent gets a reason that says what to do instead and not to retry.
- The list matches the Copilot implementer deny list. Pi blocks `curl` and `wget` in place of the Copilot `url` deny.
  A test keeps the two lists in step.
- If the packaged filter file is missing, the implementer call fails before pi starts.
- If pi cannot load the extension, pi exits with an error. If the filter code throws an error, pi blocks the tool call.
  Both behaviors were verified on pi 0.99.1.
- Not covered: a future pi version that changes or ignores the `tool_call` block result.
- The factory removes `SSH_AUTH_SOCK` from the pi child environment, so git over SSH cannot use ssh-agent keys.
  Key files and HTTPS credential helpers are out of scope, as with Copilot.
- Read-only roles have no `bash` tool on pi. This does not change.
- The startup warning for `--runtime pi` is removed.

There is no operating system sandbox. Copilot has none either.

Rejected options:

- A macOS `sandbox-exec` profile around pi.
  It goes beyond Copilot.
  Git index writes fail unless `.git/worktrees` and `.git/objects` are writable.
  It needs deny rules for ssh-agent and the Keychain.
  It also needs macOS in CI.
- Docker. It needs an image, a way to pass credentials and a boot cost for every call.

Consequences:

- pi and Copilot have the same command limits.
- The filter is pattern-strength, like the Copilot rules. It is not a security boundary.
- These are examples of what the filter does not stop. The list is not complete:
    - Script files that run the commands.
    - Wrappers such as `sudo`, `xargs`, `exec` and `nohup`.
    - Shell keywords such as `if`, `then` and `{ }`.
    - Git aliases, such as `git -c alias.p=push p`.
    - Git plumbing, such as `git send-pack` and `git update-ref`.
    - `eval` and `source`.
    - Interpreter one-liners, such as `python -c` and `node -e`.
    - Network clients other than `curl` and `wget`, such as `nc`, `ssh` and `git fetch`.
- Heredocs are not a bypass. The filter checks heredoc bodies as commands, so a blocked command in a heredoc fails closed.
  Agents write files with the write tool.
- The filter depends on the pi `tool_call` extension API. It was verified on pi 0.99.1.
- `factory doctor` requires pi 0.99.1 or later, the version the filter was verified on.
- The filter tests need Node 22 or later.

## ADR-031: pi is a recommended agent runtime

Status: accepted on 2026-09-30. This amends ADR-017 and ADR-022.

*Amended in part by [ADR-032](#adr-032-pi-implementer-shell-commands-match-the-copilot-deny-list):
the pi implementer no longer has an unrestricted `bash` tool. The rest of this decision still stands.*

The factory can now run agents with pi (`--runtime pi`) as well as with the Copilot CLI.
Every command that takes `--runtime` accepts `pi`.
Both runtimes use the same configured model and reasoning level for each role.

How the factory calls pi:

- Each agent call starts one `pi --mode rpc` process and ends it after the call.
- Each role gets a fixed tool list. The implementer gets `read`, `bash`, `edit`, `write`, `grep`, `find` and `ls`.
  Read-only roles get `read`, `grep`, `find` and `ls`.
- Only the implementer and the reviewer continue a session.
  A repair round or a re-review continues the session and sends only the prompt sections that are new or changed.
  A change of model, provider or reasoning level starts a new session.
  A failed last call, an old session or a missing session file also starts a new session.
  Triage, refiner, researcher, planner and tester always start without a session.
- Session files live under `<factory.data_dir>/pi-sessions`, readable only by the owner.
  Old files expire and the factory removes them.
- `factory doctor --runtime pi` checks the pi executable, the pi version, the Node version and the provider credential, in that order.

What pi does not do yet:

- Repository skill generation needs web research, and pi has no web tool.
  `factory skill refresh --runtime pi` fails and names `--runtime copilot`.
  The optional polish step after a green run is skipped on pi.
- pi has no equivalent of the Copilot `context_tier` setting, so the factory ignores it.

Spike result (2026-09-28, pi 0.84.4, provider `github-copilot`):

- pi reports cache reads for Claude and OpenAI models, in one process and after a resume from a session file.
- pi reports cache writes for Claude models only.
- The raw result is in `scripts/performance/pi_cache_probe_results.json`.

Benchmark result (2026-09-30):

`scripts/performance/runtime_ab.py` replayed three small merged changes (#59, #72, #77) on both runtimes.
The manifest is `scripts/performance/runtime_ab_manifest_tiny.json`, so the run can be repeated.
Both runtimes ran each task at the same time, with the same model for each role.
The benchmark config mapped every worker level to one model and accepted review debt at no risk level.
So a different triage level did not change the model or the review rules.
Triage agreed on all three tasks.

| Total | Copilot | pi |
| --- | --- | --- |
| Passed | 3 of 3 | 3 of 3 |
| Input and cache write tokens | 276,576 | 111,637 |
| Cache read tokens | 649,701 | 263,351 |
| Output tokens | 20,070 | 14,632 |
| Cache read share | 70.1% | 70.2% |
| Wall time | 415 s | 271 s |

The plan set a go bar for the word "recommended".
pi had to pass at least as often as Copilot and at least once.
Its input and cache write tokens had to stay at or under 80% of Copilot's.
Its cache read share also had to be at least 15 points above Copilot's.

The factory drops the cache read share rule.
pi sends fewer tokens, so it also reads fewer tokens from the cache.
An equal share then still means fewer tokens in total.
The report still shows the share, but the go bar does not judge it.
With the two remaining rules, pi meets the go bar, so the docs call pi recommended.

Three tasks is a small sample.
The report marks a task where the two triage results differ. The runs of such a task can use different models.

Amendment to ADR-017:

pi reports a price for each call from its own model list.
The factory stores it as `list_price_estimate_usd`.
It is an estimate at list price, not spend, because the Copilot plan pays for the calls.
Every view labels it as an estimate.
The factory never adds it to Copilot premium requests or to the Copilot usage value.
The factory does not keep its own price table. A missing price stays unknown.
The dashboard calls a missing value "unknown", and the probe and the benchmark report call it "unavailable".
Both words mean the same thing.

Amendment to ADR-022:

Under `--runtime copilot` the implementer cannot run `git commit`, `git push` or `gh`.
Under `--runtime pi` this decision first left the implementer with an unrestricted `bash` tool.
[ADR-032](#adr-032-pi-implementer-shell-commands-match-the-copilot-deny-list) replaced that with a command filter.
The factory does not refuse pi when merge or pull request delivery is enabled.
An agent with a shell has many other ways to reach the network. A refusal does not protect much.

## ADR-030: Risk assessment can be disabled

Status: accepted on 2026-09-29.

The risk model stops a run at `NEEDS_HUMAN` when the risk level is `R2` or `R3`.
Triage must also write a `risk_rationale` for these levels.
In a benchmark, both agent runtimes rated a local tooling cleanup as `R2`.
The run stopped with "risk R2 requires human approval".

Some operators want an autonomous factory.
They do not want the approval gate or the rationale.

The factory now has the `risk_assessment.enabled` setting.
The default is `true`, so the behavior and the prompts do not change.
The `--no-risk-assessment` option turns the setting off for one invocation.
It exists on `factory run`, `factory project`, `factory start` and `factory service install`.

When the setting is `false`:

- Triage still returns `risk`, because routing uses it.
- Explicit work item risk still raises the lowest route option, as before.
- No risk level needs human approval, in the controller and in route option checks.
- The factory creates no risk escalation and no approval request.
- The triage prompt does not ask for a `risk_rationale`.
- A triage result for `R2` or `R3` without a rationale is valid.

These rules stay active in both modes:

- Ineligible work items still stop for a human.
- Protected file, scope and verification gates still apply.
- Independent review still applies.
- Route ratchets still upgrade a route after a failure.

The rule "`R2` or `R3` needs a `risk_rationale`" moved out of the `TriageResult` model.
The controller now checks it after each triage call.
It applies the check only when the setting is `true`.
A missing rationale is then a structural failure with one retry, as before.
The model no longer rejects the result, so a triage stored without assessment loads again.

A run keeps the choice it started with.
The controller reads `risk_assessment_enabled` from the run for every gate check, also on resume and reopen.
A project stores the same choice, and a resumed project starts its remaining tasks with it.
So one project never runs under two policies.
A resume with `--no-risk-assessment` cannot remove an approval that a run already needs.

Each run stores `risk_assessment_enabled`.
The controller logs a warning when a run starts with the setting off.
`factory status --json` and the dashboard show the value, so an operator can audit it.

This trades a human check on sensitive work for autonomy.
The operator accepts that trade when they turn the setting off.
Runs that need a human check must keep the default.

## ADR-029: Writing rules are advisory

Status: accepted on 2026-09-29. This amends ADR-023. Amended by ADR-036.

ADR-023 made the writing policy a gate. A prose finding failed the result, and
the factory sent one correction prompt. Publication text failed before Git or
GitHub mutation.

A benchmark showed the cost of that gate.
Both agent runtimes failed triage twice on `requirements_quality`.
The field had a hidden limit of 12 words that the prompt did not state.
The retry prompt also did not carry the correction, so the retry had no way to fix it.
A wording rule stopped the run and spent tokens without a better result.

The factory now treats writing rules as advice.

- The prompt states the rules, the word limit of each field and some filler words.
- The controller checks each successful result and logs the findings.
- The controller stores the findings in `writing_findings` on the invocation record.
- A writing finding never fails a result, never causes a retry and never blocks a run.
- Publication text findings are logged. The factory still publishes the text.
- Blank publication text is still an error.
- Blank agent text is also an error. Model validation rejects it, so it takes the ordinary retry.
- Retries stay for structural failures. Examples are invalid JSON, schema errors and missing data.

One table in `writing_policy.py` holds the word limits.
The check and the prompt both read it, so they cannot differ.

Triage, refiner and researcher retries now receive the failure reason.
Before this change these three roles dropped it.

The factory removes `requirements_quality` from `TriageResult`.
No decision used it.
Old `triage.json` files that contain the key still load.

The `CORRECT_CHANGE_SET` purpose stays in the model, because old run files can name it.
The controller no longer starts it.

This trades a guaranteed style for lower cost and fewer failed runs.
Operators can read the logged findings and improve the prompts.

## ADR-028: Pull requests must add no SonarCloud issues

SonarCloud analyses every pull request.
The built-in "Sonar way" quality gate only checks ratings, coverage, duplication, and hotspot review.
A pull request can add new issues and still pass that gate while the ratings stay at A.
This happened on pull request 55, which added two issues and passed.

The SonarCloud free plan cannot assign a custom quality gate to a project.
The API refuses the change with a 403 error.

So CI enforces the rule instead.
The `sonar-new-issues` job runs on pull requests only.
It waits for SonarCloud to finish its analysis of the pull request head commit.
Then it asks the public SonarCloud API for open or confirmed issues on that pull request.
It fails when the count is not zero, or when the response is not what it expects.
`ci-gate` requires this job on every branch except `main`.
So a manual run on a pull request branch cannot skip it.

This makes merges depend on SonarCloud.
If SonarCloud is down or does not analyse the commit within 15 minutes, the job fails.
Re-run the job when SonarCloud recovers.
Do not mark real issues as Accepted to pass the job.
Fix them, or mark a confirmed false positive in code with `# NOSONAR(<rule>)` and a reason.

## ADR-027: Adaptive Jev-driven execution routing

Amended by ADR-035.
Amended by ADR-037.

Simple tasks do not always need the full factory pipeline.
When enabled, Jev acts as the single semantic if/else router.
Jev is a classifier from TypeSafe.
The factory calls it over HTTPS.
System One is the TypeSafe product that serves Jev.
It selects one controller-offered Choice option with probabilities and confidence.

The controller keeps exclusive authority over validation, safety floors, state transitions, and route execution.

The factory defines four configured routes: `SINGLE`, `CRITIQUE`, `FULL`, and `MANUAL_TRIAGE`.
`FULL_REVIEW` is a controller-only post-implementation route.
Route controls workflow stages.
Model profile controls worker strength.
Worker model escalation is the existing cascade behavior in the factory.
We do not add a duplicate cascade route.

Deterministic safety floors constrain offered options before the factory calls Jev.
The controller enforces floors for risk, missing acceptance criteria, missing verify commands, required research, protected files, and high complexity.
When only one legal option exists, the controller skips Jev.

When routing is enabled, the controller makes one HTTPS request to Jev.
Jev chooses one controller-created option identifier.
Strict validation checks the model identifier, choice answer, option membership, probabilities, and confidence thresholds.
When routing is disabled, unavailable, or invalid, the controller falls back to the first legal `FULL` option, or `MANUAL_TRIAGE` if no legal `FULL` option exists.
The packaged default disables routing and makes no network call.

For `SINGLE` and `CRITIQUE` routes, the controller synthesizes triage, specification, and execution plan artifacts.
These artifacts record explicit `SYNTHESIZED` provenance.
`SINGLE` runs the Implementer and deterministic verification.
`CRITIQUE` runs the Implementer, deterministic verification, and the independent Reviewer.
`FULL` runs the complete multi-agent pipeline.
It retains triage, refinement, optional research, planning, implementation, verification, optional polish attempt, Tester, and Reviewer.

We amend the independent review rule narrowly.
Deterministic verification can accept `SINGLE` only when every configured sufficiency condition holds.
All other work requires independent model review.

Monotonic ratchets protect execution safety.
When verification fails, the controller upgrades `SINGLE` to `CRITIQUE`.
Post-implementation ratchets upgrade `SINGLE` or `CRITIQUE` to `FULL_REVIEW`.
`FULL_REVIEW` runs full independent Tester and Reviewer gates without restarting earlier stages.
The controller never downgrades a route.

Read the [adaptive routing guide](guides/adaptive-routing.md) for setup details.

## ADR-026: Authorized answers for unresolved plan decisions

ADR-025 stops work before an agent guesses a material decision.
Stopping permanently makes that gate hard to use.

When GitHub escalation is enabled, the factory lists each unresolved decision
with a number. An authorized contributor can reply with every numbered answer.

```text
@factory answer v1 run=<run-id> episode=<episode-id>
1. First decision answer.
2. Second decision answer.
```

The controller checks the author, target, run, episode, reply window, comment
edit state, and response order. It stores the typed answer artifact before it
reopens the run.

The controller is the only component that can reopen work. It returns a valid
plan-decision reply to `PLANNING`. The Planner receives only validated answer
fields. It creates a replacement plan. The controller checks readiness before
it starts implementation.

This flow does not rerun triage, refinement, or research. It does not change
scope, models, commands, quality gates, or retry budgets. Existing reopen
limits bound the number of accepted answer cycles.

`RISK_APPROVAL` keeps the separate `@factory resume` reply and returns to
`REFINING`. Project child runs keep their current `NEEDS_HUMAN` behavior.

## ADR-025: Pre-implementation readiness gate for unresolved decisions

Autonomous software development can fail when an agent guesses answers to missing decisions.
To evaluate patterns for scaling delivery by uncertainty, we inspected external evidence from [BMAD-METHOD](https://github.com/bmad-code-org/BMAD-METHOD).
We reviewed commit `94b6727b00c8316557828c8a8ff2a48ff60d60cc` from 2026-09-11 and newest observed tag `v6.12.0`.

The factory adds an optional `unresolved_decisions` list of concise strings to `ExecutionPlan`.
It records only material choices.
These choices cannot be derived from task intent, Specification, repository evidence, or existing constraints.

The controller evaluates the initial plan before implementation begins.
If unresolved decisions exist, the controller asks the Planner once more to resolve evidence-answerable items.
If material choices remain after this single retry, the controller persists the final plan.
The controller then halts before implementation in `NEEDS_HUMAN`.
It records a dedicated escalation record that points to `execution-plan.json`.

This gate applies only to the initial pre-implementation plan.
It does not reject the metadata-only scope replan after deterministic verification.

ADR-026 allows an authorized contributor to answer these decisions through
GitHub escalation. The controller then returns the same run to planning.
Project child runs preserve their existing `NEEDS_HUMAN` behavior.

We defer a project-level architecture decision registry and acceptance-to-test mapping.
Existing dependency graphs, typed artifacts, repository skills, deterministic verification, independent testing, and bounded review already provide equivalent value.

We reject model-owned workflow state and agent-owned Git commits, reverts, or retries.
We reject linear story scheduling instead of dependency graphs.
We reject same-workflow self-approval.
We reject synchronous all-agent coordination and interactive persona workflows on the autonomous path.
We reject mutable-main update checks, large Markdown artifact sets, plugin architectures, and prose-only governance.

BMAD is licensed under the MIT License, but its trademarks are excluded.
This factory uses independently expressed concepts and copies no BMAD code, templates, or prose.

## ADR-024: Controller-owned GitHub escalation and authorized human reply loop

Amended by ADR-035.

When a run enters `NEEDS_HUMAN`, the factory can notify a human operator on GitHub.
This behavior is opt-in and disabled by default.

The factory posts a status comment on the open factory pull request when present.
If no open pull request exists, the factory falls back to the source issue.
The factory never searches GitHub for arbitrary linked pull requests.

The comment uses only fixed guidance fields.
The factory never publishes raw failure text, workspace paths, issue descriptions, diffs, or logs.
Each notification includes an unpredictable episode token and a hidden marker.

For a risk approval, an authorized human can reply on the same thread with:

```text
@factory resume v1 run=<run-id> episode=<episode-id>
```

The factory polls replies on the exact thread.
The comment author must be an authorized human user.
The author must have an allowed association such as `OWNER`, `MEMBER`, or `COLLABORATOR`.
The factory rejects bots, edits, and its own account.

The controller re-fetches the comment before acceptance to detect edits.
The factory records an accepted reply receipt before reopening.
The receipt stores comment metadata and never stores raw reply bodies.

The controller reopens the same run. It never creates a replacement run or
resets attempt records. `RISK_APPROVAL` transitions to `REFINING`.
ADR-026 adds a separate complete numbered-answer reply for `PLAN_DECISION`.
It transitions to `PLANNING`. Other halt categories remain non-resumable.

When risk requires approval, the notice explains the causal risk chain.
Triage records this case-specific causal rationale in its typed contract.
The rationale covers the intended outcome and the sensitive boundary.
It explains why that operation is necessary for the task.
It details the credible failure scenario, mitigations, and residual risk.

The controller snapshots this decision context in the escalation record.
It persists the record before notification.
The GitHub notice presents explicit SimpleEnglish sections.
The sections state why approval is required and state the requested decision.
The notice specifies authorized actions and explicit exclusions.
Approval authorizes moving the same run to `REFINING`.
Approval does not change task scope or retry budgets.
Approval does not bypass quality gates or alter permissions.
Approval does not change deployment policy or merge policy.
All verification and review conditions remain in force.
A risk approval escalation without valid decision context fails closed.
An oversized notice disables remote resume and closes the reply cursor.
The operator must inspect local artifacts in those cases.

The approval context fingerprint binds each displayed decision and authority field.
The accepted reply receipt records this exact fingerprint.
The controller verifies the receipt fingerprint against the persisted contract before reopening.

A resume at a delivery checkpoint does not ask for a second approval.
The controller authorizes an `R2` or `R3` run only when a dispatched accepted receipt matches.
The receipt fingerprint must equal the approval context rebuilt from the persisted work item and triage.
A change to an approved field after approval revokes it.
A release that changes the fixed approval text makes older approvals fail closed.

Reopened work uses the same executor, concurrency limit, and daily run quota.
The backlog filter continues to block the source issue from fresh dispatch.

## ADR-023: Enforce concise controlled writing

Amended by ADR-029 and ADR-036.

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

ADR-029 replaces this rule. Writing findings are now advisory.

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

Amended by ADR-035.

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

ADR-033 amends the data minimization rule below: the dashboard now shows
redacted failure reasons. ADR-033 also replaces the read-only rule below with
two named write actions that only create a request file.

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

*Superseded in part by [ADR-034](#adr-034-the-factory-sets-up-the-target-toolchain-and-repository-skills):
a fixed catalog of skill templates replaces skill research. The profiling rules stand.
Superseded by ADR-034 for skill generation: the factory no longer generates skills with a model.*

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

*Superseded by [ADR-034](#adr-034-the-factory-sets-up-the-target-toolchain-and-repository-skills):
the factory writes guidance and tools to the target repository through a setup pull request.
The polish attempt uses fixed guidance. Generated guidance and the human overlay are removed.*

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
