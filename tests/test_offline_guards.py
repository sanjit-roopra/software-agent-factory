"""Tests for the ``tests/conftest.py`` offline-enforcement guard (PLAN.md
Phase 15 core safety foundation).

These import ``conftest`` directly (the same pattern already used for
``factory_testing`` elsewhere in this suite) to exercise the guard's pure
helpers and its installed patches without depending on real network access
or the real ``gh``/``copilot`` binaries being present on ``PATH``.
"""

from __future__ import annotations

import socket
import subprocess

import conftest
import pytest

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "family,address,expected",
    [
        (socket.AF_INET, ("127.0.0.1", 8080), True),
        (socket.AF_INET, ("127.5.6.7", 8080), True),
        (socket.AF_INET, ("localhost", 8080), True),
        (socket.AF_INET, ("93.184.216.34", 80), False),
        (socket.AF_INET, ("example.invalid", 80), False),
        (socket.AF_INET6, ("::1", 80, 0, 0), True),
        (socket.AF_INET6, ("2001:db8::1", 80, 0, 0), False),
        (socket.AF_UNIX, "/tmp/some.sock", True),
        (socket.AF_INET, (), False),
        (socket.AF_INET, None, False),
    ],
)
def test_is_loopback_address(family: int, address: object, expected: bool) -> None:
    assert conftest._is_loopback_address(family, address) is expected


@pytest.mark.parametrize(
    "command,expected",
    [
        (["gh", "pr", "create"], "gh"),
        (["copilot", "-C", "/repo", "--model", "x"], "copilot"),
        (["git", "-C", "/repo", "status"], "git"),
        (["/opt/homebrew/bin/gh", "issue", "list"], "gh"),
        ("gh", "gh"),
        ([], None),
        (None, None),
    ],
)
def test_executable_name(command: object, expected: str | None) -> None:
    assert conftest._executable_name(command) == expected


def test_is_opted_in_reads_marker_and_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    env_var = "FACTORY_TEST_GUARD_UNIT_TEST_OPT_IN"
    monkeypatch.delenv(env_var, raising=False)

    assert conftest._is_opted_in(None, env_var) is False
    assert conftest._is_opted_in(object(), env_var) is True

    monkeypatch.setenv(env_var, "1")
    assert conftest._is_opted_in(None, env_var) is True

    monkeypatch.setenv(env_var, "0")
    assert conftest._is_opted_in(None, env_var) is False


# ---------------------------------------------------------------------------
# Installed guard behavior (default: no opt-in)
# ---------------------------------------------------------------------------


def test_default_blocks_a_non_loopback_socket_connect() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match="non-loopback network connection"):
            sock.connect(("93.184.216.34", 80))
    finally:
        sock.close()


def test_default_allows_a_real_loopback_socket_connect() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    host, port = server.getsockname()

    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Must not raise: loopback traffic is never blocked.
        client.connect((host, port))
    finally:
        client.close()
        server.close()


def test_default_blocks_direct_execution_of_gh_and_copilot() -> None:
    for executable in ("gh", "copilot"):
        with pytest.raises(RuntimeError, match=f"real {executable!r} binary"):
            subprocess.Popen([executable, "--version"])


def test_default_allows_real_git_subprocess_execution() -> None:
    # Must not raise, and must actually run: git is never guarded.
    result = subprocess.run(["git", "--version"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "git version" in result.stdout


def test_default_allows_a_monkeypatched_popen_test_double(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A test that replaces ``subprocess.Popen`` wholesale (the existing
    ``test_copilot_runtime.py`` pattern) is never affected by this guard:
    the replacement callable is used directly and the guard's own patched
    ``__init__`` is never reached."""
    calls: list[list[str]] = []

    class _FakePopen:
        def __init__(self, args: list[str], **_kwargs: object) -> None:
            calls.append(list(args))

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    subprocess.Popen(["copilot", "-C", "/repo"])

    assert calls == [["copilot", "-C", "/repo"]]


# ---------------------------------------------------------------------------
# Opt-in via marker
# ---------------------------------------------------------------------------


@pytest.mark.allow_network
def test_allow_network_marker_leaves_socket_connect_unpatched() -> None:
    assert socket.socket.connect is conftest._real_socket_connect
    assert socket.socket.connect_ex is conftest._real_socket_connect_ex


@pytest.mark.allow_real_binaries
def test_allow_real_binaries_marker_leaves_popen_unpatched() -> None:
    assert subprocess.Popen.__init__ is conftest._real_popen_init


def test_markers_are_registered_so_strict_marker_runs_would_not_warn(
    pytestconfig: pytest.Config,
) -> None:
    marker_names = {line.split(":", 1)[0].strip() for line in pytestconfig.getini("markers")}
    assert "allow_network" in marker_names
    assert "allow_real_binaries" in marker_names
