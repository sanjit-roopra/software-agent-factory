"""Run the Node tests of the pi command filter (``tests/pi_extensions``) under pytest."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

MINIMUM_NODE_MAJOR = 22
NODE_TEST_DIRECTORY = Path(__file__).parent / "pi_extensions"
NODE_TIMEOUT_SECONDS = 120


def _node_major(node: str) -> int | None:
    result = subprocess.run(
        [node, "--version"], capture_output=True, text=True, check=False, timeout=30
    )
    version = result.stdout.strip().removeprefix("v")
    major = version.split(".", 1)[0]
    return int(major) if result.returncode == 0 and major.isdigit() else None


def _unavailable(reason: str) -> None:
    """Skip locally; fail in CI, where a skipped filter test would hide a broken filter."""
    if os.environ.get("CI") == "true":
        pytest.fail(reason)
    pytest.skip(reason)


def test_command_filter_node_tests_pass() -> None:
    node = shutil.which("node")
    if node is None:
        _unavailable("node is not installed; the command filter tests need Node 22 or newer")
        return
    major = _node_major(node)
    if major is None or major < MINIMUM_NODE_MAJOR:
        _unavailable(f"the command filter tests need Node {MINIMUM_NODE_MAJOR} or newer")
        return

    test_files = sorted(str(path) for path in NODE_TEST_DIRECTORY.glob("*.test.mjs"))
    assert test_files, f"no *.test.mjs files under {NODE_TEST_DIRECTORY}"

    result = subprocess.run(
        [node, "--test", *test_files],
        capture_output=True,
        text=True,
        check=False,
        timeout=NODE_TIMEOUT_SECONDS,
    )

    assert result.returncode == 0, result.stdout + result.stderr
