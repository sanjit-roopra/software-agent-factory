<!-- spec-version: 13.3.0 -->
# Spec: Run dashboard redesign (#80)

## Intent Description

The operator watches and compares factory runs in `factory dashboard`. The current page cannot answer basic questions. Which model did each call use? How many tokens and how much cost did each call and run use? Why did a run stop, and how does the operator continue it? How does a Copilot run compare with a pi run of the same task? The data is already in `run.json`. The page does not show it in a usable way.

The redesign puts usability first. For each run, the operator sees a call timeline and token and cost totals. A failed call or run shows a redacted reason. A halted run shows a "What next" panel that explains the next step. Two runs can be compared side by side, per role. The page has a modern layout with light and dark themes, and it updates live. The design takes ideas from modern admin dashboards (side navigation, a row of key figures, cards). It does not copy any template.

The operator can also approve a risk halt or answer plan decisions from the dashboard. The dashboard only creates an approval request file. The running factory service turns it into a receipt and reopens the run through the same controller path as an authorized GitHub reply. This adds the first write path to the dashboard, so a new ADR replaces the read-only rule of ADR-016.

Out of scope: changes to what runtimes record, hosting beyond localhost, retrying `FAILED` runs, and any new resume class.

## Architecture Specification

**Components**

| Component | Change |
| --- | --- |
| New `src/software_agent_factory/redaction.py` | Pure credential redaction moved out of `verification.py`, plus a bounded reason helper. No `subprocess`. |
| `src/software_agent_factory/dashboard/sanitize.py` | Allowlists and redaction only. Adds call fields, per-call cost and redacted failure reasons. |
| New `dashboard/aggregate.py`, `dashboard/next_step.py` | Pure run totals, per-role breakdown and the "Needs you" view model. |
| `src/software_agent_factory/dashboard/handler.py` | New `GET /api/compare`. New `POST /api/runs/<id>/approve` and `/answer`. Every other write method returns `405`. |
| `src/software_agent_factory/dashboard/static/app.js`, `style.css` | JS and CSS move out of Python strings into package files read with `importlib.resources`. No framework, npm or build step. |
| New `src/software_agent_factory/dashboard/actions.py` | Validates an approve or answer request. Calls an injected requester. |
| `src/software_agent_factory/store.py` | Create-only request file per run, episode and fingerprint. |
| `src/software_agent_factory/models.py` | `AcceptedReplyReceipt` and `PlanDecisionAnswers` gain `source`. A new `DashboardResumeRequest` model. |
| New `src/software_agent_factory/resume.py` | Pure answer rules, acceptance check and ingest, shared by `escalation.py`, `service.py` and the dashboard. |
| `src/software_agent_factory/service.py` | Ingests requests and reopens the run, even when GitHub escalation is off. |
| `src/software_agent_factory/observability.py` | Derives 24-hour key figures. |
| Docs | ADR-033 in `docs/decisions.md`, `AGENTS.md`, `docs/architecture.md`, `docs/guides/operations.md`, `docs/reference/safety.md`. |

**Constraints**

- Standard library only. Loopback bind, per-start token and strict `Host` checks stay.
- `POST` needs the token in the header (`X-Factory-Token`), a present and exact `Origin`, and a JSON body of at most 16 KB.
- The dashboard never writes `run.json`. It only creates a request file. The service is the single writer and reopens through `controller.reopen`.
- A request binds `run_id`, `episode_id` and the context fingerprint shown on the page.
- Costs stay in each runtime's own unit (ADR-017). The page never adds them together.
- Command logs and diffs stay hidden.
- Live updates use the existing 5-second poll.
- No new runtime dependencies.

**Slices (one PR each)**

1. Shell, themes, hash routes and refresh with connection notices.
2. Run detail: call timeline, totals, redacted reasons, a read-only "Needs you" panel.
3. Approval core with no HTTP: ADR-033, receipt source, request file, service ingest.
4. Approve and answer from the page.
5. Two-run comparison and key figures.

## Acceptance Criteria

- AC1 Each call shows number, role, model, outcome, duration, total tokens and cost in its own unit. Expanding the row shows purpose, reasoning level, start, the five token classes and the failure reason. Unreported values show "not reported". A reported `0` shows `0`. A running call shows "running". Calls are ordered by number.
- AC2 Run totals show tokens by class, calls, failed calls and duration. Each reported cost unit is shown on its own, labeled, with a one-line help text. A partial sum shows "N of M calls reported".
- AC3 Failure reasons for run, attempt and call are redacted first, then cut to at most 500 characters, keeping the start and the end. A cut reason is marked and names `factory show <run>` for the full text. Escalation free text is redacted too.
- AC4 A run waiting for a human shows a "Needs you" panel. It gives a plain sentence for the reason code, the resume class, and the approval scope or the numbered questions. It also shows "reopens used x of y", the comment link (https only) and a copyable reply. `NOT_RESUMABLE` and `FAILED` runs say they cannot continue and why. Other runs show no panel.
- AC5 A comparison of two runs shows a per-role table. It lists calls, failed calls, models, tokens by class, duration and cost per unit, one column set per run.
- AC6 `/api/compare` with an unknown, invalid or identical id returns `404` or `400` and no data from either run.
- AC7 Side navigation (Runs, Compare, Projects, Health) holds the existing views and the new ones. A key-figure row shows active runs, runs that need you, runs failed in the last 24 hours, and tokens in the last 24 hours.
- AC8 System theme by default, a remembered toggle, and an invalid stored value falls back to the system theme. Text contrast is at least 4.5:1, and large text and controls at least 3:1, in both themes.
- AC9 The open view refreshes every 5 seconds without a reload. Scroll position, expanded rows, a half-typed answer and an open confirm dialog survive a refresh. A fetch failure shows "connection lost, updated 12s ago" (whole seconds). A `401` shows "dashboard restarted, reload the page". The last data stays visible.
- AC10 No horizontal page scroll at 1280 px. Wide tables scroll inside their card.
- AC11 With a valid approval context, Approve opens a confirm dialog that lists what approval authorizes and excludes and says it starts agent work. Confirming records a request. The service reopens the run to `REFINING`.
- AC12 Plan answers follow the same rules as a GitHub reply. Submit stays disabled until every answer has 1 to 500 characters on one line. The service reopens the run to `PLANNING`, and the new plan prompt contains the answers.
- AC13 The server rejects a POST in these cases. A wrong `Host` gives `400`. A missing or wrong `Origin` gives `403`. A wrong or missing header token gives `401`. A non-JSON body gives `415`, and a body over 16 KB gives `413`. Bad JSON or fields give `400`, and an unknown run gives `404`. These give `409`: a run that is not waiting, a stale episode or fingerprint, the wrong action, a reached reopen limit, or an existing request. Nothing is written in those cases. Other `POST`, `PUT`, `PATCH` and `DELETE` requests return `405`.
- AC14 A dashboard receipt passes the same reopen checks as a GitHub receipt (ADR-024). These are the reopen limit, quota, concurrency, approval-context match and R2 or R3 delivery resume.
- AC15 After approval the panel shows "Approved at <time>, queued for the factory service. If `factory start` is not running, start it." The state comes from the server and survives a reload.
- AC16 ADR-033 replaces ADR-016's read-only rule and records that holding the per-start token replaces ADR-024 author checks for local approvals. `AGENTS.md` lists the two write actions.
- AC17 Each accepted or rejected POST writes one log event with run id and result, never the token or the body.
- AC18 Coverage ≥ 90%, 0 new Sonar issues (JS cognitive complexity ≤ 15), `ruff`, `mypy --strict` and the Simple English check pass.

## Glossary

| Term | Definition | Status | Source |
|------|------------|--------|--------|
| Invocation | One agent call, stored in `run.json` `invocation_records[*]` | `verified` | `models.py` `InvocationRecord`, issue #80 |
| Halt | A run in `NEEDS_HUMAN` with an escalation record | `verified` | ADR-024 |
| Resume class | `RISK_APPROVAL`, `PLAN_DECISION` or `NOT_RESUMABLE` | `verified` | `models.py` `ResumeClassification` |
| Dashboard request | A create-only file with an approval or answers that the service turns into a receipt | `verified` | User decision and plan review, 2026-10-01 |
| Cost unit | Premium requests, AI usage value (USD) or list-price estimate (USD). Units are never added together. | `verified` | ADR-017 |
| "Needs you" panel | Run detail section that explains why a run stopped and how to continue | `verified` | User request, 2026-10-01 |

## Ambiguity Log

| Decision | Classification | Resolved By | Rationale / Answer |
|----------|---------------|-------------|-------------------|
| Approve or continue from the dashboard | `requires-stakeholder-input` | human | Yes: write path with an Approve button. A new ADR replaces ADR-016's read-only rule. |
| Show free-text failure reasons | `requires-stakeholder-input` | human | Yes: redacted, capped at 500 characters, with an expand toggle. Amends ADR-016 data minimization. |
| Which actions | `requires-stakeholder-input` | human | Risk approval and plan answers only. `FAILED` and `NOT_RESUMABLE` runs get guidance only. |
| How an approval reaches the controller | `requires-stakeholder-input` | human | Local receipt. The running service calls `controller.reopen`. The dashboard never runs agents. |
| Opt-in flag for actions | `requires-stakeholder-input` | human | No flag. Always on for anyone with the per-start token. |
| Slicing | `requires-stakeholder-input` | human | Four slices, one PR each. The write path is slice 4 with its own ADR. |
| Live update mechanism | `inferable` | inference | The existing 5-second poll is standard library only. SSE adds a long-lived connection for no user gain. Alternative (SSE) rejected: ADR-016 favors the smallest server. |
| Theme selection | `inferable` | inference | Follow the system setting with a remembered toggle. This is the common convention. Alternative (toggle only) is ruled out by "light and dark mode". |
| Cost display across runtimes | `inferable` | inference | ADR-017 forbids adding units. Show each reported unit, labeled. |
| How the page knows the runtime | `inferable` | inference | `run.json` has no runtime field. The page labels cost by unit, which identifies the runtime without a new field. |
| Unreported token values | `inferable` | inference | ADR-017: unknown is never zero. |
| Comparison aggregation location | `inferable` | inference | The dashboard package is self-contained and cannot import `scripts/performance`. It gets its own pure per-role aggregation. |
| CSRF protection for POST | `inferable` | inference | Header token plus exact `Origin` plus JSON content type blocks cross-site form posts. This follows the existing `security.py` checks. |
| Duplicate approval | `inferable` | inference | One accepted receipt per episode, the same as the GitHub path. |
| No service running | `inferable` | inference | The receipt persists, and the next `factory start` dispatches it. The panel says so. |
| Completeness: delete or edit receipts | `inferable` | inference | Not offered. Receipts are an audit trail, and GitHub receipts cannot be deleted either. |
| Completeness: audit | `inferable` | inference | The receipt records source, time and fingerprint. A structured log event is written. |
| Completeness: authorization | `inferable` | inference | The per-start token is the only identity (single local operator, ADR-016). Human decision: no extra flag. |
| Who writes `run.json` | `inferable` | plan review | The store has no per-run lock. The service stays the only writer. The dashboard creates a request file with a create-only write, which also blocks a double approval. |
| Plan answers storage | `inferable` | plan review | Answers live in `PlanDecisionAnswers`, which needs the same `source` field as the receipt. |
| Status codes | `inferable` | plan review | Keep the existing `401` for a bad token and `403` for `Origin`. `POST` requires `Origin`. |
| Reopen limit at request time | `inferable` | plan review | Accept only while `reopen_count < max_reopens`, the same as the GitHub path. |
| Dashboard receipt identity | `inferable` | plan review | `user_login` is the fixed value `dashboard-local`. The per-start token replaces the GitHub author check (ADR-033). |
| Truncation shape | `inferable` | plan review | Redact first, then keep the start and the end. Causes often sit at the end of an error. |
| Slice count | `inferable` | plan review | Five PRs. The approval slice splits into core and page parts. The shell goes first so later slices land in the final layout. |
| Service running detection | `LOW_VALUE` | plan review | Skipped. The approved panel always says to start `factory start` if it is not running. |

## Consistency Gate

- [x] Intent is unambiguous
- [x] Every behavior/goal maps to an acceptance criterion
- [x] Architecture constrains without over-engineering
- [x] Terminology consistent across artifacts
- [x] No contradictions between artifacts
- [x] Every gap/ambiguity finding is logged: inferable with rationale or resolved by human

**Verdict: PASS**
