"""Tests for software_agent_factory.doctor.

Every external boundary (subprocess execution, ``PATH`` lookup, platform
info, frozen-executable detection) is injected through ``DoctorEnvironment``;
no test spawns a real ``git``/``gh``/``copilot`` process or reads the real
host's ``PATH``/platform.

Coverage:

- ``_version_check`` (via ``check_git``/``check_gh``/``check_copilot``):
  required-missing is an error, optional-missing is ok, resolved-but-broken
  is a warning, never a silent success.
- ``check_copilot`` never invokes anything but a bounded ``--version`` probe
  (no paid agent call is possible through this module).
- ``check_verification_commands``: safe ``shlex`` first-token parsing, no
  shell, malformed-command handling, and de-duplication.
- ``check_config``: missing file, unreadable file, invalid YAML, failed
  validation, and success.
- ``check_data_dir``: writable and not-writable.
- ``check_platform``/``check_executable``/``check_launchctl``.
- ``run_doctor``: the offline default never requires ``gh``/``copilot``, and
  each becomes required only when the corresponding feature is enabled or
  requested.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

import pytest
from factory_testing import build_config

from software_agent_factory.doctor import (
    CheckStatus,
    DoctorEnvironment,
    DoctorReport,
    check_config,
    check_copilot,
    check_data_dir,
    check_executable,
    check_gh,
    check_git,
    check_launchctl,
    check_platform,
    check_verification_commands,
    default_command_runner,
    run_doctor,
)


@dataclass
class FakeRunner:
    """Records every call; never spawns a real process."""

    responses: dict[str, subprocess.CompletedProcess[str]] = field(default_factory=dict)
    timeout_for: set[str] = field(default_factory=set)
    oserror_for: set[str] = field(default_factory=set)
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def __call__(
        self, argv: Sequence[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(argv))
        key = argv[0]
        if key in self.timeout_for:
            raise subprocess.TimeoutExpired(cmd=list(argv), timeout=timeout)
        if key in self.oserror_for:
            raise OSError("permission denied")
        return self.responses.get(
            key, subprocess.CompletedProcess(list(argv), 0, stdout="v1.0.0\n", stderr="")
        )


def make_which(available: dict[str, str]) -> Callable[[str], str | None]:
    def _which(name: str) -> str | None:
        return available.get(name)

    return _which


def make_env(
    *,
    available: dict[str, str] | None = None,
    runner: FakeRunner | None = None,
    system: str = "Darwin",
    machine: str = "arm64",
    is_frozen: bool = False,
    executable_path: Path = Path("/usr/bin/factory"),
) -> tuple[DoctorEnvironment, FakeRunner]:
    fake_runner = runner if runner is not None else FakeRunner()
    env = DoctorEnvironment(
        run_command=fake_runner,
        which=make_which(available or {}),
        system=system,
        machine=machine,
        is_frozen=is_frozen,
        executable_path=executable_path,
    )
    return env, fake_runner


# -- git / gh / copilot version checks ------------------------------------


def test_check_git_missing_is_error() -> None:
    env, _ = make_env(available={})
    result = check_git(env)
    assert result.status is CheckStatus.ERROR
    assert "git" in result.message
    assert result.remediation is not None


def test_check_git_found_is_ok() -> None:
    env, runner = make_env(available={"git": "/usr/bin/git"})
    result = check_git(env)
    assert result.status is CheckStatus.OK
    assert runner.calls == [("/usr/bin/git", "--version")]


def test_check_gh_not_required_and_missing_is_ok() -> None:
    env, runner = make_env(available={})
    result = check_gh(env, required=False)
    assert result.status is CheckStatus.OK
    assert runner.calls == []  # never even probed since it wasn't resolved


def test_check_gh_required_and_missing_is_error() -> None:
    env, _ = make_env(available={})
    result = check_gh(env, required=True)
    assert result.status is CheckStatus.ERROR
    assert result.remediation is not None


def test_check_gh_required_and_found_is_ok() -> None:
    env, _ = make_env(available={"gh": "/opt/homebrew/bin/gh"})
    result = check_gh(env, required=True)
    assert result.status is CheckStatus.OK
    assert "/opt/homebrew/bin/gh" in result.message


def test_check_copilot_not_requested_and_missing_is_ok() -> None:
    env, runner = make_env(available={})
    result = check_copilot(env, required=False)
    assert result.status is CheckStatus.OK
    assert runner.calls == []


def test_check_copilot_requested_and_missing_is_error() -> None:
    env, _ = make_env(available={})
    result = check_copilot(env, required=True)
    assert result.status is CheckStatus.ERROR


def test_check_copilot_never_calls_anything_but_bounded_version_probe() -> None:
    """Doctor must never make a paid Copilot agent call -- only ``--version``."""
    env, runner = make_env(available={"copilot": "/usr/local/bin/copilot"})
    result = check_copilot(env, required=True)
    assert result.status is CheckStatus.OK
    assert runner.calls == [("/usr/local/bin/copilot", "--version")]


def test_version_check_timeout_for_required_tool_is_error() -> None:
    """A required tool that is resolved but unresponsive is not usable: it
    must be an ERROR, not a WARNING (a required tool cannot silently degrade
    to advisory-only)."""
    runner = FakeRunner(timeout_for={"/usr/bin/git"})
    env, _ = make_env(available={"git": "/usr/bin/git"}, runner=runner)
    result = check_git(env)
    assert result.status is CheckStatus.ERROR


def test_version_check_timeout_for_optional_tool_is_warning() -> None:
    runner = FakeRunner(timeout_for={"/opt/homebrew/bin/gh"})
    env, _ = make_env(available={"gh": "/opt/homebrew/bin/gh"}, runner=runner)
    result = check_gh(env, required=False)
    assert result.status is CheckStatus.WARNING


def test_version_check_oserror_required_is_error() -> None:
    runner = FakeRunner(oserror_for={"/usr/bin/git"})
    env, _ = make_env(available={"git": "/usr/bin/git"}, runner=runner)
    result = check_git(env)
    assert result.status is CheckStatus.ERROR


def test_version_check_oserror_not_required_is_warning() -> None:
    runner = FakeRunner(oserror_for={"/opt/homebrew/bin/gh"})
    env, _ = make_env(available={"gh": "/opt/homebrew/bin/gh"}, runner=runner)
    result = check_gh(env, required=False)
    assert result.status is CheckStatus.WARNING


def test_version_check_nonzero_exit_for_required_tool_is_error() -> None:
    """A required tool that resolves but exits non-zero for --version is not
    usable and must be an ERROR."""
    runner = FakeRunner(
        responses={
            "/usr/bin/git": subprocess.CompletedProcess(
                ["/usr/bin/git", "--version"], 1, stdout="", stderr="boom"
            )
        }
    )
    env, _ = make_env(available={"git": "/usr/bin/git"}, runner=runner)
    result = check_git(env)
    assert result.status is CheckStatus.ERROR


def test_version_check_nonzero_exit_for_optional_tool_is_warning() -> None:
    runner = FakeRunner(
        responses={
            "/opt/homebrew/bin/gh": subprocess.CompletedProcess(
                ["/opt/homebrew/bin/gh", "--version"], 1, stdout="", stderr="boom"
            )
        }
    )
    env, _ = make_env(available={"gh": "/opt/homebrew/bin/gh"}, runner=runner)
    result = check_gh(env, required=False)
    assert result.status is CheckStatus.WARNING


# -- verification command executables --------------------------------------


def test_check_verification_commands_parses_first_token_only() -> None:
    env, runner = make_env(available={"bun": "/usr/local/bin/bun"})
    results = check_verification_commands(env, ["bun run lint --fix"])
    assert len(results) == 1
    assert results[0].status is CheckStatus.OK
    # Only the first argv token is ever resolved/executed -- never a shell.
    assert runner.calls == [("/usr/local/bin/bun", "--version")]


def test_check_verification_commands_missing_executable_is_error() -> None:
    env, _ = make_env(available={})
    results = check_verification_commands(env, ["bun run lint"])
    assert len(results) == 1
    assert results[0].status is CheckStatus.ERROR


def test_check_verification_commands_malformed_is_error() -> None:
    env, _ = make_env(available={})
    results = check_verification_commands(env, ["echo 'unterminated"])
    assert len(results) == 1
    assert results[0].status is CheckStatus.ERROR
    assert "parse" in results[0].message


def test_check_verification_commands_dedupes_same_executable() -> None:
    env, runner = make_env(available={"bun": "/usr/local/bin/bun"})
    results = check_verification_commands(env, ["bun run lint", "bun run typecheck"])
    assert len(results) == 1
    assert len(runner.calls) == 1


def test_check_verification_commands_empty_list_is_empty() -> None:
    env, _ = make_env(available={})
    assert check_verification_commands(env, []) == []


# -- config -----------------------------------------------------------------


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_check_config_missing_file_is_error(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.yaml"
    result, config = check_config(missing)
    assert result.status is CheckStatus.ERROR
    assert config is None


def test_check_config_invalid_yaml_is_error(tmp_path: Path) -> None:
    path = _write(tmp_path, "bad.yaml", "factory: [unterminated\n")
    result, config = check_config(path)
    assert result.status is CheckStatus.ERROR
    assert config is None


def test_check_config_failed_validation_is_error(tmp_path: Path) -> None:
    path = _write(tmp_path, "invalid.yaml", "factory:\n  data_dir: /tmp/x\n")
    result, config = check_config(path)
    assert result.status is CheckStatus.ERROR
    assert config is None


def test_check_config_valid_is_ok(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    factory_config = build_config(data_dir)
    config_path = tmp_path / "factory.yaml"
    import yaml

    config_path.write_text(
        yaml.safe_dump(factory_config.model_dump(mode="json")), encoding="utf-8"
    )
    result, loaded = check_config(config_path)
    assert result.status is CheckStatus.OK
    assert loaded is not None
    assert loaded.data_dir == data_dir


def test_check_config_default_when_none_is_ok() -> None:
    result, config = check_config(None)
    assert result.status is CheckStatus.OK
    assert config is not None


# -- data dir -----------------------------------------------------------------


def test_check_data_dir_writable_is_ok(tmp_path: Path) -> None:
    target = tmp_path / "data"
    result = check_data_dir(target)
    assert result.status is CheckStatus.OK
    assert target.exists()
    # probe file must not be left behind
    assert list(target.iterdir()) == []


def test_check_data_dir_not_writable_is_error(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    target = blocker / "data"  # mkdir(parents=True) must fail: parent is a file
    result = check_data_dir(target)
    assert result.status is CheckStatus.ERROR
    assert result.remediation is not None


# -- platform / executable / launchctl --------------------------------------


def test_check_platform_macos_supported_arch_is_ok() -> None:
    env, _ = make_env(system="Darwin", machine="arm64")
    result = check_platform(env)
    assert result.status is CheckStatus.OK


def test_check_platform_non_macos_is_warning() -> None:
    env, _ = make_env(system="Linux", machine="x86_64")
    result = check_platform(env)
    assert result.status is CheckStatus.WARNING


def test_check_platform_unsupported_arch_is_warning() -> None:
    env, _ = make_env(system="Darwin", machine="i386")
    result = check_platform(env)
    assert result.status is CheckStatus.WARNING


def test_check_executable_reports_frozen_status() -> None:
    env, _ = make_env(is_frozen=True, executable_path=Path("/Applications/factory"))
    result = check_executable(env)
    assert result.status is CheckStatus.OK
    assert "frozen" in result.message
    assert "/Applications/factory" in result.message


def test_check_executable_reports_source_status() -> None:
    env, _ = make_env(is_frozen=False)
    result = check_executable(env)
    assert "source" in result.message


def test_check_launchctl_macos_missing_is_error() -> None:
    env, _ = make_env(system="Darwin", available={})
    result = check_launchctl(env)
    assert result.status is CheckStatus.ERROR


def test_check_launchctl_macos_found_is_ok() -> None:
    env, _ = make_env(system="Darwin", available={"launchctl": "/bin/launchctl"})
    result = check_launchctl(env)
    assert result.status is CheckStatus.OK


def test_check_launchctl_non_macos_is_ok() -> None:
    env, _ = make_env(system="Linux", available={})
    result = check_launchctl(env)
    assert result.status is CheckStatus.OK


# -- run_doctor ---------------------------------------------------------------


def test_run_doctor_offline_default_never_requires_gh_or_copilot(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    factory_config = build_config(data_dir)
    assert factory_config.pull_request.enabled is False
    assert factory_config.ci.enabled is False

    config_path = tmp_path / "factory.yaml"
    import yaml

    config_path.write_text(
        yaml.safe_dump(factory_config.model_dump(mode="json")), encoding="utf-8"
    )

    env, _ = make_env(
        available={"git": "/usr/bin/git", "launchctl": "/bin/launchctl"}
    )  # no gh, no copilot
    report = run_doctor(
        config_path=config_path,
        requested_runtime_copilot=False,
        environment=env,
    )
    assert report.success is True
    gh_check = next(c for c in report.checks if c.name == "gh")
    copilot_check = next(c for c in report.checks if c.name == "copilot")
    assert gh_check.status is CheckStatus.OK
    assert copilot_check.status is CheckStatus.OK


def test_run_doctor_requires_gh_when_pull_request_enabled(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    factory_config = build_config(data_dir, pull_request={"enabled": True})
    config_path = tmp_path / "factory.yaml"
    import yaml

    config_path.write_text(
        yaml.safe_dump(factory_config.model_dump(mode="json")), encoding="utf-8"
    )

    env, _ = make_env(available={"git": "/usr/bin/git"})  # gh missing
    report = run_doctor(config_path=config_path, environment=env)
    assert report.success is False
    gh_check = next(c for c in report.checks if c.name == "gh")
    assert gh_check.status is CheckStatus.ERROR


def test_run_doctor_requires_copilot_when_runtime_requested(tmp_path: Path) -> None:
    env, _ = make_env(available={"git": "/usr/bin/git"})  # copilot missing
    report = run_doctor(
        config_path=None,
        requested_runtime_copilot=True,
        environment=env,
    )
    assert report.success is False
    copilot_check = next(c for c in report.checks if c.name == "copilot")
    assert copilot_check.status is CheckStatus.ERROR


def test_run_doctor_uses_config_data_dir_when_not_overridden(tmp_path: Path) -> None:
    data_dir = tmp_path / "configured-data"
    factory_config = build_config(data_dir)
    config_path = tmp_path / "factory.yaml"
    import yaml

    config_path.write_text(
        yaml.safe_dump(factory_config.model_dump(mode="json")), encoding="utf-8"
    )

    env, _ = make_env(available={"git": "/usr/bin/git"})
    report = run_doctor(config_path=config_path, environment=env)
    data_dir_check = next(c for c in report.checks if c.name == "data_dir")
    assert data_dir_check.status is CheckStatus.OK
    assert data_dir.exists()


def test_run_doctor_data_dir_override_wins(tmp_path: Path) -> None:
    configured_dir = tmp_path / "configured"
    override_dir = tmp_path / "override"
    factory_config = build_config(configured_dir)
    config_path = tmp_path / "factory.yaml"
    import yaml

    config_path.write_text(
        yaml.safe_dump(factory_config.model_dump(mode="json")), encoding="utf-8"
    )

    env, _ = make_env(available={"git": "/usr/bin/git"})
    report = run_doctor(
        config_path=config_path, data_dir_override=override_dir, environment=env
    )
    data_dir_check = next(c for c in report.checks if c.name == "data_dir")
    assert override_dir.exists()
    assert str(override_dir) in data_dir_check.message
    assert not configured_dir.exists()


def test_run_doctor_bad_config_error_does_not_crash(tmp_path: Path) -> None:
    missing = tmp_path / "no-such-file.yaml"
    env, _ = make_env(available={"git": "/usr/bin/git"})
    report = run_doctor(config_path=missing, environment=env)
    assert report.success is False
    config_check = next(c for c in report.checks if c.name == "config")
    assert config_check.status is CheckStatus.ERROR


def test_doctor_report_success_false_when_any_error() -> None:
    from software_agent_factory.doctor import CheckResult

    report = DoctorReport(
        checks=(
            CheckResult(name="a", status=CheckStatus.OK, message="fine"),
            CheckResult(name="b", status=CheckStatus.WARNING, message="meh"),
            CheckResult(name="c", status=CheckStatus.ERROR, message="broken"),
        )
    )
    assert report.success is False


def test_doctor_report_success_true_with_only_warnings() -> None:
    from software_agent_factory.doctor import CheckResult

    report = DoctorReport(
        checks=(
            CheckResult(name="a", status=CheckStatus.OK, message="fine"),
            CheckResult(name="b", status=CheckStatus.WARNING, message="meh"),
        )
    )
    assert report.success is True


def test_doctor_report_to_dict_is_json_serializable() -> None:
    import json

    from software_agent_factory.doctor import CheckResult

    report = DoctorReport(
        checks=(CheckResult(name="a", status=CheckStatus.OK, message="fine"),)
    )
    payload = report.to_dict()
    serialized = json.dumps(payload)
    assert '"status": "ok"' in serialized or '"status":"ok"' in serialized


# -- default_command_runner: real, bounded, never a shell --------------------


def test_default_command_runner_runs_without_a_shell(tmp_path: Path) -> None:
    import sys

    script = "import sys; print(sys.argv[1])"
    result = default_command_runner(
        [sys.executable, "-c", script, "hello; echo shell-would-run-this"], timeout=5.0
    )
    assert result.returncode == 0
    # If a shell were involved, ';' would separate commands; here it is one
    # literal argv element instead.
    assert result.stdout.strip() == "hello; echo shell-would-run-this"


def test_default_command_runner_bounded_by_timeout() -> None:
    import sys

    with pytest.raises(subprocess.TimeoutExpired):
        default_command_runner(
            [sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2
        )


# -- gh is required by every GitHub-touching feature ---------------------------


def _write_config(tmp_path: Path, **kwargs: object) -> Path:
    import yaml

    factory_config = build_config(tmp_path / "data", **kwargs)
    config_path = tmp_path / "factory.yaml"
    config_path.write_text(
        yaml.safe_dump(factory_config.model_dump(mode="json")), encoding="utf-8"
    )
    return config_path


def test_requires_gh_is_true_for_every_github_touching_feature(tmp_path: Path) -> None:
    from software_agent_factory.doctor import requires_gh

    assert requires_gh(build_config(tmp_path / "data")) is False
    assert requires_gh(build_config(tmp_path / "data", pull_request={"enabled": True})) is True
    assert (
        requires_gh(
            build_config(
                tmp_path / "data", pull_request={"enabled": True}, ci={"enabled": True}
            )
        )
        is True
    )
    assert (
        requires_gh(build_config(tmp_path / "data", scheduler={"enabled": True})) is True
    )


def test_run_doctor_requires_gh_when_the_scheduler_is_enabled(tmp_path: Path) -> None:
    """The backlog daemon polls GitHub Issues through ``gh``, so a scheduler
    without it fails on its first tick rather than at PR time."""
    config_path = _write_config(tmp_path, scheduler={"enabled": True})

    env, _ = make_env(available={"git": "/usr/bin/git"})  # gh missing
    report = run_doctor(config_path=config_path, environment=env)

    gh_check = next(check for check in report.checks if check.name == "gh")
    assert gh_check.status is CheckStatus.ERROR
    assert report.success is False


# -- missing_prerequisites ----------------------------------------------------


def test_missing_prerequisites_always_requires_git() -> None:
    from software_agent_factory.doctor import missing_prerequisites

    env, runner = make_env(available={})
    assert missing_prerequisites(environment=env) == ["git"]
    # A PATH lookup only: nothing is executed by this gate.
    assert runner.calls == []


def test_missing_prerequisites_reports_only_requested_tools() -> None:
    from software_agent_factory.doctor import missing_prerequisites

    env, _ = make_env(available={"git": "/usr/bin/git"})

    assert missing_prerequisites(environment=env) == []
    assert missing_prerequisites(require_gh=True, environment=env) == ["gh"]
    assert missing_prerequisites(require_copilot=True, environment=env) == ["copilot"]
    assert missing_prerequisites(
        require_gh=True, require_copilot=True, environment=env
    ) == ["gh", "copilot"]


def test_missing_prerequisites_is_empty_when_everything_is_present() -> None:
    from software_agent_factory.doctor import missing_prerequisites

    env, _ = make_env(
        available={
            "git": "/usr/bin/git",
            "gh": "/usr/bin/gh",
            "copilot": "/usr/bin/copilot",
        }
    )

    assert (
        missing_prerequisites(require_gh=True, require_copilot=True, environment=env) == []
    )
