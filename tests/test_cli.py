"""Tests for the ``factory`` CLI.

Uses Typer's CliRunner against a temp Git repository and temp data
directory, isolated from global Git config/signing/hooks the same way as
tests/test_workspace.py and tests/test_workflow.py.

``tests/test_cli_operations.py`` covers the Phase 15 operational commands
(``doctor``, ``status``, ``dashboard``, ``service``); this module covers the
core workflow commands plus the cross-cutting version and prerequisite
behavior every command shares.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import textwrap
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from typer.testing import CliRunner

from software_agent_factory.__main__ import main as module_main
from software_agent_factory.cli import app
from software_agent_factory.version import format_version_line, get_version

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Factory Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "factory-test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Factory Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "factory-test@example.invalid")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "factory-test@example.invalid")
    _git(repo, "config", "user.name", "Factory Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial commit")
    return repo


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture
def path_without(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Replace ``PATH`` with a directory containing only the named shims.

    Lets a test prove the prerequisite gate fires for a genuinely missing
    executable, using the same ``shutil.which`` lookup the CLI and
    ``factory doctor`` both use, without stubbing any factory code.
    """

    def _configure(*available: str) -> Path:
        bin_dir = _shim_dir(tmp_path, available)
        monkeypatch.setenv("PATH", str(bin_dir))
        return bin_dir

    return _configure


@pytest.fixture
def path_with(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Prepend shims for the named executables to the existing ``PATH``.

    Used where a command's prerequisite gate demands a tool the test never
    actually invokes (``gh`` for ``factory start``, whose tracker is stubbed),
    so the test does not silently depend on that tool being installed on the
    developer's machine while still exercising the real gate.
    """

    def _configure(*names: str) -> Path:
        bin_dir = _shim_dir(tmp_path, names)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        return bin_dir

    return _configure


def _shim_dir(tmp_path: Path, names) -> Path:
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir(exist_ok=True)
    for name in names:
        shim = bin_dir / name
        shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        shim.chmod(0o755)
    return bin_dir


# -- version -------------------------------------------------------------


def test_version_option_prints_the_shared_version_line() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == format_version_line()
    assert get_version() in result.output


def test_version_is_identical_through_the_module_entrypoint() -> None:
    """The installed console script, ``python -m`` and the Typer app all
    resolve the same line from ``version.py``."""
    app_output = runner.invoke(app, ["--version"]).output.strip()

    captured = io.StringIO()
    with redirect_stdout(captured):
        exit_code = module_main(["--version"])

    assert exit_code == 0
    assert captured.getvalue().strip() == app_output


def test_bare_invocation_shows_help_without_running_anything() -> None:
    result = runner.invoke(app, [])

    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output
    assert "dashboard" in result.output


# -- external prerequisites ----------------------------------------------


def test_run_without_git_fails_with_an_explicit_prerequisite_error(
    source_repo: Path, data_dir: Path, path_without
) -> None:
    """Phase 15.2: a missing prerequisite is one explicit line and exit code
    2, never a traceback from inside the workspace code."""
    path_without()  # no git at all

    result = runner.invoke(
        app,
        [
            "run",
            "--repo",
            str(source_repo),
            "--title",
            "Test task",
            "--description",
            "A demonstration task",
            "--data-dir",
            str(data_dir),
        ],
    )

    assert result.exit_code == 2
    assert "missing required executable(s) on PATH: git" in result.output
    assert "factory doctor" in result.output
    assert "Traceback" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert list(data_dir.iterdir()) == []


def test_default_fake_run_requires_only_git(
    source_repo: Path, data_dir: Path, path_without
) -> None:
    """No ``gh``, no ``copilot``: the offline default must still run."""
    path_without("git")

    result = runner.invoke(
        app,
        [
            "run",
            "--repo",
            str(source_repo),
            "--title",
            "Test task",
            "--description",
            "A demonstration task",
            "--data-dir",
            str(data_dir),
        ],
    )

    # The shimmed 'git' is not a real git, so the run cannot succeed -- but it
    # must get past the prerequisite gate rather than being refused for a
    # missing gh/copilot.
    assert "missing required executable" not in result.output


def test_run_with_copilot_runtime_requires_copilot(
    source_repo: Path, data_dir: Path, path_without
) -> None:
    path_without("git")

    result = runner.invoke(
        app,
        [
            "run",
            "--repo",
            str(source_repo),
            "--title",
            "Test task",
            "--description",
            "A demonstration task",
            "--runtime",
            "copilot",
            "--data-dir",
            str(data_dir),
        ],
    )

    assert result.exit_code == 2
    assert "missing required executable(s) on PATH: copilot" in result.output


def test_invalid_config_fails_with_one_line_and_no_traceback(
    source_repo: Path, tmp_path: Path
) -> None:
    config_path = tmp_path / "broken.yaml"
    config_path.write_text("factory: [not, a, mapping]\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "run",
            "--repo",
            str(source_repo),
            "--title",
            "T",
            "--description",
            "D",
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == 2
    assert "is invalid" in result.output
    assert "Traceback" not in result.output


def test_missing_config_file_fails_cleanly(source_repo: Path, tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["runs", "--config", str(tmp_path / "absent.yaml")],
    )

    assert result.exit_code == 2
    assert "config file not found" in result.output


def test_run_happy_path_reaches_pr_ready_with_zero_exit(source_repo: Path, data_dir: Path) -> None:
    result = runner.invoke(
        app,
        [
            "run",
            "--repo",
            str(source_repo),
            "--title",
            "Test task",
            "--description",
            "A demonstration task",
            "--data-dir",
            str(data_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "state: PR_READY" in result.output
    assert "changed files: FACTORY_NOTES.md" in result.output


def test_run_non_pr_ready_outcome_uses_nonzero_exit_code(
    source_repo: Path, data_dir: Path, tmp_path: Path
) -> None:
    config_path = tmp_path / "factory.yaml"
    config_path.write_text(
        textwrap.dedent(
            f"""
            factory:
              data_dir: "{data_dir}"
              retries:
                same_model_attempts: 1
                max_total_attempts: 1
            models:
              triage: {{model: "claude-sonnet-5", reasoning: "medium"}}
              refiner: {{model: "claude-opus-5", reasoning: "high"}}
              researcher: {{model: "gpt-5.6-sol", reasoning: "high"}}
              planner: {{model: "claude-opus-5", reasoning: "high"}}
              workers:
                L0: {{model: "mai-code-1.1-flash", reasoning: "medium"}}
                L1: {{model: "claude-sonnet-5", reasoning: "medium"}}
                L2: {{model: "claude-opus-5", reasoning: "high"}}
                L3: {{model: "claude-opus-5", reasoning: "high"}}
              tester: {{model: "claude-sonnet-5", reasoning: "high"}}
              reviewer: {{model: "gpt-5.6-sol", reasoning: "high"}}
            repository:
              branch_prefix: "factory/"
              command_timeout_seconds: 30
              commands:
                install: []
                verify: ["false"]
                build: []
            risk:
              R0: {{human_approval: false}}
              R1: {{human_approval: false}}
              R2: {{human_approval: true}}
              R3: {{human_approval: true}}
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "run",
            "--repo",
            str(source_repo),
            "--title",
            "Test task",
            "--description",
            "A demonstration task that always fails verification",
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == 1, result.output
    assert "state: NEEDS_HUMAN" in result.output
    assert "reason:" in result.output


def test_runs_and_show_commands_work_with_temp_data_dir(source_repo: Path, data_dir: Path) -> None:
    run_result = runner.invoke(
        app,
        [
            "run",
            "--repo",
            str(source_repo),
            "--title",
            "Test task",
            "--description",
            "A demonstration task",
            "--data-dir",
            str(data_dir),
        ],
    )
    assert run_result.exit_code == 0, run_result.output

    runs_result = runner.invoke(app, ["runs", "--data-dir", str(data_dir)])
    assert runs_result.exit_code == 0, runs_result.output
    assert "PR_READY" in runs_result.output

    run_id = runs_result.output.splitlines()[0].split("\t")[0]

    show_result = runner.invoke(app, ["show", run_id, "--data-dir", str(data_dir)])
    assert show_result.exit_code == 0, show_result.output
    payload = json.loads(show_result.output)
    assert payload["id"] == run_id
    assert payload["state"] == "PR_READY"


def test_runs_command_reports_no_runs_found_for_empty_data_dir(data_dir: Path) -> None:
    result = runner.invoke(app, ["runs", "--data-dir", str(data_dir)])

    assert result.exit_code == 0
    assert "no runs found" in result.output


def test_show_command_fails_clearly_for_unknown_run_id(data_dir: Path) -> None:
    result = runner.invoke(app, ["show", "does-not-exist", "--data-dir", str(data_dir)])

    assert result.exit_code == 1
    assert "no such run" in result.output


# -- runtime selection ---------------------------------------------------


def test_run_defaults_to_the_fake_runtime_and_never_builds_copilot(
    source_repo: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default must never be able to make a paid model call."""
    built: list[str] = []

    class ExplodingCopilotRuntime:
        def __init__(self, *args: object, **kwargs: object) -> None:
            built.append("copilot")
            raise AssertionError("the default runtime must never be CopilotAgentRuntime")

    monkeypatch.setattr("software_agent_factory.cli.CopilotAgentRuntime", ExplodingCopilotRuntime)

    result = runner.invoke(
        app,
        [
            "run",
            "--repo",
            str(source_repo),
            "--title",
            "Test task",
            "--description",
            "A demonstration task",
            "--data-dir",
            str(data_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert built == []


def test_run_with_copilot_runtime_selects_the_real_runtime(
    source_repo: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--runtime copilot`` opts in. The real subprocess is stubbed here so
    the test still makes zero paid calls."""
    from software_agent_factory.agents import AgentResult, FakeAgentRuntime
    from software_agent_factory.models import AgentRole

    built: list[str] = []
    delegate = FakeAgentRuntime()

    class StubCopilotRuntime:
        def __init__(self, *args: object, **kwargs: object) -> None:
            built.append("copilot")

        def run(self, request: object) -> AgentResult:
            assert getattr(request, "role") in set(AgentRole)
            return delegate.run(request)  # type: ignore[arg-type]

    monkeypatch.setattr("software_agent_factory.cli.CopilotAgentRuntime", StubCopilotRuntime)
    monkeypatch.setattr(
        "software_agent_factory.cli.missing_prerequisites",
        lambda **_kwargs: (),
    )

    result = runner.invoke(
        app,
        [
            "run",
            "--repo",
            str(source_repo),
            "--title",
            "Test task",
            "--description",
            "A demonstration task",
            "--runtime",
            "copilot",
            "--data-dir",
            str(data_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert built == ["copilot"]
    assert "state: PR_READY" in result.output


def test_run_rejects_an_unknown_runtime(source_repo: Path, data_dir: Path) -> None:
    result = runner.invoke(
        app,
        [
            "run",
            "--repo",
            str(source_repo),
            "--title",
            "T",
            "--description",
            "D",
            "--runtime",
            "openai",
            "--data-dir",
            str(data_dir),
        ],
    )

    assert result.exit_code != 0


def test_explicit_work_item_id_is_used_for_deduplication(source_repo: Path, data_dir: Path) -> None:
    args = [
        "run",
        "--repo",
        str(source_repo),
        "--title",
        "Test task",
        "--description",
        "A demonstration task",
        "--work-item-id",
        "tracker-acme/repo#5",
        "--data-dir",
        str(data_dir),
    ]

    first = runner.invoke(app, args)
    second = runner.invoke(app, args)

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output

    runs_result = runner.invoke(app, ["runs", "--data-dir", str(data_dir)])
    work_item_ids = {line.split("\t")[2] for line in runs_result.output.strip().splitlines()}
    assert work_item_ids == {"tracker-acme/repo#5"}


# -- factory start -------------------------------------------------------


def _scheduler_config(
    config_path: Path,
    data_dir: Path,
    *,
    enabled: bool,
    max_runs_per_day: int | None = None,
) -> Path:
    import yaml

    from software_agent_factory.config import DEFAULT_CONFIG_FILENAME

    packaged = Path(__import__("software_agent_factory").__file__).parent / DEFAULT_CONFIG_FILENAME
    payload = yaml.safe_load(packaged.read_text(encoding="utf-8"))
    payload["factory"]["data_dir"] = str(data_dir)
    payload["scheduler"]["enabled"] = enabled
    if max_runs_per_day is not None:
        payload["scheduler"]["max_runs_per_day"] = max_runs_per_day
    config_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return config_path


def test_start_refuses_to_run_when_the_scheduler_is_disabled(
    source_repo: Path, data_dir: Path, tmp_path: Path
) -> None:
    config_path = _scheduler_config(tmp_path / "factory.yaml", data_dir, enabled=False)

    result = runner.invoke(
        app,
        [
            "start",
            "--repo",
            str(source_repo),
            "--github-repo",
            "acme/repo",
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == 2
    assert "scheduler is disabled" in result.output


def _tracker_item(source_repo: Path, *, identifier: str = "acme/repo#11"):
    from datetime import datetime, timezone

    from software_agent_factory.scheduler import TrackerItem

    return TrackerItem(
        opaque_id=identifier,
        identifier=identifier,
        title="Reject empty names",
        description="Return HTTP 400 for blank names.",
        state="OPEN",
        labels=("agent-ready",),
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        repository_path=str(source_repo),
    )


def _install_local_provider(
    monkeypatch: pytest.MonkeyPatch, source_repo: Path, *, items: list
) -> None:
    """Give ``FactoryService`` an in-memory tracker and make any attempt to
    construct the real GitHub provider an immediate test failure."""

    class LocalProvider:
        def fetch_candidates(self):  # noqa: ANN201 - test double
            return list(items)

        def fetch_by_ids(self, opaque_ids):  # noqa: ANN001, ANN201 - test double
            wanted = set(opaque_ids)
            return [item for item in items if item.opaque_id in wanted]

    def no_github(*args: object, **kwargs: object) -> None:
        raise AssertionError("factory start --once must not reach GitHub in tests")

    monkeypatch.setattr("software_agent_factory.service.GitHubIssueProvider", no_github)

    import software_agent_factory.service as service_module

    original_init = service_module.FactoryService.__post_init__

    def patched_init(self: object) -> None:
        self.provider = LocalProvider()  # type: ignore[attr-defined]
        original_init(self)

    monkeypatch.setattr(service_module.FactoryService, "__post_init__", patched_init)


def test_start_once_runs_one_bounded_tick_without_touching_github(
    source_repo: Path,
    data_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path_with,
) -> None:
    path_with("gh")  # start's prerequisite gate; the tracker itself is stubbed
    _install_local_provider(monkeypatch, source_repo, items=[_tracker_item(source_repo)])

    config_path = _scheduler_config(tmp_path / "factory.yaml", data_dir, enabled=True)

    result = runner.invoke(
        app,
        [
            "start",
            "--repo",
            str(source_repo),
            "--github-repo",
            "acme/repo",
            "--once",
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "dispatched: acme/repo#11" in result.output
    assert "fake runtime still persists completed runs" in result.output

    runs_result = runner.invoke(app, ["runs", "--data-dir", str(data_dir)])
    assert "PR_READY" in runs_result.output
    assert "tracker-acme/repo#11" in runs_result.output


def test_start_requires_gh_because_it_polls_github_issues(
    source_repo: Path, data_dir: Path, tmp_path: Path, path_without
) -> None:
    """The backlog daemon reaches GitHub through ``gh`` even when pull
    requests and CI observation are both disabled."""
    config_path = _scheduler_config(tmp_path / "factory.yaml", data_dir, enabled=True)
    path_without("git")  # git present, gh missing

    result = runner.invoke(
        app,
        [
            "start",
            "--repo",
            str(source_repo),
            "--github-repo",
            "acme/repo",
            "--once",
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == 2
    assert "missing required executable(s) on PATH: gh" in result.output
    assert "Traceback" not in result.output


def test_start_once_reports_the_daily_run_ceiling(
    source_repo: Path,
    data_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path_with,
) -> None:
    path_with("gh")
    _install_local_provider(monkeypatch, source_repo, items=[])
    config_path = _scheduler_config(
        tmp_path / "factory.yaml", data_dir, enabled=True, max_runs_per_day=3
    )

    result = runner.invoke(
        app,
        [
            "start",
            "--repo",
            str(source_repo),
            "--github-repo",
            "acme/repo",
            "--once",
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "daily run limit: 3/day" in result.output
    assert "rate limited" not in result.output


def test_start_once_reports_a_rate_limited_tick(
    source_repo: Path,
    data_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path_with,
) -> None:
    """With the day's quota already spent, the tick claims nothing new and
    says so instead of looking like an empty backlog."""
    from datetime import datetime, timezone

    from software_agent_factory.models import FactoryRun, WorkflowState
    from software_agent_factory.store import FileRunStore

    path_with("gh")
    item = _tracker_item(source_repo, identifier="acme/repo#12")
    _install_local_provider(monkeypatch, source_repo, items=[item])

    store = FileRunStore(data_dir)
    store.save_run(
        FactoryRun(
            id="run-already-today",
            work_item_id="tracker-acme/repo#99",
            state=WorkflowState.DONE,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
        )
    )

    config_path = _scheduler_config(
        tmp_path / "factory.yaml", data_dir, enabled=True, max_runs_per_day=1
    )

    result = runner.invoke(
        app,
        [
            "start",
            "--repo",
            str(source_repo),
            "--github-repo",
            "acme/repo",
            "--once",
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "dispatched: (none)" in result.output
    assert "rate limited: the daily run limit (1/day)" in result.output
