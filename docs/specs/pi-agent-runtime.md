<!-- spec-version: 13.3.0 -->
# Spec: Pi agent runtime

## Intent Description

The factory runs every agent call through GitHub Copilot CLI. Each call starts a new
process with no memory of earlier calls. A repair round therefore re-sends the full instructions, work item and
code context from scratch. Repair rounds follow a test failure, a review finding, or a
scope or CI problem. Input tokens dominate agentic spend (about
82% of the bill in the "Hidden Economics of Agentic Software Engineering" talk, Matthias
Lau, 2026-09-24). Most of that input repeats, so the share served from the provider
prompt cache decides the bill. The same talk reports 35–52% lower cost for the same
model when only the agent program changes.

This change adds pi (`@earendil-works/pi-coding-agent`) as a second agent runtime,
selected for a whole run with `--runtime pi`. Pi keeps a persisted session per work
item and role. The IMPLEMENTER and the REVIEWER continue their own session on each
repair or re-review round. They send only what changed. The earlier history is then a
stable prefix the provider can serve from cache. Billing stays on GitHub Copilot at
first because pi logs in to the `github-copilot` provider. Other pi providers stay
possible through configuration.

The change is done when two things hold. The pi runtime runs the full workflow end to
end. An A/B benchmark against Copilot CLI replays issues of this repository. It reports
tokens, cache-read share, cost and pass rate for both runtimes side by side. Tool
restriction beyond pi's built-in tool allowlist, and sandboxing, are out of scope and
tracked as a follow-up.

## Architecture Specification

### Components

| Component | Change |
| --- | --- |
| `pi_runtime.py` (new) | `PiAgentRuntime` implements the existing `AgentRuntime` protocol (`agents.py`, single synchronous `run(request) -> AgentResult`). |
| `cli.py` | `RuntimeChoice.PI = "pi"`. `_build_runtime` constructs `PiAgentRuntime`. every command that accepts `--runtime` accepts `pi`. |
| `service_install.py` | `ServiceRuntime.PI`. |
| `doctor.py` | `check_pi`: `pi` on PATH, version at least the pinned minimum, Node at least 22.19, provider credentials present. |
| `config.py` | New `pi` block (see Configuration). |
| `prompts.py` | Continuation prompt: only the round-specific sections (repair context, prior findings, changes since previous review, typed-output correction). |
| `models.py` | `ModelUsage` and `UsageMetrics` gain `list_price_estimate_usd: float | None`. |
| `dashboard/` | Shows the list-price estimate labelled as an estimate, never summed with Copilot usage value. |
| `scripts/performance/runtime_ab.py` (new) | Offline A/B driver and report. |
| `docs/decisions.md` | New ADR for the pi runtime. amendment to ADR-017 for the estimate field. |

### Pi process contract

- One `pi --mode rpc` subprocess per `run()` call. The call starts pi, sends commands
  as JSONL on stdin, reads JSONL records from stdout, then closes stdin and reaps the
  process. The runtime holds no live processes between calls.
- Working directory is `request.workspace_path` (same rule as `agents.workspace_cwd`).
- Flags always passed: `--mode rpc`, `--provider <pi.provider>`, `--model <id>`,
  `--thinking <level>`, `--tools <role allowlist>` (or `--no-tools`), `--no-extensions`,
  `--no-skills`, `--no-prompt-templates`, `--no-context-files`, `--no-approve`, and
  either `--session <file>` or `--no-session`.
- Completion is the `agent_settled` event. Final text comes from
  `get_last_assistant_text`. It is parsed with the existing `parse_copilot_artifact`
  so typed-artifact failure wording, and therefore `is_retryable_typed_artifact_failure`,
  is unchanged.
- An assistant message with `stopReason` `error` or `aborted` yields a failed
  `AgentResult` with a sanitized `failure_reason`.
- Timeout (`request.timeout_seconds`): send `abort`, close stdin (pi exits on stdin
  EOF, not necessarily in response to `abort`), wait a short grace period, then kill
  the process group. Partial usage is still recorded.
- The child environment is scrubbed like the Copilot runtime, except for the
  variables pi needs for the configured provider.

### Role tool allowlists (v1)

| Request | pi tools |
| --- | --- |
| IMPLEMENTER | `read,bash,edit,write,grep,find,ls` |
| TRIAGE, REFINER, RESEARCHER, PLANNER, TESTER, REVIEWER | `read,grep,find,ls` |
| `CORRECT_CHANGE_SET` | `--no-tools` |
| `GENERATE_REPOSITORY_SKILL` | Not supported on pi in v1 (no web fetch tool). |

Bash command denial (`git commit`, `git push`, `gh`) and sandboxing are out of scope.
This means the pi implementer does not meet the ADR-022 rule that implementers
cannot run `git commit` directly. The Copilot runtime still denies it. There is no
runtime refusal for pi, because the implementer has every tool and can find
another way to push. ADR-029 records the gap as an amendment to ADR-022, and issue
#70 closes it.

### Sessions

- Session key: work item identifier and role. Session files live under
  `<factory.data_dir>/pi-sessions/<work item>/<role>.jsonl`.
- Only IMPLEMENTER and REVIEWER sessions are continued. All other roles run with
  `--no-session` (or a fresh session file) every call.
- A call continues an existing session only when all hold:
  - the session file exists and is readable
  - model, provider and reasoning level equal those of the session's last call
  - the last call ended less than `pi.session_reuse_max_age_seconds` ago
  Otherwise the call starts a new session file and sends the full prompt. The old
  file is kept while it is younger than `pi.session_reuse_max_age_seconds`. A new
  session also deletes that role's older files. It deletes a file that was last
  written that long ago or longer, because it can never be continued. Nothing else
  deletes a session file.
- Session files hold prompts, repository content and tool output. The `pi-sessions`
  folder and each work item folder are private to the owner (mode `0700`). Session
  files and the sidecar are private to the owner too (mode `0600`).
- A continued call sends the continuation prompt (round-specific sections only). A
  new session sends the full prompt from `build_prompt`.
- IMPLEMENTER and REVIEWER sessions are always separate. Model switching inside a
  session never happens. Escalation to another model starts a new session.
- Parallel work items (bounded by `scheduler.max_concurrent_tasks`) never share a
  session file because the key includes the work item.

### Usage mapping

| pi field (per assistant message `usage`, summed per model) | Factory field |
| --- | --- |
| `input` | `input_tokens` |
| `output` | `output_tokens` |
| `reasoning` | `reasoning_tokens` (already included in `output`) |
| `cacheRead` | `cache_read_tokens` |
| `cacheWrite` (+ `cacheWrite1h`) | `cache_write_tokens` |
| `cost.total` | `list_price_estimate_usd` |
| count of assistant messages | `requests` |

Premium-request cost and nano-AIU stay `None` for pi runs. A field pi did not report
stays `None`, never zero.

### Configuration

```yaml
pi:
  executable: pi
  provider: github-copilot
  session_reuse_max_age_seconds: 3600
  cache_retention: long   # sets PI_CACHE_RETENTION for the child
```

Model ids and reasoning levels in `models:` are passed through unchanged. Copilot's
`--context` tier has no pi equivalent and is ignored with a debug log.

### Constraints

- No change to the `AgentRuntime` protocol, to `WorkflowController`, or to the
  Copilot runtime's behavior.
- Tests never start a real pi process or make a paid call. The subprocess boundary is
  faked the same way as `tests/test_copilot_runtime.py`.
- The default runtime stays `fake` (ADR-018).

### Dependencies

- `@earendil-works/pi-coding-agent` (0.87.1 at time of writing), Node 22.19 or later.
  The old `@mariozechner/pi-coding-agent` package stops at 0.73.1 and is not supported.

## Acceptance Criteria

1. `factory run --runtime pi` (and every other command that accepts `--runtime`)
   runs the workflow through `PiAgentRuntime`. An unknown runtime value is rejected
   with the existing error.
2. `factory doctor --runtime pi` fails with a named reason when `pi` is missing, too
   old, Node is too old, or provider credentials are absent. It passes otherwise.
   Checks run in that order and the first failure is reported. The message gives the
   found and required version or the command that fixes it.
3. Each role invokes pi with the model, thinking level, working directory and tool
   allowlist in the tables above.
4. A pi response is turned into the same typed artifact the Copilot runtime
   produces for the same assistant text. Malformed output produces the same
   retryable failure wording as today.
5. A pi assistant error, abort or non-zero exit yields a failed `AgentResult` with a
   sanitized `failure_reason`. No credential appears in it.
6. A call that exceeds `timeout_seconds` is aborted, then killed if still alive, and
   returns a failed result that still carries the usage reported so far.
7. An IMPLEMENTER repair round, and a REVIEWER re-review, continue the previous
   session and send only the continuation prompt when the reuse conditions hold.
8. When any reuse condition fails, the call starts a new session with the full prompt.
   Failing conditions are a missing or unreadable file, a changed model, provider or
   reasoning level, or a session that is too old.
9. TRIAGE, REFINER, RESEARCHER, PLANNER and TESTER never continue a session.
10. Two work items running at the same time never read or write the same session file.
11. Usage is recorded per the mapping table. Unreported fields are `None`. Premium
    request cost and nano-AIU are `None` for pi runs.
12. The dashboard shows `list_price_estimate_usd` labelled as a list-price estimate and
    never adds it to the Copilot usage value.
13. `skill refresh --runtime pi` fails fast with a clear "not supported on pi"
    message that names `--runtime copilot` as the alternative.
14. A spike records whether `github-copilot` calls through pi report `cacheRead` and
    `cacheWrite` for Claude and for OpenAI models. It checks two cases: two prompts in
    one process, and a second process that resumes the same session file. The result
    is written into the ADR.
    If Copilot reports no cache counts, the benchmark states that the cache-read share
    is unavailable for that provider.
15. `scripts/performance/runtime_ab.py` replays a manifest of about 10 closed issues
    of this repository at their base commit, once per runtime, with identical models
    and reasoning. It stops when a configured budget is reached.
16. The A/B report shows these values per task and in total, for each runtime:
    - pass (run reached PR-ready with verification green)
    - tokens by class
    - cache-read share (`cache_read / (input + cache_read + cache_write)`)
    - each runtime's own cost unit
    - wall time and number of repair rounds
17. A new ADR records the pi runtime decision. ADR-017 is amended to allow a labelled
    list-price estimate that is never presented as spend. The ADR and the docs call pi
    recommended only when the A/B result meets the go bar in the plan. Otherwise they
    call it experimental.
18. A follow-up issue exists for pi tool-call restriction (bash command denial) and
    sandboxing.
19. Selecting `--runtime pi` logs a warning that the pi shell tool is unrestricted.
    The warning links the follow-up issue.

## Glossary

| Term | Definition | Status | Source |
|------|------------|--------|--------|
| Agent program | The program that drives a model: tools, context handling, caching behavior. Copilot CLI and pi are agent programs. | `verified` | Talk. user conversation |
| Pi session | Pi's persisted JSONL conversation file. Resuming it re-sends its history as the prompt prefix. | `unverified` | pi docs `session-format.md` |
| Continuation prompt | The prompt sent into a continued session: only the round-specific sections. | `verified` | User conversation (append, do not rewrite) |
| Cache-read share | `cache_read / (input + cache_read + cache_write)` tokens. | `unverified` | Agent definition. talk uses "share of input served from cache" |
| List-price estimate | Cost pi computes from its own model price catalog. Not what the provider billed. | `verified` | pi source `calculateCost`. user decision |
| Repair round | A new IMPLEMENTER call triggered by `VERIFICATION`, `REVIEW`, `SCOPE`, `CI`, `POLISH` or `IMPLEMENTER_FAILURE`. | `unverified` | `AttemptTrigger` in `models.py` |

## Ambiguity Log

| Decision | Classification | Resolved By | Rationale / Answer |
|----------|---------------|-------------|-------------------|
| How pi is selected | `requires-stakeholder-input` | human | Global `--runtime pi`, whole run on one agent program, for a clean A/B. |
| Which roles continue a session | `requires-stakeholder-input` | human | IMPLEMENTER and REVIEWER. |
| pi process lifetime | `requires-stakeholder-input` | human | New process per call, resuming the session file. Runtime stays stateless. |
| What to do with pi's list-price cost | `requires-stakeholder-input` | human | Persist as a separate labelled estimate field. amend ADR-017. |
| Benchmark corpus | `requires-stakeholder-input` | human | Replay about 10 closed issues of this repository. |
| Tool restriction and sandbox | `requires-stakeholder-input` | human | Out of scope for v1. follow-up issue. Built-in `--tools` allowlist still used because it costs nothing. |
| Billing provider | `requires-stakeholder-input` | human | `github-copilot` through pi first. |
| Integration mode (RPC vs print vs SDK) | `inferable` | inference | SDK is Node-only. RPC gives `abort`, `get_session_stats`, `get_last_assistant_text`. |
| Model id and reasoning mapping | `inferable` | inference | Pass through. pi thinking levels are a superset of the reasoning values used today. |
| Copilot `--context` tier | `inferable` | inference | No pi equivalent. ignore and log. |
| Resume when model or reasoning changed | `inferable` | inference | The cache is per model. switching re-writes the whole history. Start a new session. |
| Resume after the cache expires | `inferable` | inference | Old history is re-sent uncached. Start fresh past `session_reuse_max_age_seconds`. |
| Skill generation on pi | `inferable` | inference | Pi has no web fetch tool. reject clearly rather than run without it. |
| Session file deletion | `inferable` | inference | Keep until the run ends. retention follows existing run data retention. |
| Package name | `inferable` | inference | `@mariozechner/...` is frozen at 0.73.1. `@earendil-works/...` is current. |
| Headless auth for `github-copilot` | `inferable` | inference | Operator runs `pi` `/login` once. doctor checks the credential exists. `COPILOT_GITHUB_TOKEN` with a plain PAT is unverified. |
| Copilot cache-count reporting | `inferable` | inference | Unknown. handled by the spike in criterion 14 instead of assumed. |
| Cancellation from the scheduler | `inferable` | inference | Scheduler cancel does not stop a running subprocess today. v1 keeps parity. Timeout uses `abort`. |
| Completeness: sessions (create, read, update, delete) | `inferable` | inference | Create on first call, read and append on continuation, no manual update, removed with the run. |
| Completeness: auth, audit, errors | `inferable` | inference | Auth by pi credentials (criterion 2). audit through existing `InvocationRecord`. errors per criteria 5 and 6. |

## Consistency Gate

- [x] Intent is unambiguous
- [x] Every behavior/goal maps to an acceptance criterion
- [x] Architecture constrains without over-engineering
- [x] Terminology consistent across artifacts
- [x] No contradictions between artifacts
- [x] Every gap/ambiguity finding is logged, inferable with rationale or resolved by human
