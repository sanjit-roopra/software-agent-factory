# Roadmap and status

The implementation order is a numbered phase list. It lives in
[`PLAN.md`](https://github.com/sanjit-roopra/software-agent-factory/blob/main/PLAN.md),
which is the authoritative record. This page summarizes it.

Phases 0–14 and Phase 16 are implemented and integrated. Five Phase 15
sub-phases were explicitly requested and are implemented. Every other Phase 15
item, including staging and deployment, is deferred.

## Phases

| Phase | Scope | Status |
| --- | --- | --- |
| 0 | Architecture sanity check | done |
| 1 | Deterministic vertical slice | done |
| 2 | Real Copilot planner | done (`CopilotAgentRuntime`) |
| 3 | Real specification + triage | done |
| 4 | Real implementer | done |
| 5 | Repository verification policy | done (`governance.RepositoryVerifier`) |
| 6 | Tester + reviewer | done (independent `TestReport`/`ReviewReport`) |
| 7 | Routing calibration | done (`routing.ModelRouter`) |
| 8 | Research | done (optional, at most once per run) |
| 9 | Scope drift | done (`governance.ScopeDriftPolicy`) |
| 10 | Pull request creation | done, opt-in (`pull_request.enabled`) |
| 11 | GitHub Actions observation | done, opt-in (`ci.enabled`) |
| 12 | CI repair | done, bounded by `ci.repair_attempts` |
| 13 | Local backlog daemon | done (`factory start`, opt-in) |
| 14 | Parallelism | done (`max_concurrent_tasks` 1 or 2) |
| 15.0 | Factory CI for this repository | done |
| 15.1 | Tag-driven release / continuous delivery | done |
| 15.2 | macOS packaging + user launchd service | done |
| 15.3 | Staging environment | deferred |
| 15.4 | Deployment / promotion | deferred |
| 15.5 | Local monitoring and health | done (`factory doctor`, `factory status`) |
| 15.6 | Docker sandboxed execution | deferred |
| 15.7 | Remote workers | deferred |
| 15.8 | Postgres run store | deferred |
| 15.9 | Temporal / durable workflow engine | deferred |
| 15.10 | Jira and other trackers | deferred |
| 15.11 | Read-only local dashboard | done (`factory dashboard`) |
| 15.12 | Kubernetes workers | deferred |
| 16 | Repository capability layer + bounded post-green polish | done |

## Known limits

- **Platform.** The supported platform is macOS, native arm64 and native
  x86_64, with no `universal2`. A source checkout needs Python 3.13+. Other
  platforms are not tested or supported.
- **Signing.** Release archives are unsigned or ad-hoc signed. Developer ID
  signing and notarization are deferred, so Gatekeeper quarantine is expected.
  See [Releases](releases.md).
- **Release immutability.** The release workflow refuses to replace an existing
  release, but GitHub's own release immutability is a repository setting that is
  not enabled; releases currently report `immutable=false`. Verify
  `SHA256SUMS`.
- **Cost reporting.** No runtime reports token usage or cost today, so those
  fields stay unknown. They are never estimated.
- **Trackers.** GitHub Issues is the only backlog provider.
- **Concurrency.** `scheduler.max_concurrent_tasks` is validated to `1` or `2`.
- **Capabilities.** Repository skills are generated fresh, per eligible
  polish run, by the configured Researcher, with web access limited to
  `polish.official_documentation_origins` and `polish.practice_reference_urls`.
  There is no fixed built-in catalog, cross-run cache, or repository-provided
  skill/plugin system, and a skill that cannot be generated or verified is
  skipped rather than failing the run.
- **Two release behaviours can only be proven in CI.** Publishing a real `v*`
  tag, and the native Intel (`macos-15-intel`) build. Both are implemented and
  statically tested; the first real tag exercises them end to end.

## Deliberately not planned

Deferred means "not now". These are stronger than deferred — they are design
constraints for V1:

- autonomous merge
- autonomous deployment
- a hosted, multi-user or networked service
- a control plane, or any dashboard write path
- telemetry, analytics or a metrics exporter
- long-term semantic memory or a vector database
- an agent swarm
- a generic workflow DSL or plugin architecture
- LangGraph

The read-only local dashboard is the single, documented exception to the V1 ban
on web UIs. Its bounds are recorded in
[ADR-016](../decisions.md#adr-016-the-local-dashboard-is-a-bounded-exception-to-the-v1-ban).

## How scope changes

The rule in
[`AGENTS.md`](https://github.com/sanjit-roopra/software-agent-factory/blob/main/AGENTS.md):
if implementation reveals the architecture should change, stop, describe the
problem, propose the smallest correction, update the architecture
documentation, and only then implement. Silent redesign is not allowed.

New decisions are recorded as ADRs in [Decisions](../decisions.md).
