# Software Agent Factory. Agent Instructions

A local-first factory that takes a work item through triage, plan, implement,
verify, review, PR and CI repair on a developer Mac. Models run remotely through
an agent runtime (Copilot, pi or Claude Code). Everything else runs locally.
Read `docs/architecture.md` before you change orchestration code. Then read
`docs/symphony-alignment.md` and `PLAN.md`.

## Core principle

LLMs provide intelligence. The factory provides authority.
Agents plan, edit, test, review and diagnose. Deterministic factory code owns
workflow state, retry budgets, routing, quality gates, merging, credentials and
whether an agent's output is accepted. `docs/reference/safety.md` has the full list.

## Delivery principle

- Choose the fastest sufficient solution. Stop when acceptance criteria and gates pass.
- One coherent work item and one pull request per independently verifiable outcome.
- Tests, docs and cleanup belong to the outcome they serve, not to separate items.
- Reuse existing code and boundaries before adding abstractions, services or config.

## Implementation rules

- Only the workflow controller changes a run's state. Agents return artifacts.
- Agents exchange typed, persisted artifacts, not one shared conversation.
  Each agent gets only the context its job needs. See `docs/architecture.md`.
- A model never approves its own work. Deterministic checks run first, then an
  independent reviewer, preferably from a different model family.
- Every retry is bounded and records its reason.
- Complexity picks model strength. Risk picks governance. Routes and ratchets:
  `docs/guides/adaptive-routing.md` (ADR-037). The controller never downgrades a route.
- If a check can run as code, run it as code. LLM judgement only supplements it.
- Keep V1 small. The banned list is in `docs/project/roadmap.md`. The local
  dashboard is the one web UI exception, bounded in `docs/reference/safety.md`
  (ADR-016, ADR-033). A new dashboard write action needs an ADR first.
- Autonomous merge exists only as the opt-in, controller-owned path in ADR-022.
- Keep model names in configuration, not in source code.
- Follow Symphony concepts for scheduling and recovery. Document why before you
  diverge from them (`docs/symphony-alignment.md`).

## Code rules

- Delete code before you add code. Remove code your change makes dead in the same PR.
- Keep one source of truth for each concept, such as a state table or a command list.
- Prefer explicit domain types, small modules and plain control flow. No god
  classes, Manager objects, workflow DSLs or clever metaprogramming.
- Fail closed and visibly. A check that ran nothing is not a pass.
- Give each wait and each subprocess a timeout, and let it honor a stop request.
- Split a module before it grows past 800 lines. `tests/test_repo_rules.py`
  checks this, the paths in this file and its length.
- Do not add a suppression, a skipped test or a lower threshold to make a check pass.
- Tests never make paid LLM calls or reach GitHub. Use fakes for external
  boundaries, including `AgentRuntime`.
- Do not hard-code counts in documentation. Name the source instead.
- Add a configuration option or an abstraction only for a current need.
- When behavior changes, update `docs/` and the ADR in `docs/decisions.md` in the same PR.
- Implement only the requested phase. If the architecture must change, stop,
  propose the smallest correction, update the docs, then implement.

## Writing rule

Use concise technical English for all factory-authored text. For `README.md`
and `docs/`, apply the pinned
[SimpleEnglish skill](https://github.com/AminBlg/SimpleEnglish/blob/61ee200efbd423050aab982eed94226229891ae0/skills/simple-english/SKILL.md).
Rules: `docs/reference/writing-policy.md`. Check:
`uv run --no-sync python scripts/docs/check_simple_english.py`.

## Gates and delivery

- Gate commands: `.claude/skills/pr/SKILL.md` (mirrors CI) and `docs/project/development.md`.
- After verified changes, commit, push and open or update a PR, unless the user
  asks to keep them local.
