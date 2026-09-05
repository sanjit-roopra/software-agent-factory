# Changelog

This file records user-visible changes.

The project follows [Semantic Versioning](https://semver.org/) while the public
API is still `0.x`.

## Unreleased

### Added

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
