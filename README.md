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

The initial milestones deliberately stop before automatic PR creation.

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

Eventually:

```bash
uv sync
uv run pytest
uv run factory run ...
```

## Documentation

Read:
- `AGENTS.md`
- `docs/architecture.md`
- `docs/symphony-alignment.md`
- `PLAN.md`

`PLAN.md` defines the implementation order.

Do not implement later phases before earlier milestones work.
