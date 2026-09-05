# Changelog

This file records user-visible changes.

The project follows [Semantic Versioning](https://semver.org/) while the public
API is still `0.x`.

## 0.2.0 - 2026-09-05

### Added

- Deterministic repository profiling with persisted technologies, test tools,
  package managers, markers, warnings, and versioned built-in skills.
- Role-filtered advisory skill context for Planner, Implementer, Tester, and
  Reviewer.
- An optional, bounded post-green Implementer polish pass with mandatory
  deterministic re-verification.
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
