from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from software_agent_factory.command_probe import ProbeLimits
from software_agent_factory.models import (
    CommandResult,
    MutationReport,
    MutationStatus,
    VerificationReport,
)
from software_agent_factory.mutation_gate import (
    MUTANTS_DIR,
    mutation_check_result,
    mutation_targets,
    run_mutation_gate,
)

_LIMITS = ProbeLimits(timeout_seconds=60, env_passthrough=(), capture_bytes=65536)
_PREFIX = "uv run --no-sync"
_RUN = f"{_PREFIX} mutmut run 'calc.ops.*'"
_RESULTS = f"{_PREFIX} mutmut results --all true"


# double-waiver: B1 — the real runner spawns mutmut, which runs the test suite per mutant.
class _Mutmut:
    def __init__(
        self,
        results: str = "",
        failing: frozenset[str] = frozenset(),
        timing_out: frozenset[str] = frozenset(),
    ) -> None:
        self.results = results
        self.failing = failing
        self.timing_out = timing_out
        self.commands: list[str] = []

    def run(
        self,
        commands: Sequence[str],
        cwd: Path,
        timeout_seconds: int,
        *,
        env_passthrough: Sequence[str] = (),
        capture_bytes: int = 0,
    ) -> VerificationReport:
        command = commands[0]
        self.commands.append(command)
        if " mutmut run " in command:
            (cwd / MUTANTS_DIR / "src").mkdir(parents=True, exist_ok=True)
        timed_out = command in self.timing_out
        exit_code = -1 if timed_out else 1 if command in self.failing else 0
        stdout = self.results if command.endswith("--all true") else ""
        return VerificationReport(
            passed=exit_code == 0,
            deterministic_checks=[
                CommandResult(
                    command=command,
                    exit_code=exit_code,
                    stdout=stdout,
                    stderr="",
                    duration_seconds=0.0,
                    timed_out=timed_out,
                )
            ],
            failures=[] if exit_code == 0 else ["x"],
            confidence=1.0,
        )


def _gate(
    tmp_path: Path, mutmut: _Mutmut, modules: tuple[str, ...] = ("calc.ops",)
) -> MutationReport:
    return run_mutation_gate(mutmut, tmp_path, _PREFIX, modules, _LIMITS)


def test_targets_are_changed_source_modules_only() -> None:
    assert mutation_targets(
        [
            "src/calc/ops.py",
            "src/calc/__init__.py",
            "app.py",
            "tests/test_ops.py",
            "src/calc/test_helpers.py",
            "src/calc/ops_test.py",
            "conftest.py",
            "docs/index.md",
            "src/calc/ops.py",
        ]
    ) == ("calc.ops", "calc", "app")


@pytest.mark.parametrize("path", ["src/my-pkg/mod.py", "src/calc/x;rm -rf ~.py", "1bad/mod.py"])
def test_paths_that_are_not_module_names_are_left_out(path: str) -> None:
    assert mutation_targets([path]) == ()


def test_killed_mutants_pass_and_survivors_are_listed(tmp_path: Path) -> None:
    mutmut = _Mutmut(
        "    calc.x_add__mutmut_1: not checked\n"
        "    calc.ops.x_clamp__mutmut_1: killed\n"
        "    calc.ops.x_clamp__mutmut_2: survived\n"
        "    calc.ops.x_clamp__mutmut_3: no tests\n"
        "    calc.ops.x_clamp__mutmut_4: timeout\n"
    )

    report = _gate(tmp_path, mutmut)

    assert report == MutationReport(
        status=MutationStatus.PASSED,
        modules=("calc.ops",),
        killed=1,
        survived=("calc.ops.x_clamp__mutmut_2",),
        no_tests=1,
        other=1,
    )
    assert mutmut.commands == [_RUN, _RESULTS]
    assert not (tmp_path / MUTANTS_DIR).exists()


def test_a_module_with_no_killed_mutant_is_reported(tmp_path: Path) -> None:
    mutmut = _Mutmut(
        "    calc.ops.x_clamp__mutmut_1: survived\n    calc.ops.x_clamp__mutmut_2: survived\n"
    )

    report = _gate(tmp_path, mutmut)

    assert report.status is MutationStatus.NO_KILL
    assert report.reason == "the tests kill no mutant of: calc.ops"
    assert report.survived == ("calc.ops.x_clamp__mutmut_1", "calc.ops.x_clamp__mutmut_2")


def test_a_module_without_mutants_passes_with_a_reason(tmp_path: Path) -> None:
    report = _gate(tmp_path, _Mutmut("    other.x_f__mutmut_1: survived\n"))

    assert report == MutationReport(
        status=MutationStatus.PASSED,
        modules=("calc.ops",),
        reason="no mutants in the changed modules",
    )


@pytest.mark.parametrize(
    ("mutmut", "reason"),
    [
        (_Mutmut(failing=frozenset({_RUN})), "mutmut run did not finish (exit code 1)"),
        (_Mutmut(timing_out=frozenset({_RUN})), "mutmut run did not finish (timed out)"),
        (_Mutmut(failing=frozenset({_RESULTS})), "mutmut results did not finish (exit code 1)"),
    ],
)
def test_a_mutmut_problem_skips_the_gate_and_cleans_up(
    tmp_path: Path, mutmut: _Mutmut, reason: str
) -> None:
    report = _gate(tmp_path, mutmut)

    assert report == MutationReport(
        status=MutationStatus.SKIPPED, modules=("calc.ops",), reason=reason
    )
    assert not (tmp_path / MUTANTS_DIR).exists()


def test_an_existing_mutants_directory_is_never_deleted(tmp_path: Path) -> None:
    owned = tmp_path / MUTANTS_DIR
    owned.mkdir()
    (owned / "keep.txt").write_text("repository file\n", encoding="utf-8")
    mutmut = _Mutmut()

    report = _gate(tmp_path, mutmut)

    assert report.status is MutationStatus.SKIPPED
    assert report.reason == "mutants/ already exists in the repository"
    assert mutmut.commands == []
    assert (owned / "keep.txt").exists()


def test_check_result_is_advisory_and_lists_survivors() -> None:
    report = MutationReport(
        status=MutationStatus.NO_KILL,
        modules=("calc.ops",),
        survived=("calc.ops.x_clamp__mutmut_1",),
        reason="the tests kill no mutant of: calc.ops",
    )

    check = mutation_check_result(report, 1.5)

    assert check.exit_code == 0
    assert check.stdout.splitlines() == [
        "mutation gate: no_kill",
        "modules: calc.ops",
        "killed: 0, survived: 1, no tests: 0, other: 0",
        "the tests kill no mutant of: calc.ops",
        "surviving mutants (show one with `mutmut show <name>`):",
        "- calc.ops.x_clamp__mutmut_1",
    ]
