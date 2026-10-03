"""Mutation gate for Python changes (ADR-034).

After the verify commands pass, the gate runs ``mutmut`` 3 on the Python
source modules that the change touched. ``mutmut`` needs no configuration: it
finds the package, copies it into ``mutants/`` and runs the tests against each
mutant. Surviving mutants, and a changed module of which the tests kill no
mutant, are evidence for the tester and the reviewer.

The gate is advisory. It never fails verification: ``mutmut`` also makes
equivalent mutants, such as ``<`` to ``<=`` where both return the same value,
and no test can kill those. The reviewer judges each survivor. A missing tool,
a crash or a timeout skips the gate and records why. It always removes
``mutants/``, so the Git tree stays as the change left it.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from .command_probe import CommandRunner, ProbeLimits
from .models import CommandResult, MutationReport, MutationStatus, VerificationReport

MUTANTS_DIR = "mutants"
MAX_LISTED_SURVIVORS = 50
_MODULE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_RESULT_LINE = re.compile(r"^\s*(?P<name>\S+): (?P<status>.+?)\s*$")
_TEST_DIRS = frozenset({"tests", "test", "testing"})


def mutation_targets(changed_files: Sequence[str]) -> tuple[str, ...]:
    """Return the dotted module names of the changed Python source files.

    Test files and paths that are not safe module names are left out, so no
    repository text reaches a shell command.
    """

    modules: list[str] = []
    for path in changed_files:
        posix = PurePosixPath(path)
        if posix.suffix != ".py" or _is_test_path(posix):
            continue
        parts = list(posix.with_suffix("").parts)
        if parts and parts[0] == "src":
            parts = parts[1:]
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        module = ".".join(parts)
        if module and _MODULE_NAME.match(module) and module not in modules:
            modules.append(module)
    return tuple(modules)


def run_mutation_gate(
    command_runner: CommandRunner,
    worktree: Path,
    exec_prefix: str,
    modules: tuple[str, ...],
    limits: ProbeLimits,
) -> MutationReport:
    """Run ``mutmut`` on ``modules`` and judge the result. Always remove ``mutants/``."""

    if (worktree / MUTANTS_DIR).exists() or (worktree / MUTANTS_DIR).is_symlink():
        # The repository owns a mutants/ path. The gate must not delete it.
        return MutationReport(
            status=MutationStatus.SKIPPED,
            modules=modules,
            reason=f"{MUTANTS_DIR}/ already exists in the repository",
        )
    patterns = " ".join(f"'{module}.*'" for module in modules)
    try:
        run = _run(command_runner, worktree, f"{exec_prefix} mutmut run {patterns}", limits)
        if not run.passed:
            return _skipped(modules, "mutmut run did not finish", run)
        results = _run(command_runner, worktree, f"{exec_prefix} mutmut results --all true", limits)
        if not results.passed:
            return _skipped(modules, "mutmut results did not finish", results)
        statuses = _parse_results(results.deterministic_checks[-1].stdout, modules)
    finally:
        _remove_mutants(worktree)
    return _judge(modules, statuses)


def mutation_check_result(report: MutationReport, duration_seconds: float) -> CommandResult:
    """Describe the gate as one deterministic check for the verification report."""
    lines = [
        f"mutation gate: {report.status}",
        f"modules: {', '.join(report.modules)}",
        f"killed: {report.killed}, survived: {len(report.survived)}, "
        f"no tests: {report.no_tests}, other: {report.other}",
    ]
    if report.reason:
        lines.append(report.reason)
    if report.survived:
        lines.append("surviving mutants (show one with `mutmut show <name>`):")
        lines.extend(f"- {name}" for name in report.survived[:MAX_LISTED_SURVIVORS])
    return CommandResult(
        command=f"mutmut run {' '.join(report.modules)}",
        exit_code=0,
        stdout="\n".join(lines),
        stderr="",
        duration_seconds=duration_seconds,
    )


def _run(
    command_runner: CommandRunner, worktree: Path, command: str, limits: ProbeLimits
) -> VerificationReport:
    return command_runner.run(
        [command],
        worktree,
        limits.timeout_seconds,
        env_passthrough=limits.env_passthrough,
        capture_bytes=limits.capture_bytes,
    )


def _parse_results(output: str, modules: tuple[str, ...]) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for line in output.splitlines():
        match = _RESULT_LINE.match(line)
        if match is None:
            continue
        name = match["name"]
        if any(name.startswith(f"{module}.") for module in modules):
            statuses[name] = match["status"]
    return statuses


def _judge(modules: tuple[str, ...], statuses: dict[str, str]) -> MutationReport:
    killed = sum(1 for status in statuses.values() if status == "killed")
    survived = tuple(sorted(name for name, status in statuses.items() if status == "survived"))
    no_tests = sum(1 for status in statuses.values() if status == "no tests")
    other = len(statuses) - killed - len(survived) - no_tests
    unkilled = [
        module
        for module in modules
        if _mutants_of(module, statuses)
        and not any(statuses[name] == "killed" for name in _mutants_of(module, statuses))
    ]
    status = MutationStatus.NO_KILL if unkilled else MutationStatus.PASSED
    reason: str | None = None
    if unkilled:
        reason = f"the tests kill no mutant of: {', '.join(unkilled)}"
    elif not statuses:
        reason = "no mutants in the changed modules"
    return MutationReport(
        status=status,
        modules=modules,
        killed=killed,
        survived=survived[:MAX_LISTED_SURVIVORS],
        no_tests=no_tests,
        other=other,
        reason=reason,
    )


def _mutants_of(module: str, statuses: dict[str, str]) -> list[str]:
    return [name for name in statuses if name.startswith(f"{module}.")]


def _skipped(modules: tuple[str, ...], reason: str, report: VerificationReport) -> MutationReport:
    check = report.deterministic_checks[-1] if report.deterministic_checks else None
    detail = ""
    if check is not None:
        detail = " (timed out)" if check.timed_out else f" (exit code {check.exit_code})"
    return MutationReport(status=MutationStatus.SKIPPED, modules=modules, reason=reason + detail)


def _is_test_path(path: PurePosixPath) -> bool:
    name = path.name
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
        or any(part in _TEST_DIRS for part in path.parts[:-1])
    )


def _remove_mutants(worktree: Path) -> None:
    mutants = worktree / MUTANTS_DIR
    if mutants.is_symlink():
        mutants.unlink()
    elif mutants.is_dir():
        shutil.rmtree(mutants)
