"""Revert check for tests that the change adds or edits (ADR-034).

After the verify commands pass, the check puts every changed file that is not
a test back to the base commit, runs the verify commands again and then puts
the change back. If a verify command now fails, the tests depend on the
change. If all of them pass, the tests do not exercise the change.

The check is advisory. It never fails verification: a refactor or a
characterization test passes without the change, as it should. The tester and
the reviewer judge the result. A timeout skips the check and records why.
"""

from __future__ import annotations

from collections.abc import Sequence

from .command_probe import CommandRunner, ProbeLimits
from .models import CommandResult
from .mutation_gate import is_test_path
from .workspace import GitWorktreeWorkspace

REVERT_CHECK_COMMAND = "revert check"
TESTS_PASS_WITHOUT_CHANGE = "warning: the tests pass without the production change"


def revert_check_sources(changed_files: Sequence[str]) -> tuple[str, ...]:
    """Return the changed files that are not tests, when the change also has a test file."""
    sources = tuple(path for path in changed_files if not is_test_path(path))
    return sources if len(sources) < len(changed_files) else ()


def run_revert_check(
    command_runner: CommandRunner,
    workspace: GitWorktreeWorkspace,
    tree: str,
    verify: Sequence[str],
    sources: tuple[str, ...],
    limits: ProbeLimits,
) -> str:
    """Run ``verify`` with ``sources`` at the base commit and return the verdict.

    ``tree`` is the reviewed tree. The sources always go back to it.
    """
    workspace.restore_paths(sources)
    try:
        report = command_runner.run(
            verify,
            workspace.path,
            limits.timeout_seconds,
            env_passthrough=limits.env_passthrough,
            capture_bytes=limits.capture_bytes,
        )
    finally:
        workspace.restore_paths(sources, tree)
    failed = next((c for c in report.deterministic_checks if c.exit_code != 0), None)
    if failed is None:
        return TESTS_PASS_WITHOUT_CHANGE
    if failed.timed_out:
        return f"skipped: {failed.command} timed out"
    return f"passed: {failed.command} fails without the production change"


def revert_check_result(
    verdict: str, sources: tuple[str, ...], duration_seconds: float
) -> CommandResult:
    """Describe the check as one deterministic check for the verification report."""
    lines = [f"revert check: {verdict}", "reverted to the base commit:"]
    lines.extend(f"- {path}" for path in sources)
    return CommandResult(
        command=REVERT_CHECK_COMMAND,
        exit_code=0,
        stdout="\n".join(lines),
        stderr="",
        duration_seconds=duration_seconds,
    )
