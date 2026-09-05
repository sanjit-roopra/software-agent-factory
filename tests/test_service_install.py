"""Tests for software_agent_factory.service_install.

Every test uses a temporary ``launch_agents_dir`` (never the real
``~/Library/LaunchAgents``) and an injected fake ``launchctl`` runner (never
a real subprocess), per PLAN.md Phase 15.2 / ADR-018.

Coverage:

- default runtime is ``fake``; ``copilot`` requires an explicit opt-in
- exact plist content: label, ``ProgramArguments``, ``KeepAlive``,
  ``RunAtLoad``, ``ThrottleInterval``, ``/dev/null`` stdio
- the ``KeepAlive.Crashed``-only design: no exit code (0, the CLI's config
  error 2, or anything else) auto-restarts the job, only a signal crash does
- ``PATH`` capture/sanitization: required dirs, dedup, rejection of
  NUL/newline/malformed entries, fallback on a failed shell probe
- label validation: bounded reverse-DNS-safe ASCII, rejecting path
  traversal/slashes/backslashes, applied in ``plist_path`` and every
  lifecycle function
- path validation: executable must be a regular, executable file; repo must
  be a directory containing ``.git`` (checked without invoking ``git``);
  config must be a regular file; symlink ambiguity is checked on both sides
- atomic, mode-0600 plist installation, including a fault-injection test for
  guaranteed temp-file cleanup and a partial-``os.write`` fault-injection test
- idempotent install/uninstall/status
- rejection of relative/missing paths
- rejection of a frozen executable under Downloads/temp locations, with an
  explicit ``allow_source_dev`` bypass
- the exact ``launchctl`` argv construction (bootout/bootstrap/print), never
  invoked through a shell, and explicit ``ServiceInstallError`` conversion of
  ``launchctl`` ``OSError``/timeout failures (never a silent install success)
"""

from __future__ import annotations

import os
import plistlib
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import pytest

from software_agent_factory.service_install import (
    DEFAULT_LABEL,
    DEFAULT_PATH_DIRS,
    MIN_THROTTLE_INTERVAL_SECONDS,
    ServiceInstallError,
    ServiceInstallRequest,
    ServiceRuntime,
    build_launch_agent_plist,
    build_program_arguments,
    capture_login_shell_path,
    get_service_status,
    install_service,
    plist_path,
    sanitize_path_value,
    uninstall_service,
    validate_install_paths,
    validate_label,
)


@dataclass
class FakeRunner:
    """Records every call; never spawns a real process. ``responses`` maps
    the joined argv[0:2] (e.g. ``"launchctl bootstrap"``) to a canned
    ``CompletedProcess``. ``raises`` maps that same key to an exception
    instance to raise instead."""

    responses: dict[str, subprocess.CompletedProcess[str]] = field(default_factory=dict)
    raises: dict[str, BaseException] = field(default_factory=dict)
    default_returncode: int = 0
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def __call__(self, argv: Sequence[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(argv))
        key = " ".join(argv[:2])
        if key in self.raises:
            raise self.raises[key]
        if key in self.responses:
            return self.responses[key]
        return subprocess.CompletedProcess(
            list(argv), self.default_returncode, stdout="", stderr=""
        )


def _make_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir()
    return path


def make_request(
    tmp_path: Path,
    *,
    runtime: ServiceRuntime = ServiceRuntime.FAKE,
    poll_interval_seconds: int = 30,
    with_config: bool = False,
    label: str = DEFAULT_LABEL,
) -> ServiceInstallRequest:
    executable = _make_executable(tmp_path / "bin" / "factory")
    repo = _make_repo(tmp_path / "repo")

    config_path = None
    if with_config:
        config_path = tmp_path / "factory.yaml"
        config_path.write_text("factory: {}\n", encoding="utf-8")

    data_dir = tmp_path / "data"

    return ServiceInstallRequest(
        executable=executable,
        repo=repo,
        github_repo="owner/name",
        data_dir=data_dir,
        config_path=config_path,
        poll_interval_seconds=poll_interval_seconds,
        runtime=runtime,
        label=label,
        allow_source_dev=True,  # tmp_path lives under a system temp root
    )


# -- defaults: fake unless explicitly requested -----------------------------


def test_default_runtime_is_fake(tmp_path: Path) -> None:
    request = make_request(tmp_path)
    assert request.runtime is ServiceRuntime.FAKE
    args = build_program_arguments(request)
    assert args[-2:] == ["--runtime", "fake"]


def test_explicit_copilot_opt_in(tmp_path: Path) -> None:
    request = make_request(tmp_path, runtime=ServiceRuntime.COPILOT)
    args = build_program_arguments(request)
    assert args[-2:] == ["--runtime", "copilot"]


# -- ProgramArguments / plist exactness --------------------------------------


def test_build_program_arguments_without_config(tmp_path: Path) -> None:
    request = make_request(tmp_path, with_config=False)
    args = build_program_arguments(request)
    assert args == [
        str(request.executable),
        "start",
        "--repo",
        str(request.repo),
        "--github-repo",
        "owner/name",
        "--data-dir",
        str(request.data_dir),
        "--runtime",
        "fake",
    ]


def test_build_program_arguments_with_config(tmp_path: Path) -> None:
    request = make_request(tmp_path, with_config=True)
    args = build_program_arguments(request)
    assert "--config" in args
    idx = args.index("--config")
    assert args[idx + 1] == str(request.config_path)


def test_plist_exact_shape(tmp_path: Path) -> None:
    request = make_request(tmp_path)
    payload = build_launch_agent_plist(request, "/usr/bin:/bin")
    assert payload["Label"] == DEFAULT_LABEL
    assert payload["ProgramArguments"] == build_program_arguments(request)
    assert payload["EnvironmentVariables"] == {"PATH": "/usr/bin:/bin"}
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] == {"Crashed": True}
    assert payload["StandardOutPath"] == "/dev/null"
    assert payload["StandardErrorPath"] == "/dev/null"
    assert payload["ThrottleInterval"] >= MIN_THROTTLE_INTERVAL_SECONDS


def test_keep_alive_never_restarts_on_any_exit_code_only_on_crash(tmp_path: Path) -> None:
    """launchd cannot key off a specific exit code, so ``KeepAlive``
    intentionally omits ``SuccessfulExit`` entirely: only an OS-level crash
    (signal) restarts the job. This means the CLI's config-error exit code
    (2), and indeed any other clean nonzero exit, is genuinely never
    auto-restarted -- not merely throttled."""
    request = make_request(tmp_path)
    payload = build_launch_agent_plist(request, "/usr/bin")
    assert payload["KeepAlive"] == {"Crashed": True}
    assert "SuccessfulExit" not in payload["KeepAlive"]


def test_plist_throttle_interval_floors_low_poll_interval(tmp_path: Path) -> None:
    request = make_request(tmp_path, poll_interval_seconds=5)
    payload = build_launch_agent_plist(request, "/usr/bin")
    assert payload["ThrottleInterval"] == MIN_THROTTLE_INTERVAL_SECONDS


def test_plist_throttle_interval_honors_larger_poll_interval(tmp_path: Path) -> None:
    large = MIN_THROTTLE_INTERVAL_SECONDS + 300
    request = make_request(tmp_path, poll_interval_seconds=large)
    payload = build_launch_agent_plist(request, "/usr/bin")
    assert payload["ThrottleInterval"] == large


# -- label validation ---------------------------------------------------------


def test_validate_label_accepts_default_label() -> None:
    validate_label(DEFAULT_LABEL)  # must not raise


@pytest.mark.parametrize(
    "label",
    [
        "",
        "a" * 201,
        "com.github/evil",
        "com.github\\evil",
        "../../etc/passwd",
        "com..github",
        ".com.github",
        "com.github.",
        "com github",
        "com.git\x00hub",
        "com.gi\nthub",
        "nodotatall",
        "com.g\u00e4thub",  # non-ASCII letter must be rejected
    ],
)
def test_validate_label_rejects_unsafe_values(label: str) -> None:
    with pytest.raises(ServiceInstallError):
        validate_label(label)


def test_plist_path_rejects_traversal_label(tmp_path: Path) -> None:
    with pytest.raises(ServiceInstallError):
        plist_path("../../etc/passwd", tmp_path)


def test_plist_path_never_escapes_launch_agents_dir_for_valid_label(tmp_path: Path) -> None:
    result = plist_path(DEFAULT_LABEL, tmp_path)
    assert result.parent == tmp_path


def test_install_rejects_traversal_label(tmp_path: Path) -> None:
    request = make_request(tmp_path, label="../../etc/passwd")
    with pytest.raises(ServiceInstallError):
        install_service(
            request,
            launch_agents_dir=tmp_path / "LaunchAgents",
            run_command=FakeRunner(),
            uid=501,
            path_value="/usr/bin",
        )


def test_uninstall_rejects_traversal_label(tmp_path: Path) -> None:
    with pytest.raises(ServiceInstallError):
        uninstall_service(
            "../../etc/passwd",
            launch_agents_dir=tmp_path / "LaunchAgents",
            run_command=FakeRunner(),
            uid=501,
        )


def test_get_service_status_rejects_traversal_label(tmp_path: Path) -> None:
    with pytest.raises(ServiceInstallError):
        get_service_status(
            "../../etc/passwd",
            launch_agents_dir=tmp_path / "LaunchAgents",
            run_command=FakeRunner(),
            uid=501,
        )


# -- PATH capture / sanitization --------------------------------------------


def test_sanitize_path_value_includes_required_dirs() -> None:
    result = sanitize_path_value("/usr/bin:/bin")
    for required in DEFAULT_PATH_DIRS:
        assert required in result.split(":")


def test_sanitize_path_value_dedupes_preserving_order() -> None:
    result = sanitize_path_value("/a:/b:/a:/b")
    assert result.split(":")[:2] == ["/a", "/b"]
    assert result.count("/a") == 1


def test_sanitize_path_value_rejects_nul_byte() -> None:
    with pytest.raises(ServiceInstallError):
        sanitize_path_value("/usr/bin:\x00/bin")


def test_sanitize_path_value_rejects_newline() -> None:
    with pytest.raises(ServiceInstallError):
        sanitize_path_value("/usr/bin\n:/bin")


def test_sanitize_path_value_rejects_relative_entry() -> None:
    with pytest.raises(ServiceInstallError):
        sanitize_path_value("relative/bin:/usr/bin")


def test_capture_login_shell_path_success() -> None:
    def fake_call(argv: Sequence[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            list(argv), 0, stdout="/opt/homebrew/bin:/usr/bin\n", stderr=""
        )

    result = capture_login_shell_path(fake_call)
    assert "/opt/homebrew/bin" in result.split(":")
    assert "/usr/bin" in result.split(":")
    for required in DEFAULT_PATH_DIRS:
        assert required in result.split(":")


def test_capture_login_shell_path_falls_back_on_timeout() -> None:
    def fake_call(argv: Sequence[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=list(argv), timeout=timeout)

    result = capture_login_shell_path(fake_call, fallback="/custom/bin")
    assert "/custom/bin" in result.split(":")


def test_capture_login_shell_path_falls_back_on_oserror() -> None:
    def fake_call(argv: Sequence[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        raise OSError("no such shell")

    result = capture_login_shell_path(fake_call, fallback="/custom/bin")
    assert "/custom/bin" in result.split(":")


def test_capture_login_shell_path_falls_back_on_nonzero_exit() -> None:
    def fake_call(argv: Sequence[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(argv), 1, stdout="", stderr="no such file")

    result = capture_login_shell_path(fake_call, fallback="/custom/bin")
    assert "/custom/bin" in result.split(":")


# -- path validation: existence / kind / permissions -------------------------


def test_validate_install_paths_rejects_relative_executable(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    with pytest.raises(ServiceInstallError, match="absolute"):
        validate_install_paths(
            executable=Path("relative/factory"),
            repo=repo,
            config_path=None,
            data_dir=tmp_path / "data",
            allow_source_dev=True,
            home_dir=tmp_path,
        )


def test_validate_install_paths_rejects_missing_executable(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")
    with pytest.raises(ServiceInstallError, match="does not exist"):
        validate_install_paths(
            executable=tmp_path / "no-such-binary",
            repo=repo,
            config_path=None,
            data_dir=tmp_path / "data",
            allow_source_dev=True,
            home_dir=tmp_path,
        )


def test_validate_install_paths_rejects_executable_that_is_a_directory(
    tmp_path: Path,
) -> None:
    executable_dir = tmp_path / "factory"
    executable_dir.mkdir()
    repo = _make_repo(tmp_path / "repo")
    with pytest.raises(ServiceInstallError, match="not a regular file"):
        validate_install_paths(
            executable=executable_dir,
            repo=repo,
            config_path=None,
            data_dir=tmp_path / "data",
            allow_source_dev=True,
            home_dir=tmp_path,
        )


def test_validate_install_paths_rejects_non_executable_file(tmp_path: Path) -> None:
    executable = tmp_path / "factory"
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o644)  # no +x
    repo = _make_repo(tmp_path / "repo")
    with pytest.raises(ServiceInstallError, match=r"\+x"):
        validate_install_paths(
            executable=executable,
            repo=repo,
            config_path=None,
            data_dir=tmp_path / "data",
            allow_source_dev=True,
            home_dir=tmp_path,
        )


def test_validate_install_paths_rejects_missing_repo(tmp_path: Path) -> None:
    executable = _make_executable(tmp_path / "factory")
    with pytest.raises(ServiceInstallError, match="does not exist"):
        validate_install_paths(
            executable=executable,
            repo=tmp_path / "no-such-repo",
            config_path=None,
            data_dir=tmp_path / "data",
            allow_source_dev=True,
            home_dir=tmp_path,
        )


def test_validate_install_paths_rejects_repo_that_is_a_file(tmp_path: Path) -> None:
    executable = _make_executable(tmp_path / "factory")
    repo_file = tmp_path / "repo"
    repo_file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ServiceInstallError, match="not a directory"):
        validate_install_paths(
            executable=executable,
            repo=repo_file,
            config_path=None,
            data_dir=tmp_path / "data",
            allow_source_dev=True,
            home_dir=tmp_path,
        )


def test_validate_install_paths_rejects_repo_without_git_marker(tmp_path: Path) -> None:
    executable = _make_executable(tmp_path / "factory")
    repo = tmp_path / "repo"
    repo.mkdir()  # no .git
    with pytest.raises(ServiceInstallError, match=r"\.git"):
        validate_install_paths(
            executable=executable,
            repo=repo,
            config_path=None,
            data_dir=tmp_path / "data",
            allow_source_dev=True,
            home_dir=tmp_path,
        )


def test_validate_install_paths_accepts_repo_with_git_as_worktree_file(
    tmp_path: Path,
) -> None:
    """A linked Git worktree has ``.git`` as a *file* (pointing at the real
    git dir), not a directory; either form must satisfy the check, and it is
    checked without invoking ``git`` itself."""
    executable = _make_executable(tmp_path / "factory")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: /elsewhere/.git/worktrees/repo\n", encoding="utf-8")

    validate_install_paths(
        executable=executable,
        repo=repo,
        config_path=None,
        data_dir=tmp_path / "data",
        allow_source_dev=True,
        home_dir=tmp_path,
    )  # must not raise


def test_validate_install_paths_rejects_relative_data_dir(tmp_path: Path) -> None:
    executable = _make_executable(tmp_path / "factory")
    repo = _make_repo(tmp_path / "repo")
    with pytest.raises(ServiceInstallError, match="absolute"):
        validate_install_paths(
            executable=executable,
            repo=repo,
            config_path=None,
            data_dir=Path("relative/data"),
            allow_source_dev=True,
            home_dir=tmp_path,
        )


def test_validate_install_paths_rejects_missing_config(tmp_path: Path) -> None:
    executable = _make_executable(tmp_path / "factory")
    repo = _make_repo(tmp_path / "repo")
    with pytest.raises(ServiceInstallError, match="does not exist"):
        validate_install_paths(
            executable=executable,
            repo=repo,
            config_path=tmp_path / "no-such-config.yaml",
            data_dir=tmp_path / "data",
            allow_source_dev=True,
            home_dir=tmp_path,
        )


def test_validate_install_paths_rejects_config_that_is_a_directory(tmp_path: Path) -> None:
    executable = _make_executable(tmp_path / "factory")
    repo = _make_repo(tmp_path / "repo")
    config_dir = tmp_path / "config-is-a-dir"
    config_dir.mkdir()
    with pytest.raises(ServiceInstallError, match="not a regular file"):
        validate_install_paths(
            executable=executable,
            repo=repo,
            config_path=config_dir,
            data_dir=tmp_path / "data",
            allow_source_dev=True,
            home_dir=tmp_path,
        )


def test_validate_install_paths_accepts_valid_source_dev(tmp_path: Path) -> None:
    executable = _make_executable(tmp_path / "factory")
    repo = _make_repo(tmp_path / "repo")
    validate_install_paths(
        executable=executable,
        repo=repo,
        config_path=None,
        data_dir=tmp_path / "data",
        allow_source_dev=True,
        home_dir=tmp_path,
    )  # must not raise


# -- stable-location checks: Downloads / temp / symlink ambiguity -----------


def test_validate_install_paths_rejects_downloads_location(tmp_path: Path) -> None:
    home = tmp_path / "home"
    downloads = home / "Downloads" / "software-agent-factory-1.0.0-macos-arm64"
    executable = _make_executable(downloads / "factory")
    repo = _make_repo(tmp_path / "repo")

    with pytest.raises(ServiceInstallError, match="Downloads"):
        validate_install_paths(
            executable=executable,
            repo=repo,
            config_path=None,
            data_dir=tmp_path / "data",
            allow_source_dev=False,
            home_dir=home,
        )


def test_validate_install_paths_downloads_allowed_with_source_dev(tmp_path: Path) -> None:
    home = tmp_path / "home"
    executable = _make_executable(home / "Downloads" / "factory")
    repo = _make_repo(tmp_path / "repo")

    validate_install_paths(
        executable=executable,
        repo=repo,
        config_path=None,
        data_dir=tmp_path / "data",
        allow_source_dev=True,
        home_dir=home,
    )  # must not raise: explicit opt-in bypasses the stability check


def test_validate_install_paths_rejects_tmp_location(tmp_path: Path) -> None:
    # Simulate a frozen executable literally under /tmp by using one of the
    # checked temp roots directly rather than pytest's own tmp_path (which
    # would otherwise always trigger this branch on macOS).
    real_tmp_candidate = Path("/tmp/safactory-service-install-test")
    executable = _make_executable(real_tmp_candidate / "factory")
    repo = _make_repo(tmp_path / "repo")
    try:
        with pytest.raises(ServiceInstallError, match="tmp"):
            validate_install_paths(
                executable=executable,
                repo=repo,
                config_path=None,
                data_dir=tmp_path / "data",
                allow_source_dev=False,
                home_dir=tmp_path / "home",
            )
    finally:
        executable.unlink(missing_ok=True)
        real_tmp_candidate.rmdir()


def test_validate_install_paths_accepts_stable_location(tmp_path: Path) -> None:
    # pytest's own tmp_path lives under the OS temp root (e.g. macOS
    # /var/folders), so a genuinely "stable" location for this test has to
    # sit outside of it -- use a throwaway directory under this repository
    # checkout instead, which is neither Downloads nor a temp root.
    home = tmp_path / "home"
    home.mkdir(parents=True)
    repo_root = Path(__file__).resolve().parents[1]
    stable_dir = repo_root / ".pytest-stable-location-check"
    stable_dir.mkdir(exist_ok=True)
    executable = _make_executable(stable_dir / "factory")
    repo = _make_repo(tmp_path / "repo")

    try:
        validate_install_paths(
            executable=executable,
            repo=repo,
            config_path=None,
            data_dir=tmp_path / "data",
            allow_source_dev=False,
            home_dir=home,
        )  # must not raise: not under Downloads or a temp root
    finally:
        executable.unlink(missing_ok=True)
        stable_dir.rmdir()


def test_validate_install_paths_rejects_symlink_resolving_into_downloads(
    tmp_path: Path,
) -> None:
    """A symlink that itself sits in a stable location but resolves into
    Downloads must still be rejected: the resolved target is checked too."""
    home = tmp_path / "home"
    real_target = _make_executable(home / "Downloads" / "real-factory")
    repo_root = Path(__file__).resolve().parents[1]
    stable_dir = repo_root / ".pytest-symlink-check"
    stable_dir.mkdir(exist_ok=True)
    symlink = stable_dir / "factory"
    try:
        symlink.symlink_to(real_target)
        repo = _make_repo(tmp_path / "repo")
        with pytest.raises(ServiceInstallError, match="Downloads"):
            validate_install_paths(
                executable=symlink,
                repo=repo,
                config_path=None,
                data_dir=tmp_path / "data",
                allow_source_dev=False,
                home_dir=home,
            )
    finally:
        symlink.unlink(missing_ok=True)
        stable_dir.rmdir()


def test_validate_install_paths_rejects_symlink_literally_in_downloads(
    tmp_path: Path,
) -> None:
    """A symlink that sits literally in Downloads must be rejected even if
    it resolves to a stable target elsewhere -- the literal path is checked
    too (fail closed on symlink ambiguity)."""
    home = tmp_path / "home"
    repo_root = Path(__file__).resolve().parents[1]
    stable_dir = repo_root / ".pytest-symlink-check-2"
    stable_dir.mkdir(exist_ok=True)
    real_target = _make_executable(stable_dir / "real-factory")
    downloads = home / "Downloads"
    downloads.mkdir(parents=True)
    symlink = downloads / "factory"
    try:
        symlink.symlink_to(real_target)
        repo = _make_repo(tmp_path / "repo")
        with pytest.raises(ServiceInstallError, match="Downloads"):
            validate_install_paths(
                executable=symlink,
                repo=repo,
                config_path=None,
                data_dir=tmp_path / "data",
                allow_source_dev=False,
                home_dir=home,
            )
    finally:
        real_target.unlink(missing_ok=True)
        stable_dir.rmdir()


# -- install / uninstall / status -------------------------------------------


def test_install_writes_exactly_one_plist_with_mode_0600(tmp_path: Path) -> None:
    request = make_request(tmp_path)
    launch_agents_dir = tmp_path / "LaunchAgents"
    runner = FakeRunner()

    status = install_service(
        request,
        launch_agents_dir=launch_agents_dir,
        run_command=runner,
        uid=501,
        path_value="/usr/bin:/bin",
    )

    plist_files = list(launch_agents_dir.glob("*.plist"))
    assert len(plist_files) == 1
    assert plist_files[0] == plist_path(request.label, launch_agents_dir)

    mode = stat.S_IMODE(plist_files[0].stat().st_mode)
    assert mode == 0o600

    # nothing else was written into launch_agents_dir (no leftover temp files)
    assert list(launch_agents_dir.iterdir()) == plist_files

    assert status.installed is True
    assert status.label == request.label


def test_install_plist_content_matches_render(tmp_path: Path) -> None:
    request = make_request(tmp_path)
    launch_agents_dir = tmp_path / "LaunchAgents"
    runner = FakeRunner()

    install_service(
        request,
        launch_agents_dir=launch_agents_dir,
        run_command=runner,
        uid=501,
        path_value="/usr/bin:/bin",
    )

    target = plist_path(request.label, launch_agents_dir)
    with target.open("rb") as handle:
        payload = plistlib.load(handle)
    assert payload["ProgramArguments"] == build_program_arguments(request)
    assert payload["EnvironmentVariables"]["PATH"] == "/usr/bin:/bin"


def test_install_calls_bootout_then_bootstrap(tmp_path: Path) -> None:
    request = make_request(tmp_path)
    launch_agents_dir = tmp_path / "LaunchAgents"
    runner = FakeRunner()

    install_service(
        request,
        launch_agents_dir=launch_agents_dir,
        run_command=runner,
        uid=501,
        path_value="/usr/bin",
    )

    assert runner.calls[0][:3] == ("launchctl", "bootout", "gui/501")
    assert runner.calls[1][:3] == ("launchctl", "bootstrap", "gui/501")
    target = plist_path(request.label, launch_agents_dir)
    assert runner.calls[1][3] == str(target)
    # status probe after install uses "print", never a shell/string command
    assert runner.calls[2][0] == "launchctl"
    assert runner.calls[2][1] == "print"


def test_install_raises_on_bootstrap_nonzero_exit(tmp_path: Path) -> None:
    request = make_request(tmp_path)
    launch_agents_dir = tmp_path / "LaunchAgents"
    runner = FakeRunner(
        responses={
            "launchctl bootstrap": subprocess.CompletedProcess(
                ["launchctl", "bootstrap"], 1, stdout="", stderr="service failed to load"
            )
        }
    )

    with pytest.raises(ServiceInstallError, match="bootstrap failed"):
        install_service(
            request,
            launch_agents_dir=launch_agents_dir,
            run_command=runner,
            uid=501,
            path_value="/usr/bin",
        )


def test_install_raises_service_install_error_on_bootstrap_oserror(tmp_path: Path) -> None:
    """A launchctl that cannot even be executed must be a clear
    ServiceInstallError, never a silently-reported success or a raw
    OSError leaking out of this module."""
    request = make_request(tmp_path)
    launch_agents_dir = tmp_path / "LaunchAgents"
    runner = FakeRunner(raises={"launchctl bootstrap": OSError("launchctl vanished")})

    with pytest.raises(ServiceInstallError, match="bootstrap"):
        install_service(
            request,
            launch_agents_dir=launch_agents_dir,
            run_command=runner,
            uid=501,
            path_value="/usr/bin",
        )


def test_install_raises_service_install_error_on_bootstrap_timeout(tmp_path: Path) -> None:
    request = make_request(tmp_path)
    launch_agents_dir = tmp_path / "LaunchAgents"
    runner = FakeRunner(
        raises={
            "launchctl bootstrap": subprocess.TimeoutExpired(
                cmd=["launchctl", "bootstrap"], timeout=10.0
            )
        }
    )

    with pytest.raises(ServiceInstallError, match="bootstrap"):
        install_service(
            request,
            launch_agents_dir=launch_agents_dir,
            run_command=runner,
            uid=501,
            path_value="/usr/bin",
        )


def test_install_raises_service_install_error_on_bootout_oserror(tmp_path: Path) -> None:
    request = make_request(tmp_path)
    launch_agents_dir = tmp_path / "LaunchAgents"
    runner = FakeRunner(raises={"launchctl bootout": OSError("launchctl vanished")})

    with pytest.raises(ServiceInstallError, match="bootout"):
        install_service(
            request,
            launch_agents_dir=launch_agents_dir,
            run_command=runner,
            uid=501,
            path_value="/usr/bin",
        )


def test_install_raises_service_install_error_on_bootout_timeout(tmp_path: Path) -> None:
    request = make_request(tmp_path)
    launch_agents_dir = tmp_path / "LaunchAgents"
    runner = FakeRunner(
        raises={
            "launchctl bootout": subprocess.TimeoutExpired(
                cmd=["launchctl", "bootout"], timeout=10.0
            )
        }
    )

    with pytest.raises(ServiceInstallError, match="bootout"):
        install_service(
            request,
            launch_agents_dir=launch_agents_dir,
            run_command=runner,
            uid=501,
            path_value="/usr/bin",
        )


def test_install_is_idempotent(tmp_path: Path) -> None:
    request = make_request(tmp_path)
    launch_agents_dir = tmp_path / "LaunchAgents"
    runner = FakeRunner()

    install_service(
        request,
        launch_agents_dir=launch_agents_dir,
        run_command=runner,
        uid=501,
        path_value="/usr/bin",
    )
    install_service(
        request,
        launch_agents_dir=launch_agents_dir,
        run_command=runner,
        uid=501,
        path_value="/usr/bin",
    )

    plist_files = list(launch_agents_dir.glob("*.plist"))
    assert len(plist_files) == 1  # never a duplicate copy


def test_install_rejects_relative_executable(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "repo")

    request = ServiceInstallRequest(
        executable=Path("bin/factory"),  # relative
        repo=repo,
        github_repo="owner/name",
        data_dir=tmp_path / "data",
        allow_source_dev=True,
    )
    with pytest.raises(ServiceInstallError):
        install_service(
            request,
            launch_agents_dir=tmp_path / "LaunchAgents",
            run_command=FakeRunner(),
            uid=501,
            path_value="/usr/bin",
        )


def test_install_rejects_downloads_without_source_dev(tmp_path: Path) -> None:
    home = tmp_path / "home"
    executable = _make_executable(home / "Downloads" / "factory")
    repo = _make_repo(tmp_path / "repo")

    request = ServiceInstallRequest(
        executable=executable,
        repo=repo,
        github_repo="owner/name",
        data_dir=tmp_path / "data",
        allow_source_dev=False,
    )
    with pytest.raises(ServiceInstallError, match="Downloads"):
        install_service(
            request,
            launch_agents_dir=tmp_path / "LaunchAgents",
            run_command=FakeRunner(),
            uid=501,
            home_dir=home,
            path_value="/usr/bin",
        )


def test_uninstall_removes_plist_and_boots_out(tmp_path: Path) -> None:
    request = make_request(tmp_path)
    launch_agents_dir = tmp_path / "LaunchAgents"
    runner = FakeRunner()
    install_service(
        request,
        launch_agents_dir=launch_agents_dir,
        run_command=runner,
        uid=501,
        path_value="/usr/bin",
    )

    removed = uninstall_service(
        request.label, launch_agents_dir=launch_agents_dir, run_command=runner, uid=501
    )
    assert removed is True
    assert not plist_path(request.label, launch_agents_dir).exists()


def test_uninstall_is_idempotent_when_nothing_installed(tmp_path: Path) -> None:
    launch_agents_dir = tmp_path / "LaunchAgents"
    launch_agents_dir.mkdir()
    runner = FakeRunner()

    removed = uninstall_service(
        DEFAULT_LABEL, launch_agents_dir=launch_agents_dir, run_command=runner, uid=501
    )
    assert removed is False


def test_uninstall_raises_service_install_error_on_bootout_oserror(tmp_path: Path) -> None:
    launch_agents_dir = tmp_path / "LaunchAgents"
    launch_agents_dir.mkdir()
    runner = FakeRunner(raises={"launchctl bootout": OSError("launchctl vanished")})

    with pytest.raises(ServiceInstallError, match="bootout"):
        uninstall_service(
            DEFAULT_LABEL, launch_agents_dir=launch_agents_dir, run_command=runner, uid=501
        )


def test_uninstall_never_touches_other_files(tmp_path: Path) -> None:
    launch_agents_dir = tmp_path / "LaunchAgents"
    launch_agents_dir.mkdir()
    other = launch_agents_dir / "com.example.other.plist"
    other.write_text("keep me", encoding="utf-8")

    request = make_request(tmp_path)
    runner = FakeRunner()
    install_service(
        request,
        launch_agents_dir=launch_agents_dir,
        run_command=runner,
        uid=501,
        path_value="/usr/bin",
    )
    uninstall_service(
        request.label, launch_agents_dir=launch_agents_dir, run_command=runner, uid=501
    )

    assert other.exists()
    assert other.read_text() == "keep me"


def test_get_service_status_not_installed(tmp_path: Path) -> None:
    launch_agents_dir = tmp_path / "LaunchAgents"
    launch_agents_dir.mkdir()
    runner = FakeRunner(
        responses={
            "launchctl print": subprocess.CompletedProcess(
                ["launchctl", "print"], 3, stdout="", stderr="Could not find service"
            )
        }
    )

    status = get_service_status(
        DEFAULT_LABEL, launch_agents_dir=launch_agents_dir, run_command=runner, uid=501
    )
    assert status.installed is False
    assert status.loaded is False


def test_get_service_status_installed_and_loaded(tmp_path: Path) -> None:
    request = make_request(tmp_path)
    launch_agents_dir = tmp_path / "LaunchAgents"
    runner = FakeRunner(
        responses={
            "launchctl print": subprocess.CompletedProcess(
                ["launchctl", "print"], 0, stdout="state = running", stderr=""
            )
        }
    )
    install_service(
        request,
        launch_agents_dir=launch_agents_dir,
        run_command=runner,
        uid=501,
        path_value="/usr/bin",
    )

    status = get_service_status(
        request.label, launch_agents_dir=launch_agents_dir, run_command=runner, uid=501
    )
    assert status.installed is True
    assert status.loaded is True
    assert "running" in status.detail

    payload = status.to_dict()
    assert payload["label"] == request.label
    assert payload["loaded"] is True


def test_get_service_status_handles_runner_failure(tmp_path: Path) -> None:
    launch_agents_dir = tmp_path / "LaunchAgents"

    def failing_runner(argv: Sequence[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=list(argv), timeout=timeout)

    status = get_service_status(
        DEFAULT_LABEL,
        launch_agents_dir=launch_agents_dir,
        run_command=failing_runner,
        uid=501,
    )
    assert status.loaded is False
    assert "failed" in status.detail


# -- atomic plist write: fault injection ------------------------------------


def test_install_atomic_write_cleans_up_temp_file_on_chmod_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = make_request(tmp_path)
    launch_agents_dir = tmp_path / "LaunchAgents"
    runner = FakeRunner()

    real_chmod = os.chmod

    def failing_chmod(path: object, mode: int, *args: object, **kwargs: object) -> None:
        if str(path).endswith(".plist.tmp"):
            raise OSError("simulated chmod failure")
        real_chmod(path, mode, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("software_agent_factory.service_install.os.chmod", failing_chmod)

    with pytest.raises(OSError, match="simulated chmod failure"):
        install_service(
            request,
            launch_agents_dir=launch_agents_dir,
            run_command=runner,
            uid=501,
            path_value="/usr/bin",
        )

    # no leaked temp file, and no partially-installed final plist either
    assert launch_agents_dir.exists()
    assert list(launch_agents_dir.glob("*.plist.tmp")) == []
    assert not plist_path(request.label, launch_agents_dir).exists()
    # launchctl must never have been invoked: the fault happened before load
    assert runner.calls == []


def test_install_atomic_write_cleans_up_temp_file_on_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = make_request(tmp_path)
    launch_agents_dir = tmp_path / "LaunchAgents"
    runner = FakeRunner()

    def failing_replace(src: object, dst: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("software_agent_factory.service_install.os.replace", failing_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        install_service(
            request,
            launch_agents_dir=launch_agents_dir,
            run_command=runner,
            uid=501,
            path_value="/usr/bin",
        )

    assert list(launch_agents_dir.glob("*.plist.tmp")) == []
    assert not plist_path(request.label, launch_agents_dir).exists()
    assert runner.calls == []


def test_install_atomic_write_handles_partial_os_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if the underlying ``os.write`` only accepts a few bytes at a
    time, the full plist payload must still end up on disk (looped write,
    never a truncated file)."""
    request = make_request(tmp_path)
    launch_agents_dir = tmp_path / "LaunchAgents"
    runner = FakeRunner()

    real_write = os.write

    def partial_write(fd: int, data: bytes) -> int:
        return real_write(fd, data[:4])  # simulate a short write every time

    monkeypatch.setattr("software_agent_factory.service_install.os.write", partial_write)

    install_service(
        request,
        launch_agents_dir=launch_agents_dir,
        run_command=runner,
        uid=501,
        path_value="/usr/bin:/bin",
    )

    target = plist_path(request.label, launch_agents_dir)
    with target.open("rb") as handle:
        payload = plistlib.load(handle)
    assert payload["ProgramArguments"] == build_program_arguments(request)
    assert payload["EnvironmentVariables"]["PATH"] == "/usr/bin:/bin"


# -- resolve_factory_executable ------------------------------------------------


def _doctor_env(**overrides: object):
    """A ``DoctorEnvironment`` with every host boundary injected, so
    executable resolution never depends on this machine's ``PATH``,
    ``sys.frozen`` or ``sys.executable``."""
    from software_agent_factory.doctor import DoctorEnvironment

    defaults: dict[str, object] = {
        "which": lambda _name: None,
        "is_frozen": False,
        "executable_path": Path("/usr/bin/python3"),
    }
    defaults.update(overrides)
    return DoctorEnvironment(**defaults)  # type: ignore[arg-type]


def test_resolve_factory_executable_prefers_an_explicit_path(tmp_path: Path) -> None:
    from software_agent_factory.service_install import resolve_factory_executable

    explicit = tmp_path / "factory"
    explicit.write_text("", encoding="utf-8")

    resolved = resolve_factory_executable(
        explicit, environment=_doctor_env(which=lambda _n: "/usr/local/bin/factory")
    )

    assert resolved == explicit


def test_resolve_factory_executable_uses_the_frozen_binary(tmp_path: Path) -> None:
    """A frozen build *is* the factory, so launchd should run it directly."""
    from software_agent_factory.service_install import resolve_factory_executable

    frozen = tmp_path / "software-agent-factory" / "factory"
    frozen.parent.mkdir()
    frozen.write_text("", encoding="utf-8")

    resolved = resolve_factory_executable(
        None, environment=_doctor_env(is_frozen=True, executable_path=frozen)
    )

    assert resolved == frozen.resolve()


def test_resolve_factory_executable_falls_back_to_the_console_script(
    tmp_path: Path,
) -> None:
    from software_agent_factory.service_install import resolve_factory_executable

    installed = tmp_path / "bin" / "factory"
    installed.parent.mkdir()
    installed.write_text("", encoding="utf-8")

    def fake_which(name: str) -> str | None:
        return str(installed) if name == "factory" else None

    resolved = resolve_factory_executable(None, environment=_doctor_env(which=fake_which))

    assert resolved == installed.resolve()


def test_resolve_factory_executable_raises_when_nothing_resolves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Better an explicit refusal than a plist launchd will fail to run."""
    import sys

    from software_agent_factory.service_install import resolve_factory_executable

    monkeypatch.setattr(sys, "executable", str(tmp_path / "python3"))

    with pytest.raises(ServiceInstallError, match="could not resolve"):
        resolve_factory_executable(None, environment=_doctor_env())
