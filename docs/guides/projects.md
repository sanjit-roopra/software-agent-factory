# Project briefs and generated work

When the input is a product or feature description, use `factory project`.
Do not use it for one implementation-ready issue.

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
bounded `ProjectPlan`. It is instructed to choose the fastest sufficient solution:

- Use one task for one bounded change that you can implement, test, review,
  and merge as one pull request.
- Split work only for an independently verifiable outcome, a hard prerequisite,
  safe parallel execution, or a scope limit.
- Treat dependencies as gates to merge before starting new tasks. Omit dependencies
  when tasks can run concurrently in isolated worktrees.
- Reuse existing code and boundaries.
- Do not create separate tasks for setup, tests, documentation, or cleanup.
- Do not add speculative abstractions, dependencies, services, or infrastructure.

The factory validates task numbering, dependency direction, focused acceptance
criteria, and description size before an issue or implementation starts.
A plan can contain at most 12 tasks. A rejected decomposition gets one bounded
correction attempt with the deterministic reason. It cannot loop or continuously
replan.

## Execution

The default is local-only integration. For pull requests, CI repair, and
automatic delivery to the target branch, use [autonomous delivery](#autonomous-delivery).

Each generated task becomes an ordinary `WorkItem` and passes through the full
factory pipeline. Ready tasks execute in waves using
`scheduler.max_concurrent_tasks`. This setting is bounded to `1` or `2`.
The factory adds sibling task titles to each child `WorkItem` as hard scope
boundaries. This prevents a task-level plan or scope replan from absorbing work
assigned to a later issue. Execution-plan scope entries must be repository-relative
path prefixes, not conceptual component names. The implementer can add supporting
files without replanning when changes stay within the planned area.
The estimated file range in the plan is advisory. The configured limit for
changed files in the repository remains the hard publication limit.

The factory keeps a persistent project integration worktree.
After a child run passes verification and review, the factory cherry-picks its
local commit onto that branch. Dependent tasks start from predecessor changes
instead of the original repository revision.
After all commits are combined, the factory runs the configured deterministic
commands on the integration branch.

The project stops when:

- Every task is integrated (`DONE`).
- A child run reaches `NEEDS_HUMAN` or `FAILED`.
- Task integration produces a conflict.
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

The command intentionally does not add the `agent-ready` label.
The local project runner owns execution. Allowing `factory start` to claim the
same generated issues duplicates work.

## Artifacts and result

```text
<data_dir>/projects/<project-id>/
├── project-brief.json
├── project-plan.json
├── execution.json
└── logs/
```

The command prints the delivery approach and task count.
It also prints child run identifiers and enabled issue URLs.
Finally, it prints the integration worktree and branch.

Local execution uses `pull_request.enabled: false`, `ci.enabled: false`, and
`merge.enabled: false`. It produces one local integration branch.

Reusing an existing project ID without `--resume` is rejected to prevent
overwriting planning artifacts.

The concurrency cap applies per factory process.
Do not operate a backlog daemon and a project command against the same repository.
Their combined concurrency can exceed safe local capacity.

## Autonomous delivery

This is the opt-in path to run a project and inspect the result on `main`.
Each task goes through local verification, independent review, a pull request,
CI observation, bounded repair, and an automatic merge.
Only a confirmed merge releases the next task.

The configured independent Reviewer must approve every pull request revision.
CI repair changes go through verification, Tester, and Reviewer again.
The controller binds approval to the published commit. Green CI alone is not
permission to merge. The latest review outcome is included in the PR body.
This does not replace additional GitHub approvals required by branch rules.

Publication also binds the exact parent and records the controller-created
commit before push. The controller does not adopt an agent-created commit
as reviewed work. This rule applies to an empty commit or history that adds
and removes a file.

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
of the first functional task.
The explicitly authorized manifest, lockfile, and workflow paths must also appear
in step file lists of the plan.
Authorization does not waive risk approval, scope limits, protected files,
or independent review.

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

The target must be an existing Git repository with an initial commit and the
configured remote target branch. Remote tasks execute serially, regardless of
local wave concurrency. Before each task, the factory fetches the target and
fast-forwards its dedicated integration worktree. It never resets or switches
your source checkout. The factory persists the PR URL and merged commit for
each task. Final verification runs on the composed target checkout.

Required check names must be present and successful on the exact reviewed
head. The active GitHub protection policy of the target must also enforce those
checks. Configure those branch rules before you start autonomous delivery.
Require pull requests, an up-to-date branch, and enforcement without
administrator bypasses. The factory will not create or weaken those rules.
Several conditions block merging: a missing check, a changed head, a merge
conflict, or branch protection that requires human approval. The factory
does not use administrator overrides, force pushes, or autonomous deployments. Retry exhaustion stops the
project with evidence instead of creating more work. The factory does not roll
back merged pull requests if a later task fails.

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

Resume loads the stored brief and immutable plan.
It uses saved child run identifiers.
Then it reconciles task outcomes before it dispatches new work.
Delivery policy and repository identity must still match. The factory does not
implement completed tasks again. Pull request delivery checkpoints reuse existing
artifacts, reviews, and retry budgets. A safe delivery checkpoint needs no
fresh planner call or duplicate pull request.

If publishing was interrupted, the saved commit receipt identifies the only
commit that the factory can push. Recovery never infers approval from a matching
tree at an arbitrary `HEAD`.

An interrupted implementation before a safe checkpoint remains ambiguous and
stops at `NEEDS_HUMAN`. Resume does not spend a fresh retry or reopen an
exhausted terminal run. The factory preserves local integration conflicts and
uncertain workspace state rather than discarding them.
