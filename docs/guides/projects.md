# Project briefs and generated work

Use `factory project` when the input is a product or feature description rather
than one implementation-ready issue.

```bash
uv run factory project \
  --repo ~/projects/example \
  --title "Build customer onboarding" \
  --description "Add signup, email verification, and the first-login flow." \
  --acceptance-criterion "A new customer can complete onboarding." \
  --constraint "Reuse the existing authentication service." \
  --runtime copilot
```

The project planner runs read-only against the repository and returns a typed,
bounded `ProjectPlan`. It is explicitly instructed to choose the **fastest
sufficient solution**:

- use one task only for one bounded change that can be implemented, tested,
  reviewed and merged as one pull request
- split only for an independently verifiable outcome, a hard prerequisite,
  safe parallel execution, or a scope limit
- treat dependencies as integration/merge-before-start gates; omit them when
  tasks are safe to run concurrently in isolated worktrees
- reuse existing code and boundaries
- do not manufacture separate setup, testing, documentation or cleanup tasks
- do not add speculative abstractions, dependencies, services or infrastructure

The factory validates task numbering, dependency direction, focused acceptance
criteria, and the maximum size of a single-task description before any issue or
implementation starts. A plan may contain at most 12 tasks. A rejected
decomposition gets one bounded correction attempt with the deterministic
reason; it cannot loop or continuously replan.

## Execution

The default is local-only integration. For PRs, CI repair and automatic
delivery to the target branch, use [autonomous delivery](#autonomous-delivery).

Each generated task becomes an ordinary `WorkItem` and passes through the full
factory pipeline. Ready tasks execute in waves using
`scheduler.max_concurrent_tasks`, which remains bounded to `1` or `2`.

The factory keeps a persistent project integration worktree. After a child run
passes verification and review, its local commit is cherry-picked onto that
branch. Dependent tasks therefore start from predecessor changes rather than
from the original repository revision. After all commits are composed, the
factory runs the configured deterministic repository commands once more
against the complete integration branch.

The project stops when:

- every task is integrated (`DONE`);
- a child reaches `NEEDS_HUMAN` or `FAILED`;
- task integration conflicts; or
- GitHub issue creation fails.

If issue closure fails after a task is integrated, the project records a
warning and continues. A failure in final integration-branch verification
produces `NEEDS_HUMAN`.

It never generates an unbounded stream of follow-up work.

## Optional GitHub issues

Add a repository to create one issue per validated task:

```bash
uv run factory project \
  --repo ~/projects/example \
  --github-repo acme/example \
  --title "Build customer onboarding" \
  --description "Add signup, email verification, and the first-login flow." \
  --runtime copilot
```

Issue bodies include acceptance criteria, constraints, predecessor issue links,
and a stable project/task marker. An issue closes after its task is integrated.

The command intentionally does **not** add the scheduler's `agent-ready` label.
The local project runner owns execution; allowing `factory start` to claim the
same generated issues would duplicate work.

## Artifacts and result

```text
<data_dir>/projects/<project-id>/
├── project-brief.json
├── project-plan.json
├── execution.json
└── logs/
```

The command prints the selected delivery approach, task count, child run ids,
issue URLs when enabled, and the final integration worktree and branch.

Local execution uses `pull_request.enabled: false`, `ci.enabled: false` and
`merge.enabled: false`. It produces one local integration branch.

Reusing an existing project ID without `--resume` is refused so immutable
planning artifacts cannot be overwritten.

The concurrency cap applies per factory process. Do not run a backlog daemon
and a project command against the same repository when their combined
concurrency would exceed the repository's safe local capacity.

## Autonomous delivery

This is the opt-in path for "give the factory a project and inspect the result
on `main`". Each task goes through local verification, independent review, a
PR, CI observation and bounded code/test repair, then a guarded automatic
merge. Only a confirmed merge releases the next task.

**Every PR revision must be approved by the configured independent Reviewer.**
CI repair changes go through verification, Tester and Reviewer again. The
controller binds approval to the published commit; green CI alone is not
permission to merge. The latest review outcome is included in the PR body.
This does not replace any additional GitHub approvals required by branch rules.

Publication also binds the exact parent and records the controller-created
commit before pushing. An agent-created commit, including an empty commit or
history that adds and then removes a file, is not adopted as reviewed work.

Copy `config/factory.example.yaml` to an operator-owned configuration outside
the target repository, then edit these sections. This is a fragment, not a
standalone configuration: custom YAML does not merge with packaged defaults.

```yaml
repository:
  branch_prefix: "factory/"
  command_timeout_seconds: 900
  commands:
    install: ["uv sync --locked"]
    verify:
      - "uv run --no-sync ruff check ."
      - "uv run --no-sync ruff format --check ."
      - "uv run --no-sync ty check"
      - "uv run --no-sync pytest --cov=jira_glory_mapper --cov-fail-under=90"
    build: []
scope_drift:
  max_replans: 1
  approved_sensitive_files:
    - "pyproject.toml"
    - "uv.lock"
    - ".github/workflows/ci.yml"
pull_request:
  enabled: true
  remote: "origin"
  base_branch: "main"
  draft: false
  allowed_hosts: ["github.com"]
ci:
  enabled: true
  poll_interval_seconds: 30
  max_wait_seconds: 1800
  repair_attempts: 3
merge:
  enabled: true
  method: "squash"
  allowed_repositories: ["acme/jira-glory-mapper"]
  required_checks: ["quality", "tests"]
```

Replace the repository, package and check names with your actual values.
Declare Ruff, ty, pytest and pytest-cov in the target's development dependency
group and require the same commands in GitHub Actions. Bootstrap can be part
of the first functional task: the explicitly authorized manifest, lockfile and
workflow paths must also appear in the execution plan's step file lists.
Authorization does not waive risk approval, ordinary scope limits, protected
files or independent review.

```bash
uv run factory project \
  --repo ~/projects/jira-glory-mapper \
  --project-id jira-migration-v1 \
  --title "Safe Jira field migration" \
  --description "Implement the migration requirements in docs/migration-spec.md. Include the Python toolchain and CI in the smallest sufficient functional work item." \
  --acceptance-criterion "Ruff, ty and pytest with at least 90 percent application coverage pass." \
  --acceptance-criterion "Tests are offline, dry-run is the default, and repeated application cannot corrupt fields." \
  --constraint "Do not access live Jira or execute a migration." \
  --runtime copilot \
  --config ~/factory-jira.yaml
```

The target must already be a Git repository with an initial commit and the
configured remote/target branch. Remote tasks execute serially, regardless of
the local wave-concurrency setting. Before each task the factory fetches the
target and fast-forwards its dedicated integration worktree. It never resets
or switches your source checkout. Each task's PR URL and merged commit are
persisted; final verification runs on the composed target checkout.

Required check names must be present and successful on the exact reviewed
head and must also be enforced by the target's active GitHub protection
policy. Configure those branch rules before starting autonomous delivery;
require PRs, an up-to-date branch, and enforcement without administrator/ruleset
bypasses. The factory will not create or weaken those rules.
A skipped/missing check, changed head, conflicting or outdated PR, or
branch protection that requires human approval blocks merging. No
administrator override, force push or autonomous deployment is performed.
Retry exhaustion stops the project with evidence instead of inventing more
work. Intermediate PR merges are not rolled back if a later task fails.

For an existing implementation used as reference, commit sanitized reference
notes or selected code in the target repository. There is still no implicit
sibling-repository access grant. Never copy credentials, production exports or
runtime state. Merging a migration tool is not permission to run it on Jira.

## Explicit recovery

```bash
uv run factory project \
  --repo ~/projects/jira-glory-mapper \
  --project-id jira-migration-v1 \
  --resume \
  --runtime copilot \
  --config ~/factory-jira.yaml
```

Resume loads the stored brief and immutable plan, uses the saved child run
identifiers, and reconciles task outcomes before dispatching anything new.
Delivery policy and repository identity must still match. Confirmed completed
tasks are not implemented again, and PR delivery checkpoints reuse existing
artifacts, reviews and retry budgets. No fresh planner call or duplicate PR is
needed for a safe delivery checkpoint.

If publishing was interrupted, the saved commit receipt identifies the only
commit that may be pushed. Recovery never infers approval from a matching tree
at an arbitrary `HEAD`.

An interrupted implementation before a safe checkpoint remains ambiguous and
stops at `NEEDS_HUMAN`; resume does not spend a fresh retry or reopen an
exhausted terminal run. Local integration conflicts and uncertain workspace
state are also preserved rather than discarded.
