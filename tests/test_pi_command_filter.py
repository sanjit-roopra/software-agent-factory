"""Run the Node tests of the pi command filter (``tests/pi_extensions``) under pytest."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import NoReturn

import pytest
from prompt_fixtures import make_request

import software_agent_factory
from software_agent_factory.copilot_runtime import _permission_profile
from software_agent_factory.models import AgentRole

MINIMUM_NODE_MAJOR = 22
NODE_TEST_DIRECTORY = Path(__file__).parent / "pi_extensions"
NODE_TIMEOUT_SECONDS = 120
NODE_VERSION_TIMEOUT_SECONDS = 30
COMMAND_FILTER = (
    Path(software_agent_factory.__file__).parent / "pi_extensions" / "command_filter.mjs"
)


def _node_major(node_path: str) -> int | None:
    result = subprocess.run(
        [node_path, "--version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=NODE_VERSION_TIMEOUT_SECONDS,
    )
    version = result.stdout.strip().removeprefix("v")
    major = version.split(".", 1)[0]
    return int(major) if result.returncode == 0 and major.isdigit() else None


def _skip_or_fail_in_ci(reason: str) -> NoReturn:
    """Skip locally; fail in CI, where a skipped filter test would hide a broken filter."""
    if os.environ.get("CI") == "true":
        pytest.fail(reason)
    pytest.skip(reason)


def test_command_filter_node_tests_pass() -> None:
    node_path = shutil.which("node")
    if node_path is None:
        _skip_or_fail_in_ci("node is not installed; the command filter tests need Node 22 or newer")
    major = _node_major(node_path)
    if major is None or major < MINIMUM_NODE_MAJOR:
        _skip_or_fail_in_ci(f"the command filter tests need Node {MINIMUM_NODE_MAJOR} or newer")

    test_files = sorted(str(path) for path in NODE_TEST_DIRECTORY.glob("*.test.mjs"))
    assert test_files, f"no *.test.mjs files under {NODE_TEST_DIRECTORY}"

    result = subprocess.run(
        [node_path, "--test", *test_files],
        capture_output=True,
        text=True,
        check=False,
        timeout=NODE_TIMEOUT_SECONDS,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def _pi_rule_names() -> set[str]:
    return set(
        re.findall(r'^\s*\{?\s*name: "([^"]+)"', COMMAND_FILTER.read_text(encoding="utf-8"), re.M)
    )


def _pi_rule_names_for(copilot_permission: str) -> set[str]:
    """Map one Copilot denied permission onto the pi rules that must mirror it."""
    if copilot_permission == "url":
        return {"curl", "wget"}
    shell_rule = re.fullmatch(r"shell\((.+?)(?::\*)?\)", copilot_permission)
    assert shell_rule, f"no pi rule is mapped for the Copilot permission {copilot_permission!r}"
    return {shell_rule.group(1)}


def test_pi_command_filter_rules_match_the_copilot_implementer_deny_list() -> None:
    denied = _permission_profile(make_request(AgentRole.IMPLEMENTER)).denied_permissions

    expected = set().union(*(_pi_rule_names_for(permission) for permission in denied))

    assert _pi_rule_names() == expected
