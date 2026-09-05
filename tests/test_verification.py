"""Tests for software_agent_factory.verification.DeterministicVerifier.

No pyproject.toml / installed package exists yet, so ``src/`` is added to
``sys.path`` directly here rather than via a conftest.py (out of scope for
this ownership boundary).

``verification.py`` imports ``CommandResult`` and ``VerificationReport``
from the sibling ``software_agent_factory.models`` module, which is owned by
another in-progress agent and may not exist yet. When it is unavailable we
still verify that the owned source at least compiles, and skip (rather than
error) the behavioral tests with a clear reason identifying the missing
dependency.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_IMPORT_ERROR: Exception | None
try:
    from software_agent_factory.verification import DeterministicVerifier

    _IMPORT_ERROR = None
except ModuleNotFoundError as exc:  # models.py not created yet
    DeterministicVerifier = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc

requires_models = pytest.mark.skipif(
    _IMPORT_ERROR is not None,
    reason=(
        "software_agent_factory.models is not available yet "
        f"(dependency of verification.py): {_IMPORT_ERROR}"
    ),
)


def test_verification_module_compiles() -> None:
    """The owned source must compile even before models.py exists."""
    import py_compile

    module_path = _SRC / "software_agent_factory" / "verification.py"
    py_compile.compile(str(module_path), doraise=True)


@requires_models
def test_empty_command_list_passes(tmp_path: Path) -> None:
    report = DeterministicVerifier().run([], cwd=tmp_path, timeout_seconds=5)

    assert report.passed is True
    assert report.deterministic_checks == []
    assert report.failures == []


@requires_models
def test_successful_commands_all_pass(tmp_path: Path) -> None:
    report = DeterministicVerifier().run(["echo hello", "true"], cwd=tmp_path, timeout_seconds=5)

    assert report.passed is True
    assert len(report.deterministic_checks) == 2
    assert report.failures == []
    first = report.deterministic_checks[0]
    assert first.command == "echo hello"
    assert first.exit_code == 0
    assert first.stdout.strip() == "hello"
    assert first.timed_out is False
    assert first.duration_seconds >= 0


@requires_models
def test_stops_after_first_failure(tmp_path: Path) -> None:
    marker = tmp_path / "marker.txt"
    report = DeterministicVerifier().run(
        ["false", f"echo should-not-run > {marker}"],
        cwd=tmp_path,
        timeout_seconds=5,
    )

    assert report.passed is False
    assert len(report.deterministic_checks) == 1
    assert report.deterministic_checks[0].command == "false"
    assert report.deterministic_checks[0].exit_code != 0
    assert len(report.failures) == 1
    assert "false" in report.failures[0]
    # The second command must never have run.
    assert not marker.exists()


@requires_models
def test_timeout_is_recorded_as_failed_result_not_raised(tmp_path: Path) -> None:
    started = time.monotonic()
    report = DeterministicVerifier().run(["sleep 5"], cwd=tmp_path, timeout_seconds=1)
    elapsed = time.monotonic() - started

    assert report.passed is False
    assert len(report.deterministic_checks) == 1
    result = report.deterministic_checks[0]
    assert result.timed_out is True
    assert result.exit_code != 0
    assert len(report.failures) == 1
    assert "timed out" in report.failures[0]
    # Should not have waited for the full sleep duration.
    assert elapsed < 5


@requires_models
def test_timeout_kills_descendant_processes(tmp_path: Path) -> None:
    marker = tmp_path / "orphaned-child.txt"
    command = f"(sleep 2; echo orphaned > {marker}) & wait"

    report = DeterministicVerifier().run([command], cwd=tmp_path, timeout_seconds=1)
    time.sleep(1.5)

    assert report.passed is False
    assert report.deterministic_checks[0].timed_out is True
    assert not marker.exists()


@requires_models
def test_command_runs_with_cwd_exactly_the_workspace(tmp_path: Path) -> None:
    report = DeterministicVerifier().run(["pwd"], cwd=tmp_path, timeout_seconds=5)

    reported_cwd = Path(report.deterministic_checks[0].stdout.strip()).resolve()
    assert reported_cwd == tmp_path.resolve()


@requires_models
def test_unrelated_errors_are_not_silently_caught(tmp_path: Path) -> None:
    missing_cwd = tmp_path / "does-not-exist"

    with pytest.raises((FileNotFoundError, NotADirectoryError)):
        DeterministicVerifier().run(["true"], cwd=missing_cwd, timeout_seconds=5)


@requires_models
def test_ambient_credentials_are_not_visible_to_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_TOKEN", "ghp_thisisafakegithubtokenvalue123456")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_anotherfaketokenvalue1234567890")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "a" * 40)
    monkeypatch.setenv("FACTORY_UNRELATED", "leaky-value")

    report = DeterministicVerifier().run(
        [
            'echo "gh=${GH_TOKEN:-absent} gha=${GITHUB_TOKEN:-absent} '
            'aws=${AWS_SECRET_ACCESS_KEY:-absent} other=${FACTORY_UNRELATED:-absent}"'
        ],
        cwd=tmp_path,
        timeout_seconds=10,
    )

    stdout = report.deterministic_checks[0].stdout
    assert report.passed is True
    assert "gh=absent" in stdout
    assert "gha=absent" in stdout
    assert "aws=absent" in stdout
    assert "other=absent" in stdout


@requires_models
def test_base_allowlist_is_provided_and_extra_names_can_be_passed_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("FACTORY_BUILD_PROFILE", "ci")

    report = DeterministicVerifier().run(
        [
            'echo "home=${HOME:-absent} lang=${LANG:-absent} '
            'profile=${FACTORY_BUILD_PROFILE:-absent}"'
        ],
        cwd=tmp_path,
        timeout_seconds=10,
        env_passthrough=["FACTORY_BUILD_PROFILE"],
    )

    stdout = report.deterministic_checks[0].stdout
    assert "home=absent" not in stdout
    assert "lang=absent" not in stdout
    assert "profile=ci" in stdout


@requires_models
def test_secrets_in_output_are_redacted(tmp_path: Path) -> None:
    report = DeterministicVerifier().run(
        [
            'echo "token ghp_abcdefghijklmnopqrstuvwxyz012345"; '
            'echo "aws AKIAIOSFODNN7EXAMPLE"; '
            'echo "-----BEGIN RSA PRIVATE KEY-----"; '
            'echo "GH_TOKEN=supersecretvalue123" >&2'
        ],
        cwd=tmp_path,
        timeout_seconds=10,
    )

    result = report.deterministic_checks[0]
    assert "ghp_abcdefghijklmnopqrstuvwxyz012345" not in result.stdout
    assert "AKIAIOSFODNN7EXAMPLE" not in result.stdout
    assert "BEGIN RSA PRIVATE KEY" not in result.stdout
    assert "supersecretvalue123" not in result.stderr
    assert "[REDACTED]" in result.stdout
    assert "[REDACTED]" in result.stderr


@requires_models
def test_output_is_bounded_by_the_capture_limit(tmp_path: Path) -> None:
    report = DeterministicVerifier().run(
        ["for i in $(seq 1 5000); do echo 'chatty build output line'; done"],
        cwd=tmp_path,
        timeout_seconds=30,
        capture_bytes=2048,
    )

    stdout = report.deterministic_checks[0].stdout
    assert report.passed is True
    assert len(stdout.encode("utf-8")) < 2048 + 100
    assert "truncated" in stdout
    # Head and tail are both preserved.
    assert stdout.startswith("chatty build output line")
    assert stdout.rstrip().endswith("chatty build output line")


@requires_models
def test_secrets_are_redacted_before_truncation(tmp_path: Path) -> None:
    from software_agent_factory.verification import sanitize_output

    noisy = "x" * 400 + " ghp_abcdefghijklmnopqrstuvwxyz012345 " + "y" * 400

    sanitized = sanitize_output(noisy, 200)

    assert "ghp_abcdefghijklmnopqrstuvwxyz012345" not in sanitized
    assert len(sanitized.encode("utf-8")) < 300


@requires_models
def test_capture_limit_applies_to_timed_out_commands(tmp_path: Path) -> None:
    report = DeterministicVerifier().run(
        ["echo 'token ghp_abcdefghijklmnopqrstuvwxyz012345'; sleep 5"],
        cwd=tmp_path,
        timeout_seconds=1,
    )

    result = report.deterministic_checks[0]
    assert result.timed_out is True
    assert "ghp_abcdefghijklmnopqrstuvwxyz012345" not in result.stdout
