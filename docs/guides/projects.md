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

- prefer one coherent task
- split only for an independently verifiable outcome, a hard prerequisite,
  safe parallel execution, or a scope limit
- reuse existing code and boundaries
- do not manufacture separate setup, testing, documentation or cleanup tasks
- do not add speculative abstractions, dependencies, services or infrastructure

The factory validates task numbering and dependency direction before any work
starts. A plan may contain at most 12 tasks.

## Execution

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

Project execution currently requires `pull_request.enabled: false` and
`ci.enabled: false`. It produces one local integration branch; autonomous merge
and deployment remain out of scope.

Interrupted projects are not resumed automatically. Reusing an existing project
ID is refused so immutable planning artifacts cannot be overwritten. A later
invocation marks an abandoned `PLANNING` or `RUNNING` execution
`NEEDS_HUMAN` before refusing the duplicate ID.

The concurrency cap applies per factory process. Do not run a backlog daemon
and a project command against the same repository when their combined
concurrency would exceed the repository's safe local capacity.
