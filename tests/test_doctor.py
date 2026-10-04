"""Tests for software_agent_factory.doctor.

Every external boundary (subprocess execution, ``PATH`` lookup, platform
info, frozen-executable detection, environment variables, home directory and
file reads) is injected through ``DoctorEnvironment``; no test spawns a real
``git``/``gh``/``copilot``/``pi``/``node`` process, reads the real host's
``PATH``/platform, or touches a real ``~/.pi/agent/auth.json``.

Coverage:

- ``_version_check`` (via ``check_git``/``check_gh``/``check_copilot``):
  required-missing is an error, optional-missing is ok, resolved-but-broken
  is a warning, never a silent success.
- ``check_copilot`` never invokes anything but a bounded ``--version`` probe
  (no paid agent call is possible through this module).
- ``check_pi``: fixed order, first failure wins (executable, pi version,
  Node version, provider credential); not required short-circuits before any
  probe; every failure names what was found and what is required, plus a
  fix.
- ``check_verification_commands``: safe ``shlex`` first-token parsing, no
  shell, malformed-command handling, and de-duplication.
- ``check_config``: missing file, unreadable file, invalid YAML, failed
  validation, and success.
- ``check_data_dir``: writable and not-writable.
- ``check_platform``/``check_executable``/``check_launchctl``.
- ``run_doctor``: the offline default never requires ``gh``/``copilot``/
  ``pi``, and each becomes required only when the corresponding feature is
  enabled or requested.
- ``missing_prerequisites``: the cheap ``PATH``-only gate, including ``pi``.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

import pytest
from factory_testing import build_config

from software_agent_factory.config import PiConfig
from software_agent_factory.doctor import (
    CheckStatus,
    DoctorEnvironment,
    DoctorReport,
    check_claude_code,
    check_config,
    check_copilot,
    check_data_dir,
    check_executable,
    check_gh,
    check_git,
    check_launchctl,
    check_pi,
    check_platform,
    check_verification_commands,
    default_command_runner,
    missing_prerequisites,
    run_doctor,
)
from software_agent_factory.models import RuntimeName


@dataclass
class FakeRunner:
    """Records every call; never spawns a real process."""

    responses: dict[str, subprocess.CompletedProcess[str]] = field(default_factory=dict)
    timeout_for: set[str] = field(default_factory=set)
    oserror_for: set[str] = field(default_factory=set)
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def __call__(self, argv: Sequence[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
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


def make_getenv(values: dict[str, str] | None = None) -> Callable[[str], str | None]:
    """A ``DoctorEnvironment.getenv`` double: no real process environment is
    ever read by a test."""

    def _getenv(name: str) -> str | None:
        return (values or {}).get(name)

    return _getenv


def make_read_text(files: dict[Path, str] | None = None) -> Callable[[Path], str | None]:
    """A ``DoctorEnvironment.read_text`` double: no real filesystem read (in
    particular no real ``~/.pi/agent/auth.json``) happens in a test."""

    def _read_text(path: Path) -> str | None:
        return (files or {}).get(path)

    return _read_text


def make_env(
    *,
    available: dict[str, str] | None = None,
    runner: FakeRunner | None = None,
    system: str = "Darwin",
    machine: str = "arm64",
    is_frozen: bool = False,
    executable_path: Path = Path("/usr/bin/factory"),
    getenv: Callable[[str], str | None] | None = None,
    home_dir: Path = Path("/fake-home"),
    read_text: Callable[[Path], str | None] | None = None,
) -> tuple[DoctorEnvironment, FakeRunner]:
    fake_runner = runner if runner is not None else FakeRunner()
    env = DoctorEnvironment(
        run_command=fake_runner,
        which=make_which(available or {}),
        system=system,
        machine=machine,
        is_frozen=is_frozen,
        executable_path=executable_path,
        getenv=getenv if getenv is not None else make_getenv(),
        home_dir=home_dir,
        read_text=read_text if read_text is not None else make_read_text(),
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


def test_check_claude_code_not_requested_and_missing_is_ok() -> None:
    env, runner = make_env(available={})
    assert check_claude_code(env, required=False).status is CheckStatus.OK
    assert runner.calls == []


def test_check_claude_code_requested_and_missing_is_error() -> None:
    env, _ = make_env(available={})
    assert check_claude_code(env, required=True).status is CheckStatus.ERROR


def test_check_claude_code_never_calls_anything_but_bounded_version_probe() -> None:
    """Doctor must never make a Claude Code agent call -- only ``--version``."""
    env, runner = make_env(available={"claude": "/usr/local/bin/claude"})
    assert check_claude_code(env, required=True).status is CheckStatus.OK
    assert runner.calls == [("/usr/local/bin/claude", "--version")]


def test_run_doctor_requires_claude_when_runtime_requested() -> None:
    env, _ = make_env(available={"git": "/usr/bin/git"})  # claude missing
    report = run_doctor(
        config_path=None, requested_runtime=RuntimeName.CLAUDE_CODE, environment=env
    )
    assert report.success is False
    claude_check = next(c for c in report.checks if c.name == "claude-code")
    assert claude_check.status is CheckStatus.ERROR


def test_run_doctor_default_runtime_never_requires_claude() -> None:
    env, _ = make_env(available={"git": "/usr/bin/git"})  # claude missing, but not requested
    report = run_doctor(config_path=None, environment=env)
    claude_check = next(c for c in report.checks if c.name == "claude-code")
    assert claude_check.status is CheckStatus.OK


# -- pi runtime prerequisite checks ------------------------------------------
#
# Fixed order, first failure wins: executable -> pi version -> Node version
# -> provider credential (plans/pi-agent-runtime.md Step 2.3).


def _pi_ready_available() -> dict[str, str]:
    return {"pi": "/usr/local/bin/pi", "node": "/usr/local/bin/node"}


def _pi_ready_runner() -> FakeRunner:
    return FakeRunner(
        responses={
            "/usr/local/bin/pi": subprocess.CompletedProcess(
                ["/usr/local/bin/pi", "--version"], 0, stdout="0.99.1\n", stderr=""
            ),
            "/usr/local/bin/node": subprocess.CompletedProcess(
                ["/usr/local/bin/node", "--version"], 0, stdout="v22.19.0\n", stderr=""
            ),
        }
    )


def test_check_pi_not_required_short_circuits_before_any_probe() -> None:
    """Mirrors ``check_copilot``: not required (``--runtime pi`` was not
    requested) is OK before anything -- including a resolved executable -- is
    probed."""
    env, runner = make_env(available={})
    result = check_pi(env, PiConfig(), required=False)
    assert result.status is CheckStatus.OK
    assert runner.calls == []


def test_check_pi_missing_executable_is_error_naming_the_npm_install_fix() -> None:
    env, _ = make_env(available={})
    result = check_pi(env, PiConfig(), required=True)
    assert result.status is CheckStatus.ERROR
    assert result.message == "'pi' was not found on PATH"
    assert result.remediation is not None
    assert "npm install -g @earendil-works/pi-coding-agent" in result.remediation


def test_check_pi_old_pi_version_is_error_naming_found_and_required() -> None:
    runner = FakeRunner(
        responses={
            "/usr/local/bin/pi": subprocess.CompletedProcess(
                ["/usr/local/bin/pi", "--version"], 0, stdout="0.99.0\n", stderr=""
            )
        }
    )
    env, _ = make_env(available={"pi": "/usr/local/bin/pi"}, runner=runner)
    result = check_pi(env, PiConfig(), required=True)
    assert result.status is CheckStatus.ERROR
    assert "0.99.0" in result.message
    assert "0.99.1" in result.message
    assert "npm install -g @earendil-works/pi-coding-agent" in (result.remediation or "")
    # Node/credential are unreachable once the pi version check fails first.
    assert runner.calls == [("/usr/local/bin/pi", "--version")]


def test_check_pi_unparseable_pi_version_is_error_naming_the_raw_output() -> None:
    runner = FakeRunner(
        responses={
            "/usr/local/bin/pi": subprocess.CompletedProcess(
                ["/usr/local/bin/pi", "--version"], 0, stdout="not a version\n", stderr=""
            )
        }
    )
    env, _ = make_env(available={"pi": "/usr/local/bin/pi"}, runner=runner)
    result = check_pi(env, PiConfig(), required=True)
    assert result.status is CheckStatus.ERROR
    assert "not a version" in result.message


def test_check_pi_missing_node_is_error() -> None:
    runner = FakeRunner(
        responses={
            "/usr/local/bin/pi": subprocess.CompletedProcess(
                ["/usr/local/bin/pi", "--version"], 0, stdout="0.99.1\n", stderr=""
            )
        }
    )
    env, _ = make_env(available={"pi": "/usr/local/bin/pi"}, runner=runner)
    result = check_pi(env, PiConfig(), required=True)
    assert result.status is CheckStatus.ERROR
    assert "node" in result.message.lower()


def test_check_pi_old_node_version_is_error_naming_found_and_required() -> None:
    runner = FakeRunner(
        responses={
            "/usr/local/bin/pi": subprocess.CompletedProcess(
                ["/usr/local/bin/pi", "--version"], 0, stdout="0.99.1\n", stderr=""
            ),
            "/usr/local/bin/node": subprocess.CompletedProcess(
                ["/usr/local/bin/node", "--version"], 0, stdout="v18.2.0\n", stderr=""
            ),
        }
    )
    env, _ = make_env(available=_pi_ready_available(), runner=runner)
    result = check_pi(env, PiConfig(), required=True)
    assert result.status is CheckStatus.ERROR
    assert "v18.2.0" in result.message
    assert "22.19" in result.message


def test_check_pi_missing_credential_is_error() -> None:
    env, _ = make_env(available=_pi_ready_available(), runner=_pi_ready_runner())
    result = check_pi(env, PiConfig(), required=True)
    assert result.status is CheckStatus.ERROR
    assert "credential" in result.message
    assert "github-copilot" in result.message
    assert result.remediation == "Run 'pi' then '/login' and choose github-copilot."


def test_check_pi_credential_via_provider_env_var_passes() -> None:
    env, _ = make_env(
        available=_pi_ready_available(),
        runner=_pi_ready_runner(),
        getenv=make_getenv({"COPILOT_GITHUB_TOKEN": "secret-token"}),
    )
    result = check_pi(env, PiConfig(), required=True)
    assert result.status is CheckStatus.OK


def test_check_pi_env_credential_rejected_when_env_credentials_not_accepted() -> None:
    """Service-install preflight (``accept_env_credentials=False``): an
    operator-shell env var must not satisfy the credential check, since a
    launchd job never inherits it -- only auth.json does."""
    env, _ = make_env(
        available=_pi_ready_available(),
        runner=_pi_ready_runner(),
        getenv=make_getenv({"COPILOT_GITHUB_TOKEN": "secret-token"}),
    )
    result = check_pi(env, PiConfig(), required=True, accept_env_credentials=False)
    assert result.status is CheckStatus.ERROR
    assert "credential" in result.message
    assert "do not reach the service" in result.message


def test_check_pi_auth_json_still_passes_when_env_credentials_not_accepted() -> None:
    home = Path("/fake-home")
    auth_path = home / ".pi" / "agent" / "auth.json"
    env, _ = make_env(
        available=_pi_ready_available(),
        runner=_pi_ready_runner(),
        home_dir=home,
        read_text=make_read_text({auth_path: '{"github-copilot": {"token": "x"}}'}),
    )
    result = check_pi(env, PiConfig(), required=True, accept_env_credentials=False)
    assert result.status is CheckStatus.OK


def test_check_pi_credential_via_auth_json_passes() -> None:
    home = Path("/fake-home")
    auth_path = home / ".pi" / "agent" / "auth.json"
    env, _ = make_env(
        available=_pi_ready_available(),
        runner=_pi_ready_runner(),
        home_dir=home,
        read_text=make_read_text({auth_path: '{"github-copilot": {"token": "x"}}'}),
    )
    result = check_pi(env, PiConfig(), required=True)
    assert result.status is CheckStatus.OK


def test_check_pi_credential_respects_pi_coding_agent_dir_override() -> None:
    override_dir = Path("/custom/pi-agent-dir")
    auth_path = override_dir / "auth.json"
    env, _ = make_env(
        available=_pi_ready_available(),
        runner=_pi_ready_runner(),
        getenv=make_getenv({"PI_CODING_AGENT_DIR": str(override_dir)}),
        read_text=make_read_text({auth_path: '{"github-copilot": {}}'}),
    )
    result = check_pi(env, PiConfig(), required=True)
    assert result.status is CheckStatus.OK


def test_check_pi_different_provider_uses_its_own_env_var() -> None:
    env, _ = make_env(
        available=_pi_ready_available(),
        runner=_pi_ready_runner(),
        getenv=make_getenv({"ANTHROPIC_API_KEY": "secret"}),
    )
    result = check_pi(env, PiConfig(provider="anthropic"), required=True)
    assert result.status is CheckStatus.OK


def test_check_pi_google_provider_accepts_gemini_api_key() -> None:
    """``doctor`` and the runtime share one provider -> credential map; the
    ``google`` provider authenticates from ``GEMINI_API_KEY``."""
    env, _ = make_env(
        available=_pi_ready_available(),
        runner=_pi_ready_runner(),
        getenv=make_getenv({"GEMINI_API_KEY": "secret"}),
    )
    result = check_pi(env, PiConfig(provider="google"), required=True)
    assert result.status is CheckStatus.OK


def test_check_pi_anthropic_provider_accepts_an_oauth_token() -> None:
    env, _ = make_env(
        available=_pi_ready_available(),
        runner=_pi_ready_runner(),
        getenv=make_getenv({"ANTHROPIC_OAUTH_TOKEN": "secret"}),
    )
    result = check_pi(env, PiConfig(provider="anthropic"), required=True)
    assert result.status is CheckStatus.OK


def _check_bedrock(variables: dict[str, str]) -> CheckStatus:
    env, _ = make_env(
        available=_pi_ready_available(),
        runner=_pi_ready_runner(),
        getenv=make_getenv(variables),
    )
    return check_pi(env, PiConfig(provider="amazon-bedrock"), required=True).status


@pytest.mark.parametrize("variable", ["AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN"])
def test_check_pi_amazon_bedrock_lone_aws_variable_is_not_a_credential(variable: str) -> None:
    """A key id (or session token) without its secret cannot sign a request."""
    assert _check_bedrock({variable: "value"}) is CheckStatus.ERROR


@pytest.mark.parametrize("secret", ["AWS_SECRET_ACCESS_KEY", "AWS_SECRET_KEY"])
def test_check_pi_amazon_bedrock_key_id_with_a_secret_passes(secret: str) -> None:
    assert _check_bedrock({"AWS_ACCESS_KEY_ID": "id", secret: "secret"}) is CheckStatus.OK


def test_check_pi_amazon_bedrock_secret_without_a_key_id_fails() -> None:
    assert _check_bedrock({"AWS_SECRET_ACCESS_KEY": "secret"}) is CheckStatus.ERROR


@pytest.mark.parametrize(
    "variable",
    [
        "AWS_BEARER_TOKEN_BEDROCK",
        "AWS_PROFILE",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    ],
)
def test_check_pi_amazon_bedrock_self_sufficient_variable_passes(variable: str) -> None:
    assert _check_bedrock({variable: "value"}) is CheckStatus.OK


def test_check_pi_several_failures_names_the_executable_first() -> None:
    """No executable and no credential both apply; the executable check runs
    first in the fixed order, so its message wins."""
    env, _ = make_env(available={})
    result = check_pi(env, PiConfig(), required=True)
    assert result.status is CheckStatus.ERROR
    assert "was not found on PATH" in result.message


def test_check_pi_minimum_pi_version_is_ok() -> None:
    """Exact boundary: ``PI_MIN_VERSION`` itself (``"0.99.1"``) must pass, not
    just versions strictly above it."""
    runner = FakeRunner(
        responses={
            "/usr/local/bin/pi": subprocess.CompletedProcess(
                ["/usr/local/bin/pi", "--version"], 0, stdout="0.99.1\n", stderr=""
            ),
            "/usr/local/bin/node": subprocess.CompletedProcess(
                ["/usr/local/bin/node", "--version"], 0, stdout="v22.19.0\n", stderr=""
            ),
        }
    )
    env, _ = make_env(
        available=_pi_ready_available(),
        runner=runner,
        getenv=make_getenv({"COPILOT_GITHUB_TOKEN": "secret-token"}),
    )
    result = check_pi(env, PiConfig(), required=True)
    assert result.status is CheckStatus.OK


def test_check_pi_minimum_node_version_is_ok() -> None:
    """Exact boundary: ``NODE_MIN_VERSION`` itself (``"v22.19.0"``) must
    pass, not just versions strictly above it."""
    runner = FakeRunner(
        responses={
            "/usr/local/bin/pi": subprocess.CompletedProcess(
                ["/usr/local/bin/pi", "--version"], 0, stdout="0.99.1\n", stderr=""
            ),
            "/usr/local/bin/node": subprocess.CompletedProcess(
                ["/usr/local/bin/node", "--version"], 0, stdout="v22.19.0\n", stderr=""
            ),
        }
    )
    env, _ = make_env(
        available=_pi_ready_available(),
        runner=runner,
        getenv=make_getenv({"COPILOT_GITHUB_TOKEN": "secret-token"}),
    )
    result = check_pi(env, PiConfig(), required=True)
    assert result.status is CheckStatus.OK


def test_check_pi_all_prerequisites_present_is_ok() -> None:
    env, _ = make_env(
        available=_pi_ready_available(),
        runner=_pi_ready_runner(),
        getenv=make_getenv({"COPILOT_GITHUB_TOKEN": "secret-token"}),
    )
    result = check_pi(env, PiConfig(), required=True)
    assert result.status is CheckStatus.OK
    assert result.remediation is None


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

    config_path.write_text(yaml.safe_dump(factory_config.model_dump(mode="json")), encoding="utf-8")
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

    config_path.write_text(yaml.safe_dump(factory_config.model_dump(mode="json")), encoding="utf-8")

    env, _ = make_env(
        available={"git": "/usr/bin/git", "launchctl": "/bin/launchctl"}
    )  # no gh, no copilot
    report = run_doctor(
        config_path=config_path,
        requested_runtime=None,
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

    config_path.write_text(yaml.safe_dump(factory_config.model_dump(mode="json")), encoding="utf-8")

    env, _ = make_env(available={"git": "/usr/bin/git"})  # gh missing
    report = run_doctor(config_path=config_path, environment=env)
    assert report.success is False
    gh_check = next(c for c in report.checks if c.name == "gh")
    assert gh_check.status is CheckStatus.ERROR


def test_run_doctor_requires_copilot_when_runtime_requested(tmp_path: Path) -> None:
    env, _ = make_env(available={"git": "/usr/bin/git"})  # copilot missing
    report = run_doctor(
        config_path=None,
        requested_runtime=RuntimeName.COPILOT,
        environment=env,
    )
    assert report.success is False
    copilot_check = next(c for c in report.checks if c.name == "copilot")
    assert copilot_check.status is CheckStatus.ERROR


def test_run_doctor_requires_pi_when_runtime_requested() -> None:
    env, _ = make_env(available={"git": "/usr/bin/git"})  # pi missing
    report = run_doctor(
        config_path=None,
        requested_runtime=RuntimeName.PI,
        environment=env,
    )
    assert report.success is False
    pi_check = next(c for c in report.checks if c.name == "pi")
    assert pi_check.status is CheckStatus.ERROR


def test_run_doctor_forwards_accept_pi_env_credentials_to_check_pi() -> None:
    """``accept_pi_env_credentials=False`` (the service-install preflight)
    must make ``run_doctor`` reject an env-var pi credential too, not just
    ``check_pi`` called directly."""
    env, _ = make_env(
        available=_pi_ready_available(),
        runner=_pi_ready_runner(),
        getenv=make_getenv({"COPILOT_GITHUB_TOKEN": "secret-token"}),
    )
    report = run_doctor(
        config_path=None,
        requested_runtime=RuntimeName.PI,
        accept_pi_env_credentials=False,
        environment=env,
    )
    assert report.success is False
    pi_check = next(c for c in report.checks if c.name == "pi")
    assert pi_check.status is CheckStatus.ERROR
    assert "do not reach the service" in pi_check.message


def test_run_doctor_default_runtime_never_requires_pi() -> None:
    env, _ = make_env(available={"git": "/usr/bin/git"})  # pi missing, but not requested
    report = run_doctor(config_path=None, environment=env)
    pi_check = next(c for c in report.checks if c.name == "pi")
    assert pi_check.status is CheckStatus.OK


def test_run_doctor_uses_config_data_dir_when_not_overridden(tmp_path: Path) -> None:
    data_dir = tmp_path / "configured-data"
    factory_config = build_config(data_dir)
    config_path = tmp_path / "factory.yaml"
    import yaml

    config_path.write_text(yaml.safe_dump(factory_config.model_dump(mode="json")), encoding="utf-8")

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

    config_path.write_text(yaml.safe_dump(factory_config.model_dump(mode="json")), encoding="utf-8")

    env, _ = make_env(available={"git": "/usr/bin/git"})
    report = run_doctor(config_path=config_path, data_dir_override=override_dir, environment=env)
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

    report = DoctorReport(checks=(CheckResult(name="a", status=CheckStatus.OK, message="fine"),))
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
        default_command_runner([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.2)


# -- gh is required by every GitHub-touching feature ---------------------------


def _write_config(tmp_path: Path, **kwargs: object) -> Path:
    import yaml

    factory_config = build_config(tmp_path / "data", **kwargs)
    config_path = tmp_path / "factory.yaml"
    config_path.write_text(yaml.safe_dump(factory_config.model_dump(mode="json")), encoding="utf-8")
    return config_path


def test_requires_gh_is_true_for_every_github_touching_feature(tmp_path: Path) -> None:
    from software_agent_factory.doctor import requires_gh

    assert requires_gh(build_config(tmp_path / "data")) is False
    assert requires_gh(build_config(tmp_path / "data", pull_request={"enabled": True})) is True
    assert (
        requires_gh(
            build_config(tmp_path / "data", pull_request={"enabled": True}, ci={"enabled": True})
        )
        is True
    )
    assert requires_gh(build_config(tmp_path / "data", scheduler={"enabled": True})) is True


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

    env, runner = make_env(available={})
    assert missing_prerequisites(environment=env) == ["git"]
    # A PATH lookup only: nothing is executed by this gate.
    assert runner.calls == []


def test_missing_prerequisites_uses_the_configured_pi_executable_name() -> None:
    """``pi_executable`` names the executable to look up, so a custom
    ``config.pi.executable`` is honored instead of a literal ``"pi"`` -- and
    a shim literally named ``"pi"`` must not satisfy a differently-named
    requirement."""

    env, _ = make_env(available={"git": "/usr/bin/git", "pi": "/usr/local/bin/pi"})

    assert missing_prerequisites(
        runtimes={RuntimeName.PI}, pi_executable="custom-pi-agent", environment=env
    ) == ["custom-pi-agent"]
    assert (
        missing_prerequisites(runtimes={RuntimeName.PI}, pi_executable="pi", environment=env) == []
    )


def test_missing_prerequisites_reports_only_requested_tools() -> None:

    env, _ = make_env(available={"git": "/usr/bin/git"})

    assert missing_prerequisites(environment=env) == []
    assert missing_prerequisites(require_gh=True, environment=env) == ["gh"]
    assert missing_prerequisites(runtimes={RuntimeName.COPILOT}, environment=env) == ["copilot"]
    assert missing_prerequisites(runtimes={RuntimeName.PI}, environment=env) == ["pi"]
    assert missing_prerequisites(
        runtimes={RuntimeName.COPILOT}, require_gh=True, environment=env
    ) == [
        "gh",
        "copilot",
    ]
    assert missing_prerequisites(runtimes={RuntimeName.CLAUDE_CODE}, environment=env) == ["claude"]
    assert missing_prerequisites(
        runtimes={RuntimeName.COPILOT, RuntimeName.PI}, require_gh=True, environment=env
    ) == ["gh", "copilot", "pi"]
    assert missing_prerequisites(
        runtimes={RuntimeName.COPILOT, RuntimeName.PI, RuntimeName.CLAUDE_CODE},
        require_gh=True,
        environment=env,
    ) == ["gh", "copilot", "pi", "claude"]


def test_missing_prerequisites_is_empty_when_everything_is_present() -> None:

    env, _ = make_env(
        available={
            "git": "/usr/bin/git",
            "gh": "/usr/bin/gh",
            "copilot": "/usr/bin/copilot",
            "pi": "/usr/local/bin/pi",
        }
    )

    assert (
        missing_prerequisites(
            runtimes={RuntimeName.COPILOT, RuntimeName.PI}, require_gh=True, environment=env
        )
        == []
    )
