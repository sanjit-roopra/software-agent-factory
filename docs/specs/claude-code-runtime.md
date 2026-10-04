<!-- spec-version: 13.3.0 -->
# Spec: Claude Code runtime and mixed runtimes

## Intent Description

The factory has two real agent runtimes: GitHub Copilot CLI and pi. One `--runtime`
choice applies to every agent call in a run. A Claude subscription cannot be used
through pi, because the subscription terms do not allow it in a third-party agent. The
Claude Code CLI can use the subscription.

This change adds Claude Code as a third runtime. It also lets each role and each worker
tier choose its own runtime. One run can then mix runtimes, for example a pi triage, a
Claude Code planner and Copilot workers. Each subscription then pays for the steps it is
best at.

A role can also name a fallback runtime and model. The factory uses the fallback when
the first runtime cannot serve the call, for example at a Claude usage limit. A wrong
result is not a reason to fall back. The existing repair and escalation rules handle
that.

The change is done when two things hold. One run with roles on all three runtimes completes
end to end. A forced usage-limit error on Claude Code moves the call to its fallback.

## Architecture Specification

### Components

| Component | Change |
| --- | --- |
| `claude_code_runtime.py` (new) | `ClaudeCodeAgentRuntime` implements `AgentRuntime`. One `claude -p` subprocess per `run()`. |
| `runtime_router.py` (new) | `RoutingAgentRuntime` implements `AgentRuntime`. It sends each request to the runtime the request names, and calls the fallback once when the first runtime is unavailable. |
| `models.py` | `RuntimeName` enum (`copilot`, `pi`, `claude-code`). `ModelUsage.runtime`. |
| `config.py` | `RoleModelConfig.runtime` and `RoleModelConfig.fallback`. Optional `claude_code` block. |
| `agents.py` | `AgentRequest.runtime` and `AgentRequest.fallback`. `AgentResult.runtime_unavailable`. |
| `routing.py` | The escalation key includes the runtime. |
| `workflow.py` | Copies `runtime` and `fallback` from the resolved `RoleModelConfig` into each `AgentRequest`. |
| `cli.py`, `service_install.py` | `--runtime claude-code`. `_build_runtime` returns a `RoutingAgentRuntime`. |
| `doctor.py` | Checks every runtime that the configuration or `--runtime` uses. |
| `docs/` | ADR-045, configuration reference, runtime guide. |

### Claude Code process contract

- Command: `claude -p --output-format stream-json --verbose --model <model>
  --effort <reasoning> --no-session-persistence --strict-mcp-config
  --setting-sources "" --permission-mode acceptEdits --tools <list>
  [--allowedTools Bash] --disallowedTools <list>`. The prompt goes to stdin.
- `cwd` is the run worktree, as for Copilot.
- Auth is the logged-in subscription. `--bare` is not used, because it reads only
  `ANTHROPIC_API_KEY`. The child process does not get `ANTHROPIC_API_KEY`,
  `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_USE_BEDROCK` or `CLAUDE_CODE_USE_VERTEX`.
- `acceptEdits` keeps the file tools inside the worktree. A live call confirmed
  that a read and a write outside it are denied. `bypassPermissions` is not used,
  because it allows both.
- `--setting-sources ""` stops the user's settings, plugins, hooks and rules from
  loading. `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` turns off auto-memory. A live call
  confirmed both on Claude Code 2.1.289.
- Tool profiles map `AgentCapability` like the Copilot profiles do:
  - read-only roles: `--tools Read,Grep,Glob`.
  - implementer: `--tools Read,Edit,Write,Bash,Grep,Glob`, `--allowedTools Bash`,
    and `--disallowedTools` for `git commit`, `git push`, `gh`, `curl`, `wget`,
    `WebFetch` and `WebSearch`, as for pi (ADR-032).
- The final `result` event carries the text, `usage` and `total_cost_usd`. The text is
  parsed with the shared artifact parser. Tokens go to `ModelUsage`.
  `total_cost_usd` goes to `list_price_estimate_usd`, because a subscription does not
  bill per call.
- `reasoning` maps 1:1 to `--effort` (`low`, `medium`, `high`, `xhigh`, `max`). Any
  other value is a configuration error for a `claude-code` role.

### Runtime choice per call

- A role without `runtime` uses the `--runtime` value. Existing configurations behave as
  before.
- `--runtime fake` sends every call to the fake runtime and ignores per-role runtimes,
  so a dry run never makes a paid call.
- `RoutingAgentRuntime` builds a runtime the first time a request needs it.
- Worker escalation treats two tiers with the same model but different runtimes as
  distinct models.

### Fallback

```yaml
models:
  workers:
    L3:
      runtime: claude-code
      model: claude-opus-5-5
      reasoning: high
      fallback: {runtime: pi, model: gpt-5.6-terra, reasoning: high}
```

- `fallback` has `runtime`, `model`, `reasoning` and optional `context_tier`. It has no
  nested fallback.
- A result is `runtime_unavailable` when:
  - the executable is missing (all runtimes), or
  - Claude Code reports a usage limit, a rate limit, an overload or an auth error.
- On `runtime_unavailable` with a fallback set, the router calls the fallback once with
  the same request. The attempt counter does not change.
- The result records the runtime and model that served it in `ModelUsage`. The log
  records the fallback and its reason.
- Without a fallback, an unavailable result fails the attempt as today.

### Out of scope

- Session continuation for Claude Code. Each call is a fresh process, as for Copilot.
- Fallback detection for Copilot and pi beyond a missing executable.
- A choice of runtime per run or per issue.

## Acceptance Criteria

1. `factory run --runtime claude-code` completes a run in which every role uses Claude
   Code.
2. A configuration with roles on `copilot`, `pi` and `claude-code` sends each call to its
   runtime. A test with recording fakes proves this.
3. A configuration without any `runtime` field produces the same requests as before.
4. `--runtime fake` never builds a real runtime.
5. A Claude Code usage-limit result with a fallback set returns the fallback result, and
   the attempt number stays the same.
6. A model failure (bad artifact, failing tests) never triggers the fallback.
7. A `claude-code` role with a `reasoning` value that `--effort` does not accept fails
   configuration load with a clear message.
8. `factory doctor` checks `claude` only when the configuration or `--runtime` uses it.
9. The Claude Code command never contains `--bare` and always contains
   `--strict-mcp-config` and `--no-session-persistence`.
10. Each persisted `ModelUsage` names the runtime that served the call.

## Delivery

One PR per slice:

1. `ClaudeCodeAgentRuntime`, `--runtime claude-code`, doctor check, service install.
   ADR-045.
2. Per-role `runtime`, `RoutingAgentRuntime`, escalation key, `ModelUsage.runtime`.
3. `fallback` and `runtime_unavailable`.
