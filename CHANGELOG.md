# Changelog

This file records user-visible changes.

The project follows [Semantic Versioning](https://semver.org/) while the public
API is still `0.x`.

## Unreleased

### Added

- Opt-in autonomous project delivery: task PRs pass local verification,
  independent review and bounded CI repair before guarded merging into the
  configured target branch. Dependent tasks start from merged predecessors.
- A separate `merge` policy requiring explicit repository and check allowlists,
  a target branch, non-draft PRs and deterministic verification commands.
  Merges match the reviewed head and never bypass branch protection.
- Explicit `factory project --resume` reconciliation of persisted plans, task
  identities and safe delivery checkpoints, without fresh retry budgets.
  Ambiguous interrupted implementation still stops for human intervention.
- Exact `scope_drift.approved_sensitive_files` authorization for planned
  dependency and CI bootstrap files, preserving all other governance gates.
- Project and task progress in the read-only dashboard, including the currently
  active invocation and lease-derived `running`, `stale`, `crashed` or
  `abandoned` status.
- A dashboard-only AI usage value in USD when Copilot reports nano-AIU. Raw
  usage remains persisted unchanged, and the displayed value is not an invoice
  charge.

### Changed

- Pytest now runs the complete suite in parallel with work stealing and at most
  12 workers, keeping the deterministic branch-coverage gate intact while
  reducing local verification time.
- Reviewer repair now uses a persisted typed blocker ledger with stable
  controller-owned ids, required dispositions and exact reviewed-tree deltas.
  Repair regressions remain blocking, one late-finding batch may be adopted,
  and contradictory or drip-fed review loops stop early with an actionable
  `review-impasse.json` instead of generic attempt-budget exhaustion.
- Repository-skill generation now retries once after either invalid guidance
  or an infrastructure failure. Invalid output receives the bounded validation
  reason, and a second failure still skips optional polish safely.
- Pull-request creation now retries one transient GitHub failure. It checks for
  an already-created PR before retrying, so a lost response cannot create a
  duplicate.
- Every published revision, including CI repairs, is bound to independent
  Reviewer approval. The current review result is reflected in the PR body.
- PR publication can reconcile a matching factory-owned PR after interruption
  rather than creating a duplicate.
- Git push now retries one transient transport or remote-backend failure and
  reconciles the exact remote branch tip before retrying or failing.
- GitHub CLI operations use the authenticated `gh` host independently from an
  allowlisted Git SSH transport alias. PR identity is rechecked during CI and
  merge operations.
- CI polling treats an initial "no checks reported" response as pending, not as
  an immediate command failure.
- Planned file-count ranges are advisory. The configured changed-file ceiling
  remains the hard publication limit.
- Planner, Tester and Reviewer schema failures get bounded same-model output
  correction with the exact validation error. Tester and Reviewer corrections
  do not consume implementation attempts.
- Verification commands that modify the Git tree are detected before review.
  The Implementer receives a repair request to remove generated artifacts or
  make the verification step read-only.
- Tester and Reviewer prompts now include the work item and execution plan.
  Reviewer repair attempts also retain earlier blocking findings from the run.

## 0.4.1 - 2026-09-07

### Added

- `factory run` now accepts repeatable `--acceptance-criterion` and
  `--constraint` options and persists them on the manual `WorkItem`.

### Fixed

- Worktree administration is now serialized through the repository's common
  Git directory, preventing concurrent factories with different data
  directories from racing on shared worktree metadata.
- Repository verification uses a non-login shell, preventing shell profiles
  from reintroducing credentials and other filtered environment variables.
- Project-generated commits now use the deterministic Software Agent Factory
  author and committer identity even when the host environment sets Git
  identity variables.

## 0.4.0 - 2026-09-07

### Added

- Complete selectable `default`, `economy` and `security` model profiles.
  `--model-profile` is available on every agent-invoking command, `doctor` and
  `service install`; unknown profiles fail before workspace creation or a paid
  call.
- Per-role Copilot context tiers. Every invocation now passes
  `--context default|long_context` explicitly instead of inheriting interactive
  CLI state.
- Typed per-invocation telemetry for runtime-reported token usage, timing,
  nano-AIU and premium-request cost. Workflow runs, project planning and
  standalone repository-skill refreshes persist their records, while status and
  the local dashboard expose bounded minimized summaries.
- A model-selection guide covering the current Copilot catalog, pricing,
  context and reasoning controls, coding, research, instruction-following,
  long-context and security evidence.

### Changed

- The default routing now uses GPT-5.6 Terra for triage, GPT-5.5 for
  refinement, Claude Opus 5 for research and planning, MAI-Code-1.1-Flash
  through Claude Opus 5 for complexity-routed implementation, Gemini 3.8 Flash
  for testing and GPT-5.6 Sol for final review.
- The economy profile uses GPT-5.6 Luna and Terra for low-cost analysis,
  Gemini 3.8 Flash at medium effort for research, MAI/Gemini workers and
  testing, and GPT-5.6 Sol for final review.
- The security profile uses GPT-6 Astra for adversarial testing followed by an
  independent GPT-5.6 Sol review.
- Usage values remain raw runtime-reported units. Missing values stay unknown,
  and the factory does not convert premium requests or nano-AIU into AI Credits
  or USD.

## 0.3.0 - 2026-09-06

### Added

- `factory project` accepts a broad product or feature brief, asks the
  configured Planner for the smallest sufficient bounded task DAG, and executes
  dependency-ready work through the existing full SDLC controller.
- Persistent project artifacts, a local integration branch, optional GitHub
  issue creation and closure, deterministic final verification of the composed
  tree, and abandoned-project reconciliation.
- Reusable repository guidance. A generated `RepositorySkill` is now stored
  under `factory.data_dir` in repository-scoped storage, keyed by the canonical
  local repository identity and the profile's `dependency_fingerprint`
  (template `<data_dir>/repository-skills/v1/<repository-key>/...`). Runs reuse
  valid guidance, generate only when the current fingerprint has no generated
  skill, and never overwrite an existing generated file. Every load is
  revalidated in full. A dependency change selects a new file while earlier
  files remain; nothing expires on a timer.
- A human-owned repository-level `repository-skill-overlay.yaml`, stored beside
  the generated files and outside the target repository. It carries guidance
  prose only (`mode: extend|replace` plus optional simplify/polish blocks),
  survives dependency changes, and is never created, rewritten, normalized,
  refreshed or deleted by the factory. An invalid overlay is preserved, warned
  about and ignored while valid generated guidance still applies.
- `factory skill path`, `factory skill validate` and `factory skill refresh`
  for discovering, checking and explicitly refreshing repository guidance.
  `path` and `validate` are read-only; `refresh` writes generated guidance only
  and never touches the overlay.
- Immutable per-run snapshots taken before agents consume guidance —
  `repository-skill.json` (effective guidance), `repository-skill-overlay.json`
  (the overlay as read, when valid) and `repository-skill-use.json`
  (provenance and content hashes) — so mid-run human edits affect later runs
  only.

### Changed

- Skill generation is repository-wide instead of task-scoped: the Researcher
  receives the normalized `RepositoryProfile` and the configured source lists
  only, and no longer sees changed filenames.
- Repository identity for guidance storage is the canonical local Git common
  directory, so linked worktrees share one directory and a moved or re-cloned
  repository selects a new key. Use `factory skill path` before moving a
  repository to carry its guidance across.
- A normal eligible polish run no longer makes a research call when reusable
  guidance already exists for the current dependency fingerprint.

## 0.2.0 - 2026-09-05

### Added

- Deterministic repository profiling with persisted technologies, test tools,
  package managers, markers, warnings, version files, exact dependency
  declarations parsed from `pyproject.toml` (PEP 621, `dependency-groups`,
  `requires-python` and Poetry tables), `requirements.txt`/`requirements-*.txt`
  and `package.json` (exact versions resolved from `uv.lock`,
  `package-lock.json` and `pnpm-lock.yaml` when unambiguous; `poetry.lock`,
  `yarn.lock` and `bun.lock` detected and fingerprinted only), a semantic
  `dependency_fingerprint`, and a `manifest_fingerprint` kept as file-content
  provenance.
- A version-aware `RepositorySkill`, generated fresh for each eligible
  post-green polish attempt by a bounded, web-only Researcher call that sees
  only the normalized profile and changed file paths. Official documentation,
  migration guides and release notes (`polish.official_documentation_origins`)
  are authoritative; the exact, commit-pinned curated
  `polish.practice_reference_urls` may contribute generic heuristics only. It is applied by that polish attempt's
  Implementer, Tester and Reviewer. There is no built-in skill catalog.
- An optional, bounded post-green Implementer polish pass that simplifies
  first and then applies version-specific polish, with mandatory deterministic
  re-verification. Failed profiling, research, validation or a stale skill
  records a warning and skips polish instead of failing an already-green run.
- Public documentation site.
- Open-source license and community files.
- Documentation build and GitHub Pages deployment.
- Generated release notes grouped by pull request label.
- A stable `ci-gate` check for branch protection.

### Changed

- Package metadata now links to the public project resources.
- Public repositories upload CodeQL results and review dependency changes.

## 0.1.1 - 2026-09-05

### Added

- Tag-driven GitHub Releases.
- Native macOS arm64 and x86_64 archives.
- Wheel and source distribution artifacts.
- Artifact checksums, build metadata, and public-repository attestations.
- Local health checks, status reporting, dashboard, and launchd service.

## 0.1.0 - 2026-09-04

### Added

- Initial local-first software agent factory.
- Typed workflow artifacts and filesystem persistence.
- Fake and Copilot agent runtimes.
- Git worktree isolation and deterministic verification.
- Optional pull request creation, CI observation, repair, and issue polling.
