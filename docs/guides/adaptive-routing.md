# Adaptive execution routing

Simple tasks do not always need the full multi-agent pipeline.
Adaptive execution routing selects the lightest safe execution route for a work item.
The controller makes this decision with fixed rules (ADR-037).
Routing makes no network call and needs no API key.

## Execution routes

The factory defines four configured routes: `SINGLE`, `CRITIQUE`, `FULL`, and `MANUAL_TRIAGE`.
`FULL_REVIEW` is a controller-only post-implementation route.

| Route | Execution path | Review gate |
| --- | --- | --- |
| `SINGLE` | Implementer and deterministic verification | Deterministic check only |
| `CRITIQUE` | Implementer and deterministic verification | Independent Reviewer |
| `FULL` | Triage, Planner, Implementer, verification, optional polish | Independent Tester and Reviewer |
| `MANUAL_TRIAGE` | Safe stop before implementation | Human intervention |
| `FULL_REVIEW` | Upgrade after implementation | Independent Tester and Reviewer |

`SINGLE` does not invoke Triage, Planner, Tester, Reviewer, or the polish attempt.
Deterministic verification accepts the change only when every sufficiency condition passes.
`CRITIQUE` adds the independent Reviewer to `SINGLE`.
`MANUAL_TRIAGE` moves the run to `NEEDS_HUMAN` before implementation starts.
`FULL_REVIEW` runs the full Tester and Reviewer gates. It does not restart triage or planning.

For `SINGLE` and `CRITIQUE`, the controller skips the polish attempt.
For `FULL_REVIEW` and `FULL`, the polish attempt is eligible when `polish.enabled` is `true`.

## How the controller selects a route

1. The safety floors remove each configured option that is not safe for the work item.
2. If routing is disabled, the controller selects the first legal `FULL` option.
   If no legal `FULL` option exists, it selects `MANUAL_TRIAGE`.
3. If no option is legal, the controller selects `MANUAL_TRIAGE`.
4. If one option is legal, the controller selects it.
5. If more than one option is legal, the controller selects the lightest legal route.
   The order is `SINGLE`, `CRITIQUE`, `FULL` and then `MANUAL_TRIAGE`.
   If two options have the same route, the first option in the configuration wins.

The human administrator owns the option list, the risk mappings and `full_only_terms`.
The controller owns the safety floors, workflow state, budgets, ratchets, review and delivery.

When the selected route is `FULL`, the selected option complexity can raise the Triage complexity.
It cannot lower it.

## Model selection and worker cascade

Each route option defines an execution route, worker complexity, and optional model profile.
Short routes (`SINGLE` and `CRITIQUE`) also define a provisional risk.
Worker model escalation is the existing cascade behavior in the factory.

## Setup

1. Copy the complete `routing` block from `config/factory.example.yaml`.
2. Set `routing.enabled: true`.
3. Configure at least one verification command in `repository.commands.verify`.

YAML lists replace the complete default list.
When you specify `full_only_terms` or `options`, YAML does not merge items into the packaged defaults.
Copy the complete block before you edit it.

Short routes require at least one verification command.
If no verification command is configured, the controller disallows short routes.

Option contracts require specific fields:

- `MANUAL_TRIAGE` options must omit `complexity` and `risk`.
- `SINGLE` and `CRITIQUE` options require `complexity` and `risk`.
- `FULL` options require `complexity`.
- The option list must contain at least one `FULL` option and at most 255 options.

`FULL_REVIEW` cannot be configured in `options`.

An old configuration file can contain the removed classifier keys, for example `api_url` or `min_confidence`.
The factory ignores them.

## Safety floors

The human-approval floor is configuration driven.
Packaged defaults enable human approval for `R2` and `R3`.

- `SINGLE` requires explicitly named, repository-relative file paths in the work item text.
- Missing acceptance criteria disallow `SINGLE` and `CRITIQUE`.
- Missing verification commands disallow `SINGLE` and `CRITIQUE`.
- Explicit work item complexity of `L2` or `L3` disallows `SINGLE`.
- Options with worker complexity below explicit work item complexity are disallowed.
- Explicit work item risk that requires human approval disallows `SINGLE` and `CRITIQUE`.
- Options with risk below explicit work item risk are disallowed.
- Route options whose configured risk requires human approval cannot use `SINGLE` or `CRITIQUE`.
- Labels `full-only` and `full` disallow `SINGLE` and `CRITIQUE`.
- Labels `no-single` and `critique-or-full` disallow `SINGLE`.
- Labels `research`, `research-required`, and `needs-research` disallow `SINGLE` and `CRITIQUE`.
- Mentions of protected file patterns or version manifests disallow `SINGLE` and `CRITIQUE`.
- A match for any term in `routing.full_only_terms` disallows `SINGLE` and `CRITIQUE`.

## Route ratchets

The controller ratchets routes monotonically:

- If deterministic verification fails on `SINGLE`, the controller ratchets to `CRITIQUE`.
- If changes touch files outside the synthesized scope, the controller ratchets to `FULL_REVIEW`.
- If changed files exceed `single_max_changed_files`, the controller ratchets to `FULL_REVIEW`.
- If changes touch protected paths or manifest files, the controller ratchets to `FULL_REVIEW`.
- If dependency fingerprints change, the controller ratchets to `FULL_REVIEW`.
- If scope drift occurs, the controller ratchets to `FULL_REVIEW`.
- If CI repair runs, the controller ratchets to `FULL_REVIEW`.

The controller never downgrades an execution route.

## Route decision artifact

The controller records every decision in `route-decision.json` before implementation starts.
A reopened run reuses this decision.

```bash
cat <data_dir>/runs/<run-id>/route-decision.json
```

The `source` field shows the origin of the decision:

- `rule`: more than one option was legal, and the controller selected the lightest one.
- `single_option`: only one option was legal.
- `safety_floor`: no option was legal.
- `disabled`: routing is disabled.

Run records from before ADR-037 can show `jev` or `fallback`.
They also keep their `probabilities`, `model_id` and `usage`.

Other fields include `offered_options`, `selected_option`, `initial_route`, `effective_route`,
`selected_worker_complexity`, `selected_risk`, `selected_model_profile`, `fallback_reason` and `adjustments`.
For `disabled` or `safety_floor` sources, `offered_options` lists all configured options.

## Troubleshooting

### No short routes offered

If the work item has no named file paths, `SINGLE` is not legal.
If the task mentions terms in `full_only_terms`, short routes are not legal.
If verification commands are missing, short routes are not legal.
Check `offered_options` in `route-decision.json` to see which options are legal.

### Unexpected route ratchet

If a `SINGLE` run ratchets to `FULL_REVIEW`, inspect `adjustments` in `route-decision.json`.
Examine whether the Implementer modified extra files or exceeded `single_max_changed_files`.
