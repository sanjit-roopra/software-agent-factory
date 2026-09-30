<!-- spec-version: 13.3.0 -->
# Spec: pi command filter (#70)

## Intent Description

The pi runtime (ADR-031, recommended runtime) gives the IMPLEMENTER an unrestricted `bash` tool. pi has no approval layer, so an implementer can run `git commit`, `git push`, `gh`, `curl` or `wget`. The Copilot runtime denies these with pattern rules. This change gives pi the same restrictions, and no more.

A factory-owned pi extension inspects every `bash` tool call from the implementer. It blocks denied commands with a reason that tells the agent what to do instead. The factory also removes `SSH_AUTH_SOCK` from pi's environment, so `git push` over SSH has no keys. The unconditional `--runtime pi` startup warning goes away.

An OS sandbox for pi is out of scope. Copilot has none either.

## Architecture Specification

**Components**

| Component | Change |
| --- | --- |
| New `src/software_agent_factory/pi_extensions/command_filter.mjs` | Plain ESM with no dependencies. A pure `blockedReason(command)` plus a default export that registers `pi.on("tool_call")` and returns `{ block: true, reason }` for `bash` calls. |
| `src/software_agent_factory/pi_runtime.py` | `_build_command` adds `-e <filter path>` for IMPLEMENTER_WRITE. It keeps `--no-extensions`, which still honours explicit `-e`. When the filter file is missing, the call fails. `_child_env_and_scrubbed` removes `SSH_AUTH_SOCK`. |
| `pyproject.toml`, `packaging/pyinstaller.spec` | Ship the `.mjs` file in the wheel and in the frozen build. |
| `src/software_agent_factory/cli.py` | Remove `_warn_pi_unrestricted_shell`, `PI_UNRESTRICTED_SHELL_FOLLOWUP_URL` and the "unrestricted shell tool" help text. |
| Docs | `docs/reference/safety.md`, `docs/get-started/pi.md`, `docs/reference/cli.md`, and a short ADR in `docs/decisions.md` that amends ADR-031. |

**Command filter rules (IMPLEMENTER_WRITE only)**

- Blocked: `git commit`, `git push`, any `gh` command, `curl`, `wget`. This mirrors the Copilot deny list (`shell(git commit)`, `shell(git push)`, `shell(gh:*)`, `url`).
- The filter checks each command segment. Segments are split on `;`, `&&`, `||`, `|`, newlines, `$(…)` and backticks. It unwraps `sh -c`/`bash -c` bodies, `env`/`command` prefixes and variable assignments. It skips `git` global options (`-C`, `-c`, `--git-dir`). It matches the program by its base name, so `/usr/bin/git push` counts.
- Quoted arguments of other programs are not commands. For example, `echo "git push"` and `grep -rn gh src` pass.
- A `bash` call with a missing or non-string command is blocked.
- Reason format: `Blocked by the factory command filter: '<rule>' is not allowed. <what to do instead>. Do not retry or rephrase this command.`
- The filter has the same strength as Copilot's pattern rules. It is not a security boundary.

**Constraints**

- No change to the Copilot runtime or to read-only and no-tools pi roles. They have no `bash` tool.
- The filter tests need Node 22 or newer. CI sets up Node. A local run without Node skips them, and CI fails if they skip.

## Acceptance Criteria

1. **AC1** An IMPLEMENTER on pi that runs `git commit`, `git push`, `gh`, `curl` or `wget`, alone or inside a compound command, gets a blocked tool call. The reason follows the reason format and names the rule.
2. **AC2** `git status`, `git diff`, `git add`, `uv run pytest`, `ls`, `echo "git push"` and `grep -rn https:// src` are not blocked.
3. **AC3** Only IMPLEMENTER pi commands load the filter. When the packaged filter file is missing, the implementer call fails with a reason that names the file.
4. **AC4** pi's child environment has no `SSH_AUTH_SOCK`.
5. **AC5** The wheel and the frozen build contain the filter file.
6. **AC6** `_warn_pi_unrestricted_shell`, `PI_UNRESTRICTED_SHELL_FOLLOWUP_URL` and the "unrestricted shell tool" help text are removed. No warning is printed for `run` or `service install` with `--runtime pi`.
7. **AC7** The safety docs and the pi guide describe the filter. An ADR records it and amends ADR-031.
8. **AC8** One manual check with a real pi process shows that `git push` is blocked with the reason.
9. **AC9** Coverage stays at or above 90%, with no new Sonar issues.

## Glossary

| Term | Definition | Status | Source |
|------|------------|--------|--------|
| command filter | The packaged pi extension's `tool_call` handler that blocks denied `bash` commands | `verified` | user, /specs 2026-09-30 |
| blocked | A tool call that pi does not run, returned to the agent with a reason | `verified` | pi `tool_call` API |

## Ambiguity Log

| Decision | Classification | Resolved By | Rationale / Answer |
|----------|---------------|-------------|-------------------|
| OS sandbox | `requires-stakeholder-input` | human | Dropped. Match Copilot, which has no sandbox. "Always go lean." |
| URL rule | `requires-stakeholder-input` | human | Block `curl` and `wget` only, as the lean Copilot match. |
| SSH agent keys | `inferable` | inference | A spike showed ssh-agent keys authenticate to GitHub. Removing `SSH_AUTH_SOCK` is one line. |
| Filter file missing | `inferable` | inference | Fail the call. Running unfiltered silently drops the control. |
| Quoted text | `inferable` | inference | Only command positions count, so docs and grep work stays possible. |
| Read-only roles | `inferable` | inference | They have no `bash` tool, so no filter is needed. |
| Extension file type | `inferable` | inference | `.mjs` needs no build step. The manual check confirms pi loads it. |

## Consistency Gate
- [x] Intent is unambiguous
- [x] Every behavior/goal maps to an acceptance criterion
- [x] Architecture constrains without over-engineering
- [x] Terminology consistent across artifacts
- [x] No contradictions between artifacts
- [x] Every gap/ambiguity finding is logged: inferable with rationale or resolved by human
