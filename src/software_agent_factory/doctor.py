"""macOS/environment preflight ("factory doctor").

``PLAN.md`` Phase 15.2 and ``docs/architecture.md`` ("Local service", "Health
and metrics"): a frozen runtime bundles Python and the factory, but still
needs external tools on ``PATH``:

.. code-block:: text

    required always      git
    required if enabled  gh        (pull_request.enabled / ci.enabled /
                                    scheduler.enabled)
    required if chosen   copilot   (--runtime copilot)

Preflight validates prerequisites for *enabled* features only, so a default
offline run never demands ``gh`` or ``copilot``. This module never makes a
paid Copilot call: the only ``copilot`` interaction it may perform is a
bounded ``copilot --version`` subprocess, never a real agent request.

Every external boundary -- subprocess execution, ``PATH`` lookup, platform
info and frozen-executable detection -- is injected through
:class:`DoctorEnvironment`, defaulting to real implementations, so this
module is fully deterministic and unit-testable without touching the host.

Public API, used by ``factory doctor`` and by the ``run``/``start``/``service
install`` prerequisite gate (``cli.py``):

- :class:`CheckStatus`, :class:`CheckResult`, :class:`DoctorReport` -- the
  typed report
- :class:`DoctorEnvironment`, :class:`CommandRunner`,
  :func:`default_command_runner` -- the injectable seam
- :func:`run_doctor` -- the full preflight
- :func:`missing_prerequisites` -- the cheap ``PATH``-only subset a command
  runs before doing any work
"""

from __future__ import annotations

import platform
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Callable, Protocol, Sequence

import yaml
from pydantic import ValidationError

from .config import FactoryConfig, load_config

__all__ = [
    "CheckResult",
    "CheckStatus",
    "CommandRunner",
    "DEFAULT_COMMAND_TIMEOUT_SECONDS",
    "DoctorEnvironment",
    "DoctorReport",
    "SUPPORTED_MACHINES",
    "check_config",
    "check_copilot",
    "check_data_dir",
    "check_executable",
    "check_git",
    "check_gh",
    "check_launchctl",
    "check_platform",
    "check_verification_commands",
    "default_command_runner",
    "missing_prerequisites",
    "requires_gh",
    "run_doctor",
]

#: Bound every prerequisite probe to a short, deterministic timeout. Doctor
#: never runs a real build/test command -- only ``--version`` (or equivalent)
#: on the resolved executable.
DEFAULT_COMMAND_TIMEOUT_SECONDS = 5.0

#: Architectures the release process actually publishes (PLAN.md Phase 15.1:
#: separate native ``arm64``/``x86_64`` archives, no ``universal2``).
SUPPORTED_MACHINES: frozenset[str] = frozenset({"arm64", "x86_64"})


class CheckStatus(StrEnum):
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class CheckResult:
    """One preflight finding.

    ``remediation`` is populated whenever ``status`` is not ``OK`` so a human
    (or the CLI rendering this report) always has an explicit next step
    instead of a bare failure.
    """

    name: str
    status: CheckStatus
    message: str
    remediation: str | None = None


@dataclass(frozen=True)
class DoctorReport:
    """The full preflight result. ``success`` is false if any check errored;
    warnings do not fail the report."""

    checks: tuple[CheckResult, ...]

    @property
    def success(self) -> bool:
        return all(check.status is not CheckStatus.ERROR for check in self.checks)

    def to_dict(self) -> dict[str, object]:
        return {
            "success": self.success,
            "checks": [
                {
                    "name": check.name,
                    "status": check.status.value,
                    "message": check.message,
                    "remediation": check.remediation,
                }
                for check in self.checks
            ],
        }


class CommandRunner(Protocol):
    """Injectable bounded process runner. Real and fake implementations share
    this shape so tests never spawn a real ``git``/``gh``/``copilot``
    process."""

    def __call__(
        self, argv: Sequence[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]: ...


def default_command_runner(
    argv: Sequence[str], *, timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` directly (never through a shell), bounded by ``timeout``."""
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


@dataclass(frozen=True)
class DoctorEnvironment:
    """Injectable seam for every host fact doctor checks need.

    Every field defaults to the real implementation so production code can
    construct ``DoctorEnvironment()`` with no arguments, while tests override
    individual fields to make every check deterministic.
    """

    run_command: CommandRunner = default_command_runner
    which: Callable[[str], str | None] = shutil.which
    system: str = field(default_factory=platform.system)
    machine: str = field(default_factory=platform.machine)
    is_frozen: bool = field(default_factory=lambda: bool(getattr(sys, "frozen", False)))
    executable_path: Path = field(default_factory=lambda: Path(sys.executable))


def _version_check(
    env: DoctorEnvironment,
    *,
    name: str,
    executable: str,
    required: bool,
    reason: str,
    version_args: Sequence[str] = ("--version",),
) -> CheckResult:
    """Resolve ``executable`` on ``PATH`` and bounded-version-check it.

    Missing-but-not-required resolves to ``OK`` (a default offline run must
    not warn about ``gh``/``copilot``). Missing-and-required is an ``ERROR``.

    A *resolved* executable that fails its version probe (timeout, exec
    failure, or non-zero exit) is not usable either way, but the severity
    depends on whether it is required: for a required tool that is not a
    silent success -- it is an ``ERROR``, since the factory cannot actually
    use a broken required tool. For an optional tool the same failure is
    only a ``WARNING``, since some tools do not cleanly support
    ``--version`` and the feature that would need them is not enabled.
    """
    resolved = env.which(executable)
    if resolved is None:
        if required:
            return CheckResult(
                name=name,
                status=CheckStatus.ERROR,
                message=f"'{executable}' was not found on PATH",
                remediation=f"Install {executable} ({reason}) and ensure it is on PATH.",
            )
        return CheckResult(
            name=name,
            status=CheckStatus.OK,
            message=f"'{executable}' not found on PATH (not required: {reason})",
        )

    unusable_status = CheckStatus.ERROR if required else CheckStatus.WARNING

    try:
        result = env.run_command(
            [resolved, *version_args], timeout=DEFAULT_COMMAND_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name=name,
            status=unusable_status,
            message=(
                f"'{executable}' at {resolved} did not respond within "
                f"{DEFAULT_COMMAND_TIMEOUT_SECONDS}s"
            ),
            remediation=f"Verify the {executable} installation manually.",
        )
    except OSError as exc:
        return CheckResult(
            name=name,
            status=unusable_status,
            message=f"'{executable}' at {resolved} could not be executed: {exc}",
            remediation=f"Reinstall or repair {executable} ({reason}).",
        )

    if result.returncode != 0:
        return CheckResult(
            name=name,
            status=unusable_status,
            message=(
                f"'{executable}' at {resolved} exited {result.returncode} for "
                f"{' '.join(version_args)}"
            ),
            remediation=f"Verify the {executable} installation manually.",
        )

    output = (result.stdout or result.stderr).strip()
    version_text = output.splitlines()[0] if output else ""
    message = f"'{executable}' found at {resolved}"
    if version_text:
        message = f"{message} ({version_text})"
    return CheckResult(name=name, status=CheckStatus.OK, message=message)


def check_git(env: DoctorEnvironment) -> CheckResult:
    """``git`` is required for every run, enabled or not."""
    return _version_check(
        env, name="git", executable="git", required=True, reason="required for every run"
    )


def check_gh(env: DoctorEnvironment, *, required: bool) -> CheckResult:
    """``gh`` is required when any GitHub-touching feature is enabled.

    That is ``pull_request.enabled`` or ``ci.enabled`` (publishing and CI
    observation) *and* ``scheduler.enabled``: the backlog daemon polls
    GitHub Issues through ``gh`` (``github_tracker.GitHubIssueProvider``), so
    a scheduler without ``gh`` fails on its very first tick.
    """
    return _version_check(
        env,
        name="gh",
        executable="gh",
        required=required,
        reason="pull_request.enabled, ci.enabled or scheduler.enabled",
    )


def check_copilot(env: DoctorEnvironment, *, required: bool) -> CheckResult:
    """``copilot`` is only required when ``--runtime copilot`` is requested.

    This performs a bounded ``copilot --version`` probe only -- never a real
    (paid) agent call.
    """
    return _version_check(
        env,
        name="copilot",
        executable="copilot",
        required=required,
        reason="--runtime copilot",
    )


def check_verification_commands(
    env: DoctorEnvironment, commands: Sequence[str]
) -> list[CheckResult]:
    """Version-check the executables behind configured repository commands.

    Each configured command (``repository.commands.install/verify/build``) is
    a plain shell string (see ``verification.py``); only its first token is
    ever parsed here, using :mod:`shlex` and never a shell, and only to
    resolve/version-check the executable it names.
    """
    results: list[CheckResult] = []
    seen: set[str] = set()
    for raw_command in commands:
        try:
            tokens = shlex.split(raw_command, posix=True)
        except ValueError as exc:
            results.append(
                CheckResult(
                    name=f"command:{raw_command}",
                    status=CheckStatus.ERROR,
                    message=f"could not parse configured command {raw_command!r}: {exc}",
                    remediation=(
                        "Fix the malformed command in repository.commands configuration."
                    ),
                )
            )
            continue
        if not tokens:
            continue
        executable = tokens[0]
        if executable in seen:
            continue
        seen.add(executable)
        results.append(
            _version_check(
                env,
                name=f"command:{executable}",
                executable=executable,
                required=True,
                reason=f"configured repository command {raw_command!r}",
            )
        )
    return results


def check_config(config_path: Path | None) -> tuple[CheckResult, FactoryConfig | None]:
    """Load and validate configuration, if a path was given (or the packaged
    default otherwise). Never raises: every expected failure mode of
    ``load_config`` becomes an ``ERROR`` check result."""
    label = str(config_path) if config_path is not None else "(packaged default)"
    try:
        config = load_config(config_path)
    except FileNotFoundError as exc:
        return (
            CheckResult(
                name="config",
                status=CheckStatus.ERROR,
                message=f"config file not found: {exc}",
                remediation=f"Check the --config path ({label}).",
            ),
            None,
        )
    except OSError as exc:
        return (
            CheckResult(
                name="config",
                status=CheckStatus.ERROR,
                message=f"config file at {label} could not be read: {exc}",
                remediation="Check file permissions and encoding.",
            ),
            None,
        )
    except yaml.YAMLError as exc:
        return (
            CheckResult(
                name="config",
                status=CheckStatus.ERROR,
                message=f"config at {label} is not valid YAML: {exc}",
                remediation="Fix the YAML syntax error and re-run.",
            ),
            None,
        )
    except (ValueError, ValidationError) as exc:
        return (
            CheckResult(
                name="config",
                status=CheckStatus.ERROR,
                message=f"config at {label} failed validation: {exc}",
                remediation="Fix the reported field(s) in the configuration file.",
            ),
            None,
        )
    return (
        CheckResult(name="config", status=CheckStatus.OK, message=f"valid config ({label})"),
        config,
    )


def check_data_dir(path: Path) -> CheckResult:
    """Ensure the data/workspace directory exists (or can be created) and is
    writable by the current user."""
    probe = path / ".doctor-write-test"
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe.write_text("", encoding="utf-8")
    except OSError as exc:
        return CheckResult(
            name="data_dir",
            status=CheckStatus.ERROR,
            message=f"data directory {path} is not writable: {exc}",
            remediation=(
                "Choose a data directory the current user can write to, or fix its "
                "permissions."
            ),
        )
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass  # cleanup best-effort; the write attempt above already decided the result
    return CheckResult(name="data_dir", status=CheckStatus.OK, message=f"{path} is writable")


def check_platform(env: DoctorEnvironment) -> CheckResult:
    """Report OS/architecture. Packaging (PLAN.md Phase 15.1/15.2) only
    targets macOS ``arm64``/``x86_64``, so anything else is a warning, not a
    hard failure -- development on Linux still works with ``--runtime
    fake``/tests."""
    if env.system != "Darwin":
        return CheckResult(
            name="platform",
            status=CheckStatus.WARNING,
            message=f"running on {env.system}, not macOS",
            remediation=(
                "The packaged release and the launchd service target macOS only."
            ),
        )
    if env.machine not in SUPPORTED_MACHINES:
        return CheckResult(
            name="platform",
            status=CheckStatus.WARNING,
            message=f"unrecognized architecture {env.machine!r}",
            remediation="No prebuilt archive is published for this architecture.",
        )
    return CheckResult(
        name="platform", status=CheckStatus.OK, message=f"macOS {env.machine}"
    )


def check_executable(env: DoctorEnvironment) -> CheckResult:
    """Report whether the current process is a frozen (PyInstaller) build or
    running from source/interpreter, and where from."""
    mode = "frozen (PyInstaller)" if env.is_frozen else "source / interpreter"
    return CheckResult(
        name="executable",
        status=CheckStatus.OK,
        message=f"{mode} at {env.executable_path}",
    )


def check_launchctl(env: DoctorEnvironment) -> CheckResult:
    """``launchctl`` ships with macOS; its absence there indicates a broken
    environment. Not applicable off macOS."""
    if env.system != "Darwin":
        return CheckResult(
            name="launchctl", status=CheckStatus.OK, message="not applicable outside macOS"
        )
    resolved = env.which("launchctl")
    if resolved is None:
        return CheckResult(
            name="launchctl",
            status=CheckStatus.ERROR,
            message="launchctl not found on PATH",
            remediation="launchctl ships with macOS; this indicates a broken environment.",
        )
    return CheckResult(name="launchctl", status=CheckStatus.OK, message=f"found at {resolved}")


def requires_gh(config: FactoryConfig) -> bool:
    """Whether this configuration needs ``gh`` on ``PATH``.

    Every GitHub-touching feature counts: publishing (``pull_request``), CI
    observation (``ci``) and the backlog daemon (``scheduler``), which polls
    GitHub Issues through ``gh``. Kept as one function so the doctor report
    and the CLI's cheap prerequisite gate can never disagree about it.
    """
    return config.pull_request.enabled or config.ci.enabled or config.scheduler.enabled


def run_doctor(
    *,
    config_path: Path | None = None,
    data_dir_override: Path | None = None,
    requested_runtime_copilot: bool = False,
    environment: DoctorEnvironment | None = None,
) -> DoctorReport:
    """Run every preflight check and return one :class:`DoctorReport`.

    ``config_path``/``data_dir_override`` mirror the CLI's ``--config``/
    ``--data-dir`` options. When configuration loads successfully,
    :func:`requires_gh` decides whether ``gh`` is required, and
    ``repository.commands`` supplies the verification command executables to
    version-check. ``requested_runtime_copilot`` mirrors ``--runtime
    copilot``; it is never inferred, so a default ``fake`` run never demands
    ``copilot``.
    """
    env = environment if environment is not None else DoctorEnvironment()
    checks: list[CheckResult] = [
        check_platform(env),
        check_executable(env),
        check_launchctl(env),
        check_git(env),
    ]

    config_check, config = check_config(config_path)
    checks.append(config_check)

    gh_required = False
    verification_commands: list[str] = []
    data_dir = data_dir_override
    if config is not None:
        gh_required = requires_gh(config)
        verification_commands = [
            *config.repository.commands.install,
            *config.repository.commands.verify,
            *config.repository.commands.build,
        ]
        if data_dir is None:
            data_dir = config.data_dir

    checks.append(check_gh(env, required=gh_required))
    checks.append(check_copilot(env, required=requested_runtime_copilot))
    checks.extend(check_verification_commands(env, verification_commands))

    if data_dir is not None:
        checks.append(check_data_dir(data_dir))

    return DoctorReport(checks=tuple(checks))


def missing_prerequisites(
    *,
    require_gh: bool = False,
    require_copilot: bool = False,
    environment: DoctorEnvironment | None = None,
) -> list[str]:
    """Names of required external executables missing from ``PATH``.

    The cheap subset of :func:`run_doctor` a command runs *before* doing any
    work: a bare ``PATH`` lookup per tool, with no subprocess, no
    configuration load and no filesystem write, so ``factory run`` and
    ``factory start`` can fail with one explicit prerequisite message
    instead of a traceback from deep inside the workspace or tracker code.
    ``git`` is always required; ``gh`` and ``copilot`` only when the caller
    says the requested feature set needs them.
    """
    env = environment if environment is not None else DoctorEnvironment()
    wanted: list[str] = ["git"]
    if require_gh:
        wanted.append("gh")
    if require_copilot:
        wanted.append("copilot")
    return [executable for executable in wanted if env.which(executable) is None]
