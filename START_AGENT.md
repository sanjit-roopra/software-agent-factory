# First implementation task (historical)

This file records the original Phase 0/1 bootstrap brief. It is kept for
context only.

Phases 0-14 of `PLAN.md` are now implemented; Phase 15 remains optional and
deliberately unimplemented. Read `README.md` and `PLAN.md` for current status
before starting new work.

Read:

1. `AGENTS.md`
2. `docs/architecture.md`
3. `docs/symphony-alignment.md`
4. `PLAN.md`

We are starting the implementation of this project.

Work ONLY on Phase 0 and Phase 1 from `PLAN.md`.

Before coding:

1. critically review the architecture,
2. check the current OpenAI Symphony specification so you understand the orchestration model we are basing this on,
3. identify any contradictions or unnecessary complexity in our documents,
4. make only small corrections where justified,
5. document important decisions.

Then implement:

- Python/uv project skeleton
- domain models
- workflow state machine
- typed artifacts
- file-based `RunStore`
- `FakeAgentRuntime`
- deterministic `ModelRouter`
- retry/escalation rules
- CLI
- Git worktree workspace handling
- deterministic repository command execution
- controller-derived Git evidence
- comprehensive tests

Do NOT implement yet:

- real GitHub Copilot calls
- GitHub PR creation
- GitHub Actions integration
- GitHub backlog polling
- Jira
- SQLite/Postgres
- Temporal
- web UI
- Kubernetes
- production deployment

The implementation should end with a working deterministic fake vertical slice,
real local Git worktree isolation, and optional deterministic verification
commands.

Required checks:

```bash
uv sync
uv run ruff check .
uv run pytest
```

A demonstration command should be possible along the lines of:

```bash
uv run factory run \
  --repo /path/to/test/repo \
  --title "Example task" \
  --description "Example description"
```

The run should progress through the fake workflow and end in `PR_READY`.

Persist all run state and artifacts under the configured factory data directory.

Do not continue into Phase 2 after completing these milestones.

When finished, summarize:

- architecture decisions made
- files created
- tests implemented
- anything from the plan you deliberately changed and why
- remaining work for Phase 2
