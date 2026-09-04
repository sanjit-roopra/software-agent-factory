"""Per-user macOS launchd service lifecycle for ``factory start``.

``PLAN.md`` Phase 15.2, ``docs/architecture.md`` ("Local service") and
``docs/decisions.md`` ADR-018: continuous operation is a per-user
``LaunchAgent`` under ``~/Library/LaunchAgents``, installed only by an
explicit CLI command and by nothing else. Never a root ``LaunchDaemon``,
never installed automatically, never installed as a side effect of
extracting an archive or running any other command.

Every external boundary -- the ``LaunchAgents`` directory, the ``launchctl``
subprocess, the login-shell ``PATH`` capture and the current uid -- is
injected, so tests never touch the real ``~/Library/LaunchAgents`` or spawn a
real ``launchctl``.

Defaults matter for safety:

- ``ServiceRuntime.FAKE`` is the default runtime. An installed-but-forgotten
  service costs nothing until a human explicitly asks for
  ``ServiceRuntime.COPILOT``.
- The rendered ``ProgramArguments`` always invoke the existing ``factory
  start`` command (``cli.start_command``) with explicit ``--repo``,
  ``--github-repo``, optional ``--config``, ``--data-dir`` and ``--runtime``.
  ``start`` has no ``--poll-interval`` flag -- the poll interval lives
  entirely in configuration (``scheduler.poll_interval_seconds``, read via
  ``--config``). This module's ``poll_interval_seconds`` is used only to size
  the launchd ``ThrottleInterval`` (see below), not as a program argument.

launchd cannot special-case one exit code
------------------------------------------
launchd's ``KeepAlive`` dictionary can only key off *whether* the last exit
was successful (``SuccessfulExit``) or off a signal-based crash
(``Crashed``); it has no key for a specific exit code. So there is no plist
construct that says "restart on any failure except exit code 2" (``cli.py``'s
``CONFIG_ERROR_EXIT_CODE``).

Rather than approximate that with a throttled restart-on-any-failure policy
(which would still restart-loop on a persistent configuration error, just
more slowly), this module omits ``SuccessfulExit`` from ``KeepAlive``
entirely and sets only ``Crashed: true``. That is a genuine, structural fix:
launchd then restarts the job *only* when it is terminated by an unhandled
signal (a real crash), and never merely because it exited -- with any exit
code, ``0``, the config-error ``2``, or anything else. A persistent
configuration error therefore cannot produce a restart loop at all, tight or
otherwise.

The honest cost of that design: an ordinary unhandled Python exception is
also a clean (non-signal) process exit, so it is *not* auto-restarted either
-- the service simply stays stopped until an explicit re-install or the next
login/bootstrap. This is deliberate and consistent with this project's
general recovery posture (``service.py``: an abandoned run is escalated to a
human, never silently auto-resumed) rather than an oversight.
``ThrottleInterval`` (``MIN_THROTTLE_INTERVAL_SECONDS``) remains configured
purely as a defensive backstop bounding the rate of genuine crash-loop
restarts; it is not relied on to solve the exit-code problem.

stdout/stderr are pointed at ``/dev/null`` because the factory already writes
its own bounded, rotating, structured JSON logs under
``<data_dir>/logs/factory.log`` (``observability.configure_factory_logging``,
PLAN.md Phase 15.5); a launchd-captured stdout/stderr file is never rotated
and would grow without bound.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .doctor import CommandRunner, DoctorEnvironment, default_command_runner

__all__ = [
    "DEFAULT_LABEL",
    "DEFAULT_LOGIN_SHELL",
    "DEFAULT_PATH_DIRS",
    "MAX_LABEL_LENGTH",
    "MIN_THROTTLE_INTERVAL_SECONDS",
    "ServiceInstallError",
    "ServiceInstallRequest",
    "ServiceRuntime",
    "ServiceStatus",
    "build_launch_agent_plist",
    "build_program_arguments",
    "capture_login_shell_path",
    "default_launch_agents_dir",
    "get_service_status",
    "install_service",
    "plist_path",
    "render_plist_bytes",
    "resolve_factory_executable",
    "sanitize_path_value",
    "uninstall_service",
    "validate_install_paths",
    "validate_label",
]

#: Reverse-DNS style label for the installed LaunchAgent.
DEFAULT_LABEL = "com.github.software-agent-factory"

#: Directories every install must guarantee, in priority order, so Homebrew
#: (arm64 and Intel prefixes) and npm-installed tools (``gh``, ``copilot``)
#: resolve even though launchd agents inherit a minimal environment.
DEFAULT_PATH_DIRS: tuple[str, ...] = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)

DEFAULT_LOGIN_SHELL = "/bin/zsh"

#: Defensive backstop only (see module docstring): bounds the rate of
#: restarts launchd performs for a genuine ``KeepAlive.Crashed`` crash loop.
#: It does not need to be short, since it is no longer what prevents a
#: configuration-error restart loop -- omitting ``SuccessfulExit`` from
#: ``KeepAlive`` does that structurally.
MIN_THROTTLE_INTERVAL_SECONDS = 300

DEFAULT_LAUNCHCTL_TIMEOUT_SECONDS = 10.0
DEFAULT_PATH_CAPTURE_TIMEOUT_SECONDS = 5.0

#: Roots that are never a stable home for a long-running frozen executable.
_TEMP_ROOTS: tuple[str, ...] = ("/tmp", "/var/tmp", "/private/tmp", "/private/var/tmp")

#: Bound and character-whitelist for a LaunchAgent label. Reverse-DNS style,
#: ASCII only, ``.``-separated segments, each non-empty -- so it can never
#: contain ``/``, ``\\``, whitespace, a NUL byte or a ``..`` path-traversal
#: segment once used inside a filesystem path or a ``launchctl`` domain
#: target string (``gui/<uid>/<label>``).
MAX_LABEL_LENGTH = 200
_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+$")


class ServiceInstallError(ValueError):
    """Raised when a service install/uninstall request is unsafe or invalid."""


class ServiceRuntime(StrEnum):
    """Mirrors ``cli.RuntimeChoice``. Defined independently here so this
    module never imports ``cli`` (avoiding a reverse dependency): the CLI is
    expected to import *this* module, not the other way around."""

    FAKE = "fake"
    COPILOT = "copilot"


@dataclass(frozen=True)
class ServiceInstallRequest:
    """Everything needed to render one LaunchAgent plist.

    ``executable``, ``repo`` and ``config_path`` (when given) must be
    absolute, existing paths -- see :func:`validate_install_paths`.
    ``poll_interval_seconds`` only sizes the launchd ``ThrottleInterval``
    (see module docstring); it is never passed as a ``start`` argument.
    """

    executable: Path
    repo: Path
    github_repo: str
    data_dir: Path
    config_path: Path | None = None
    poll_interval_seconds: int = 30
    runtime: ServiceRuntime = ServiceRuntime.FAKE
    label: str = DEFAULT_LABEL
    allow_source_dev: bool = False


@dataclass(frozen=True)
class ServiceStatus:
    """Structured, CLI-renderable (JSON or human) service state."""

    label: str
    plist_path: Path
    installed: bool
    loaded: bool
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "plist_path": str(self.plist_path),
            "installed": self.installed,
            "loaded": self.loaded,
            "detail": self.detail,
        }


def default_launch_agents_dir() -> Path:
    """The real per-user LaunchAgents directory. Tests must inject a
    different path instead of calling this."""
    return Path.home() / "Library" / "LaunchAgents"


def resolve_factory_executable(
    explicit: Path | None = None,
    *,
    environment: DoctorEnvironment | None = None,
) -> Path:
    """Resolve the ``factory`` executable a LaunchAgent should invoke.

    launchd runs the job from a plist that records one absolute program
    path, so this must resolve to a real file rather than to "whatever
    ``PATH`` means at load time". Resolution order:

    1. ``explicit`` (the CLI's ``--executable``), expanded and made
       absolute -- an operator override always wins;
    2. the running process itself when it is a frozen PyInstaller build,
       because that binary *is* the factory;
    3. the installed ``factory`` console script on ``PATH``.

    Raises :class:`ServiceInstallError` when nothing resolves -- for example
    a source checkout whose virtualenv is not active -- rather than guessing
    a path launchd would later fail to execute. Uses the same injectable
    :class:`~software_agent_factory.doctor.DoctorEnvironment` seam as the
    preflight checks, so tests never depend on the host's ``PATH`` or on
    ``sys.frozen``.
    """
    if explicit is not None:
        candidate = explicit.expanduser()
        return candidate if candidate.is_absolute() else Path.cwd() / candidate

    env = environment if environment is not None else DoctorEnvironment()
    if env.is_frozen:
        return Path(env.executable_path).resolve()

    found = env.which("factory")
    if found is not None:
        return Path(found).resolve()

    interpreter_bin = Path(sys.executable).resolve().parent / "factory"
    if interpreter_bin.is_file():
        return interpreter_bin

    raise ServiceInstallError(
        "could not resolve a 'factory' executable to install: it is not a frozen "
        "build and no 'factory' console script was found on PATH; pass an explicit "
        "path (--executable) to the extracted release binary or to the installed "
        "console script."
    )


def sanitize_path_value(value: str) -> str:
    """Validate and normalize a ``PATH`` value.

    Rejects NUL bytes, newlines/carriage returns and any non-absolute entry
    (a relative ``PATH`` entry is always a bug in a launchd environment,
    which has no meaningful working directory). Deduplicates while
    preserving order, then appends any of :data:`DEFAULT_PATH_DIRS` missing
    from the input so Homebrew/npm tool locations are always present.
    """
    if "\x00" in value:
        raise ServiceInstallError("PATH value must not contain a NUL byte")
    if "\n" in value or "\r" in value:
        raise ServiceInstallError("PATH value must not contain a newline")

    seen: set[str] = set()
    deduped: list[str] = []
    for entry in value.split(":"):
        if not entry:
            continue
        if not entry.startswith("/"):
            raise ServiceInstallError(f"PATH entry must be an absolute path, got {entry!r}")
        if entry in seen:
            continue
        seen.add(entry)
        deduped.append(entry)

    for required in DEFAULT_PATH_DIRS:
        if required not in seen:
            seen.add(required)
            deduped.append(required)

    return ":".join(deduped)


def capture_login_shell_path(
    run_command: CommandRunner,
    *,
    shell: str = DEFAULT_LOGIN_SHELL,
    timeout: float = DEFAULT_PATH_CAPTURE_TIMEOUT_SECONDS,
    fallback: str | None = None,
) -> str:
    """Capture ``PATH`` from a login+interactive shell, bounded by ``timeout``.

    launchd agents inherit a minimal environment (ADR-018), so the installer
    snapshots the operator's real login-shell ``PATH`` at install time
    instead. Any failure (missing shell, timeout, non-zero exit, empty
    output) falls back to ``fallback`` (or the current process ``PATH``) --
    it never raises, and the result always passes through
    :func:`sanitize_path_value`.
    """
    candidate: str | None = None
    try:
        result = run_command([shell, "-ilc", "echo $PATH"], timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        result = None

    if result is not None and result.returncode == 0:
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        if lines:
            candidate = lines[-1].strip()

    if not candidate:
        candidate = fallback if fallback is not None else os.environ.get("PATH", "")

    return sanitize_path_value(candidate)


def build_program_arguments(request: ServiceInstallRequest) -> list[str]:
    """The exact ``ProgramArguments`` array: ``factory start`` with explicit
    ``--repo``/``--github-repo``/``--config``(optional)/``--data-dir``/
    ``--runtime``, matching ``cli.start_command``'s real option set."""
    args: list[str] = [
        str(request.executable),
        "start",
        "--repo",
        str(request.repo),
        "--github-repo",
        request.github_repo,
    ]
    if request.config_path is not None:
        args += ["--config", str(request.config_path)]
    args += ["--data-dir", str(request.data_dir)]
    args += ["--runtime", request.runtime.value]
    return args


def build_launch_agent_plist(
    request: ServiceInstallRequest, path_value: str
) -> dict[str, object]:
    """The full plist payload as a plain dict, ready for ``plistlib.dumps``."""
    validate_label(request.label)
    throttle = max(MIN_THROTTLE_INTERVAL_SECONDS, int(request.poll_interval_seconds))
    return {
        "Label": request.label,
        "ProgramArguments": build_program_arguments(request),
        "EnvironmentVariables": {"PATH": path_value},
        "RunAtLoad": True,
        # See module docstring: "SuccessfulExit" is deliberately omitted so
        # exit code is never the restart trigger (a clean exit -- 0, the
        # config-error code 2, or anything else -- is never auto-restarted).
        # Only an OS-level crash (unhandled signal) restarts the job.
        "KeepAlive": {"Crashed": True},
        "ThrottleInterval": throttle,
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
        "ProcessType": "Background",
    }


def render_plist_bytes(request: ServiceInstallRequest, path_value: str) -> bytes:
    return plistlib.dumps(build_launch_agent_plist(request, path_value))


def validate_label(label: str) -> None:
    """Reject anything but a bounded, ASCII, reverse-DNS-style label.

    This is the single choke point every lifecycle function (``plist_path``,
    ``install_service``, ``uninstall_service``, ``get_service_status``) must
    validate a label through before using it inside a filesystem path or a
    ``launchctl`` domain target string. The pattern forbids ``/``, ``\\``,
    whitespace, NUL bytes and any ``..``/empty segment (path traversal),
    since each dot-separated segment must be one or more
    ``[A-Za-z0-9_-]`` characters.
    """
    if not label or len(label) > MAX_LABEL_LENGTH:
        raise ServiceInstallError(
            f"label must be 1-{MAX_LABEL_LENGTH} characters, got {len(label)}"
        )
    if not _LABEL_PATTERN.fullmatch(label):
        raise ServiceInstallError(
            f"label {label!r} must be a bounded, reverse-DNS-style identifier "
            "(ASCII letters, digits, '-', '_' in '.'-separated segments; no "
            "'/', '\\\\', empty segments, or path traversal)"
        )


def plist_path(label: str, launch_agents_dir: Path) -> Path:
    validate_label(label)
    return launch_agents_dir / f"{label}.plist"


def _require_absolute(label: str, path: Path) -> None:
    if not path.is_absolute():
        raise ServiceInstallError(
            f"{label} must be an absolute path, got {path!s}; a launchd job has no "
            "meaningful working directory, so every path must be fully resolved."
        )


def _require_exists(label: str, path: Path) -> None:
    if not path.exists():
        raise ServiceInstallError(f"{label} does not exist: {path}")


def _require_file(label: str, path: Path) -> None:
    if not path.is_file():
        raise ServiceInstallError(f"{label} is not a regular file: {path}")


def _require_directory(label: str, path: Path) -> None:
    if not path.is_dir():
        raise ServiceInstallError(f"{label} is not a directory: {path}")


def _require_executable_permission(path: Path) -> None:
    if not os.access(path, os.X_OK):
        raise ServiceInstallError(
            f"executable is not marked executable (missing the +x permission): {path}"
        )


def _require_git_worktree(repo: Path) -> None:
    """Confirm ``repo`` looks like a Git checkout by checking for a ``.git``
    entry only -- never by invoking ``git`` itself. A normal clone has
    ``.git`` as a directory; a linked worktree has ``.git`` as a file
    pointing at the real git dir. Either satisfies this check."""
    if not (repo / ".git").exists():
        raise ServiceInstallError(
            f"repo does not look like a Git checkout (no .git found): {repo}"
        )


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _describe_unstable_location(executable: Path, *, home_dir: Path) -> str | None:
    """Return a human description of why ``executable`` is not a stable
    location for a long-running service, or ``None`` if it looks stable.

    Checks both the literal path *and* its symlink-resolved target (when
    they differ), so a symlink cannot be used to make an unstable location
    look stable, nor a stable-looking symlink hide a target that actually
    lives in Downloads or a temp directory. Either form matching is enough
    to reject -- this fails closed on symlink ambiguity.
    """
    downloads = (home_dir / "Downloads").resolve()

    candidates = [executable]
    resolved = executable.resolve()
    if resolved != executable:
        candidates.append(resolved)

    for candidate in candidates:
        if _is_within(candidate, downloads):
            return "Downloads"
        for root in _TEMP_ROOTS:
            if _is_within(candidate, Path(root)):
                return root
        if "/var/folders/" in str(candidate):
            return "a temporary directory (/var/folders)"

    return None


def validate_install_paths(
    *,
    executable: Path,
    repo: Path,
    config_path: Path | None,
    data_dir: Path,
    allow_source_dev: bool,
    home_dir: Path,
) -> None:
    """Refuse to install when any path is relative/missing/the wrong kind of
    filesystem entry, or when the executable sits in a location a frozen
    release should never be run from (Downloads, a temp directory).

    ``executable`` must be an absolute, existing, executable regular file;
    ``repo`` must be an absolute, existing directory that looks like a Git
    checkout (``.git`` present, never verified by invoking ``git``);
    ``config_path``, when given, must be an absolute, existing regular file.
    These structural checks always run. ``allow_source_dev=True`` only
    bypasses the Downloads/temp stable-location check below, for a source
    checkout deliberately run from a repository working tree.
    """
    _require_absolute("executable", executable)
    _require_exists("executable", executable)
    _require_file("executable", executable)
    _require_executable_permission(executable)

    _require_absolute("repo", repo)
    _require_exists("repo", repo)
    _require_directory("repo", repo)
    _require_git_worktree(repo)

    if config_path is not None:
        _require_absolute("config", config_path)
        _require_exists("config", config_path)
        _require_file("config", config_path)

    _require_absolute("data_dir", data_dir)

    if allow_source_dev:
        return

    unstable = _describe_unstable_location(executable, home_dir=home_dir)
    if unstable is not None:
        raise ServiceInstallError(
            f"refusing to install a service for executable at {executable}: "
            f"{unstable} is not a stable location for a long-running launchd "
            "service; move the extracted release to a permanent directory (e.g. "
            "/Applications or ~/bin) and retry, or pass allow_source_dev=True for "
            "a source checkout run from a repository working tree."
        )


def _run_launchctl(
    run_command: CommandRunner,
    argv: list[str],
    *,
    timeout: float,
    action: str,
) -> subprocess.CompletedProcess[str]:
    """Invoke ``launchctl`` through the injected runner, converting the
    expected failure modes (the binary is missing/unreachable, or it hangs)
    into an explicit :class:`ServiceInstallError` instead of letting a raw
    ``OSError``/``TimeoutExpired`` escape. Callers still decide what a
    non-zero exit code means (bootout tolerates "not loaded"; bootstrap does
    not)."""
    try:
        return run_command(argv, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ServiceInstallError(
            f"launchctl {action} timed out after {timeout}s: {exc}"
        ) from exc
    except OSError as exc:
        raise ServiceInstallError(f"launchctl {action} could not be run: {exc}") from exc


def _write_all(fd: int, payload: bytes) -> None:
    """Write every byte of ``payload`` to ``fd``, looping in case
    ``os.write`` performs a short write (e.g. an interrupted syscall).
    ``os.write`` returning ``0`` for a non-empty remainder is treated as an
    unrecoverable error rather than looping forever."""
    view = memoryview(payload)
    total = 0
    while total < len(view):
        written = os.write(fd, view[total:])
        if written <= 0:
            raise OSError(
                f"os.write made no progress after writing {total}/{len(view)} bytes"
            )
        total += written


def _atomic_write_plist(payload: bytes, *, launch_agents_dir: Path, label: str) -> Path:
    """Write ``payload`` to a private temp file inside ``launch_agents_dir``
    and atomically publish it as ``<label>.plist``.

    Guarantees:

    - the temp file is created with a private mode already (``mkstemp``
      defaults to ``0600``) and is explicitly ``chmod``'d to ``0600`` again
      before ``os.replace`` makes it visible under its final name, so the
      plist is never observable at a looser mode;
    - ``payload`` is written in full via :func:`_write_all`, never left
      partially written;
    - if *any* step -- write, chmod, or replace -- fails, the temp file is
      removed before the error propagates, so a fault never leaves a stray
      ``.plist.tmp`` file behind in ``~/Library/LaunchAgents``.
    """
    target = plist_path(label, launch_agents_dir)
    fd, tmp_name = tempfile.mkstemp(
        dir=launch_agents_dir, prefix=f".{label}.", suffix=".plist.tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        try:
            _write_all(fd, payload)
        finally:
            os.close(fd)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, target)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise
    return target


def install_service(
    request: ServiceInstallRequest,
    *,
    launch_agents_dir: Path,
    run_command: CommandRunner = default_command_runner,
    uid: int | None = None,
    home_dir: Path | None = None,
    path_value: str | None = None,
) -> ServiceStatus:
    """Validate, render and atomically install the LaunchAgent plist, then
    (re)load it via ``launchctl bootstrap``.

    Idempotent: an existing job for the same label is booted out first (its
    absence is not an error), so re-running install never leaves two loaded
    copies. Nothing outside ``launch_agents_dir`` is ever written, and the
    plist is written with mode ``0600`` before it is made visible under its
    final name (atomic ``os.replace``, see :func:`_atomic_write_plist`).

    Never silently reports success: any ``launchctl`` failure -- the binary
    could not be run at all, it timed out, or ``bootstrap`` returned a
    non-zero exit code -- raises :class:`ServiceInstallError` instead of
    returning a status that looks like a successful install.
    """
    validate_label(request.label)
    home = home_dir if home_dir is not None else Path.home()
    resolved_uid = uid if uid is not None else os.getuid()

    validate_install_paths(
        executable=request.executable,
        repo=request.repo,
        config_path=request.config_path,
        data_dir=request.data_dir,
        allow_source_dev=request.allow_source_dev,
        home_dir=home,
    )

    resolved_path_value = (
        path_value if path_value is not None else capture_login_shell_path(run_command)
    )
    payload = render_plist_bytes(request, resolved_path_value)

    launch_agents_dir.mkdir(parents=True, exist_ok=True)
    target = _atomic_write_plist(
        payload, launch_agents_dir=launch_agents_dir, label=request.label
    )

    domain = f"gui/{resolved_uid}"
    # Idempotency: tear down any previously loaded copy first. A missing job
    # is expected on first install, so a non-zero *exit code* here is
    # deliberately ignored -- but a launchctl that could not be run at all
    # (OSError/timeout) is still a real, explicit failure (see
    # ``_run_launchctl``), not a silent success.
    _run_launchctl(
        run_command,
        ["launchctl", "bootout", domain, str(target)],
        timeout=DEFAULT_LAUNCHCTL_TIMEOUT_SECONDS,
        action="bootout",
    )

    result = _run_launchctl(
        run_command,
        ["launchctl", "bootstrap", domain, str(target)],
        timeout=DEFAULT_LAUNCHCTL_TIMEOUT_SECONDS,
        action="bootstrap",
    )
    if result.returncode != 0:
        raise ServiceInstallError(
            f"launchctl bootstrap failed (exit {result.returncode}): {result.stderr.strip()}"
        )

    return get_service_status(
        request.label,
        launch_agents_dir=launch_agents_dir,
        run_command=run_command,
        uid=resolved_uid,
    )


def uninstall_service(
    label: str,
    *,
    launch_agents_dir: Path,
    run_command: CommandRunner = default_command_runner,
    uid: int | None = None,
) -> bool:
    """Unload the agent (if loaded) and remove its plist (if present).

    Idempotent and non-destructive: only the one named plist is ever
    removed, never a directory scan/broad delete. Returns ``True`` if a
    plist was removed, ``False`` if none existed (already uninstalled).
    """
    validate_label(label)
    resolved_uid = uid if uid is not None else os.getuid()
    domain = f"gui/{resolved_uid}"
    target = plist_path(label, launch_agents_dir)

    _run_launchctl(
        run_command,
        ["launchctl", "bootout", f"{domain}/{label}"],
        timeout=DEFAULT_LAUNCHCTL_TIMEOUT_SECONDS,
        action="bootout",
    )

    if not target.exists():
        return False
    target.unlink()
    return True


def get_service_status(
    label: str,
    *,
    launch_agents_dir: Path,
    run_command: CommandRunner = default_command_runner,
    uid: int | None = None,
) -> ServiceStatus:
    """Read-only status: whether the plist exists, and whether launchd
    currently has it loaded (via ``launchctl print``).

    Unlike ``install_service``/``uninstall_service``, a ``launchctl``
    failure here degrades to ``loaded=False`` with an explanatory
    ``detail`` rather than raising: this is a read-only query, so reporting
    "could not determine" is more useful to a caller than an exception.
    """
    validate_label(label)
    resolved_uid = uid if uid is not None else os.getuid()
    target = plist_path(label, launch_agents_dir)
    installed = target.exists()
    domain_target = f"gui/{resolved_uid}/{label}"

    try:
        result = run_command(
            ["launchctl", "print", domain_target], timeout=DEFAULT_LAUNCHCTL_TIMEOUT_SECONDS
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ServiceStatus(
            label=label,
            plist_path=target,
            installed=installed,
            loaded=False,
            detail=f"launchctl print failed: {exc}",
        )

    loaded = result.returncode == 0
    detail = (result.stdout or "").strip() if loaded else (result.stderr or "").strip()
    return ServiceStatus(
        label=label, plist_path=target, installed=installed, loaded=loaded, detail=detail
    )
