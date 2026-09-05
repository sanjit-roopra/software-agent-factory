from __future__ import annotations

import shlex
import stat
from pathlib import Path

import pytest

from software_agent_factory.config import (
    DEFAULT_PROTECTED_FILE_PATTERNS,
    RepositoryCommandsConfig,
)
from software_agent_factory.governance import (
    CheckPhase,
    RepositoryVerifier,
    ScopeDecision,
    ScopeDriftPolicy,
    VerificationFailureKind,
    assess_publish_gate,
    find_protected_matches,
)
from software_agent_factory.models import (
    CommandResult,
    ExecutionPlan,
    ExpectedScope,
    PlanStep,
    Risk,
)


def _python_command(code: str) -> str:
    return f"python -c {shlex.quote(code)}"


def _commands(
    *,
    install: list[str] | None = None,
    verify: list[str] | None = None,
    build: list[str] | None = None,
) -> RepositoryCommandsConfig:
    return RepositoryCommandsConfig(
        install=install or [],
        verify=verify or [],
        build=build or [],
    )


def _plan(
    *,
    modules: list[str],
    estimated_files_max: int,
) -> ExecutionPlan:
    return ExecutionPlan(
        summary="Implement scoped change",
        steps=[
            PlanStep(
                id="step-1",
                goal="Make the planned change",
                likely_files=[],
                validation=[],
            )
        ],
        expected_scope=ExpectedScope(
            modules=modules,
            estimated_files_min=0,
            estimated_files_max=estimated_files_max,
        ),
        test_strategy=[],
        risks=[],
    )


def test_repository_verifier_passes_when_all_command_lists_are_empty(tmp_path: Path) -> None:
    result = RepositoryVerifier().run(
        _commands(),
        cwd=tmp_path,
        run_dir=tmp_path / "run",
        timeout_seconds=5,
    )

    assert result.report.passed is True
    assert result.report.deterministic_checks == []
    assert result.report.failures == []
    assert result.command_logs == ()
    assert result.failure_kind is None
    assert result.failed_phase is None
    assert result.failed_command is None
    assert (tmp_path / "run" / "logs").is_dir()


def test_repository_verifier_runs_phases_in_order_and_persists_logs(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    commands = _commands(
        install=[_python_command("print('install out')")],
        verify=[
            _python_command("import sys; print('verify out'); sys.stderr.write('verify err\\n')")
        ],
        build=[_python_command("print('build out')")],
    )

    result = RepositoryVerifier().run(
        commands,
        cwd=tmp_path,
        run_dir=run_dir,
        timeout_seconds=5,
    )

    assert result.report.passed is True
    assert [item.command for item in result.report.deterministic_checks] == [
        commands.install[0],
        commands.verify[0],
        commands.build[0],
    ]
    assert [item.phase for item in result.command_logs] == [
        CheckPhase.INSTALL,
        CheckPhase.VERIFY,
        CheckPhase.BUILD,
    ]
    assert result.command_logs[0].stdout_path.name.startswith("01-install-01-")
    assert result.command_logs[1].stdout_path.name.startswith("02-verify-01-")
    assert result.command_logs[2].stdout_path.name.startswith("03-build-01-")
    assert result.command_logs[0].stdout_path.read_text(encoding="utf-8").strip() == "install out"
    assert result.command_logs[1].stdout_path.read_text(encoding="utf-8").strip() == "verify out"
    assert result.command_logs[1].stderr_path.read_text(encoding="utf-8") == "verify err\n"
    assert result.command_logs[2].stdout_path.read_text(encoding="utf-8").strip() == "build out"
    assert stat.S_IMODE(result.command_logs[0].stdout_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(result.command_logs[1].stderr_path.stat().st_mode) == 0o600


def test_repository_verifier_passes_only_named_environment_variables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FACTORY_TEST_ENV", "allowed-value")
    command = _python_command("import os; print(os.getenv('FACTORY_TEST_ENV', 'missing'))")

    blocked = RepositoryVerifier().run(
        _commands(verify=[command]),
        cwd=tmp_path,
        run_dir=tmp_path / "run-blocked",
        timeout_seconds=5,
    )
    allowed = RepositoryVerifier().run(
        _commands(verify=[command]),
        cwd=tmp_path,
        run_dir=tmp_path / "run-allowed",
        timeout_seconds=5,
        env_passthrough=("FACTORY_TEST_ENV",),
    )

    assert blocked.report.deterministic_checks[0].stdout.strip() == "missing"
    assert allowed.report.deterministic_checks[0].stdout.strip() == "allowed-value"


def test_repository_verifier_threads_capture_limit_to_verifier(tmp_path: Path) -> None:
    result = RepositoryVerifier().run(
        _commands(verify=[_python_command("print('x' * 200)")]),
        cwd=tmp_path,
        run_dir=tmp_path / "run",
        timeout_seconds=5,
        capture_bytes=32,
    )

    stdout = result.report.deterministic_checks[0].stdout
    assert "...[truncated " in stdout
    assert "x" * 200 not in stdout


def test_repository_verifier_stops_after_first_failure_and_reports_test_failure(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "should-not-run.txt"
    commands = _commands(
        install=[_python_command("print('install ok')")],
        verify=[
            _python_command(
                "import sys; sys.stderr.write('AssertionError: expected value\\n'); "
                "raise SystemExit(1)"
            ),
            _python_command(f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"),
        ],
        build=[_python_command("raise SystemExit(0)")],
    )

    result = RepositoryVerifier().run(
        commands,
        cwd=tmp_path,
        run_dir=tmp_path / "run",
        timeout_seconds=5,
    )

    assert result.report.passed is False
    assert len(result.report.deterministic_checks) == 2
    assert [item.phase for item in result.command_logs] == [
        CheckPhase.INSTALL,
        CheckPhase.VERIFY,
    ]
    assert result.failure_kind is VerificationFailureKind.TEST
    assert result.failed_phase is CheckPhase.VERIFY
    assert result.failed_command is result.command_logs[-1]
    assert result.report.failures == [f"verify: {commands.verify[0]!r} exited with code 1"]
    assert marker.exists() is False


def test_repository_verifier_records_timeout_as_infra_failure(tmp_path: Path) -> None:
    result = RepositoryVerifier().run(
        _commands(verify=["sleep 2"]),
        cwd=tmp_path,
        run_dir=tmp_path / "run",
        timeout_seconds=1,
    )

    assert result.report.passed is False
    assert len(result.report.deterministic_checks) == 1
    assert result.report.deterministic_checks[0].timed_out is True
    assert result.failure_kind is VerificationFailureKind.INFRA
    assert result.failed_phase is CheckPhase.VERIFY
    assert "timed out" in result.report.failures[0]


@pytest.mark.parametrize(
    ("phase", "result", "expected"),
    [
        (
            CheckPhase.INSTALL,
            CommandResult(
                command="uv sync",
                exit_code=1,
                stdout="",
                stderr="No matching distribution found for foo",
                duration_seconds=0.1,
            ),
            VerificationFailureKind.DEPENDENCY,
        ),
        (
            CheckPhase.VERIFY,
            CommandResult(
                command="pytest -q",
                exit_code=1,
                stdout="",
                stderr="AssertionError: expected 2",
                duration_seconds=0.1,
            ),
            VerificationFailureKind.TEST,
        ),
        (
            CheckPhase.VERIFY,
            CommandResult(
                command="pytest -q",
                exit_code=1,
                stdout="",
                stderr="flaky test failure; rerun may pass",
                duration_seconds=0.1,
            ),
            VerificationFailureKind.FLAKY,
        ),
        (
            CheckPhase.BUILD,
            CommandResult(
                command="uv run build",
                exit_code=1,
                stdout="",
                stderr="SyntaxError: invalid syntax",
                duration_seconds=0.1,
            ),
            VerificationFailureKind.CODE,
        ),
        (
            CheckPhase.BUILD,
            CommandResult(
                command="npm run build",
                exit_code=127,
                stdout="",
                stderr="/bin/sh: npm: command not found",
                duration_seconds=0.1,
            ),
            VerificationFailureKind.INFRA,
        ),
        (
            CheckPhase.INSTALL,
            CommandResult(
                command="custom-bootstrap",
                exit_code=42,
                stdout="",
                stderr="unexpected failure",
                duration_seconds=0.1,
            ),
            VerificationFailureKind.UNKNOWN,
        ),
    ],
)
def test_repository_verifier_classifies_failures(
    phase: CheckPhase,
    result: CommandResult,
    expected: VerificationFailureKind,
) -> None:
    assert RepositoryVerifier().classify_failure(phase, result) is expected


def test_scope_drift_policy_continues_for_benign_in_scope_low_risk_changes() -> None:
    assessment = ScopeDriftPolicy().assess(
        _plan(modules=["src", "tests"], estimated_files_max=3),
        changed_files=["src/app.py", "tests/test_app.py"],
        risk=Risk.R1,
    )

    assert assessment.decision is ScopeDecision.CONTINUE
    assert assessment.findings == ()
    assert assessment.changed_file_count == 2
    assert assessment.has_sensitive_findings is False


def test_scope_drift_policy_replans_for_unexpected_top_level_modules() -> None:
    assessment = ScopeDriftPolicy().assess(
        _plan(modules=["src"], estimated_files_max=3),
        changed_files=["src/app.py", "tests/test_app.py"],
        risk=Risk.R1,
    )

    assert assessment.decision is ScopeDecision.REPLAN
    assert [finding.category for finding in assessment.findings] == ["unexpected-module"]
    assert assessment.findings[0].paths == ("tests/test_app.py",)


def test_scope_drift_policy_replans_for_excessive_file_count_even_at_high_risk() -> None:
    assessment = ScopeDriftPolicy().assess(
        _plan(modules=["src"], estimated_files_max=1),
        changed_files=["src/app.py", "src/utils.py"],
        risk=Risk.R3,
    )

    assert assessment.decision is ScopeDecision.REPLAN
    assert [finding.category for finding in assessment.findings] == ["excessive-file-count"]


def test_scope_drift_policy_replans_for_dependency_manifest_changes_at_lower_risk() -> None:
    assessment = ScopeDriftPolicy().assess(
        _plan(modules=["src"], estimated_files_max=3),
        changed_files=["src/app.py", "pyproject.toml", "uv.lock"],
        risk=Risk.R1,
    )

    assert assessment.decision is ScopeDecision.REPLAN
    assert {finding.category for finding in assessment.findings} == {
        "unexpected-module",
        "dependency-change",
    }
    dependency_finding = next(
        finding for finding in assessment.findings if finding.category == "dependency-change"
    )
    assert dependency_finding.sensitive is True
    assert dependency_finding.paths == ("pyproject.toml", "uv.lock")


@pytest.mark.parametrize(
    ("changed_files", "category"),
    [
        (["src/app.py", "migrations/0002_add_widget.py"], "migration-change"),
        (["src/app.py", ".github/workflows/ci.yml"], "ci-workflow-change"),
        (["src/app.py", "infra/main.tf"], "infrastructure-change"),
    ],
)
def test_scope_drift_policy_requires_human_for_sensitive_high_risk_drift(
    changed_files: list[str],
    category: str,
) -> None:
    assessment = ScopeDriftPolicy().assess(
        _plan(modules=["src"], estimated_files_max=4),
        changed_files=changed_files,
        risk=Risk.R2,
    )

    assert assessment.decision is ScopeDecision.NEEDS_HUMAN
    assert category in {finding.category for finding in assessment.findings}
    assert assessment.has_sensitive_findings is True


# -- publish gate ------------------------------------------------------------


def test_publish_gate_allows_an_ordinary_change() -> None:
    gate = assess_publish_gate(
        ["src/app.py", "tests/test_app.py"],
        max_changed_files=10,
        protected_file_patterns=list(DEFAULT_PROTECTED_FILE_PATTERNS),
    )

    assert gate.allowed is True
    assert gate.violations == ()
    assert gate.changed_file_count == 2


@pytest.mark.parametrize(
    "changed_file",
    [
        ".env",
        ".env.production",
        "config/.env",
        "certs/server.pem",
        "deploy/id_rsa",
        "app/secrets.json",
        "home/.ssh/config",
        "creds/.aws/credentials",
    ],
)
def test_publish_gate_refuses_protected_paths(changed_file: str) -> None:
    gate = assess_publish_gate(
        ["src/app.py", changed_file],
        max_changed_files=10,
        protected_file_patterns=list(DEFAULT_PROTECTED_FILE_PATTERNS),
    )

    assert gate.allowed is False
    assert gate.protected_matches == (changed_file,)
    assert "protected patterns" in gate.violations[0]


def test_publish_gate_refuses_an_oversized_change() -> None:
    gate = assess_publish_gate(
        [f"src/file_{index}.py" for index in range(5)],
        max_changed_files=3,
        protected_file_patterns=[],
    )

    assert gate.allowed is False
    assert "max_changed_files=3" in gate.violations[0]


def test_publish_gate_reports_every_violation_at_once() -> None:
    gate = assess_publish_gate(
        [".env", "src/a.py", "src/b.py"],
        max_changed_files=2,
        protected_file_patterns=[".env"],
    )

    assert gate.allowed is False
    assert len(gate.violations) == 2


def test_find_protected_matches_normalizes_separators_and_prefixes() -> None:
    assert find_protected_matches(["/src/.env"], ["**/.env"]) == ("/src/.env",)
    assert find_protected_matches(["src\\.env"], ["**/.env"]) == ("src\\.env",)
    assert find_protected_matches(["src/app.py"], ["**/.env"]) == ()
