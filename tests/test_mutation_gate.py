from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
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
    FLAT_LAYOUT_REASON,
    MUTANTS_DIR,
    NOTHING_MATCHES,
    MutationTarget,
    mutation_check_result,
    mutation_targets,
    run_mutation_gate,
)

_LIMITS = ProbeLimits(timeout_seconds=60, env_passthrough=(), capture_bytes=65536)
_PREFIX = "uv run --no-sync"
_OPS = MutationTarget(path="src/calc/ops.py", module="calc.ops")
_INIT = MutationTarget(path="src/calc/__init__.py", module="calc")
_RUN = f"{_PREFIX} mutmut run 'calc.ops.*'"

ExitCodes = Mapping[str, int | None]


# double-waiver: B1 — the real runner spawns mutmut, which runs the test suite per mutant.
class _Mutmut:
    """Write ``.meta`` files as mutmut 3 does, or fail like it."""

    def __init__(
        self,
        meta: Mapping[str, ExitCodes | str] | None = None,
        *,
        exit_code: int = 0,
        timed_out: bool = False,
        stderr: str = "",
    ) -> None:
        self.stderr = stderr
        self.meta = meta or {}
        self.exit_code = exit_code
        self.timed_out = timed_out
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
        for path, codes in self.meta.items():
            meta = cwd / MUTANTS_DIR / f"{path}.meta"
            meta.parent.mkdir(parents=True, exist_ok=True)
            text = codes if isinstance(codes, str) else json.dumps({"exit_code_by_key": codes})
            meta.write_text(text, encoding="utf-8")
        (cwd / MUTANTS_DIR).mkdir(exist_ok=True)
        exit_code = -1 if self.timed_out else self.exit_code
        return VerificationReport(
            passed=exit_code == 0,
            deterministic_checks=[
                CommandResult(
                    command=command,
                    exit_code=exit_code,
                    stdout="",
                    stderr=self.stderr,
                    duration_seconds=0.0,
                    timed_out=self.timed_out,
                )
            ],
            failures=[] if exit_code == 0 else ["x"],
            confidence=1.0,
        )


@pytest.fixture
def src_repo(tmp_path: Path) -> Path:
    (tmp_path / "src" / "calc").mkdir(parents=True)
    return tmp_path


def _gate(
    repo: Path, mutmut: _Mutmut, targets: tuple[MutationTarget, ...] = (_OPS,)
) -> MutationReport:
    return run_mutation_gate(mutmut, repo, _PREFIX, targets, _LIMITS)


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
    ) == (_OPS, _INIT, MutationTarget(path="app.py", module="app"))


@pytest.mark.parametrize(
    "path", ["src/my-pkg/mod.py", "src/calc/x;rm -rf ~.py", "1bad/mod.py", "src/app\n.py"]
)
def test_paths_that_are_not_module_names_are_left_out(path: str) -> None:
    assert mutation_targets([path]) == ()


def test_a_package_init_selects_only_its_own_mutants() -> None:
    assert _INIT.patterns == ("calc.x_*", "calc.xǁ*")
    assert _OPS.patterns == ("calc.ops.*",)


def test_killed_mutants_pass_and_survivors_are_listed(src_repo: Path) -> None:
    mutmut = _Mutmut(
        {
            "src/calc/ops.py": {
                "calc.ops.x_clamp__mutmut_1": 1,
                "calc.ops.x_clamp__mutmut_2": 0,
                "calc.ops.x_clamp__mutmut_3": 33,
                "calc.ops.x_clamp__mutmut_4": 36,
            },
            "src/calc/other.py": {"calc.other.x_f__mutmut_1": 0},
        }
    )

    report = _gate(src_repo, mutmut)

    assert report == MutationReport(
        status=MutationStatus.PASSED,
        modules=("calc.ops",),
        killed=1,
        survived=("calc.ops.x_clamp__mutmut_2",),
        survived_count=1,
        no_tests=1,
        other=1,
    )
    assert mutmut.commands == [_RUN]
    assert not (src_repo / MUTANTS_DIR).exists()


def test_a_module_with_no_killed_mutant_is_reported(src_repo: Path) -> None:
    mutmut = _Mutmut(
        {"src/calc/ops.py": {"calc.ops.x_clamp__mutmut_1": 0, "calc.ops.x_clamp__mutmut_2": 0}}
    )

    report = _gate(src_repo, mutmut)

    assert report.status is MutationStatus.NO_KILL
    assert report.reason == "the tests kill no mutant of: calc.ops"
    assert report.survived == ("calc.ops.x_clamp__mutmut_1", "calc.ops.x_clamp__mutmut_2")


def test_kills_in_a_submodule_do_not_hide_an_unkilled_package_init(src_repo: Path) -> None:
    mutmut = _Mutmut(
        {
            "src/calc/__init__.py": {"calc.x_add__mutmut_1": 0},
            "src/calc/ops.py": {"calc.ops.x_clamp__mutmut_1": 1},
        }
    )

    report = _gate(src_repo, mutmut, (_INIT, _OPS))

    assert mutmut.commands == [f"{_PREFIX} mutmut run 'calc.x_*' 'calc.xǁ*' 'calc.ops.*'"]
    assert report.status is MutationStatus.NO_KILL
    assert report.reason == "the tests kill no mutant of: calc"


def test_survivors_are_counted_beyond_the_listed_names(src_repo: Path) -> None:
    codes: dict[str, int | None] = {f"calc.ops.x_f__mutmut_{n}": 0 for n in range(60)}
    codes["calc.ops.x_f__mutmut_99"] = 1
    codes["calc.ops.x_f; rm -rf ~"] = 0

    report = _gate(src_repo, _Mutmut({"src/calc/ops.py": codes}))

    assert report.survived_count == 61
    assert len(report.survived) == 50
    assert all("__mutmut_" in name and " " not in name for name in report.survived)
    assert mutation_check_result(report, 0.0).stdout.splitlines()[-1] == "- and 11 more"


def test_a_long_list_of_unkilled_modules_is_cut_to_fit(src_repo: Path) -> None:
    targets = tuple(
        MutationTarget(path=f"src/calc/module_{n:03d}.py", module=f"calc.module_{n:03d}")
        for n in range(60)
    )
    meta = {t.path: {f"{t.module}.x_f__mutmut_1": 0} for t in targets}

    report = _gate(src_repo, _Mutmut(meta), targets)

    assert report.status is MutationStatus.NO_KILL
    assert report.reason is not None
    assert report.reason.endswith("...")


def test_a_module_without_mutants_passes_with_a_reason(src_repo: Path) -> None:
    report = _gate(src_repo, _Mutmut({"src/calc/other.py": {"calc.other.x_f__mutmut_1": 0}}))

    assert report == MutationReport(
        status=MutationStatus.PASSED,
        modules=("calc.ops",),
        reason="no mutants in the changed modules",
    )


@pytest.mark.parametrize(
    "meta", ["not json", "[]", '{"exit_code_by_key": []}', '{"exit_code_by_key": {"a": "x"}}']
)
def test_an_unreadable_result_file_skips_the_gate(src_repo: Path, meta: str) -> None:
    report = _gate(src_repo, _Mutmut({"src/calc/ops.py": meta}))

    assert report.status is MutationStatus.SKIPPED
    assert report.reason == "mutmut left a result file that the gate cannot read"
    assert not (src_repo / MUTANTS_DIR).exists()


@pytest.mark.parametrize(
    ("mutmut", "reason"),
    [
        (_Mutmut(exit_code=1), "mutmut run did not finish (exit code 1)"),
        (_Mutmut(timed_out=True), "mutmut run did not finish (timed out)"),
    ],
)
def test_a_mutmut_problem_skips_the_gate_and_cleans_up(
    src_repo: Path, mutmut: _Mutmut, reason: str
) -> None:
    report = _gate(src_repo, mutmut)

    assert report == MutationReport(
        status=MutationStatus.SKIPPED, modules=("calc.ops",), reason=reason
    )
    assert not (src_repo / MUTANTS_DIR).exists()


def test_an_existing_mutants_directory_is_never_deleted(src_repo: Path) -> None:
    owned = src_repo / MUTANTS_DIR
    owned.mkdir()
    (owned / "keep.txt").write_text("repository file\n", encoding="utf-8")
    mutmut = _Mutmut()

    report = _gate(src_repo, mutmut)

    assert report.status is MutationStatus.SKIPPED
    assert report.reason == "mutants/ already exists in the repository"
    assert mutmut.commands == []
    assert (owned / "keep.txt").exists()


def test_a_flat_layout_without_configuration_is_skipped(tmp_path: Path) -> None:
    mutmut = _Mutmut()

    report = _gate(tmp_path, mutmut, (MutationTarget(path="app.py", module="app"),))

    assert report == MutationReport(
        status=MutationStatus.SKIPPED, modules=("app",), reason=FLAT_LAYOUT_REASON
    )
    assert mutmut.commands == []


def test_a_change_with_no_mutants_passes(src_repo: Path) -> None:
    mutmut = _Mutmut(exit_code=1, stderr=f"AssertionError: {NOTHING_MATCHES}\n")

    report = _gate(src_repo, mutmut)

    assert report == MutationReport(
        status=MutationStatus.PASSED,
        modules=("calc.ops",),
        reason="no mutants in the changed modules",
    )
    assert not (src_repo / MUTANTS_DIR).exists()


def test_a_change_outside_the_source_directory_is_skipped(src_repo: Path) -> None:
    mutmut = _Mutmut()

    report = _gate(
        src_repo, mutmut, (MutationTarget(path="scripts/tool.py", module="scripts.tool"),)
    )

    assert report == MutationReport(
        status=MutationStatus.SKIPPED,
        modules=("scripts.tool",),
        reason="no changed module under src/, which mutmut mutates",
    )
    assert mutmut.commands == []


def test_files_outside_the_source_directory_are_left_out(src_repo: Path) -> None:
    mutmut = _Mutmut()

    report = _gate(src_repo, mutmut, (MutationTarget(path="app.py", module="app"), _OPS))

    assert report.modules == ("calc.ops",)
    assert mutmut.commands == [_RUN]


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("pyproject.toml", '[tool.mutmut]\nsource_paths = ["."]\n'),
        ("setup.cfg", "[mutmut]\nsource_paths = .\n"),
    ],
)
def test_configured_source_paths_allow_a_flat_layout(tmp_path: Path, name: str, text: str) -> None:
    (tmp_path / name).write_text(text, encoding="utf-8")
    mutmut = _Mutmut({"app.py": {"app.x_f__mutmut_1": 1}})

    report = _gate(tmp_path, mutmut, (MutationTarget(path="app.py", module="app"),))

    assert mutmut.commands == [f"{_PREFIX} mutmut run 'app.*'"]
    assert report.killed == 1


def test_check_result_is_advisory_and_lists_survivors() -> None:
    report = MutationReport(
        status=MutationStatus.NO_KILL,
        modules=("calc.ops",),
        survived=("calc.ops.x_clamp__mutmut_1",),
        survived_count=1,
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
