# BMAD Research Assessment

This document assesses external research from the [BMAD-METHOD](https://github.com/bmad-code-org/BMAD-METHOD) framework.
It records what BMAD is, what is relevant to this factory, what ideas were rejected, what was adopted, and what was deferred.

## What BMAD is

BMAD-METHOD is an open-source multi-agent agile method for AI-driven software development.
It provides role definitions, lifecycle phases, and document templates for teams that use AI agents.
We inspected external evidence from the repository at commit [`94b6727b`](https://github.com/bmad-code-org/BMAD-METHOD/tree/94b6727b00c8316557828c8a8ff2a48ff60d60cc) from 2026-09-11 and newest observed tag `v6.12.0`.

## Assessment summary

| Area | BMAD-METHOD pattern | Software Agent Factory approach | Disposition |
| --- | --- | --- | --- |
| Delivery scaling | Phase scaling by project size | Complexity and risk routing across single tasks | Adopted in principle |
| Decision capture | Broad architecture documents | Shared decisions that independent work can make incompatibly | Adopted in principle |
| Readiness gate | Document completeness checklists | Pre-implementation check for unresolved decisions | Adopted |
| Workflow authority | Agent and persona self-direction | Deterministic controller owns all state transitions | Rejected |
| Git and retry bounds | Agent-directed commits and retries | Controller-owned worktrees and monotonic retry budgets | Rejected |
| Task scheduling | Linear story sequencing | Dependency graph with isolated parallel worktrees | Rejected |
| Quality approval | Self-approval and agent checklists | Independent Tester and Reviewer models with deterministic gates | Rejected |
| Agent coordination | Synchronous persona chat sessions | Asynchronous typed JSON artifacts between stages | Rejected |
| Update checks | Mutable main branch checks | Pinned commits and immutable references | Rejected |
| Artifact format | Large Markdown documents | Compact typed JSON artifacts with strict schemas | Rejected |
| Extension model | Modular plugins and frameworks | Small standard library implementation without plugin systems | Rejected |
| Governance | Prose instructions in prompts | Programmatic verification and deterministic policy code | Rejected |
| Decision registry | Project-level decision register | Task specifications, repository skills, and architecture decision records | Deferred |
| Test traceability | Acceptance criteria test matrix | Acceptance criteria in specifications, test reports, and verification | Deferred |

## Interesting ideas

Six concepts from BMAD are relevant to autonomous software development:

- Scale one delivery pattern by uncertainty, risk, and scope.
- Record only shared decisions that independent work can make incompatibly.
- Detect plans that require human-owned missing decisions before implementation.
- Use compact handoff artifacts between stages.
- Verify observable consumer behavior rather than internal model claims.
- Use immutable content-addressed inputs and deterministic validators.

## Rejected concepts

We rejected several BMAD concepts that conflict with factory principles:

- Model-owned workflow state: LLMs provide intelligence, but deterministic factory code provides authority.
- Agent-owned commits, reverts, or retries: The workflow controller owns all Git operations and attempt budgets.
- Linear story scheduling instead of dependency graphs: The factory runs independent tasks in parallel worktrees.
- Same-workflow self-approval: An implementer cannot approve its own work.
- Synchronous all-agent coordination: Agents communicate through persisted typed artifacts rather than shared chat sessions.
- Interactive party or persona workflows: The autonomous execution path must run unattended.
- Mutable-main update checks: External dependencies and base references must remain stable during execution.
- Large Markdown artifact sets: The factory uses compact, schema-validated JSON artifacts.
- Plugin or module architecture: The factory avoids premature extensibility frameworks.
- Prose-only governance: Quality gates must use deterministic programmatic checks.

## Adopted scope

The factory adopts a pre-implementation readiness gate.
This gate detects plans that require missing human decisions.

An optional `unresolved_decisions` list of concise strings is added to `ExecutionPlan`.
It records only material choices.
These choices cannot be derived from task intent, Specification, repository evidence, or existing constraints.

The controller evaluates the initial plan before implementation starts.
If unresolved decisions exist, the controller asks the Planner once more to resolve evidence-answerable items.
If material choices remain after this single retry, the controller persists the final plan.
The controller halts before implementation in `NEEDS_HUMAN`.
It records a dedicated escalation record pointing to `execution-plan.json`.
When GitHub escalation is enabled, an authorized contributor can answer the
numbered decisions. The controller stores typed answers and returns to planning.

This gate applies only to the initial pre-implementation plan.
It does not reject the metadata-only scope replan after deterministic verification.

The Planner receives only validated answer fields. It creates a new plan.
The controller checks that plan before implementation starts. The reply does
not reset budgets or change project-child behavior.

## Deferred items

We deferred two ideas:

- A project-level architecture decision registry.
- An explicit acceptance-to-test mapping matrix.

Existing capabilities already cover much of the value from these ideas.
The factory provides dependency graphs, typed artifacts, repository skills, deterministic verification, an independent Tester, and bounded review.

## Source, license, and trademark notes

BMAD-METHOD is licensed under the MIT License.
The license terms exclude its trademarks.
This repository uses independently expressed concepts and copies no BMAD code, templates, or prose.
