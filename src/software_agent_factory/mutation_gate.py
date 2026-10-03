"""Mutation gate for Python changes (ADR-034).

After the verify commands pass, the gate runs ``mutmut`` 3 on the Python
source modules that the change touched. Without configuration, ``mutmut``
mutates ``lib/`` or ``src/``, so the gate runs only for changed files in that
directory. A flat layout needs ``source_paths`` in the ``mutmut``
configuration; the gate then runs for any changed module. ``mutmut`` copies
the code into ``mutants/``, runs the tests against each mutant and records each
result in a ``.meta`` file next to the copy. The gate reads those files.
Surviving mutants, and a changed module of which the tests kill no mutant, are
evidence for the tester and the reviewer.

The gate is advisory. It never fails verification: ``mutmut`` also makes
equivalent mutants, such as ``<`` to ``<=`` where both return the same value,
and no test can kill those. The reviewer judges each survivor. A missing tool,
a crash or a timeout skips the gate and records why. It always removes
``mutants/``, so the Git tree stays as the change left it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tomllib
from collections.abc import Sequence
from configparser import ConfigParser
from configparser import Error as ConfigParserError
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .command_probe import CommandRunner, ProbeLimits
from .models import (
    MAX_COMMAND_TEXT_LENGTH,
    CommandResult,
    MutationReport,
    MutationStatus,
    VerificationReport,
)

MUTANTS_DIR = "mutants"
MAX_LISTED_SURVIVORS = 50
MAX_META_BYTES = 16 * 1024 * 1024
#: The directories that ``mutmut`` mutates without configuration, in its own order.
DEFAULT_SOURCE_DIRS = ("lib", "src")
FLAT_LAYOUT_REASON = (
    "no lib/ or src/ directory: set source_paths in the mutmut configuration for a flat layout"
)
#: ``mutmut`` 3 stops with this assertion when no mutant matches the patterns.
NOTHING_MATCHES = "Filtered for specific mutants, but nothing matches"
NO_MUTANTS_REASON = "no mutants in the changed modules"
_MODULE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")
_MUTANT_NAME = re.compile(r"[A-Za-z0-9_.ǁ]{1,200}__mutmut_\d{1,9}")
_TEST_DIRS = frozenset({"tests", "test", "testing"})
#: ``mutmut`` 3 exit codes per mutant. Every other code counts as ``other``.
_KILLED = frozenset({1, 3})
_SURVIVED = frozenset({0})
_NO_TESTS = frozenset({5, 33})


@dataclass(frozen=True)
class MutationTarget:
    """One changed Python source file and the ``mutmut`` module name of its mutants."""

    path: str
    module: str

    @property
    def patterns(self) -> tuple[str, ...]:
        """Return the ``mutmut run`` patterns that select only this file's mutants.

        ``mutmut`` names the mutants of ``pkg/__init__.py`` ``pkg.x_…`` or
        ``pkg.xǁ…``, so ``pkg.*`` would also select every submodule.
        """
        if PurePosixPath(self.path).name == "__init__.py":
            return (f"{self.module}.x_*", f"{self.module}.xǁ*")
        return (f"{self.module}.*",)


def mutation_targets(changed_files: Sequence[str]) -> tuple[MutationTarget, ...]:
    """Return the changed Python source files that ``mutmut`` can name.

    Test files and paths that are not safe module names are left out, so no
    repository text reaches a shell command.
    """

    targets: list[MutationTarget] = []
    for path in changed_files:
        posix = PurePosixPath(path)
        if posix.suffix != ".py" or _is_test_path(posix):
            continue
        # mutmut drops a leading "src." and turns ".__init__." into ".".
        parts = list(posix.with_suffix("").parts)
        if parts and parts[0] == "src":
            parts = parts[1:]
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        module = ".".join(parts)
        target = MutationTarget(path=posix.as_posix(), module=module)
        if module and _MODULE_NAME.fullmatch(module) and target not in targets:
            targets.append(target)
    return tuple(targets)


def run_mutation_gate(
    command_runner: CommandRunner,
    worktree: Path,
    exec_prefix: str,
    targets: tuple[MutationTarget, ...],
    limits: ProbeLimits,
) -> MutationReport:
    """Run ``mutmut`` on ``targets`` and judge the result. Always remove ``mutants/``."""

    modules = tuple(target.module for target in targets)
    if os.path.lexists(worktree / MUTANTS_DIR):
        # The repository owns a mutants/ path. The gate must not delete it.
        return MutationReport(
            status=MutationStatus.SKIPPED,
            modules=modules,
            reason=f"{MUTANTS_DIR}/ already exists in the repository",
        )
    if not _has_mutmut_configuration(worktree):
        source_dir = _default_source_dir(worktree)
        if source_dir is None:
            return MutationReport(
                status=MutationStatus.SKIPPED, modules=modules, reason=FLAT_LAYOUT_REASON
            )
        targets = tuple(t for t in targets if PurePosixPath(t.path).parts[0] == source_dir)
        if not targets:
            return MutationReport(
                status=MutationStatus.SKIPPED,
                modules=modules,
                reason=f"no changed module under {source_dir}/, which mutmut mutates",
            )
        modules = tuple(target.module for target in targets)
    patterns = " ".join(f"'{pattern}'" for target in targets for pattern in target.patterns)
    try:
        run = command_runner.run(
            [f"{exec_prefix} mutmut run {patterns}"],
            worktree,
            limits.timeout_seconds,
            env_passthrough=limits.env_passthrough,
            capture_bytes=limits.capture_bytes,
        )
        if not run.passed and _nothing_matches(run):
            return MutationReport(
                status=MutationStatus.PASSED, modules=modules, reason=NO_MUTANTS_REASON
            )
        if not run.passed:
            return _skipped(modules, "mutmut run did not finish", run)
        exit_codes = _read_exit_codes(worktree, targets)
    finally:
        _remove_mutants(worktree)
    if exit_codes is None:
        return MutationReport(
            status=MutationStatus.SKIPPED,
            modules=modules,
            reason="mutmut left a result file that the gate cannot read",
        )
    return _judge(modules, exit_codes)


def mutation_check_result(report: MutationReport, duration_seconds: float) -> CommandResult:
    """Describe the gate as one deterministic check for the verification report."""
    lines = [
        f"mutation gate: {report.status}",
        f"modules: {', '.join(report.modules)}",
        f"killed: {report.killed}, survived: {report.survived_count}, "
        f"no tests: {report.no_tests}, other: {report.other}",
    ]
    if report.reason:
        lines.append(report.reason)
    if report.survived:
        lines.append("surviving mutants (show one with `mutmut show <name>`):")
        lines.extend(f"- {name}" for name in report.survived)
        if report.survived_count > len(report.survived):
            lines.append(f"- and {report.survived_count - len(report.survived)} more")
    return CommandResult(
        command=f"mutmut run {' '.join(report.modules)}",
        exit_code=0,
        stdout="\n".join(lines),
        stderr="",
        duration_seconds=duration_seconds,
    )


def _has_mutmut_configuration(worktree: Path) -> bool:
    """Return whether the repository sets ``source_paths`` for ``mutmut``."""
    try:
        pyproject = tomllib.loads((worktree / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        pyproject = {}
    tool = pyproject.get("tool")
    mutmut = tool.get("mutmut") if isinstance(tool, dict) else None
    if isinstance(mutmut, dict) and ("source_paths" in mutmut or "paths_to_mutate" in mutmut):
        return True
    parser = ConfigParser()
    try:
        parser.read(worktree / "setup.cfg", encoding="utf-8")
    except (OSError, UnicodeDecodeError, ConfigParserError):
        return False
    return parser.has_option("mutmut", "source_paths") or parser.has_option(
        "mutmut", "paths_to_mutate"
    )


def _default_source_dir(worktree: Path) -> str | None:
    """Return the directory that ``mutmut`` mutates without configuration, if any."""
    for directory in DEFAULT_SOURCE_DIRS:
        if (worktree / directory).is_dir() and not (worktree / directory).is_symlink():
            return directory
    return None


def _nothing_matches(report: VerificationReport) -> bool:
    """Return whether ``mutmut`` stopped because the changed files have no mutants."""
    return any(
        NOTHING_MATCHES in check.stderr or NOTHING_MATCHES in check.stdout
        for check in report.deterministic_checks
        if not check.timed_out
    )


def _read_exit_codes(
    worktree: Path, targets: tuple[MutationTarget, ...]
) -> dict[str, int | None] | None:
    """Read each target's ``.meta`` file. ``None`` when one is not a readable result."""
    exit_codes: dict[str, int | None] = {}
    for target in targets:
        raw = _read_regular_bytes(worktree / MUTANTS_DIR / f"{target.path}.meta")
        if raw is None:
            # No .meta file: mutmut found nothing to mutate in this file.
            # The other changed files can still have mutants.
            continue
        try:
            meta = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            return None
        codes = meta.get("exit_code_by_key") if isinstance(meta, dict) else None
        if not isinstance(codes, dict):
            return None
        for name, code in codes.items():
            if isinstance(code, bool) or not (code is None or isinstance(code, int)):
                return None
            exit_codes[str(name)] = code
    return exit_codes


def _read_regular_bytes(path: Path) -> bytes | None:
    """Read a regular file without following a link. ``None`` when it is missing or odd."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return None
        raw = handle.read(MAX_META_BYTES + 1)
    return None if len(raw) > MAX_META_BYTES else raw


def _judge(modules: tuple[str, ...], exit_codes: dict[str, int | None]) -> MutationReport:
    killed = sum(1 for code in exit_codes.values() if code in _KILLED)
    survivors = [name for name, code in exit_codes.items() if code in _SURVIVED]
    no_tests = sum(1 for code in exit_codes.values() if code in _NO_TESTS)
    other = len(exit_codes) - killed - len(survivors) - no_tests
    unkilled = [module for module in modules if _no_kill(module, exit_codes)]
    status = MutationStatus.NO_KILL if unkilled else MutationStatus.PASSED
    reason: str | None = None
    if unkilled:
        reason = _bounded(f"the tests kill no mutant of: {', '.join(unkilled)}")
    elif not exit_codes:
        reason = NO_MUTANTS_REASON
    listed = sorted(name for name in survivors if _MUTANT_NAME.fullmatch(name))
    return MutationReport(
        status=status,
        modules=modules,
        killed=killed,
        survived=tuple(listed[:MAX_LISTED_SURVIVORS]),
        survived_count=len(survivors),
        no_tests=no_tests,
        other=other,
        reason=reason,
    )


def _no_kill(module: str, exit_codes: dict[str, int | None]) -> bool:
    """Return whether ``module`` has mutants and the tests kill none of them."""
    codes = [code for name, code in exit_codes.items() if _module_of(name) == module]
    return bool(codes) and not any(code in _KILLED for code in codes)


def _module_of(name: str) -> str:
    """Return the module part of a mutant name, as ``mutmut`` itself finds it."""
    parts = name.split(".")
    for index in range(len(parts) - 1, -1, -1):
        if parts[index].startswith(("x_", "xǁ")):
            return ".".join(parts[:index])
    return name.rpartition(".")[0]


def _bounded(text: str) -> str:
    if len(text) <= MAX_COMMAND_TEXT_LENGTH:
        return text
    return text[: MAX_COMMAND_TEXT_LENGTH - 3] + "..."


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
