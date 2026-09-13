---
title: Software Agent Factory
description: Software agents with a local workflow controller. Models propose changes. Factory code controls each stage.
hide:
  - navigation
  - toc
---

<div class="saf-home" markdown>

<div class="saf-hero" markdown>

<div class="saf-hero-copy" markdown>

<span class="saf-eyebrow">Software Agent Factory / macOS</span>

# From a task<br>to a reviewed<br><span class="saf-accent">code change.</span>

Software agents plan and implement your changes.
A local controller manages each stage.
The controller is code that enforces workflow rules.

<div class="saf-actions" markdown>

[Get started](get-started/install.md){ .md-button .md-button--primary }
[Explore the pipeline ↓](#the-pipeline){ .md-button }

</div>

<p class="saf-release">{{ factory_release_tag }} <span aria-hidden="true">/</span> Documentation tracks <code>main</code>.</p>

</div>

<div class="saf-machine" role="img" aria-label="Three layers: agents propose changes, the controller enforces rules, and your machine stores the evidence.">
<div class="saf-machine-grid" aria-hidden="true"></div>
<div class="saf-machine-label saf-machine-label--top" aria-hidden="true"><span>01 / Intelligence</span>Specialized agents</div>
<div class="saf-machine-stack" aria-hidden="true">
<div class="saf-layer saf-layer--base"><i></i><i></i><i></i><i></i><span>LOCAL</span></div>
<div class="saf-layer saf-layer--controller"><div class="saf-chip"><span>FACTORY</span><span>CONTROLLER</span></div></div>
<div class="saf-layer saf-layer--agents"><i></i><i></i><i></i><i></i><span>AGENTS</span></div>
</div>
<div class="saf-machine-label saf-machine-label--middle" aria-hidden="true"><span>02 / Authority</span>One controller</div>
<div class="saf-machine-label saf-machine-label--bottom" aria-hidden="true"><span>03 / Execution</span>Your machine</div>
<span class="saf-machine-caption" aria-hidden="true">Architecture / Local execution + remote models</span>
</div>

</div>

<div class="saf-facts">
<div><span class="saf-fact-number">01</span><p>One controller owns <br>all workflow transitions.</p></div>
<div><span class="saf-fact-number">JSON</span><p>Local files retain <br>the results of each stage.</p></div>
<div><span class="saf-fact-number">OFF</span><p>Network features require <br>explicit configuration.</p></div>
</div>

<section class="saf-section" markdown>

<span class="saf-eyebrow">01 / Follow the change</span>

## The pipeline

A pipeline is a sequence of processing stages.
These six groups explain the path through one task.
The controller permits the next stage only after the required checks pass.

<div class="saf-pipeline" data-pipeline markdown>

<div class="saf-stage" id="pipeline-prepare" data-label="Prepare" markdown>

<div class="saf-stage-copy" markdown>

<span class="saf-stage-number">01 / Prepare</span>

### Give the task its own workspace

A work item describes one requested change.
A Git worktree is a separate checkout of a repository.
The factory creates a worktree for the work item.

The factory reads permitted repository files to identify dependencies.
This scan uses no shell commands or network access.

[Read about workspaces](concepts/how-it-works.md#workspaces)

</div>

<div class="saf-stage-evidence" markdown>

<span class="saf-small-label">Stage sequence</span>

<div class="saf-flow"><span>Work item</span><span>Worktree</span><span>Repository profile</span></div>

<span class="saf-small-label">Saved evidence</span>

`work-item.json`<br>
`repository-profile.json`

<p class="saf-stage-note">A project brief can produce several work items before this stage.</p>

</div>

</div>

<div class="saf-stage" id="pipeline-plan" data-label="Plan" markdown>

<div class="saf-stage-copy" markdown>

<span class="saf-stage-number">02 / Plan</span>

### Define the change before implementation

Triage is the assessment of task complexity and risk.
Complexity selects model strength.
Risk determines the required controls.

The Refiner defines acceptance criteria, which are conditions for success.
If triage requests research, the Researcher examines open questions.
The Planner produces an execution plan with permitted change boundaries.

[Read about the agents](concepts/how-it-works.md#the-agents)

</div>

<div class="saf-stage-evidence" markdown>

<span class="saf-small-label">Stage sequence</span>

<div class="saf-flow"><span>Triage</span><span>Refine</span><span class="saf-optional">Research / optional</span><span>Plan</span></div>

<span class="saf-small-label">Saved evidence</span>

`triage.json`<br>
`specification.json`<br>
`research.json` (optional)<br>
`execution-plan.json`

</div>

</div>

<div class="saf-stage" id="pipeline-implement" data-label="Implement" markdown>

<div class="saf-stage-copy" markdown>

<span class="saf-stage-number">03 / Implement</span>

### Make the change inside the worktree

The Implementer receives the execution plan and repository access.
It edits source files and adds related tests.
The factory obtains the changed file list from Git.

An artifact is a saved record with a defined structure.
Each stage receives the artifacts necessary for its task.
The factory retains each attempt and its results on disk.

[Read about artifacts](concepts/how-it-works.md#typed-artifacts-not-one-long-conversation)

</div>

<div class="saf-stage-evidence" markdown>

<span class="saf-small-label">Stage sequence</span>

<div class="saf-flow"><span>Execution plan</span><span>Implementation</span><span>Git evidence</span></div>

<span class="saf-small-label">Saved evidence</span>

`change-set.json`<br>
`patch.diff`

<p class="saf-stage-note">The controller obtains the actual changes from Git.</p>

</div>

</div>

<div class="saf-stage" id="pipeline-verify" data-label="Verify" markdown>

<div class="saf-stage-copy" markdown>

<span class="saf-stage-number">04 / Verify</span>

### Require evidence from repository checks

Deterministic checks use fixed rules to assess a change.
Lint is automated source code analysis.
The factory uses the configured installation, verification, and build commands.
These commands can include lint, type checks, and tests.
The factory compares the changed files with the plan.

Polish is one optional improvement attempt after successful verification.
If polish is enabled and eligible, the Implementer applies repository guidance.
The factory then does all deterministic checks again.

[Configure repository checks](guides/configure-repository.md)

</div>

<div class="saf-stage-evidence" markdown>

<span class="saf-small-label">Stage sequence</span>

<div class="saf-flow"><span>Verification + scope</span><span class="saf-optional">Polish / optional</span><span class="saf-optional">Verification + scope again</span></div>

<span class="saf-small-label">Saved evidence</span>

`verification.json`<br>
`repository-skill-use.json` (with guidance)

<p class="saf-stage-note">The factory permits polish only when a later recovery attempt remains available.</p>

</div>

</div>

<div class="saf-stage" id="pipeline-review" data-label="Review" markdown>

<div class="saf-stage-copy" markdown>

<span class="saf-stage-number">05 / Review</span>

### Give separate agents the evidence

The Tester examines test coverage for the change.
The Reviewer examines the change and test evidence independently.
Neither agent receives the Implementer summary.

The controller applies the review policy to the findings.
The default local path ends at <code>PR_READY</code>.
This state means that the change is ready for a pull request.

[Read the review policy](concepts/how-it-works.md#the-agents)

</div>

<div class="saf-stage-evidence" markdown>

<span class="saf-small-label">Stage sequence</span>

<div class="saf-flow"><span>Independent Tester</span><span>Independent Reviewer</span><span>PR_READY</span></div>

<span class="saf-small-label">Saved evidence</span>

`test-report.json`<br>
`review.json`

<p class="saf-stage-note">Agents cannot approve their own implementation.</p>

</div>

</div>

<div class="saf-stage" id="pipeline-deliver" data-label="Deliver" markdown>

<div class="saf-stage-copy" markdown>

<span class="saf-stage-number">06 / Deliver / Optional</span>

### Publish through explicit controls

A pull request proposes a branch change on GitHub.
Continuous integration (CI) does automated checks on that change.
Pull request publication and CI observation require explicit configuration.

If an eligible CI failure occurs, the controller permits a limited repair attempt.
Merge requires separate authorization for the repository, target, and required checks.
The factory never bypasses branch protection or deploys software.

[Read about GitHub delivery](guides/github.md)

</div>

<div class="saf-stage-evidence" markdown>

<span class="saf-small-label">Stage sequence</span>

<div class="saf-flow"><span>Pull request</span><span>CI observation</span><span class="saf-optional">Merge / separate authorization</span></div>

<span class="saf-small-label">Saved evidence</span>

`ci.json`<br>
`run.json`

<p class="saf-stage-note">The controller requires confirmed merge evidence before it reports merge completion.</p>

</div>

</div>

</div>

<div class="saf-recovery" markdown>

<span class="saf-recovery-symbol" aria-hidden="true">↳</span>

<p>Recovery has limits. The controller records each retry reason and enforces attempt limits. Unsafe continuation stops at <code>NEEDS_HUMAN</code>.</p>

[All workflow states →](concepts/how-it-works.md#workflow-states)

</div>

</section>

<section class="saf-section" markdown>

<span class="saf-eyebrow">02 / Understand the boundaries</span>

## Models propose. Code controls. {#design}

<div class="saf-boundaries" markdown>

<div class="saf-boundary" markdown>

<span class="saf-small-label">Intelligence / Models</span>

### Agents produce changes and findings

Each agent has a specific role.
Real model calls use GitHub Copilot.
Configuration selects the model for each role.

[Model selection →](reference/model-selection.md)

</div>

<div class="saf-boundary saf-boundary--accent" markdown>

<span class="saf-small-label">Authority / Factory</span>

### The controller enforces the rules

Python code owns workflow transitions and retry limits.
It applies the required quality checks.
Agents cannot change these controls.

[Safety boundaries →](reference/safety.md)

</div>

<div class="saf-boundary" markdown>

<span class="saf-small-label">Execution / Your machine</span>

### The evidence stays on disk

Git worktrees, repository commands, and saved state remain local.
JSON files record the results of each stage.
You can inspect these files after a run.

[Operations guide →](guides/operations.md)

</div>

</div>

</section>

<section class="saf-section saf-try" markdown>

<div markdown>

<span class="saf-eyebrow">03 / Start with an offline example</span>

## See the workflow<br>on your machine. {#what-a-run-does}

The default runtime uses fake agents, which are deterministic test substitutes.
They exercise the workflow without paid model calls.
They do not implement the requested feature.

This source checkout example uses the packaged example configuration.
It makes no network calls.
Real Copilot agents require <code>--runtime copilot</code> and incur charges.

[First offline run →](get-started/first-run.md)

</div>

<div class="saf-terminal" markdown>

<span class="saf-terminal-title">Terminal / Source checkout</span>

```bash
uv run factory run \
  --repo ~/projects/example \
  --title "Reject empty customer names" \
  --description "Return HTTP 400 for empty or whitespace-only names." \
  --config config/factory.example.yaml
```

<div class="saf-terminal-result"><span>Expected result</span><code>PR_READY</code><p>The worktree and stage records remain available for inspection.</p></div>

</div>

</section>

<section class="saf-section" markdown>

<span class="saf-eyebrow">04 / Continue from here</span>

## Choose your next step {#where-to-start}

<div class="saf-paths" markdown>

[<span>01 / Get started</span>Install the factory<span>macOS packages and source installation →</span>](get-started/install.md)

[<span>02 / Connect</span>Use real agents<span>Copilot access and configuration →</span>](get-started/copilot.md)

[<span>03 / Configure</span>Prepare your repository<span>Commands, limits, and protected files →</span>](guides/configure-repository.md)

[<span>04 / Plan</span>Start from a project brief<span>Task breakdown and dependencies →</span>](guides/projects.md)

[<span>05 / Inspect</span>Read the architecture<span>Workflow states and saved artifacts →</span>](concepts/how-it-works.md)

[<span>06 / Reference</span>Find a command<span>Command syntax and arguments →</span>](reference/cli.md)

</div>

</section>

<div class="saf-status" markdown>

## Status

Use the factory with supervision.
The supported platform is macOS on Apple silicon and Intel.
A source installation requires Python 3.13 or later.

This documentation describes the <code>main</code> branch.
The latest published package is {{ factory_release_tag }}.

[Roadmap and status](project/roadmap.md) · [Unreleased changes](https://github.com/sanjit-roopra/software-agent-factory/blob/main/CHANGELOG.md#unreleased)

</div>

</div>
