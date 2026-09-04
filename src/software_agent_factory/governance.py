"""Repository verification and scope-governance helpers.

This module implements the Phase 5 and Phase 9 deterministic governance
mechanisms described in ``PLAN.md``:

- ``RepositoryVerifier`` composes
  :class:`~software_agent_factory.verification.DeterministicVerifier`
  to run repository-configured ``install`` / ``verify`` / ``build`` commands
  in order, persisting stdout/stderr logs per executed command under a run
  directory.
- ``ScopeDriftPolicy`` compares an :class:`~software_agent_factory.models.ExecutionPlan`'s
  expected scope against actual changed files and emits deterministic
  ``continue`` / ``replan`` / ``needs_human`` decisions based on risk and
  sensitive drift categories.

The workflow controller remains the only component allowed to transition run
state; this module only returns typed evidence for that controller to enforce.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence

from .config import RepositoryCommandsConfig
from .models import CommandResult, ExecutionPlan, Risk, VerificationReport
from .verification import DeterministicVerifier

_SAFE_FILENAME_PATTERN = re.compile(r"[^a-z0-9]+")
_FLAKY_MARKERS = (
    "flaky",
    "rerun may pass",
    "re-run may pass",
    "passed on retry",
    "retry succeeded",
)
_INFRA_MARKERS = (
    "command not found",
    "no such file or directory",
    "permission denied",
    "network is unreachable",
    "temporary failure",
    "timed out",
    "timeout",
    "connection reset",
    "connection refused",
    "could not resolve host",
    "service unavailable",
    "ssl",
    "tls",
    "certificate",
    "unable to access",
)
_DEPENDENCY_MARKERS = (
    "no matching distribution found",
    "could not resolve",
    "unable to resolve",
    "failed to resolve",
    "version solving failed",
    "resolution failed",
    "lockfile",
    "checksum mismatch",
    "integrity check failed",
    "cannot find module",
    "module not found",
    "no module named",
    "missing dependency",
    "dependency conflict",
    "peer dependency",
    "unsatisfied requirement",
)
_TEST_MARKERS = (
    "assertionerror",
    "failed:",
    "test failed",
    "tests failed",
    "expected:",
    "!= expected",
)
_TEST_COMMAND_MARKERS = (
    "pytest",
    "tox",
    "nox",
    "jest",
    "vitest",
    "mocha",
    "ava",
    "go test",
    "cargo test",
    "mvn test",
    "gradle test",
    "ctest",
    "phpunit",
    "npm test",
    "pnpm test",
    "yarn test",
    "bun test",
)
_LINT_OR_BUILD_MARKERS = (
    "ruff",
    "flake8",
    "eslint",
    "pylint",
    "mypy",
    "pyright",
    "typecheck",
    "lint",
    "build",
    "compile",
)
_DEPENDENCY_FILES = {
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "poetry.lock",
    "uv.lock",
    "Pipfile",
    "Pipfile.lock",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "Gemfile",
    "Gemfile.lock",
}
_INFRA_FILES = {
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "Pulumi.yaml",
}
_INFRA_TOP_LEVEL_DIRS = {
    "infra",
    "terraform",
    "helm",
    "k8s",
    "deploy",
    "deployment",
    "ansible",
}
_MIGRATION_PATH_PARTS = {
    "migrations",
    "migration",
    "db/migrate",
    "alembic/versions",
}


class CheckPhase(StrEnum):
    INSTALL = "install"
    VERIFY = "verify"
    BUILD = "build"


class VerificationFailureKind(StrEnum):
    CODE = "code"
    TEST = "test"
    FLAKY = "flaky"
    INFRA = "infra"
    DEPENDENCY = "dependency"
    UNKNOWN = "unknown"


class ScopeDecision(StrEnum):
    CONTINUE = "continue"
    REPLAN = "replan"
    NEEDS_HUMAN = "needs_human"


@dataclass(frozen=True)
class CommandLogMetadata:
    phase: CheckPhase
    overall_index: int
    phase_index: int
    command: str
    exit_code: int
    timed_out: bool
    duration_seconds: float
    stdout_path: Path
    stderr_path: Path


@dataclass(frozen=True)
class RepositoryVerificationResult:
    report: VerificationReport
    command_logs: tuple[CommandLogMetadata, ...]
    failure_kind: VerificationFailureKind | None
    failed_phase: CheckPhase | None
    failed_command: CommandLogMetadata | None


@dataclass(frozen=True)
class ScopeFinding:
    category: str
    message: str
    paths: tuple[str, ...] = ()
    sensitive: bool = False


@dataclass(frozen=True)
class ScopeAssessment:
    decision: ScopeDecision
    findings: tuple[ScopeFinding, ...]
    changed_file_count: int
    estimated_files_max: int

    @property
    def has_sensitive_findings(self) -> bool:
        return any(finding.sensitive for finding in self.findings)


@dataclass(frozen=True)
class PublishGate:
    """Deterministic pre-publish check against repository configuration.

    Enforced by the workflow controller immediately before any commit/push
    (``PLAN.md`` Phase 10, ``AGENTS.md`` "Prefer deterministic checks"): a
    change that touches a protected path, or that touches more files than
    ``repository.max_changed_files`` allows, must never reach GitHub.
    """

    allowed: bool
    protected_matches: tuple[str, ...] = ()
    changed_file_count: int = 0
    max_changed_files: int = 0
    violations: tuple[str, ...] = ()


def find_protected_matches(
    changed_files: Sequence[str], patterns: Sequence[str]
) -> tuple[str, ...]:
    """Return the changed files matching any configured protected glob.

    Patterns are matched against the whole repository-relative POSIX path
    with :meth:`pathlib.PurePath.full_match`, so ``**`` behaves as expected
    (``**/*.pem`` matches both ``key.pem`` and ``certs/key.pem``).
    """
    matches: list[str] = []
    for changed_file in changed_files:
        candidate = PurePosixPath(changed_file.replace("\\", "/").lstrip("/"))
        if any(candidate.full_match(pattern) for pattern in patterns):
            matches.append(changed_file)
    return tuple(matches)


def assess_publish_gate(
    changed_files: Sequence[str],
    *,
    max_changed_files: int,
    protected_file_patterns: Sequence[str],
) -> PublishGate:
    """Deterministically decide whether a change set may be published."""
    protected_matches = find_protected_matches(changed_files, protected_file_patterns)
    violations: list[str] = []
    if protected_matches:
        violations.append(
            "changed files match protected patterns: " + ", ".join(protected_matches)
        )
    if len(changed_files) > max_changed_files:
        violations.append(
            f"changed {len(changed_files)} files, which exceeds "
            f"repository.max_changed_files={max_changed_files}"
        )
    return PublishGate(
        allowed=not violations,
        protected_matches=protected_matches,
        changed_file_count=len(changed_files),
        max_changed_files=max_changed_files,
        violations=tuple(violations),
    )


class RepositoryVerifier:
    """Runs repository verification phases and persists per-command logs."""

    def __init__(self, verifier: DeterministicVerifier | None = None) -> None:
        self._verifier = verifier if verifier is not None else DeterministicVerifier()

    def run(
        self,
        commands: RepositoryCommandsConfig,
        *,
        cwd: Path,
        run_dir: Path,
        timeout_seconds: int,
        env_passthrough: Sequence[str] = (),
        capture_bytes: int = 32768,
    ) -> RepositoryVerificationResult:
        logs_dir = run_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)

        executed_results: list[CommandResult] = []
        command_logs: list[CommandLogMetadata] = []
        failures: list[str] = []
        failure_kind: VerificationFailureKind | None = None
        failed_phase: CheckPhase | None = None
        failed_command: CommandLogMetadata | None = None
        overall_index = 0

        for phase in CheckPhase:
            phase_commands = list(self._commands_for_phase(commands, phase))
            phase_report = self._verifier.run(
                phase_commands,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                env_passthrough=env_passthrough,
                capture_bytes=capture_bytes,
            )
            executed_results.extend(phase_report.deterministic_checks)

            for phase_index, result in enumerate(phase_report.deterministic_checks, start=1):
                overall_index += 1
                command_log = self._persist_command_log(
                    logs_dir=logs_dir,
                    phase=phase,
                    overall_index=overall_index,
                    phase_index=phase_index,
                    result=result,
                )
                command_logs.append(command_log)

            if not phase_report.passed:
                failed_phase = phase
                if command_logs:
                    failed_command = command_logs[-1]
                if phase_report.deterministic_checks:
                    failure_kind = self.classify_failure(
                        phase,
                        phase_report.deterministic_checks[-1],
                    )
                    failures.append(self._describe_phase_failure(phase, phase_report))
                break

        passed = failure_kind is None
        report = VerificationReport(
            passed=passed,
            deterministic_checks=executed_results,
            failures=failures,
            confidence=1.0 if passed else 0.0,
        )
        return RepositoryVerificationResult(
            report=report,
            command_logs=tuple(command_logs),
            failure_kind=failure_kind,
            failed_phase=failed_phase,
            failed_command=failed_command,
        )

    def classify_failure(
        self,
        phase: CheckPhase,
        result: CommandResult,
    ) -> VerificationFailureKind:
        combined_output = f"{result.stdout}\n{result.stderr}".lower()
        command = result.command.lower()

        if result.timed_out or self._contains_any(combined_output, _INFRA_MARKERS):
            return VerificationFailureKind.INFRA

        if self._contains_any(combined_output, _FLAKY_MARKERS):
            return VerificationFailureKind.FLAKY

        if self._looks_like_dependency_failure(combined_output):
            return VerificationFailureKind.DEPENDENCY

        if phase is CheckPhase.BUILD:
            return VerificationFailureKind.CODE

        if phase is CheckPhase.VERIFY:
            if self._looks_like_test_failure(command, combined_output):
                return VerificationFailureKind.TEST
            if self._contains_any(command, _LINT_OR_BUILD_MARKERS):
                return VerificationFailureKind.CODE
            if self._contains_any(combined_output, _TEST_MARKERS):
                return VerificationFailureKind.TEST
            return VerificationFailureKind.CODE

        return VerificationFailureKind.UNKNOWN

    def _commands_for_phase(
        self,
        commands: RepositoryCommandsConfig,
        phase: CheckPhase,
    ) -> Sequence[str]:
        if phase is CheckPhase.INSTALL:
            return commands.install
        if phase is CheckPhase.VERIFY:
            return commands.verify
        return commands.build

    def _persist_command_log(
        self,
        *,
        logs_dir: Path,
        phase: CheckPhase,
        overall_index: int,
        phase_index: int,
        result: CommandResult,
    ) -> CommandLogMetadata:
        file_stem = (
            f"{overall_index:02d}-{phase.value}-{phase_index:02d}-"
            f"{self._safe_command_slug(result.command)}"
        )
        stdout_path = logs_dir / f"{file_stem}.stdout.log"
        stderr_path = logs_dir / f"{file_stem}.stderr.log"
        stdout_path.write_text(result.stdout, encoding="utf-8")
        stderr_path.write_text(result.stderr, encoding="utf-8")
        os.chmod(stdout_path, 0o600)
        os.chmod(stderr_path, 0o600)
        return CommandLogMetadata(
            phase=phase,
            overall_index=overall_index,
            phase_index=phase_index,
            command=result.command,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            duration_seconds=result.duration_seconds,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )

    def _safe_command_slug(self, command: str) -> str:
        slug = _SAFE_FILENAME_PATTERN.sub("-", command.lower()).strip("-")
        slug = slug[:40].strip("-")
        return slug or "command"

    def _describe_phase_failure(
        self,
        phase: CheckPhase,
        report: VerificationReport,
    ) -> str:
        if not report.failures:
            return f"{phase.value}: verification failed"
        return f"{phase.value}: {report.failures[0]}"

    @staticmethod
    def _contains_any(text: str, candidates: Iterable[str]) -> bool:
        return any(candidate in text for candidate in candidates)

    def _looks_like_dependency_failure(self, combined_output: str) -> bool:
        return self._contains_any(combined_output, _DEPENDENCY_MARKERS)

    def _looks_like_test_failure(self, command: str, combined_output: str) -> bool:
        if self._contains_any(command, _TEST_COMMAND_MARKERS):
            return True
        return self._contains_any(combined_output, _TEST_MARKERS)


class ScopeDriftPolicy:
    """Deterministically assess actual changes against planned scope."""

    def assess(
        self,
        execution_plan: ExecutionPlan,
        changed_files: Sequence[str],
        risk: Risk,
    ) -> ScopeAssessment:
        normalized_files = tuple(self._unique_normalized_paths(changed_files))
        findings: list[ScopeFinding] = []

        expected_modules = self._expected_top_level_modules(execution_plan)
        if expected_modules:
            unexpected_files = tuple(
                path
                for path in normalized_files
                if self._top_level_name(path) not in expected_modules
            )
            if unexpected_files:
                findings.append(
                    ScopeFinding(
                        category="unexpected-module",
                        message=(
                            "Changed files outside expected top-level scope: "
                            f"{', '.join(unexpected_files)}"
                        ),
                        paths=unexpected_files,
                    )
                )

        max_files = execution_plan.expected_scope.estimated_files_max
        if len(normalized_files) > max_files:
            findings.append(
                ScopeFinding(
                    category="excessive-file-count",
                    message=(
                        f"Changed {len(normalized_files)} files; plan expected at most "
                        f"{max_files}."
                    ),
                    paths=normalized_files,
                )
            )

        dependency_files = tuple(
            path
            for path in normalized_files
            if PurePosixPath(path).name in _DEPENDENCY_FILES
        )
        if dependency_files:
            findings.append(
                ScopeFinding(
                    category="dependency-change",
                    message=(
                        "Changed dependency manifests or lockfiles: "
                        f"{', '.join(dependency_files)}"
                    ),
                    paths=dependency_files,
                    sensitive=True,
                )
            )

        migration_files = tuple(path for path in normalized_files if self._is_migration_path(path))
        if migration_files:
            findings.append(
                ScopeFinding(
                    category="migration-change",
                    message=f"Changed migration files: {', '.join(migration_files)}",
                    paths=migration_files,
                    sensitive=True,
                )
            )

        ci_files = tuple(path for path in normalized_files if self._is_ci_workflow_path(path))
        if ci_files:
            findings.append(
                ScopeFinding(
                    category="ci-workflow-change",
                    message=f"Changed CI workflow files: {', '.join(ci_files)}",
                    paths=ci_files,
                    sensitive=True,
                )
            )

        infrastructure_files = tuple(
            path for path in normalized_files if self._is_infrastructure_path(path)
        )
        if infrastructure_files:
            findings.append(
                ScopeFinding(
                    category="infrastructure-change",
                    message=f"Changed infrastructure files: {', '.join(infrastructure_files)}",
                    paths=infrastructure_files,
                    sensitive=True,
                )
            )

        decision = self._decide(findings, risk)
        return ScopeAssessment(
            decision=decision,
            findings=tuple(findings),
            changed_file_count=len(normalized_files),
            estimated_files_max=max_files,
        )

    def _expected_top_level_modules(self, execution_plan: ExecutionPlan) -> set[str]:
        expected: set[str] = set()
        for module in execution_plan.expected_scope.modules:
            normalized = self._normalize_path(module)
            if normalized:
                expected.add(self._top_level_name(normalized))
        return expected

    def _decide(self, findings: Sequence[ScopeFinding], risk: Risk) -> ScopeDecision:
        if not findings:
            return ScopeDecision.CONTINUE
        if any(finding.sensitive for finding in findings) and risk in {Risk.R2, Risk.R3}:
            return ScopeDecision.NEEDS_HUMAN
        return ScopeDecision.REPLAN

    def _unique_normalized_paths(self, changed_files: Sequence[str]) -> list[str]:
        unique: list[str] = []
        seen: set[str] = set()
        for path in changed_files:
            normalized = self._normalize_path(path)
            if normalized and normalized not in seen:
                unique.append(normalized)
                seen.add(normalized)
        return unique

    def _normalize_path(self, path: str) -> str:
        normalized = path.replace("\\", "/").strip()
        while normalized.startswith("./"):
            normalized = normalized[2:]
        return normalized.strip("/")

    def _top_level_name(self, path: str) -> str:
        return PurePosixPath(path).parts[0]

    def _is_migration_path(self, path: str) -> bool:
        normalized = path.lower()
        return any(marker in normalized for marker in _MIGRATION_PATH_PARTS)

    def _is_ci_workflow_path(self, path: str) -> bool:
        normalized = path.lower()
        name = PurePosixPath(path).name.lower()
        return (
            normalized.startswith(".github/workflows/")
            or normalized.startswith(".circleci/")
            or name in {"azure-pipelines.yml", "azure-pipelines.yaml", ".gitlab-ci.yml"}
        )

    def _is_infrastructure_path(self, path: str) -> bool:
        normalized = path.lower()
        name = PurePosixPath(path).name
        top_level = self._top_level_name(path).lower()
        if name in _INFRA_FILES:
            return True
        if top_level in _INFRA_TOP_LEVEL_DIRS:
            return True
        return normalized.startswith("infrastructure/")
