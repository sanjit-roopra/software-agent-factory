# Adaptive execution routing

Simple tasks do not always need the full multi-agent pipeline.
Adaptive execution routing selects the shortest safe execution route for a work item.

Jev is a classifier from TypeSafe.
The factory calls it over HTTPS.
System One is the TypeSafe product that serves Jev.
It selects one controller-offered Choice option with probabilities and confidence.

Read the [TypeSafe API specification](https://docs.typesafe.ai/api.md) and the
[TypeSafe Choice documentation](https://docs.typesafe.ai/primitives/choice.md).

## Execution routes

The factory defines four configured routes: `SINGLE`, `CRITIQUE`, `FULL`, and `MANUAL_TRIAGE`.
`FULL_REVIEW` is a controller-only post-implementation route.

| Route | Execution path | Review gate |
| --- | --- | --- |
| `SINGLE` | Implementer and deterministic verification | Deterministic check only |
| `CRITIQUE` | Implementer and deterministic verification | Independent Reviewer |
| `FULL` | Triage, Refiner, optional Researcher, Planner, Implementer, verification, optional polish | Independent Tester and Reviewer |
| `MANUAL_TRIAGE` | Safe stop before implementation | Human intervention |
| `FULL_REVIEW` | Upgrade after implementation | Independent Tester and Reviewer |

### SINGLE

`SINGLE` runs the Implementer and deterministic verification.
It does not invoke Triage, Refiner, Researcher, Planner, Tester, Reviewer, or the polish attempt.
Deterministic verification accepts the change only when every sufficiency condition passes.

### CRITIQUE

`CRITIQUE` runs the Implementer, deterministic verification, and the independent Reviewer.
It does not invoke Triage, Refiner, Researcher, Planner, Tester, or the polish attempt.
The Reviewer checks the implementation independently.

### FULL

`FULL` runs the complete factory pipeline.
It includes Triage, Refiner, optional Researcher, Planner, Implementer, and deterministic verification.
It also includes an optional polish attempt, independent Tester, and independent Reviewer.
Complex tasks or high-risk tasks retain this full pipeline.

### MANUAL_TRIAGE

`MANUAL_TRIAGE` halts execution safely before implementation starts.
The controller transitions the run to `NEEDS_HUMAN`.
A human operator must inspect the task.

### FULL_REVIEW

`FULL_REVIEW` is a controller-only post-implementation upgrade.
It runs the full independent Tester and Reviewer gates.
It does not restart triage, refinement, or planning.

For `SINGLE` and `CRITIQUE`, the controller skips the polish attempt.
For `FULL_REVIEW` and `FULL`, the polish attempt is eligible when `polish.enabled` is `true`.
It requires standard performance mode, no previous polish attempt, and at least one remaining recovery attempt.
The polish attempt never runs during CI repair.

## Decision ownership

The factory separates intelligence from authority.

- The human administrator configures the option palette, risk mappings, and `full_only_terms`.
- The controller builds legal options with safety floors before calling Jev.
- Jev selects one option among only those controller-offered options.
- The controller validates the response and owns workflow state, budgets, ratchets, fallback, review, and delivery.

Jev does not set factory policy.
Jev only selects among the options that the controller offers.

When the selected route is `FULL`, the selected option complexity can raise the Triage complexity, but cannot lower it.

## Model selection and worker cascade

Each route option defines an execution route, worker complexity, and optional model profile.
Short routes (`SINGLE` and `CRITIQUE`) also define a provisional risk.

Worker model escalation is the existing cascade behavior in the factory.
The factory walks through distinct configured worker models as attempts increase.
The factory does not have a separate `CASCADE` route.

## Setup instructions

Follow these steps to configure adaptive routing.

### 1. Obtain an API key

Obtain an API key from the [TypeSafe Console](https://console.typesafe.ai/keys).

### 2. Store the key in the environment

Store the key in your process environment:

```bash
export JEV_API_KEY="your-typesafe-api-key"
```

Do not write the key into YAML files or commit it to Git.
TypeSafe SDK examples can use `TYPESAFE_API_KEY`.
This factory uses the environment variable configured in `routing.api_key_env_var`.
The default variable name is `JEV_API_KEY`.
`factory doctor` does not verify the Jev key or network connectivity.

### 3. Copy the complete configuration

Copy the complete routing block from `config/factory.example.yaml`.
Then change `enabled` to `true`.

YAML lists replace the complete default list.
When you specify `full_only_terms` or `options`, YAML does not merge items into the packaged defaults.
Copy the complete block before you edit it.
Do not treat your file as a partial overlay.

### 4. Configure verification commands

Configure at least one deterministic verification command:

```yaml
repository:
  commands:
    verify:
      - "pytest tests/test_unit.py"
```

Short routes require at least one verification command.
If no verification command is configured, the controller disallows short routes.

### 5. Enable routing in your configuration

Set `routing.enabled: true` in your configuration file.
Use the pinned model `jev-1.13.0`.
Read the [TypeSafe models reference](https://docs.typesafe.ai/models.md).

The factory enforces a limit of at most 255 configured options at load time.

Here is the complete routing configuration:

```yaml
routing:
  enabled: true
  api_url: "https://api.typesafe.ai/v1/systemone"
  model: "jev-1.13.0"
  api_key_env_var: "JEV_API_KEY"
  timeout_seconds: 5.0
  min_confidence: 0.7
  min_probability: 0.5
  max_prompt_chars: 4000
  max_response_bytes: 65536
  single_max_changed_files: 5
  rubric_version: "1.0"
  full_only_terms:
    - "auth"
    - "authentication"
    - "authorization"
    - "permission"
    - "permissions"
    - "encrypt"
    - "encryption"
    - "credential"
    - "credentials"
    - "secret"
    - "secrets"
    - "migration"
    - "migrations"
    - "deploy"
    - "deployment"
    - "production"
    - "prod"
    - "billing"
    - "payment"
    - "payments"
  options:
    - id: "single_l0"
      route: "SINGLE"
      complexity: "L0"
      risk: "R0"
      description: "Single-pass execution with L0 worker"
    - id: "single_l1"
      route: "SINGLE"
      complexity: "L1"
      risk: "R0"
      description: "Single-pass execution with L1 worker"
    - id: "critique_l1"
      route: "CRITIQUE"
      complexity: "L1"
      risk: "R1"
      description: "Execution with L1 worker and independent review"
    - id: "critique_l2"
      route: "CRITIQUE"
      complexity: "L2"
      risk: "R1"
      description: "Execution with L2 worker and independent review"
    - id: "full_l2"
      route: "FULL"
      complexity: "L2"
      description: "Full SDLC factory pipeline with L2 worker"
    - id: "full_l3"
      route: "FULL"
      complexity: "L3"
      description: "Full SDLC factory pipeline with L3 worker"
    - id: "manual_triage"
      route: "MANUAL_TRIAGE"
      description: "Escalate for human manual triage"
```

Option contracts require specific fields:
- `MANUAL_TRIAGE` options must omit `complexity` and `risk`.
- `SINGLE` options require `complexity` and `risk`.
- `CRITIQUE` options require `complexity` and `risk`.
- `FULL` options require `complexity`.

`FULL_REVIEW` is a controller-only execution route and cannot be configured in `options`.

### 6. Run a work item

Setting `routing.enabled: true` causes a real Jev API call over HTTPS even when you pass `--runtime fake`.
For a real task, run with `--runtime copilot`.
Calling Jev can incur TypeSafe charges, and `--runtime copilot` can incur GitHub Copilot charges.

Run a work item with your configuration:

```bash
uv run factory run \
  --repo ~/projects/example \
  --title "Update button label" \
  --description "Change submit button label in src/components/Button.tsx." \
  --acceptance-criterion "The submit button displays Save." \
  --runtime copilot \
  --config ~/my-factory.yaml
```

The CLI prints the effective route:

```text
run id: <run-id>
state: PR_READY
route: SINGLE
workspace: <workspace-path>
changed files: src/components/Button.tsx
```

Inspect the saved route decision artifact to verify the decision:

```bash
cat <data_dir>/runs/<run-id>/route-decision.json
```

Confirm that `source` equals `jev`.
This confirms that Jev answered the request.
If fallback occurs, `source` contains `fallback`, and `fallback_reason` records the cause.

You can also inspect the run with `factory show`:

```bash
uv run factory show <run-id> --config ~/my-factory.yaml
```

The controller persists `RouteDecision` before implementation starts.
Reopened runs reuse this persisted decision and do not call Jev again.

## Network behavior and fallback

The factory makes one HTTPS POST request to Jev during triage.
The factory forbids HTTP redirects.
It enforces a total request deadline and an upper limit on response size.

The factory does not retry network calls to Jev.
This behavior differs from TypeSafe SDK retry guidance.
If the Jev call fails, times out, or returns invalid data, the controller falls back deterministically.
The controller falls back to the first legal `FULL` option, or `MANUAL_TRIAGE` if no legal `FULL` option exists.

## Configured thresholds and defaults

Routing is disabled by default.
The factory makes no network calls while routing is disabled.

Administrators can change these thresholds in configuration:

- `timeout_seconds`: 5.0 seconds maximum request deadline.
- `min_confidence`: 0.7 minimum classifier confidence.
- `min_probability`: 0.5 minimum probability for the selected option.
- `max_prompt_chars`: 4000 character maximum prompt length.
- `max_response_bytes`: 65536 byte maximum response body size.
- `single_max_changed_files`: 5 maximum changed files for `SINGLE` and `CRITIQUE` routes before `FULL_REVIEW`.

Read the [TypeSafe confidence score guide](https://docs.typesafe.ai/confidence.md).

## Privacy and data limits

The factory sanitizes outbound prompt text before sending it to Jev.
The sanitizer removes:

- Fenced code blocks and tilde blocks.
- Unified diffs and diff headers.
- Python, JavaScript, and Java stack traces.
- Detected secrets and credentials.
- Remote URLs.
- Absolute local filesystem paths.
- Selected prompt injection phrases.

The factory sends these metadata fields to Jev:

- Task title.
- Task description.
- Acceptance criteria.
- Constraints.
- Labels.
- Repository technologies.
- Package managers.
- Presence of verify commands.
- Configured option identifiers and descriptions.

Sanitization reduces accidental disclosure.
Sanitization is not data classification.
Sanitization does not guarantee zero retention by external services.
Read the [TypeSafe Legal Terms](https://docs.typesafe.ai/legal.md) and
the [TypeSafe Privacy Policy](https://typesafe.ai/legal/privacy-policy).
Some proprietary task text leaves the machine.

## Safety floors

The controller evaluates safety floors before offering options to Jev.
If a safety condition fails, the controller removes short routes from the offered list.

The human-approval floor is configuration driven.
Packaged defaults enable human approval for `R2` and `R3`.

Safety floor rules include:

- `SINGLE` requires explicitly named, repository-relative file paths in the work item text.
- Missing acceptance criteria disallow `SINGLE` and `CRITIQUE`.
- Missing verification commands disallow `SINGLE` and `CRITIQUE`.
- Explicit work item complexity of `L2` or `L3` disallows `SINGLE`.
- Options with worker complexity strictly below explicit work item complexity are disallowed.
- Explicit work item risk requiring human approval disallows `SINGLE` and `CRITIQUE`.
- Options with risk below explicit work item risk are disallowed.
- Route options whose configured risk requires human approval cannot use `SINGLE` or `CRITIQUE`.
- Labels `full-only` and `full` disallow `SINGLE` and `CRITIQUE`.
- Labels `no-single` and `critique-or-full` disallow `SINGLE`.
- Labels `research`, `research-required`, and `needs-research` disallow `SINGLE` and `CRITIQUE`.
- Mentions of protected file patterns or version manifests disallow `SINGLE` and `CRITIQUE`.
- Matching any term in `routing.full_only_terms` disallows `SINGLE` and `CRITIQUE`.

The `full_only_terms` setting is human-owned policy.
It is not a taxonomy invented by Jev.
Matching terms disable short routes but still allow `MANUAL_TRIAGE` and `FULL`.

If only one legal option survives safety floors, the controller skips the network call.
It adopts that single surviving option immediately.

## Route ratchets

Deterministic ratchets protect execution after implementation.
The controller ratchets routes monotonically:

- If deterministic verification fails on `SINGLE`, the controller ratchets to `CRITIQUE`.
- If changes touch files outside the synthesized scope, the controller ratchets to `FULL_REVIEW`.
- If changed files exceed `single_max_changed_files`, the controller ratchets to `FULL_REVIEW`.
- If changes touch protected paths or manifest files, the controller ratchets to `FULL_REVIEW`.
- If dependency fingerprints change, the controller ratchets to `FULL_REVIEW`.
- If scope drift occurs, the controller ratchets to `FULL_REVIEW`.
- If CI repair runs, the controller ratchets to `FULL_REVIEW`.

`single_max_changed_files` applies to `SINGLE` and `CRITIQUE` before `FULL_REVIEW`.
`FULL_REVIEW` runs independent Tester and Reviewer gates.
It never restarts earlier stages.
The controller never downgrades an execution route.

## Route decision artifact

The controller records every decision in `route-decision.json`.
The artifact includes these fields:

- `work_item_id`: Identifier of the work item.
- `source`: Decision origin (`jev`, `single_option`, `safety_floor`, `fallback`, or `disabled`).
- `offered_options`: Options that passed safety floors.
  For `disabled` or `safety_floor` sources, this field lists all configured options.
- `selected_option`: Identifier of the selected option.
- `initial_route`: Route selected before implementation.
- `effective_route`: Current route after any post-implementation ratchets.
- `selected_worker_complexity`: Worker complexity level for implementation.
- `selected_risk`: Provisional risk level for short routes.
- `selected_model_profile`: Selected model profile override if configured.
- `confidence`: Confidence score returned by Jev.
- `probabilities`: Probability distribution across offered options.
- `model_id`: Model version string returned by Jev.
- `protocol_version`: Protocol version tag, mapped from `routing.rubric_version`.
- `latency_ms`: Round-trip request latency in milliseconds.
- `usage`: Input and output token counts reported by Jev.
- `request_hash`: SHA-256 hash of the request content.
- `fallback_reason`: Explanation when fallback occurs.
- `abstention_reason`: Explanation when abstention occurs.
- `adjustments`: Chronological list of route ratchets and their reasons.
- `decided_at`: Timestamp when the route decision was recorded.

## Troubleshooting

### Missing API key

If `routing.api_key_env_var` is not set, Jev routing fails.
The controller logs a warning.
It falls back to the first legal `FULL` option, or `MANUAL_TRIAGE` if no legal `FULL` option exists.
Set the environment variable in your shell before starting the factory.

### No short routes offered

If the work item lacks named file paths, `SINGLE` is not offered.
If the task mentions terms in `full_only_terms`, short routes are not offered.
If verification commands are missing, short routes are not offered.
Check `offered_options` in `route-decision.json` to see which options survived.
If `source` is `safety_floor`, no option survived and this field lists all configured options.

### Low confidence or low probability

If Jev confidence falls below `min_confidence`, validation fails.
If the top option probability falls below `min_probability`, validation fails.
The controller logs a warning.
It falls back to the first legal `FULL` option, or `MANUAL_TRIAGE` if no legal `FULL` option exists.
Do not retry immediately.
Inspect the task description for ambiguity.

### Network timeout or non-200 HTTP status

If the request exceeds `timeout_seconds`, the controller aborts the call.
If TypeSafe returns HTTP 429, 529, or another error, the controller aborts the call.
The controller falls back to the first legal `FULL` option, or `MANUAL_TRIAGE` if no legal `FULL` option exists.
The factory does not retry failed calls in V1.

### Invalid response schema or wrong model ID

If TypeSafe returns an unexpected JSON structure, strict schema validation fails.
If the response model version does not match `routing.model`, validation fails.
The controller falls back to the first legal `FULL` option, or `MANUAL_TRIAGE` if no legal `FULL` option exists.

### Response too large

If the HTTP response exceeds `max_response_bytes`, the transport aborts reading.
The controller falls back to the first legal `FULL` option, or `MANUAL_TRIAGE` if no legal `FULL` option exists.

### Unexpected route ratchet

If a `SINGLE` run ratchets to `FULL_REVIEW`, inspect `adjustments` in `route-decision.json`.
Examine whether the Implementer modified extra files or exceeded `single_max_changed_files`.
Ensure that the task scope covers all modified files.
