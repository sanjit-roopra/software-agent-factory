"""Integration tests for the Phase 15 operational CLI commands.

Covers ``factory doctor``, ``factory status``, ``factory dashboard`` and the
``factory service`` subcommands through Typer's ``CliRunner``, plus the
cross-cutting guarantees the commands are supposed to hold:

- the offline/no-paid defaults (no runtime but ``fake``, no network, no
  launchd mutation, no dashboard socket unless explicitly requested);
- read-only commands that must not create or modify anything on disk;
- the exact arguments the launchd service will pass back to ``factory
  start``.

External boundaries that would touch the host -- ``launchctl``, the real
``~/Library/LaunchAgents`` directory, an actual listening socket -- are
replaced with test doubles. Everything else (config loading, run store
access, snapshot/health derivation, argument parsing, output rendering) runs
for real.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from software_agent_factory import cli
from software_agent_factory.cli import app
from software_agent_factory.config import DEFAULT_CONFIG_FILENAME
from software_agent_factory.doctor import CheckResult, CheckStatus, DoctorReport
from software_agent_factory.observability import (
    MonitoringSnapshot,
    OperationalHealthReport,
    RunDetail,
)
from software_agent_factory.service_install import (
    DEFAULT_LABEL,
    ServiceInstallRequest,
    ServiceRuntime,
    ServiceStatus,
    build_program_arguments,
)

runner = CliRunner()


# -- fixtures / helpers ----------------------------------------------------


@pytest.fixture
def source_repo(factory_source_repo: Path) -> Path:
    return factory_source_repo


@pytest.fixture
def data_dir(factory_data_dir: Path) -> Path:
    return factory_data_dir


def write_config(path: Path, data_dir: Path, **overrides: object) -> Path:
    """Write a config file derived from the packaged default.

    Using the packaged default as the base keeps these tests honest: they
    exercise the same offline, all-integrations-disabled configuration a real
    installation gets, with only the named keys changed.
    """
    packaged = (
        Path(__import__("software_agent_factory").__file__).parent / DEFAULT_CONFIG_FILENAME
    )
    payload = yaml.safe_load(packaged.read_text(encoding="utf-8"))
    payload["factory"]["data_dir"] = str(data_dir)
    for section, values in overrides.items():
        payload[section].update(values)  # type: ignore[union-attr]
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def passing_report() -> DoctorReport:
    return DoctorReport(
        checks=(CheckResult(name="git", status=CheckStatus.OK, message="git found"),)
    )


def failing_report() -> DoctorReport:
    return DoctorReport(
        checks=(
            CheckResult(
                name="gh",
                status=CheckStatus.ERROR,
                message="'gh' was not found on PATH",
                remediation="Install gh.",
            ),
        )
    )


def make_run(source_repo: Path, data_dir: Path, title: str = "Status task") -> str:
    result = runner.invoke(
        app,
        [
            "run",
            "--repo",
            str(source_repo),
            "--title",
            title,
            "--description",
            "A demonstration task",
            "--data-dir",
            str(data_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    return result.output.splitlines()[0].split(": ", 1)[1]


# -- factory doctor --------------------------------------------------------


def test_doctor_renders_human_readable_checks_and_a_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "run_doctor", lambda **_kwargs: passing_report())

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "git" in result.output
    assert "doctor: ok (0 error(s), 0 warning(s))" in result.output


def test_doctor_exits_nonzero_and_prints_remediation_when_a_check_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "run_doctor", lambda **_kwargs: failing_report())

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "FAIL" in result.output
    assert "Install gh." in result.output
    assert "doctor: failed (1 error(s), 0 warning(s))" in result.output


def test_doctor_json_output_is_machine_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "run_doctor", lambda **_kwargs: failing_report())

    result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["success"] is False
    assert payload["checks"][0]["name"] == "gh"


def test_doctor_passes_config_data_dir_and_runtime_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, data_dir: Path
) -> None:
    captured: dict[str, object] = {}

    def fake_run_doctor(**kwargs: object) -> DoctorReport:
        captured.update(kwargs)
        return passing_report()

    monkeypatch.setattr(cli, "run_doctor", fake_run_doctor)
    config_path = write_config(tmp_path / "factory.yaml", data_dir)

    result = runner.invoke(
        app,
        [
            "doctor",
            "--config",
            str(config_path),
            "--data-dir",
            str(data_dir),
            "--runtime",
            "copilot",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured == {
        "config_path": config_path,
        "data_dir_override": data_dir,
        "requested_runtime_copilot": True,
    }


def test_doctor_default_runtime_never_requests_copilot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_doctor(**kwargs: object) -> DoctorReport:
        captured.update(kwargs)
        return passing_report()

    monkeypatch.setattr(cli, "run_doctor", fake_run_doctor)

    assert runner.invoke(app, ["doctor"]).exit_code == 0
    assert captured["requested_runtime_copilot"] is False


@pytest.mark.allow_real_binaries
def test_doctor_end_to_end_passes_offline_with_only_git_required(
    tmp_path: Path, data_dir: Path
) -> None:
    """The real check pipeline against this host.

    Marked ``allow_real_binaries`` because doctor version-probes whichever of
    ``gh``/``copilot`` happen to exist on ``PATH``. That is a bounded
    ``--version`` subprocess, never a paid model call, and neither tool is
    *required* by this packaged (all-integrations-disabled) configuration.
    """
    config_path = write_config(tmp_path / "factory.yaml", data_dir)

    result = runner.invoke(
        app, ["doctor", "--json", "--config", str(config_path), "--data-dir", str(data_dir)]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    statuses = {check["name"]: check["status"] for check in payload["checks"]}
    assert payload["success"] is True
    assert statuses["git"] == "ok"
    assert statuses["gh"] != "error"
    assert statuses["copilot"] != "error"


# -- factory status --------------------------------------------------------


def test_status_reports_metrics_and_health_for_a_real_run(
    source_repo: Path, data_dir: Path
) -> None:
    run_id = make_run(source_repo, data_dir)

    result = runner.invoke(app, ["status", "--data-dir", str(data_dir)])

    assert result.exit_code == 0, result.output
    assert "runs: 1 total, 1 scanned" in result.output
    assert "1 succeeded" in result.output
    assert "first-pass success: 100% (1/1)" in result.output
    assert "status: complete" in result.output
    assert run_id in result.output


def test_status_json_contains_snapshot_and_health(
    source_repo: Path, data_dir: Path
) -> None:
    make_run(source_repo, data_dir)

    result = runner.invoke(app, ["status", "--json", "--data-dir", str(data_dir)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    MonitoringSnapshot.model_validate(payload["snapshot"])
    OperationalHealthReport.model_validate(payload["health"])
    assert payload["snapshot"]["counts"]["succeeded"] == 1
    assert payload["health"]["stale_runs"] == []


def test_status_never_creates_the_data_directory(tmp_path: Path) -> None:
    """``status`` is read-only: it may not even bring the store into
    existence, let alone mutate a run."""
    absent = tmp_path / "not-created"

    result = runner.invoke(app, ["status", "--data-dir", str(absent)])

    assert result.exit_code == 0, result.output
    assert not absent.exists()
    assert "runs: 0 total" in result.output


def test_status_leaves_persisted_runs_untouched(source_repo: Path, data_dir: Path) -> None:
    make_run(source_repo, data_dir)
    before = {
        path: path.read_bytes() for path in sorted(data_dir.rglob("*")) if path.is_file()
    }

    assert runner.invoke(app, ["status", "--data-dir", str(data_dir)]).exit_code == 0

    after = {
        path: path.read_bytes() for path in sorted(data_dir.rglob("*")) if path.is_file()
    }
    assert after == before


def test_status_reports_a_truncated_scan_as_degraded(
    source_repo: Path, data_dir: Path
) -> None:
    make_run(source_repo, data_dir, title="First")
    make_run(source_repo, data_dir, title="Second")

    result = runner.invoke(
        app, ["status", "--data-dir", str(data_dir), "--max-scanned-runs", "1"]
    )

    assert result.exit_code == 0, result.output
    assert "scan truncated" in result.output
    assert "status: DEGRADED (this report is partial)" in result.output


def test_status_pagination_is_bounded_by_limit_and_offset(
    source_repo: Path, data_dir: Path
) -> None:
    make_run(source_repo, data_dir, title="First")
    make_run(source_repo, data_dir, title="Second")

    result = runner.invoke(app, ["status", "--data-dir", str(data_dir), "--limit", "1"])

    assert result.exit_code == 0, result.output
    assert "runs (showing 1 of 2, offset 0):" in result.output
    assert "more runs available" in result.output


def test_status_rejects_a_nonpositive_limit(data_dir: Path) -> None:
    result = runner.invoke(app, ["status", "--data-dir", str(data_dir), "--limit", "0"])

    assert result.exit_code == 2
    assert "Traceback" not in result.output


def test_status_uses_the_configured_stall_timeout_as_the_stale_threshold(
    tmp_path: Path, data_dir: Path
) -> None:
    config_path = write_config(
        tmp_path / "factory.yaml",
        data_dir,
        scheduler={"poll_interval_seconds": 30, "stall_timeout_seconds": 1234},
    )

    result = runner.invoke(app, ["status", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    assert "stale threshold: 1234s" in result.output


def test_status_stale_threshold_can_be_overridden(data_dir: Path) -> None:
    result = runner.invoke(
        app, ["status", "--data-dir", str(data_dir), "--stale-after-seconds", "7"]
    )

    assert result.exit_code == 0, result.output
    assert "stale threshold: 7s" in result.output


# -- factory dashboard -----------------------------------------------------


class FakeDashboardServer:
    """Stands in for a bound :class:`DashboardServer` without a socket."""

    def __init__(self, config: object) -> None:
        self.config = config
        self.token = "test-token"
        self.dashboard_url = "http://127.0.0.1:8765/?token=test-token"
        self.served = False
        self.closed = False
        self.was_shut_down = False
        self.interrupt = False

    def serve_forever(self) -> None:
        self.served = True
        if self.interrupt:
            raise KeyboardInterrupt

    def shutdown(self) -> None:
        self.was_shut_down = True

    def server_close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_dashboard(monkeypatch: pytest.MonkeyPatch) -> list[FakeDashboardServer]:
    created: list[FakeDashboardServer] = []

    def fake_create_server(config: object) -> FakeDashboardServer:
        server = FakeDashboardServer(config)
        created.append(server)
        return server

    monkeypatch.setattr(cli, "create_server", fake_create_server)
    return created


def test_dashboard_binds_loopback_on_the_default_port_and_prints_the_token_url(
    data_dir: Path, fake_dashboard: list[FakeDashboardServer]
) -> None:
    result = runner.invoke(app, ["dashboard", "--data-dir", str(data_dir)])

    assert result.exit_code == 0, result.output
    server = fake_dashboard[0]
    assert server.config.host == "127.0.0.1"
    assert server.config.port == cli.DEFAULT_DASHBOARD_PORT == 8765
    assert "http://127.0.0.1:8765/?token=test-token" in result.output
    assert "read-only, loopback only" in result.output
    assert server.served is True
    assert server.closed is True


def test_dashboard_accepts_an_ephemeral_port(
    data_dir: Path, fake_dashboard: list[FakeDashboardServer]
) -> None:
    result = runner.invoke(app, ["dashboard", "--data-dir", str(data_dir), "--port", "0"])

    assert result.exit_code == 0, result.output
    assert fake_dashboard[0].config.port == 0


def test_dashboard_rejects_an_out_of_range_port(data_dir: Path) -> None:
    result = runner.invoke(app, ["dashboard", "--data-dir", str(data_dir), "--port", "99999"])

    assert result.exit_code == 2
    assert "Traceback" not in result.output


def test_dashboard_does_not_open_a_browser_by_default(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path, fake_dashboard: list[FakeDashboardServer]
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url))

    assert runner.invoke(app, ["dashboard", "--data-dir", str(data_dir)]).exit_code == 0

    assert opened == []


def test_dashboard_open_browser_flag_opens_the_tokenized_url(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path, fake_dashboard: list[FakeDashboardServer]
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: opened.append(url))

    result = runner.invoke(
        app, ["dashboard", "--data-dir", str(data_dir), "--open-browser"]
    )

    assert result.exit_code == 0, result.output
    assert opened == ["http://127.0.0.1:8765/?token=test-token"]


def test_dashboard_handles_ctrl_c_cleanly(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path
) -> None:
    servers: list[FakeDashboardServer] = []

    def fake_create_server(config: object) -> FakeDashboardServer:
        server = FakeDashboardServer(config)
        server.interrupt = True
        servers.append(server)
        return server

    monkeypatch.setattr(cli, "create_server", fake_create_server)

    result = runner.invoke(app, ["dashboard", "--data-dir", str(data_dir)])

    assert result.exit_code == 0, result.output
    assert "stopping dashboard..." in result.output
    assert servers[0].was_shut_down is True
    assert servers[0].closed is True


def test_dashboard_reports_a_bind_failure_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path
) -> None:
    def refuse(_config: object) -> None:
        raise OSError("address already in use")

    monkeypatch.setattr(cli, "create_server", refuse)

    result = runner.invoke(app, ["dashboard", "--data-dir", str(data_dir), "--port", "8765"])

    assert result.exit_code == 2
    assert "could not bind the dashboard to 127.0.0.1:8765" in result.output
    assert "Traceback" not in result.output


def test_dashboard_providers_serve_real_snapshot_health_and_detail(
    source_repo: Path, data_dir: Path, fake_dashboard: list[FakeDashboardServer]
) -> None:
    run_id = make_run(source_repo, data_dir)

    assert runner.invoke(app, ["dashboard", "--data-dir", str(data_dir)]).exit_code == 0
    config = fake_dashboard[0].config

    snapshot = config.snapshot_provider(limit=10, offset=0)
    assert isinstance(snapshot, MonitoringSnapshot)
    assert snapshot.counts.succeeded == 1

    health = config.health_provider()
    assert isinstance(health, OperationalHealthReport)
    assert health.stale_runs == []

    detail = config.run_detail_provider(run_id)
    assert isinstance(detail, RunDetail)
    assert detail.run_id == run_id
    assert [attempt.role for attempt in detail.attempts]


def test_dashboard_detail_provider_returns_none_for_unknown_or_hostile_ids(
    data_dir: Path, fake_dashboard: list[FakeDashboardServer]
) -> None:
    assert runner.invoke(app, ["dashboard", "--data-dir", str(data_dir)]).exit_code == 0
    provider = fake_dashboard[0].config.run_detail_provider

    assert provider("run-does-not-exist") is None
    assert provider("../../etc/passwd") is None


def test_dashboard_detail_never_exposes_logs_diffs_or_failure_text(
    source_repo: Path, data_dir: Path, fake_dashboard: list[FakeDashboardServer]
) -> None:
    run_id = make_run(source_repo, data_dir)
    assert runner.invoke(app, ["dashboard", "--data-dir", str(data_dir)]).exit_code == 0

    detail = fake_dashboard[0].config.run_detail_provider(run_id)
    payload = detail.model_dump(mode="json")

    forbidden = {"failure_reason", "reasoning", "logs", "patch", "diff", "prompt", "output"}
    assert forbidden.isdisjoint(payload)
    for attempt in payload["attempts"]:
        assert forbidden.isdisjoint(attempt)


def test_dashboard_uses_the_configured_stall_timeout_as_the_stale_threshold(
    tmp_path: Path, data_dir: Path, fake_dashboard: list[FakeDashboardServer]
) -> None:
    config_path = write_config(
        tmp_path / "factory.yaml",
        data_dir,
        scheduler={"poll_interval_seconds": 30, "stall_timeout_seconds": 4321},
    )

    assert runner.invoke(app, ["dashboard", "--config", str(config_path)]).exit_code == 0

    snapshot = fake_dashboard[0].config.snapshot_provider(limit=1, offset=0)
    assert snapshot.stale_after_seconds == 4321.0


def test_run_never_starts_a_dashboard(
    monkeypatch: pytest.MonkeyPatch, source_repo: Path, data_dir: Path
) -> None:
    """The dashboard is reachable only through its own command (ADR-016)."""

    def explode(_config: object) -> None:
        raise AssertionError("factory run must never start a dashboard server")

    monkeypatch.setattr(cli, "create_server", explode)

    make_run(source_repo, data_dir)


# -- factory service -------------------------------------------------------


@pytest.fixture
def macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_current_system", lambda: "Darwin")


@pytest.fixture
def launch_agents_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect every LaunchAgents lookup at a temp directory, so no test can
    read or write the operator's real ``~/Library/LaunchAgents``."""
    directory = tmp_path / "LaunchAgents"
    directory.mkdir()
    monkeypatch.setattr(cli, "default_launch_agents_dir", lambda: directory)
    return directory


@pytest.fixture
def executable(tmp_path: Path) -> Path:
    path = tmp_path / "factory"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def scheduler_config(tmp_path: Path, data_dir: Path) -> Path:
    return write_config(
        tmp_path / "service-factory.yaml",
        data_dir,
        scheduler={"enabled": True, "poll_interval_seconds": 45},
    )


def install_args(
    source_repo: Path, config_path: Path, executable: Path, *extra: str
) -> list[str]:
    return [
        "service",
        "install",
        "--repo",
        str(source_repo),
        "--github-repo",
        "acme/repo",
        "--config",
        str(config_path),
        "--executable",
        str(executable),
        *extra,
    ]


@pytest.mark.parametrize(
    "command",
    [
        ["install", "--repo", ".", "--github-repo", "acme/repo"],
        ["status"],
        ["uninstall"],
    ],
    ids=["install", "status", "uninstall"],
)
def test_service_commands_refuse_to_run_off_macos(
    monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    monkeypatch.setattr(cli, "_current_system", lambda: "Linux")

    result = runner.invoke(app, ["service", *command])

    assert result.exit_code == 2
    assert "cannot run on Linux" in result.output


def test_service_install_refuses_when_the_scheduler_is_disabled(
    macos: None,
    launch_agents_dir: Path,
    source_repo: Path,
    executable: Path,
    tmp_path: Path,
    data_dir: Path,
) -> None:
    config_path = write_config(tmp_path / "disabled.yaml", data_dir)

    result = runner.invoke(app, install_args(source_repo, config_path, executable))

    assert result.exit_code == 2
    assert "disabled scheduler" in result.output
    assert list(launch_agents_dir.iterdir()) == []


def test_service_install_refuses_when_doctor_reports_errors(
    monkeypatch: pytest.MonkeyPatch,
    macos: None,
    launch_agents_dir: Path,
    source_repo: Path,
    executable: Path,
    scheduler_config: Path,
) -> None:
    monkeypatch.setattr(cli, "run_doctor", lambda **_kwargs: failing_report())
    monkeypatch.setattr(
        cli,
        "install_service",
        lambda *_a, **_k: pytest.fail("install must not run after a failed preflight"),
    )

    result = runner.invoke(app, install_args(source_repo, scheduler_config, executable))

    assert result.exit_code == 2
    assert "'factory doctor' reports errors" in result.output
    assert "'gh' was not found on PATH" in result.output


def test_service_install_passes_configured_values_to_the_installer(
    monkeypatch: pytest.MonkeyPatch,
    macos: None,
    launch_agents_dir: Path,
    source_repo: Path,
    executable: Path,
    scheduler_config: Path,
    data_dir: Path,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "run_doctor", lambda **_kwargs: passing_report())

    def fake_install(request: ServiceInstallRequest, **kwargs: object) -> ServiceStatus:
        captured["request"] = request
        captured["launch_agents_dir"] = kwargs["launch_agents_dir"]
        return ServiceStatus(
            label=request.label,
            plist_path=launch_agents_dir / f"{request.label}.plist",
            installed=True,
            loaded=True,
            detail="loaded",
        )

    monkeypatch.setattr(cli, "install_service", fake_install)

    result = runner.invoke(app, install_args(source_repo, scheduler_config, executable))

    assert result.exit_code == 0, result.output
    request = captured["request"]
    assert isinstance(request, ServiceInstallRequest)
    assert request.executable == executable
    assert request.repo == source_repo
    assert request.github_repo == "acme/repo"
    assert request.data_dir == data_dir
    assert request.config_path == scheduler_config.resolve()
    assert request.poll_interval_seconds == 45
    assert request.runtime is ServiceRuntime.FAKE
    assert request.label == DEFAULT_LABEL
    assert request.allow_source_dev is False
    assert captured["launch_agents_dir"] == launch_agents_dir
    assert "installed service" in result.output
    assert "runtime: fake" in result.output
    assert "poll interval: 45s" in result.output
    assert "fake runtime still persists completed runs" in result.output


def test_service_install_runtime_and_flags_are_forwarded(
    monkeypatch: pytest.MonkeyPatch,
    macos: None,
    launch_agents_dir: Path,
    source_repo: Path,
    executable: Path,
    scheduler_config: Path,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_doctor(**kwargs: object) -> DoctorReport:
        captured.update(kwargs)
        return passing_report()

    def fake_install(request: ServiceInstallRequest, **_kwargs: object) -> ServiceStatus:
        captured["request"] = request
        return ServiceStatus(
            label=request.label,
            plist_path=launch_agents_dir / f"{request.label}.plist",
            installed=True,
            loaded=True,
            detail="loaded",
        )

    monkeypatch.setattr(cli, "run_doctor", fake_run_doctor)
    monkeypatch.setattr(cli, "install_service", fake_install)

    result = runner.invoke(
        app,
        install_args(
            source_repo,
            scheduler_config,
            executable,
            "--runtime",
            "copilot",
            "--allow-source-dev",
            "--label",
            "com.example.factory-test",
            "--json",
        ),
    )

    assert result.exit_code == 0, result.output
    assert captured["requested_runtime_copilot"] is True
    request = captured["request"]
    assert request.runtime is ServiceRuntime.COPILOT
    assert request.allow_source_dev is True
    assert request.label == "com.example.factory-test"
    payload = json.loads(result.output)
    assert payload["label"] == "com.example.factory-test"
    assert payload["runtime"] == "copilot"


def test_service_install_defaults_to_the_fake_runtime(
    monkeypatch: pytest.MonkeyPatch,
    macos: None,
    launch_agents_dir: Path,
    source_repo: Path,
    executable: Path,
    scheduler_config: Path,
) -> None:
    """An installed-but-forgotten agent must not be able to spend money."""
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "run_doctor", lambda **_kwargs: passing_report())

    def fake_install(request: ServiceInstallRequest, **_kwargs: object) -> ServiceStatus:
        captured["request"] = request
        return ServiceStatus(
            label=request.label,
            plist_path=launch_agents_dir / "x.plist",
            installed=True,
            loaded=True,
            detail="",
        )

    monkeypatch.setattr(cli, "install_service", fake_install)

    assert (
        runner.invoke(app, install_args(source_repo, scheduler_config, executable)).exit_code
        == 0
    )

    assert "--runtime" in build_program_arguments(captured["request"])
    assert build_program_arguments(captured["request"])[-1] == "fake"


def test_service_install_refuses_a_relative_repo_path(
    monkeypatch: pytest.MonkeyPatch,
    macos: None,
    launch_agents_dir: Path,
    executable: Path,
    scheduler_config: Path,
) -> None:
    """The real installer's path validation runs; it must refuse before it
    writes a plist or invokes ``launchctl``."""
    monkeypatch.setattr(cli, "run_doctor", lambda **_kwargs: passing_report())

    result = runner.invoke(
        app,
        [
            "service",
            "install",
            "--repo",
            "relative-repo",
            "--github-repo",
            "acme/repo",
            "--config",
            str(scheduler_config),
            "--executable",
            str(executable),
        ],
    )

    assert result.exit_code == 2
    assert "service install refused" in result.output
    assert "absolute path" in result.output
    assert list(launch_agents_dir.iterdir()) == []


def test_service_program_arguments_match_the_start_command_options(
    monkeypatch: pytest.MonkeyPatch, source_repo: Path, tmp_path: Path, data_dir: Path
) -> None:
    """Everything the launchd plist will pass back must be a real ``factory
    start`` option, or the service silently fails at load time."""
    request = ServiceInstallRequest(
        executable=tmp_path / "factory",
        repo=source_repo,
        github_repo="acme/repo",
        data_dir=data_dir,
        config_path=tmp_path / "factory.yaml",
        poll_interval_seconds=45,
    )
    arguments = build_program_arguments(request)

    assert arguments[1] == "start"
    help_output = runner.invoke(app, ["start", "--help"]).output
    for token in arguments:
        if token.startswith("--"):
            assert token in help_output


def test_service_status_is_read_only_and_renders_both_shapes(
    monkeypatch: pytest.MonkeyPatch, macos: None, launch_agents_dir: Path
) -> None:
    status = ServiceStatus(
        label=DEFAULT_LABEL,
        plist_path=launch_agents_dir / f"{DEFAULT_LABEL}.plist",
        installed=True,
        loaded=False,
        detail="Could not find service",
    )
    monkeypatch.setattr(cli, "get_service_status", lambda *_a, **_k: status)

    human = runner.invoke(app, ["service", "status"])
    assert human.exit_code == 0, human.output
    assert f"label: {DEFAULT_LABEL}" in human.output
    assert "installed: yes" in human.output
    assert "loaded: no" in human.output

    machine = runner.invoke(app, ["service", "status", "--json"])
    assert machine.exit_code == 0, machine.output
    payload = json.loads(machine.output)
    assert payload["installed"] is True
    assert payload["loaded"] is False
    assert list(launch_agents_dir.iterdir()) == []


def test_service_uninstall_reports_removal_and_preserves_history(
    monkeypatch: pytest.MonkeyPatch, macos: None, launch_agents_dir: Path
) -> None:
    monkeypatch.setattr(cli, "uninstall_service", lambda *_a, **_k: True)

    result = runner.invoke(app, ["service", "uninstall"])

    assert result.exit_code == 0, result.output
    assert "removed LaunchAgent" in result.output
    assert "runs and workspaces were left on disk" in result.output


def test_service_uninstall_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, macos: None, launch_agents_dir: Path
) -> None:
    monkeypatch.setattr(cli, "uninstall_service", lambda *_a, **_k: False)

    result = runner.invoke(app, ["service", "uninstall", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"label": DEFAULT_LABEL, "removed": False}


def test_no_command_installs_a_service_as_a_side_effect(
    monkeypatch: pytest.MonkeyPatch, source_repo: Path, data_dir: Path
) -> None:
    """ADR-018: installation happens only because someone typed the command."""

    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a service must never be installed as a side effect")

    monkeypatch.setattr(cli, "install_service", explode)

    make_run(source_repo, data_dir)
    assert runner.invoke(app, ["status", "--data-dir", str(data_dir)]).exit_code == 0
    assert runner.invoke(app, ["runs", "--data-dir", str(data_dir)]).exit_code == 0
