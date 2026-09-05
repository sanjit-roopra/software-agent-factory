"""Shared pytest fixtures.

Git-related fixtures create throwaway repositories under ``tmp_path`` with
local (not global) identity configuration, ``commit.gpgsign`` disabled, and
global/system Git config suppressed, so tests never depend on the developer
machine's Git configuration, commit signing setup, or hooks.

``_factory_offline_guard`` (autouse) additionally enforces PLAN.md Phase 15's
"offline by default" rule at the test-process boundary itself, independent of
what any individual test remembers to mock:

- non-loopback network sockets are refused (loopback -- used by, e.g., the
  Phase 15.11 local dashboard's stdlib test client -- and Unix domain sockets
  are always allowed; ``git``'s own local/loopback operations never touch a
  network socket at all)
- direct execution of the real ``gh``/``copilot`` binaries is refused; test
  doubles (``ScriptedRunner``, a monkeypatched ``subprocess.Popen``) replace
  the callable entirely and are never affected, and plain ``git`` is always
  allowed

Both guards can be lifted for one test with ``@pytest.mark.allow_network`` /
``@pytest.mark.allow_real_binaries``, or process-wide with
``FACTORY_TEST_ALLOW_NETWORK=1`` / ``FACTORY_TEST_ALLOW_REAL_BINARIES=1`` (for
example, in a deliberately opted-in local/manual smoke test).

``_restore_factory_logging`` (autouse) keeps the package logger a per-test
concern, so a command that configures the bounded on-disk log cannot leak a
file handler into another test's data directory.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Sequence

import pytest
from factory_testing import git

_ALLOW_NETWORK_ENV = "FACTORY_TEST_ALLOW_NETWORK"
_ALLOW_REAL_BINARIES_ENV = "FACTORY_TEST_ALLOW_REAL_BINARIES"
_ALLOW_NETWORK_MARKER = "allow_network"
_ALLOW_REAL_BINARIES_MARKER = "allow_real_binaries"
#: ``git`` is exercised as a real subprocess throughout this suite (see the
#: module docstring above) and is never blocked. Only the binaries that can
#: reach a real remote GitHub account or spend real money are guarded.
_GUARDED_EXECUTABLES = frozenset({"gh", "copilot"})

_real_socket_connect = socket.socket.connect
_real_socket_connect_ex = socket.socket.connect_ex
_real_popen_init = subprocess.Popen.__init__


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        f"{_ALLOW_NETWORK_MARKER}: permit this test to open non-loopback network sockets.",
    )
    config.addinivalue_line(
        "markers",
        f"{_ALLOW_REAL_BINARIES_MARKER}: permit this test to exec the real "
        "'gh'/'copilot' binaries.",
    )


def _is_loopback_address(family: int, address: object) -> bool:
    if family == getattr(socket, "AF_UNIX", None):
        # Local IPC, never network egress.
        return True
    host: object = None
    if isinstance(address, tuple) and address:
        host = address[0]
    elif isinstance(address, str):
        host = address
    if not isinstance(host, str) or not host:
        # Unrecognized address shape: fail closed rather than guess.
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def _executable_name(command: object) -> str | None:
    if isinstance(command, (str, bytes, os.PathLike)):
        first = command
    elif isinstance(command, Sequence) and command:
        first = command[0]
    else:
        return None
    try:
        return Path(os.fsdecode(first)).name
    except TypeError:
        return None


def _is_opted_in(marker: object, env_var: str) -> bool:
    """``True`` when a guard should be lifted: either this test node carries
    the given marker, or the given environment variable is set to ``"1"``.
    A plain function (not inlined in the fixture) so both opt-in paths are
    directly unit-testable without depending on pytest fixture timing."""
    return marker is not None or os.environ.get(env_var) == "1"


@pytest.fixture(autouse=True)
def _factory_offline_guard(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Autouse: fail fast on accidental network egress or direct execution of
    the real ``gh``/``copilot`` binaries. See the module docstring for what
    stays unaffected (loopback sockets, ``git``, test doubles)."""
    allow_network = _is_opted_in(
        request.node.get_closest_marker(_ALLOW_NETWORK_MARKER), _ALLOW_NETWORK_ENV
    )
    allow_real_binaries = _is_opted_in(
        request.node.get_closest_marker(_ALLOW_REAL_BINARIES_MARKER), _ALLOW_REAL_BINARIES_ENV
    )

    if not allow_network:

        def guarded_connect(self: socket.socket, address: object) -> object:
            if not _is_loopback_address(self.family, address):
                raise RuntimeError(
                    "Blocked a non-loopback network connection during tests "
                    f"(to {address!r}). Mark the test with "
                    f"@pytest.mark.{_ALLOW_NETWORK_MARKER} or set "
                    f"{_ALLOW_NETWORK_ENV}=1 to opt in explicitly."
                )
            return _real_socket_connect(self, address)

        def guarded_connect_ex(self: socket.socket, address: object) -> object:
            if not _is_loopback_address(self.family, address):
                raise RuntimeError(
                    "Blocked a non-loopback network connection during tests "
                    f"(to {address!r}). Mark the test with "
                    f"@pytest.mark.{_ALLOW_NETWORK_MARKER} or set "
                    f"{_ALLOW_NETWORK_ENV}=1 to opt in explicitly."
                )
            return _real_socket_connect_ex(self, address)

        monkeypatch.setattr(socket.socket, "connect", guarded_connect)
        monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)

    if not allow_real_binaries:

        def guarded_popen_init(self: subprocess.Popen, *args: object, **kwargs: object) -> None:
            command = kwargs.get("args", args[0] if args else None)
            name = _executable_name(command)
            if name in _GUARDED_EXECUTABLES:
                raise RuntimeError(
                    f"Blocked a direct execution of the real {name!r} binary during "
                    "tests. Use a test double (e.g. ScriptedRunner, or monkeypatch "
                    f"subprocess.Popen) or mark the test with "
                    f"@pytest.mark.{_ALLOW_REAL_BINARIES_MARKER}."
                )
            _real_popen_init(self, *args, **kwargs)

        monkeypatch.setattr(subprocess.Popen, "__init__", guarded_popen_init)


@pytest.fixture(autouse=True)
def _restore_factory_logging() -> Iterator[None]:
    """Undo any :func:`configure_factory_logging` performed by a test.

    That function deliberately configures the package-wide
    ``software_agent_factory`` logger once and sets ``propagate = False``
    (``observability.py``), which is right for a real process but would leak
    across tests: a rotating handler would keep writing into another test's
    deleted ``tmp_path``, and later tests could no longer capture log records
    through ``caplog``. Snapshot/restore keeps each test's logging setup its
    own.
    """
    logger = logging.getLogger("software_agent_factory")
    original_handlers = list(logger.handlers)
    original_level = logger.level
    original_propagate = logger.propagate
    try:
        yield
    finally:
        for handler in list(logger.handlers):
            if handler not in original_handlers:
                logger.removeHandler(handler)
                handler.close()
        logger.handlers = original_handlers
        logger.setLevel(original_level)
        logger.propagate = original_propagate


@pytest.fixture
def factory_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Factory Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "factory-test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Factory Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "factory-test@example.invalid")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    # No controller-side GitHub credentials may leak in from the developer's
    # shell: tests must never be able to reach a real GitHub account.
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


@pytest.fixture
def factory_source_repo(tmp_path: Path, factory_git_env: None) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "factory-test@example.invalid")
    git(repo, "config", "user.name", "Factory Test")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("hello\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "initial commit")
    return repo


@pytest.fixture
def factory_data_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "data"
    directory.mkdir()
    return directory
