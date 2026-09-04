"""Tests for the ``factory`` CLI.

Uses Typer's CliRunner against a temp Git repository and temp data
directory, isolated from global Git config/signing/hooks the same way as
tests/test_workspace.py and tests/test_workflow.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from software_agent_factory.cli import app

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


def test_run_happy_path_reaches_pr_ready_with_zero_exit(
    source_repo: Path, data_dir: Path
) -> None:
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


def test_runs_and_show_commands_work_with_temp_data_dir(
    source_repo: Path, data_dir: Path
) -> None:
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

    monkeypatch.setattr(
        "software_agent_factory.cli.CopilotAgentRuntime", ExplodingCopilotRuntime
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


def test_explicit_work_item_id_is_used_for_deduplication(
    source_repo: Path, data_dir: Path
) -> None:
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


def _scheduler_config(config_path: Path, data_dir: Path, *, enabled: bool) -> Path:
    import yaml

    from software_agent_factory.config import DEFAULT_CONFIG_FILENAME

    packaged = (
        Path(__import__("software_agent_factory").__file__).parent / DEFAULT_CONFIG_FILENAME
    )
    payload = yaml.safe_load(packaged.read_text(encoding="utf-8"))
    payload["factory"]["data_dir"] = str(data_dir)
    payload["scheduler"]["enabled"] = enabled
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


def test_start_once_runs_one_bounded_tick_without_touching_github(
    source_repo: Path, data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime, timezone

    from software_agent_factory.scheduler import TrackerItem

    item = TrackerItem(
        opaque_id="acme/repo#11",
        identifier="acme/repo#11",
        title="Reject empty names",
        description="Return HTTP 400 for blank names.",
        state="OPEN",
        labels=("agent-ready",),
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        repository_path=str(source_repo),
    )

    class LocalProvider:
        def fetch_candidates(self):  # noqa: ANN201 - test double
            return [item]

        def fetch_by_ids(self, opaque_ids):  # noqa: ANN001, ANN201 - test double
            return [item] if item.opaque_id in set(opaque_ids) else []

    def no_github(*args: object, **kwargs: object) -> None:
        raise AssertionError("factory start --once must not reach GitHub in tests")

    monkeypatch.setattr("software_agent_factory.service.GitHubIssueProvider", no_github)

    import software_agent_factory.service as service_module

    original_init = service_module.FactoryService.__post_init__

    def patched_init(self: object) -> None:
        self.provider = LocalProvider()  # type: ignore[attr-defined]
        original_init(self)

    monkeypatch.setattr(service_module.FactoryService, "__post_init__", patched_init)

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

    runs_result = runner.invoke(app, ["runs", "--data-dir", str(data_dir)])
    assert "PR_READY" in runs_result.output
    assert "tracker-acme/repo#11" in runs_result.output
