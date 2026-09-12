"""The ``factory`` command-line interface (``PLAN.md`` Phases 1-15).

```bash
factory --version
factory run --repo PATH --title TEXT --description TEXT [--runtime fake|copilot]
factory project --repo PATH --title TEXT --description TEXT [--runtime fake|copilot]
factory runs
factory show RUN_ID
factory start --repo PATH --github-repo OWNER/NAME [--once]
factory doctor [--json]
factory status [--json]
factory dashboard [--port 8765] [--open-browser]
factory service install|status|uninstall
factory skill path|validate|refresh --repo PATH [--runtime fake|copilot]
```

``--runtime`` defaults to ``fake`` so no command ever makes a paid model call
by accident; ``--runtime copilot`` opts in to the real
:class:`~software_agent_factory.copilot_runtime.CopilotAgentRuntime`.

Pull request creation, CI observation, the backlog daemon, the dashboard and
the launchd service are all strictly opt-in (``pull_request.enabled``,
``ci.enabled``, ``scheduler.enabled``, and an explicit ``factory dashboard`` /
``factory service install`` command). With the packaged defaults, ``factory
run`` performs no network access at all and finishes at ``PR_READY``.

``factory skill`` is the human-facing view of repository-adaptive guidance:
``skill path`` and ``skill validate`` are read-only, and ``skill refresh`` is
the only command that generates guidance on request. None of them ever
writes, normalizes or deletes the human-owned overlay file, and none of them
creates a :class:`~software_agent_factory.models.FactoryRun` or a worktree.

``--data-dir`` overrides the configured data directory so tests and demos can
point the CLI at an isolated temporary directory without editing a config
file.

Three conventions hold across every command here:

- **Fail before you work.** Configuration problems and missing external
  prerequisites (``git``, and ``gh``/``copilot`` only when the requested
  feature set needs them) exit with :data:`CONFIG_ERROR_EXIT_CODE` and one
  explicit line, never a traceback from deep inside a workspace or tracker.
- **Read-only stays read-only.** ``runs``, ``show``, ``status``, ``skill
  path`` and ``skill validate`` derive everything from persisted artifacts
  and never create or mutate a run, a workspace or configuration -- not even
  the data directory itself.
- **Structured logs where work happens.** ``run``, ``start`` and
  ``dashboard`` attach the bounded rotating JSON log under
  ``<data_dir>/logs`` once the configuration and data directory are
  resolved. The dashboard token is printed to stdout and never logged.
"""

from __future__ import annotations

import importlib
import logging
import platform
import sys
import webbrowser
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

if TYPE_CHECKING:
    from datetime import timedelta

    from .agents import AgentRuntime
    from .config import FactoryConfig
    from .models import (
        InvocationRecord,
        RepositoryProfile,
        RepositorySkill,
        WorkItem,
    )
    from .repository_skills import RepositorySkillManager

app = typer.Typer(help="Local-first autonomous software engineering factory.")
service_app = typer.Typer(
    help="Manage the opt-in per-user macOS launchd service (never automatic)."
)
app.add_typer(service_app, name="service")
skill_app = typer.Typer(
    help="Inspect, validate and refresh this repository's generated skill and overlay."
)
app.add_typer(skill_app, name="skill")

logger = logging.getLogger(__name__)

#: States that mean "the factory finished this work item successfully".
SUCCESS_STATES: frozenset[str] = frozenset({"PR_READY", "DONE"})

#: Exit code for "you asked for something this environment or configuration
#: cannot do": invalid/unloadable configuration, a disabled feature, a missing
#: external prerequisite, an unusable port, or a refused service install.
CONFIG_ERROR_EXIT_CODE = 2

#: Exit code for "the command ran, and the answer is no": a run that did not
#: reach a success state, an unknown run id, or a doctor report with errors.
FAILURE_EXIT_CODE = 1

#: Default dashboard port. Fixed and memorable so a bookmark keeps working
#: across restarts; ``--port 0`` asks the OS for an ephemeral free port.
DEFAULT_DASHBOARD_PORT = 8765

#: Default page size for ``factory status``. Small enough to stay readable in
#: a terminal; ``--limit``/``--offset`` page through the rest.
DEFAULT_STATUS_LIMIT = 20

#: Default cap on scanned runs.
DEFAULT_MAX_SCANNED_RUNS = 1000

#: Reverse-DNS style label for the installed LaunchAgent.
DEFAULT_LABEL = "com.github.software-agent-factory"

#: Neutral working directory (under the data directory) that ``factory skill
#: refresh`` runs the skill researcher from. Repository-level guidance is
#: produced from the normalized profile alone, so the researcher must never
#: run inside the repository, a worktree or the operator's shell cwd.
SKILL_GENERATION_DIRNAME = "skill-generation"

_DEFERRED_EXPORTS: dict[str, tuple[str, str]] = {
    "CopilotAgentRuntime": (".copilot_runtime", "CopilotAgentRuntime"),
    "WorkflowController": (".workflow", "WorkflowController"),
    "ProjectRunner": (".projects", "ProjectRunner"),
    "run_doctor": (".doctor", "run_doctor"),
    "missing_prerequisites": (".doctor", "missing_prerequisites"),
    "create_server": (".dashboard", "create_server"),
    "default_launch_agents_dir": (".service_install", "default_launch_agents_dir"),
    "install_service": (".service_install", "install_service"),
    "get_service_status": (".service_install", "get_service_status"),
    "uninstall_service": (".service_install", "uninstall_service"),
    "FakeAgentRuntime": (".agents", "FakeAgentRuntime"),
}


def __getattr__(name: str) -> Any:
    if name in _DEFERRED_EXPORTS:
        module_path, attr_name = _DEFERRED_EXPORTS[name]
        module = importlib.import_module(module_path, package=__package__)
        attr = getattr(module, attr_name)
        globals()[name] = attr
        return attr
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _seam(name: str) -> Any:
    """Return a monkeypatchable symbol, looking in this module's globals or
    triggering deferred resolution via __getattr__."""
    return getattr(sys.modules[__name__], name)


class RuntimeChoice(StrEnum):
    FAKE = "fake"
    COPILOT = "copilot"


class PerformanceModeChoice(StrEnum):
    STANDARD = "standard"
    FAST = "fast"


def _current_system() -> str:
    """The host OS name. A function (not an inline ``platform.system()``
    call) so macOS-only commands can be exercised deterministically from any
    development platform."""
    return platform.system()


def _fail(message: str, *, code: int = CONFIG_ERROR_EXIT_CODE) -> typer.Exit:
    """Print ``message`` to stderr and return an ``Exit`` for the caller to
    raise. Returning rather than raising keeps ``raise _fail(...)`` explicit
    at the call site, so a reader always sees the control flow."""
    typer.echo(message, err=True)
    return typer.Exit(code=code)


def _load_config(
    config: Path | None,
    data_dir: Path | None,
    model_profile: str | None = None,
    performance_mode: PerformanceModeChoice | None = None,
) -> FactoryConfig:
    """Load configuration, applying an optional ``--data-dir`` override.

    Every expected failure mode -- a missing file, an unreadable file,
    malformed YAML, or a schema violation -- becomes one explicit stderr line
    and :data:`CONFIG_ERROR_EXIT_CODE`, never a traceback.
    """
    label = str(config) if config is not None else "(packaged default)"
    import yaml
    from pydantic import ValidationError

    from .config import load_config

    try:
        loaded = load_config(config, model_profile=model_profile)
    except FileNotFoundError:
        raise _fail(f"config file not found: {label}") from None
    except OSError as exc:
        raise _fail(f"config file at {label} could not be read: {exc}") from None
    except yaml.YAMLError as exc:
        raise _fail(f"config at {label} is not valid YAML: {exc}") from None
    except (ValidationError, ValueError) as exc:
        raise _fail(f"config at {label} is invalid: {exc}") from None

    if data_dir is not None:
        loaded = loaded.model_copy(
            update={
                "factory": loaded.factory.model_copy(update={"data_dir": data_dir.expanduser()})
            }
        )
    if performance_mode is not None:
        loaded = loaded.model_copy(
            update={
                "performance": loaded.performance.model_copy(
                    update={"mode": performance_mode.value}
                )
            }
        )
        if (
            loaded.performance.mode == "fast"
            and loaded.performance.fast_model_profile not in loaded.model_profiles
        ):
            raise _fail(
                "fast performance mode requires performance.fast_model_profile "
                "to name a configured model profile"
            )
    return loaded


def _require_prerequisites(*, require_gh: bool, require_copilot: bool) -> None:
    """Refuse to start work when a required external executable is absent.

    Uses the same ``PATH`` lookup ``factory doctor`` uses
    (:func:`~software_agent_factory.doctor.missing_prerequisites`), so the
    two can never disagree, and runs before any workspace, tracker or agent
    code -- the alternative is a traceback from a failed ``git`` exec several
    layers down.
    """
    missing_checker = _seam("missing_prerequisites")
    missing = missing_checker(require_gh=require_gh, require_copilot=require_copilot)
    if not missing:
        return
    raise _fail(
        f"missing required executable(s) on PATH: {', '.join(missing)}. "
        "Install them and retry; 'factory doctor' explains each requirement."
    )


def _configure_logging(config: FactoryConfig) -> None:
    """Attach the bounded structured log under ``<data_dir>/logs``.

    A logging destination that cannot be created is reported as a warning
    rather than aborting the command: losing the on-disk log copy must never
    stop the factory (or the read-only dashboard) from running.
    """
    try:
        from .observability import configure_factory_logging

        configure_factory_logging(config.data_dir)
    except OSError as exc:
        typer.echo(f"warning: could not open the structured log: {exc}", err=True)


def _build_runtime(choice: RuntimeChoice) -> AgentRuntime:
    if choice is RuntimeChoice.COPILOT:
        runtime_cls = _seam("CopilotAgentRuntime")
        return runtime_cls()  # type: ignore[no-any-return]
    fake_runtime_cls = _seam("FakeAgentRuntime")
    return fake_runtime_cls()  # type: ignore[no-any-return]


def _warn_fake_backlog_claims() -> None:
    """Explain the non-obvious consequence of polling with the fake runtime."""
    typer.echo(
        "warning: the fake runtime still persists completed runs, so matching "
        "backlog items will not be dispatched again automatically. Use it only "
        "for a deliberate dry run, or select --runtime copilot before polling "
        "real agent-ready issues.",
        err=True,
    )


def _delivery_target_text(repository: str | None, base_branch: str | None) -> str:
    """Describe where a merge landed, degrading gracefully when unrecorded."""
    branch = base_branch or "(unknown branch)"
    return f"{repository}@{branch}" if repository else branch


def _stale_after(config: FactoryConfig, override_seconds: int | None) -> timedelta:
    """Staleness threshold for monitoring surfaces.

    Defaults to the configured scheduler stall timeout, so "stale" means the
    same thing to ``factory status``, the dashboard and the scheduler's own
    stall detection instead of being an independently drifting constant.
    """
    from datetime import timedelta

    seconds = (
        override_seconds
        if override_seconds is not None
        else (config.scheduler.stall_timeout_seconds)
    )
    return timedelta(seconds=seconds)


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show the factory version and exit.",
        is_eager=True,
    ),
) -> None:
    """Print the version, or show help when invoked with no subcommand.

    The exact line ``python -m software_agent_factory --version`` and the
    installed console script print, resolved once in ``version.py`` so the
    frozen bundle, the wheel and a source checkout can never disagree.
    """
    if version:
        from .version import format_version_line

        typer.echo(format_version_line())
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()


@app.command("run")
def run_command(
    repo: Path = typer.Option(..., "--repo", help="Path to the target Git repository."),
    title: str = typer.Option(..., "--title", help="Short title for the work item."),
    description: str = typer.Option(
        ..., "--description", help="Description of the work to perform."
    ),
    acceptance_criteria: list[str] | None = typer.Option(
        None,
        "--acceptance-criterion",
        help="Required work item outcome. Repeat for multiple criteria.",
    ),
    constraints: list[str] | None = typer.Option(
        None,
        "--constraint",
        help="Work item constraint. Repeat for multiple constraints.",
    ),
    work_item_id: str = typer.Option(
        None,
        "--work-item-id",
        help=(
            "Explicit work item id. Use a stable id (e.g. the scheduler's "
            "'tracker-owner/repo#12') so a manual run and the daemon cannot "
            "duplicate the same work. Defaults to a random ad hoc id."
        ),
    ),
    runtime: RuntimeChoice = typer.Option(
        RuntimeChoice.FAKE,
        "--runtime",
        help="Agent runtime: 'fake' (default, no model calls) or 'copilot' (paid).",
    ),
    model_profile: str = typer.Option(
        "default",
        "--model-profile",
        help="Configured model profile to use (default: top-level models block).",
    ),
    performance_mode: PerformanceModeChoice | None = typer.Option(
        None,
        "--performance-mode",
        help="Workflow performance mode override: 'standard' or eligible low-risk 'fast'.",
    ),
    config: Path = typer.Option(
        None, "--config", help="Path to a factory config YAML file (default: packaged config)."
    ),
    data_dir: Path = typer.Option(
        None, "--data-dir", help="Override the configured data directory."
    ),
) -> None:
    """Run one work item synchronously through the factory workflow."""
    from uuid import uuid4

    from .models import ChangeSet, WorkItem
    from .store import FileRunStore

    factory_config = _load_config(config, data_dir, model_profile, performance_mode)
    # A manual run needs ``gh`` only for the publishing/CI features it would
    # actually reach; the scheduler is irrelevant here, so an offline default
    # run requires nothing but ``git``.
    _require_prerequisites(
        require_gh=factory_config.pull_request.enabled or factory_config.ci.enabled,
        require_copilot=runtime is RuntimeChoice.COPILOT,
    )
    _configure_logging(factory_config)

    store = FileRunStore(factory_config.data_dir)
    controller_cls = _seam("WorkflowController")
    controller = controller_cls(factory_config, store, _build_runtime(runtime))

    work_item = WorkItem(
        id=work_item_id or f"WI-{uuid4().hex[:12]}",
        title=title,
        description=description,
        acceptance_criteria=acceptance_criteria or [],
        constraints=constraints or [],
    )

    run = controller.run(work_item, repo)

    typer.echo(f"run id: {run.id}")
    typer.echo(f"state: {run.state}")
    if run.workspace_path is not None:
        typer.echo(f"workspace: {run.workspace_path}")
    if run.commit_sha is not None:
        typer.echo(f"commit: {run.commit_sha}")
    if run.pull_request_url is not None:
        typer.echo(f"pull request: {run.pull_request_url}")
    # DONE alone does not mean the change reached the target branch, so the
    # merge commit and where it landed are reported explicitly.
    if run.merge_commit_sha is not None:
        typer.echo(f"merged commit: {run.merge_commit_sha}")
        target = _delivery_target_text(run.delivery_repository, run.delivery_base_branch)
        typer.echo(f"merged into: {target}")
    elif run.pull_request_url is not None:
        typer.echo("merged: no (the pull request was not merged by this run)")
    if run.failure_reason is not None:
        typer.echo(f"reason: {run.failure_reason}")

    try:
        change_set = store.load_artifact(run.id, ChangeSet)
    except FileNotFoundError:
        change_set = None
    if change_set is not None:
        typer.echo(f"changed files: {', '.join(change_set.changed_files) or '(none)'}")

    if run.state not in SUCCESS_STATES:
        raise typer.Exit(code=FAILURE_EXIT_CODE)


@app.command("project")
def project_command(
    repo: Path = typer.Option(..., "--repo", help="Path to the target Git repository."),
    title: str = typer.Option(
        None, "--title", help="Short title for the project. Required unless --resume."
    ),
    description: str = typer.Option(
        None,
        "--description",
        help="High-level description of what to build. Required unless --resume.",
    ),
    acceptance_criteria: list[str] | None = typer.Option(
        None,
        "--acceptance-criterion",
        help="Required project outcome. Repeat for multiple criteria.",
    ),
    constraints: list[str] | None = typer.Option(
        None,
        "--constraint",
        help="Project constraint. Repeat for multiple constraints.",
    ),
    project_id: str = typer.Option(
        None,
        "--project-id",
        help="Stable project id. Defaults to a generated id.",
    ),
    resume: bool = typer.Option(
        False,
        "--resume",
        help=(
            "Continue an interrupted project from its persisted brief, immutable plan "
            "and recorded task evidence. Requires --project-id. Never replans, never "
            "resets a retry budget and never repeats delivered work."
        ),
    ),
    github_repo: str = typer.Option(
        None,
        "--github-repo",
        help=(
            "Optional GitHub repository in OWNER/NAME form. Creates one issue per validated "
            "task and closes it after successful integration."
        ),
    ),
    runtime: RuntimeChoice = typer.Option(
        RuntimeChoice.FAKE,
        "--runtime",
        help="Agent runtime: 'fake' (default, no model calls) or 'copilot' (paid).",
    ),
    model_profile: str = typer.Option(
        "default",
        "--model-profile",
        help="Configured model profile to use (default: top-level models block).",
    ),
    performance_mode: PerformanceModeChoice | None = typer.Option(
        None,
        "--performance-mode",
        help="Workflow performance mode override: 'standard' or eligible low-risk 'fast'.",
    ),
    config: Path = typer.Option(
        None, "--config", help="Path to a factory config YAML file (default: packaged config)."
    ),
    data_dir: Path = typer.Option(
        None, "--data-dir", help="Override the configured data directory."
    ),
) -> None:
    """Derive the smallest sufficient work plan and execute it to completion."""
    if resume:
        if project_id is None:
            raise _fail("--resume requires --project-id")
        for name, value in (
            ("--title", title),
            ("--description", description),
            ("--github-repo", github_repo),
        ):
            if value is not None:
                raise _fail(f"{name} cannot be combined with --resume; the stored project wins")
        if acceptance_criteria or constraints:
            raise _fail(
                "--acceptance-criterion and --constraint cannot be combined with --resume; "
                "the stored project brief is authoritative"
            )
    else:
        if title is None or description is None:
            raise _fail("--title and --description are required unless --resume is used")

    factory_config = _load_config(config, data_dir, model_profile, performance_mode)
    _require_prerequisites(
        require_gh=(
            github_repo is not None
            or factory_config.pull_request.enabled
            or factory_config.ci.enabled
            or factory_config.merge.enabled
        ),
        require_copilot=runtime is RuntimeChoice.COPILOT,
    )
    _configure_logging(factory_config)

    from uuid import uuid4

    from .models import ProjectBrief, ProjectState
    from .projects import FileProjectStore, ProjectError
    from .store import FileRunStore

    run_store = FileRunStore(factory_config.data_dir)
    runner_cls = _seam("ProjectRunner")
    try:
        project_runner = runner_cls(
            factory_config,
            run_store,
            _build_runtime(runtime),
        )
        if resume:
            assert project_id is not None
            execution = project_runner.resume(project_id, repo)
        else:
            assert title is not None and description is not None
            brief = ProjectBrief(
                id=project_id or f"project-{uuid4().hex[:12]}",
                title=title,
                description=description,
                repository_path=str(repo.expanduser().resolve()),
                acceptance_criteria=acceptance_criteria or [],
                constraints=constraints or [],
            )
            execution = project_runner.run(
                brief,
                repo,
                github_repository=github_repo,
            )
    except (OSError, ProjectError, ValueError) as exc:
        raise _fail(str(exc)) from None

    project_store = FileProjectStore(factory_config.data_dir)
    typer.echo(f"project id: {execution.project_id}")
    typer.echo(f"state: {execution.state}")
    typer.echo(f"delivery: {execution.delivery_mode}")
    if execution.delivery_mode == "merge":
        typer.echo(
            "target: "
            + _delivery_target_text(execution.delivery_repository, execution.delivery_base_branch)
        )
        merged = sum(1 for task in execution.tasks if task.merge_commit_sha is not None)
        typer.echo(f"merged tasks: {merged}/{len(execution.tasks)}")
    try:
        plan = project_store.load_plan(execution.project_id)
    except FileNotFoundError:
        plan = None
    if plan is not None:
        typer.echo(f"approach: {plan.delivery_approach}")
        typer.echo(f"tasks: {len(plan.tasks)}")
    if execution.integration_workspace is not None:
        typer.echo(f"workspace: {execution.integration_workspace}")
    if execution.integration_branch is not None:
        typer.echo(f"branch: {execution.integration_branch}")
    for task in execution.tasks:
        details = [f"task {task.task_id}: {task.state}"]
        if task.issue_url is not None:
            details.append(task.issue_url)
        if task.run_id is not None:
            details.append(f"run {task.run_id}")
        if task.pull_request_url is not None:
            details.append(task.pull_request_url)
        if task.merge_commit_sha is not None:
            details.append(f"merged {task.merge_commit_sha}")
        elif execution.delivery_mode == "merge":
            details.append("not merged")
        typer.echo(" | ".join(details))
    if execution.failure_reason is not None:
        typer.echo(f"reason: {execution.failure_reason}")
    typer.echo(f"artifacts: {project_store.project_dir(execution.project_id)}")

    if execution.state is not ProjectState.DONE:
        raise typer.Exit(code=FAILURE_EXIT_CODE)


@app.command("start")
def start_command(
    repo: Path = typer.Option(..., "--repo", help="Path to the target Git repository."),
    github_repo: str = typer.Option(
        ..., "--github-repo", help="Backlog repository in 'OWNER/NAME' format."
    ),
    runtime: RuntimeChoice = typer.Option(
        RuntimeChoice.FAKE,
        "--runtime",
        help="Agent runtime: 'fake' (default, no model calls) or 'copilot' (paid).",
    ),
    model_profile: str = typer.Option(
        "default",
        "--model-profile",
        help="Configured model profile to use (default: top-level models block).",
    ),
    performance_mode: PerformanceModeChoice | None = typer.Option(
        None,
        "--performance-mode",
        help="Workflow performance mode override: 'standard' or eligible low-risk 'fast'.",
    ),
    once: bool = typer.Option(
        False, "--once", help="Run one bounded scheduler tick instead of polling forever."
    ),
    config: Path = typer.Option(
        None, "--config", help="Path to a factory config YAML file (default: packaged config)."
    ),
    data_dir: Path = typer.Option(
        None, "--data-dir", help="Override the configured data directory."
    ),
) -> None:
    """Poll a GitHub Issues backlog and dispatch eligible work.

    Refuses to run (and never touches GitHub) unless ``scheduler.enabled`` is
    set in configuration.
    """
    factory_config = _load_config(config, data_dir, model_profile, performance_mode)
    if not factory_config.scheduler.enabled:
        raise _fail(
            "scheduler is disabled: set 'scheduler.enabled: true' in the factory "
            "configuration before running 'factory start'."
        )
    # Polling the backlog is a GitHub operation, so ``gh`` is required here
    # even when publishing and CI observation are both disabled.
    _require_prerequisites(require_gh=True, require_copilot=runtime is RuntimeChoice.COPILOT)
    if runtime is RuntimeChoice.FAKE:
        _warn_fake_backlog_claims()
    _configure_logging(factory_config)

    import signal
    import threading

    from .service import FactoryService
    from .store import FileRunStore

    store = FileRunStore(factory_config.data_dir)
    service = FactoryService(
        config=factory_config,
        store=store,
        runtime=_build_runtime(runtime),
        source_repo=repo,
        github_repo=github_repo,
    )

    daily_limit = factory_config.scheduler.max_runs_per_day
    daily_limit_text = "unbounded" if daily_limit is None else f"{daily_limit}/day"

    if once:
        try:
            report = service.run_once()
            typer.echo(f"candidates: {report.candidates_fetched}")
            typer.echo(f"dispatched: {', '.join(report.dispatched) or '(none)'}")
            typer.echo(f"daily run limit: {daily_limit_text}")
            if report.rate_limited:
                typer.echo(
                    "rate limited: the daily run limit "
                    f"({daily_limit_text}) stopped further dispatch this tick"
                )
            if report.at_capacity:
                typer.echo("at capacity: no dispatch slot was free this tick")
        finally:
            service.shutdown()
        return

    stop_event = threading.Event()

    def _request_stop(*_args: object) -> None:
        typer.echo("stopping after the current cycle...")
        stop_event.set()

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    typer.echo(
        f"polling {github_repo} every {factory_config.scheduler.poll_interval_seconds}s "
        f"(concurrency {factory_config.scheduler.max_concurrent_tasks}, "
        f"daily run limit {daily_limit_text})"
    )
    try:
        service.run_forever(stop_event)
    except Exception as exc:  # noqa: BLE001 - top-level daemon boundary
        logger.exception("factory backlog daemon stopped unexpectedly")
        raise _fail(
            f"factory backlog daemon stopped unexpectedly: {exc}",
            code=FAILURE_EXIT_CODE,
        ) from None


@app.command("runs")
def runs_command(
    config: Path = typer.Option(
        None, "--config", help="Path to a factory config YAML file (default: packaged config)."
    ),
    data_dir: Path = typer.Option(
        None, "--data-dir", help="Override the configured data directory."
    ),
) -> None:
    """List persisted runs, most recently created last."""
    from .store import FileRunStore

    factory_config = _load_config(config, data_dir)
    store = FileRunStore(factory_config.data_dir)

    runs = store.list_runs()
    if not runs:
        typer.echo("no runs found")
        return

    for run in runs:
        typer.echo(f"{run.id}\t{run.state}\t{run.work_item_id}\t{run.created_at.isoformat()}")


@app.command("show")
def show_command(
    run_id: str = typer.Argument(..., help="The run id to display."),
    config: Path = typer.Option(
        None, "--config", help="Path to a factory config YAML file (default: packaged config)."
    ),
    data_dir: Path = typer.Option(
        None, "--data-dir", help="Override the configured data directory."
    ),
) -> None:
    """Show the persisted details of one run as JSON."""
    from .store import FileRunStore

    factory_config = _load_config(config, data_dir)
    store = FileRunStore(factory_config.data_dir)

    try:
        run = store.load_run(run_id)
    except (FileNotFoundError, ValueError):
        raise _fail(f"no such run: {run_id}", code=FAILURE_EXIT_CODE) from None

    typer.echo(run.model_dump_json(indent=2))


@app.command("doctor")
def doctor_command(
    config: Path = typer.Option(
        None, "--config", help="Path to a factory config YAML file (default: packaged config)."
    ),
    data_dir: Path = typer.Option(
        None, "--data-dir", help="Override the configured data directory."
    ),
    runtime: RuntimeChoice = typer.Option(
        RuntimeChoice.FAKE,
        "--runtime",
        help="Check prerequisites for this runtime ('copilot' additionally requires copilot).",
    ),
    model_profile: str = typer.Option(
        "default",
        "--model-profile",
        help="Configured model profile to validate (default: top-level models block).",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit the report as JSON instead of human-readable text."
    ),
) -> None:
    """Check this machine's prerequisites for the configured feature set.

    Never makes a paid model call: the only ``copilot`` interaction is a
    bounded ``copilot --version`` probe, and only when ``--runtime copilot``
    is requested. ``gh`` is required only when configuration enables pull
    requests, CI observation or the backlog daemon. Exits nonzero if any
    check errored; warnings alone do not fail the report.
    """
    import json

    from .cli_output import render_doctor_report

    doctor_runner = _seam("run_doctor")
    report = doctor_runner(
        config_path=config,
        data_dir_override=data_dir,
        model_profile=model_profile,
        requested_runtime_copilot=runtime is RuntimeChoice.COPILOT,
    )

    if json_output:
        typer.echo(json.dumps(report.to_dict(), indent=2))
    else:
        for line in render_doctor_report(report):
            typer.echo(line)

    if not report.success:
        raise typer.Exit(code=FAILURE_EXIT_CODE)


@app.command("status")
def status_command(
    config: Path = typer.Option(
        None, "--config", help="Path to a factory config YAML file (default: packaged config)."
    ),
    data_dir: Path = typer.Option(
        None, "--data-dir", help="Override the configured data directory."
    ),
    limit: int = typer.Option(
        DEFAULT_STATUS_LIMIT, "--limit", min=1, help="How many runs to list."
    ),
    offset: int = typer.Option(0, "--offset", min=0, help="Where to start the run listing."),
    stale_after_seconds: int = typer.Option(
        None,
        "--stale-after-seconds",
        min=1,
        help="Idle time before a non-terminal run counts as stale "
        "(default: scheduler.stall_timeout_seconds).",
    ),
    max_scanned_runs: int = typer.Option(
        DEFAULT_MAX_SCANNED_RUNS,
        "--max-scanned-runs",
        min=1,
        help="Hard cap on how many run files this command parses.",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit snapshot and health as JSON instead of human-readable text."
    ),
) -> None:
    """Report derived run metrics and operational health, read-only.

    Everything is recomputed from persisted artifacts on each call
    (``ADR-017``): this command never creates, mutates or repairs a run, a
    workspace, a lock or the data directory itself. A scan that was
    truncated by ``--max-scanned-runs``, or that hit an unreadable run, is
    reported as ``DEGRADED`` rather than presented as a complete picture.
    """
    factory_config = _load_config(config, data_dir)
    from .cli_output import render_status_report
    from .observability import (
        build_monitoring_snapshot,
        build_operational_health,
        scan_readable_runs,
    )
    from .store import FileRunStore

    store = FileRunStore(factory_config.data_dir)
    stale_after = _stale_after(factory_config, stale_after_seconds)

    scan = scan_readable_runs(store, max_scanned_runs=max_scanned_runs)
    snapshot = build_monitoring_snapshot(
        store,
        stale_after=stale_after,
        limit=limit,
        offset=offset,
        max_scanned_runs=max_scanned_runs,
        scan=scan,
    )
    health = build_operational_health(
        store,
        data_dir=factory_config.data_dir,
        stale_after=stale_after,
        max_scanned_runs=max_scanned_runs,
        scan=scan,
    )

    if json_output:
        import json

        payload = {
            "data_dir": str(factory_config.data_dir),
            "snapshot": snapshot.model_dump(mode="json"),
            "health": health.model_dump(mode="json"),
        }
        typer.echo(json.dumps(payload, indent=2))
        return

    typer.echo(f"data dir: {factory_config.data_dir}")
    for line in render_status_report(snapshot, health):
        typer.echo(line)


@app.command("dashboard")
def dashboard_command(
    config: Path = typer.Option(
        None, "--config", help="Path to a factory config YAML file (default: packaged config)."
    ),
    data_dir: Path = typer.Option(
        None, "--data-dir", help="Override the configured data directory."
    ),
    port: int = typer.Option(
        DEFAULT_DASHBOARD_PORT,
        "--port",
        min=0,
        max=65535,
        help="Loopback port to listen on (0 asks the OS for a free port).",
    ),
    open_browser: bool = typer.Option(
        False,
        "--open-browser",
        help="Open the tokenized dashboard URL in the default browser.",
    ),
    max_scanned_runs: int = typer.Option(
        DEFAULT_MAX_SCANNED_RUNS,
        "--max-scanned-runs",
        min=1,
        help="Hard cap on how many run files one dashboard request parses.",
    ),
) -> None:
    """Serve the read-only local dashboard until interrupted (ADR-016).

    Blocks in the foreground and is the *only* thing that ever starts a
    dashboard: nothing in ``factory run`` or ``factory start`` opens a
    socket. The server binds ``127.0.0.1`` and nothing else, answers ``GET``
    only, and is protected by a token generated for this process; the
    tokenized URL is printed to stdout once and never written to the log.
    Ctrl-C stops it and closes the socket.
    """
    factory_config = _load_config(config, data_dir)
    _configure_logging(factory_config)

    from .dashboard import LOOPBACK_HOST, DashboardConfig
    from .observability import (
        RunScanCache,
        build_active_invocation_summary,
        build_monitoring_snapshot,
        build_operational_health,
        build_run_detail,
    )
    from .projects import FileProjectStore
    from .store import FileRunStore

    store = FileRunStore(factory_config.data_dir)
    stale_after = _stale_after(factory_config, None)
    scan_cache = RunScanCache(store)

    def snapshot_provider(*, limit: int, offset: int) -> object:
        return build_monitoring_snapshot(
            store,
            stale_after=stale_after,
            limit=limit,
            offset=offset,
            max_scanned_runs=max_scanned_runs,
            scan=scan_cache.get_scan(max_scanned_runs),
        )

    def run_detail_provider(run_id: str) -> object | None:
        # Returns None (rendered as 404) for a run that does not exist or
        # cannot be read, and only ever carries allowlisted summary fields
        # plus attempt metadata -- never a log, a diff, a prompt or a raw
        # artifact body.
        return build_run_detail(store, run_id, stale_after=stale_after)

    def health_provider() -> object:
        return build_operational_health(
            store,
            data_dir=factory_config.data_dir,
            stale_after=stale_after,
            max_scanned_runs=max_scanned_runs,
            scan=scan_cache.get_scan(max_scanned_runs),
        )

    def project_provider() -> object:
        projects_dir = factory_config.data_dir / "projects"
        if not projects_dir.is_dir():
            return {"projects": []}
        execution_paths = sorted(
            projects_dir.glob("*/execution.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:max_scanned_runs]
        project_store = FileProjectStore(factory_config.data_dir)
        projects: list[dict[str, object]] = []
        for execution_path in execution_paths:
            project_id = execution_path.parent.name
            try:
                execution = project_store.load_execution(project_id)
                try:
                    plan = project_store.load_plan(project_id)
                    titles = {task.id: task.title for task in plan.tasks}
                except (FileNotFoundError, OSError, ValueError):
                    titles = {}
            except (FileNotFoundError, OSError, ValueError):
                continue
            execution_data = execution.model_dump(mode="json")
            tasks = [
                {
                    **task.model_dump(mode="json"),
                    "title": titles.get(task.task_id),
                }
                for task in execution.tasks
            ]
            models: list[dict[str, object]] = []
            for invocation in execution.invocation_records:
                models.append(
                    {
                        **invocation.model_dump(mode="json"),
                        "scope": "project",
                        "task_id": None,
                    }
                )
            for task in execution.tasks:
                if task.run_id is None:
                    continue
                try:
                    run = store.load_run(task.run_id)
                except (FileNotFoundError, OSError, ValueError):
                    continue
                for invocation in run.invocation_records:
                    models.append(
                        {
                            **invocation.model_dump(mode="json"),
                            "scope": f"task {task.task_id}",
                            "task_id": task.task_id,
                        }
                    )
                active_invocation = build_active_invocation_summary(
                    run,
                    stale_after=stale_after,
                )
                if active_invocation is not None:
                    models.append(
                        {
                            **active_invocation.model_dump(mode="json"),
                            "scope": f"task {task.task_id}",
                            "task_id": task.task_id,
                            "success": None,
                            "completed_at": None,
                            "usage": None,
                        }
                    )
            projects.append(
                {
                    "project_id": execution_data["project_id"],
                    "state": execution_data["state"],
                    "delivery_mode": execution_data["delivery_mode"],
                    "delivery_repository": execution_data["delivery_repository"],
                    "delivery_base_branch": execution_data["delivery_base_branch"],
                    "integration_branch": execution_data["integration_branch"],
                    "created_at": execution_data["created_at"],
                    "updated_at": execution_data["updated_at"],
                    "completed_at": execution_data["completed_at"],
                    "task_count": len(tasks),
                    "tasks": tasks,
                    "models": models,
                }
            )
        return {"projects": projects}

    try:
        create_server_fn = _seam("create_server")
        server = create_server_fn(
            DashboardConfig(
                snapshot_provider=snapshot_provider,
                run_detail_provider=run_detail_provider,
                health_provider=health_provider,
                project_provider=project_provider,
                host=LOOPBACK_HOST,
                port=port,
            )
        )
    except OSError as exc:
        raise _fail(f"could not bind the dashboard to {LOOPBACK_HOST}:{port}: {exc}") from None

    typer.echo(f"dashboard: {server.dashboard_url}")
    typer.echo("read-only, loopback only. press Ctrl-C to stop.")
    if open_browser:
        webbrowser.open(server.dashboard_url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        typer.echo("stopping dashboard...")
    finally:
        server.shutdown()
        server.server_close()


# -- factory service -------------------------------------------------------


def _require_macos() -> None:
    system = _current_system()
    if system != "Darwin":
        raise _fail(
            f"'factory service' manages a macOS launchd LaunchAgent and cannot run on {system}."
        )


@service_app.command("install")
def service_install_command(
    repo: Path = typer.Option(..., "--repo", help="Absolute path to the target Git repository."),
    github_repo: str = typer.Option(
        ..., "--github-repo", help="Backlog repository in 'OWNER/NAME' format."
    ),
    config: Path = typer.Option(
        None,
        "--config",
        help="Config file the service will load (must enable scheduler.enabled).",
    ),
    data_dir: Path = typer.Option(
        None, "--data-dir", help="Override the configured data directory for the service."
    ),
    runtime: RuntimeChoice = typer.Option(
        RuntimeChoice.FAKE,
        "--runtime",
        help="Runtime the service runs with. Defaults to 'fake' so it cannot spend money.",
    ),
    model_profile: str = typer.Option(
        "default",
        "--model-profile",
        help="Configured model profile the service will use.",
    ),
    executable: Path = typer.Option(
        None,
        "--executable",
        help="Explicit 'factory' executable to run (default: this frozen build or the "
        "installed console script).",
    ),
    label: str = typer.Option(DEFAULT_LABEL, "--label", help="LaunchAgent label to install under."),
    allow_source_dev: bool = typer.Option(
        False,
        "--allow-source-dev",
        help="Permit an executable in an otherwise-refused location (source checkout).",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit the resulting service status as JSON."
    ),
) -> None:
    """Install the per-user LaunchAgent that runs ``factory start``.

    Only ever happens because someone typed this command: nothing installs a
    service as a side effect of extracting an archive, running the factory or
    upgrading it (``ADR-018``). Refuses unless the target configuration
    enables the scheduler, refuses if ``factory doctor`` reports any error,
    and defaults to ``--runtime fake`` so an installed-but-forgotten agent
    cannot spend money.
    """
    _require_macos()

    factory_config = _load_config(config, data_dir, model_profile)
    if not factory_config.scheduler.enabled:
        raise _fail(
            "refusing to install a service for a disabled scheduler: set "
            "'scheduler.enabled: true' in the configuration passed with --config."
        )
    if runtime is RuntimeChoice.FAKE:
        _warn_fake_backlog_claims()

    run_doctor_fn = _seam("run_doctor")
    report = run_doctor_fn(
        config_path=config,
        data_dir_override=data_dir,
        model_profile=model_profile,
        requested_runtime_copilot=runtime is RuntimeChoice.COPILOT,
    )
    if not report.success:
        from .cli_output import render_doctor_report

        for line in render_doctor_report(report):
            typer.echo(line, err=True)
        raise _fail("refusing to install a service while 'factory doctor' reports errors.")

    from .cli_output import render_service_status
    from .service_install import (
        ServiceInstallError,
        ServiceInstallRequest,
        ServiceRuntime,
        resolve_factory_executable,
    )

    try:
        resolved_executable = resolve_factory_executable(executable)
        request = ServiceInstallRequest(
            executable=resolved_executable,
            repo=repo.expanduser(),
            github_repo=github_repo,
            data_dir=factory_config.data_dir,
            config_path=config.expanduser().resolve() if config is not None else None,
            poll_interval_seconds=factory_config.scheduler.poll_interval_seconds,
            runtime=ServiceRuntime(runtime.value),
            model_profile=model_profile,
            label=label,
            allow_source_dev=allow_source_dev,
        )
        install_service_fn = _seam("install_service")
        default_launch_agents_dir_fn = _seam("default_launch_agents_dir")
        status = install_service_fn(request, launch_agents_dir=default_launch_agents_dir_fn())
    except ServiceInstallError as exc:
        raise _fail(f"service install refused: {exc}") from None

    if json_output:
        import json

        typer.echo(json.dumps({**status.to_dict(), "runtime": runtime.value}, indent=2))
        return

    typer.echo(f"installed service for {resolved_executable}")
    typer.echo(f"runtime: {runtime.value}")
    typer.echo(f"poll interval: {factory_config.scheduler.poll_interval_seconds}s")
    for line in render_service_status(status):
        typer.echo(line)


@service_app.command("status")
def service_status_command(
    label: str = typer.Option(DEFAULT_LABEL, "--label", help="LaunchAgent label to inspect."),
    json_output: bool = typer.Option(False, "--json", help="Emit the service status as JSON."),
) -> None:
    """Report whether the LaunchAgent is installed and loaded (read-only)."""
    _require_macos()
    from .cli_output import render_service_status
    from .service_install import ServiceInstallError

    try:
        get_service_status_fn = _seam("get_service_status")
        default_launch_agents_dir_fn = _seam("default_launch_agents_dir")
        status = get_service_status_fn(label, launch_agents_dir=default_launch_agents_dir_fn())
    except ServiceInstallError as exc:
        raise _fail(f"service status unavailable: {exc}") from None

    if json_output:
        import json

        typer.echo(json.dumps(status.to_dict(), indent=2))
        return
    for line in render_service_status(status):
        typer.echo(line)


@service_app.command("uninstall")
def service_uninstall_command(
    label: str = typer.Option(DEFAULT_LABEL, "--label", help="LaunchAgent label to remove."),
    json_output: bool = typer.Option(False, "--json", help="Emit the uninstall result as JSON."),
) -> None:
    """Unload the LaunchAgent and remove its plist.

    Leaves every run, artifact and workspace on disk: uninstalling the
    service stops future polling, it does not delete history.
    """
    _require_macos()
    from .service_install import ServiceInstallError

    try:
        uninstall_service_fn = _seam("uninstall_service")
        default_launch_agents_dir_fn = _seam("default_launch_agents_dir")
        removed = uninstall_service_fn(label, launch_agents_dir=default_launch_agents_dir_fn())
    except ServiceInstallError as exc:
        raise _fail(f"service uninstall failed: {exc}") from None

    if json_output:
        import json

        typer.echo(json.dumps({"label": label, "removed": removed}, indent=2))
        return
    if removed:
        typer.echo(f"removed LaunchAgent {label}; runs and workspaces were left on disk")
    else:
        typer.echo(f"no LaunchAgent plist found for {label}; nothing to remove")


# -- factory skill ---------------------------------------------------------


def _skill_manager(config: FactoryConfig, repo: Path) -> RepositorySkillManager:
    """Resolve the skill storage for ``repo`` without creating anything."""
    from .repository_skills import RepositorySkillError, RepositorySkillManager

    try:
        return RepositorySkillManager.for_repository(config.data_dir, repo.expanduser())
    except RepositorySkillError as exc:
        raise _fail(f"cannot resolve repository skill storage: {exc}") from None


def _skill_profile(repo: Path) -> RepositoryProfile:
    """Profile the repository as it is currently checked out (read-only).

    ``profile_repository`` records unreadable files as profile warnings, so
    the only failure it raises is an unusable repository root.
    """
    from .repository_profile import profile_repository

    try:
        return profile_repository(repo.expanduser())
    except ValueError as exc:
        raise _fail(f"cannot profile the repository at {repo}: {exc}") from None


def _presence(path: Path) -> str:
    return "present" if path.exists() else "absent"


def _skill_generation_work_item() -> WorkItem:
    """A synthetic work item, required only because :class:`AgentRequest`
    carries one. Repository-level guidance is generated from the profile
    alone: the skill-generation prompt is given no work item, specification,
    plan, diff or changed files, and must not describe any single task."""
    from .models import WorkItem

    return WorkItem(
        id="repository-skill-generation",
        title="Generate repository-level guidance",
        description=(
            "Generate reusable simplify and polish guidance for this repository's "
            "detected technologies and dependency versions. This is not a task to "
            "implement."
        ),
    )


def _save_standalone_invocation(path: Path, record: InvocationRecord) -> None:
    """Atomically persist the latest non-run invocation for operator audit."""
    from uuid import uuid4

    temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temp.write_text(record.model_dump_json(indent=2) + "\n", encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


@skill_app.command("path")
def skill_path_command(
    repo: Path = typer.Option(..., "--repo", help="Path to the target Git repository."),
    config: Path = typer.Option(
        None, "--config", help="Path to a factory config YAML file (default: packaged config)."
    ),
    data_dir: Path = typer.Option(
        None, "--data-dir", help="Override the configured data directory."
    ),
) -> None:
    """Show where this repository's generated skill and overlay live.

    Read-only, and deliberately so: it creates no directory (not even the
    data directory), no generated file and no overlay. The repository is
    identified by its Git *common* directory, so a linked worktree reports
    the same paths as its main checkout, and the generated path shown is the
    one selected by the repository's *current* dependency fingerprint.
    """
    factory_config = _load_config(config, data_dir)
    _require_prerequisites(require_gh=False, require_copilot=False)

    manager = _skill_manager(factory_config, repo)
    profile = _skill_profile(repo)
    generated_path = manager.generated_path(profile.dependency_fingerprint)

    typer.echo(f"repository key: {manager.repository_key}")
    typer.echo(f"git common dir: {manager.identity.git_common_dir}")
    typer.echo(f"dependency fingerprint: {profile.dependency_fingerprint}")
    typer.echo(f"generated skill: {generated_path} ({_presence(generated_path)})")
    typer.echo(f"overlay: {manager.overlay_path} ({_presence(manager.overlay_path)})")


@skill_app.command("validate")
def skill_validate_command(
    repo: Path = typer.Option(..., "--repo", help="Path to the target Git repository."),
    config: Path = typer.Option(
        None, "--config", help="Path to a factory config YAML file (default: packaged config)."
    ),
    data_dir: Path = typer.Option(
        None, "--data-dir", help="Override the configured data directory."
    ),
) -> None:
    """Check the stored generated skill and the human overlay, read-only.

    Reports each of them as ``valid``, ``missing`` or ``invalid``, and --
    when both exist -- whether they still combine into an effective skill.
    A missing overlay is normal and never an error; anything invalid, and a
    generated skill that has not been produced yet, exit nonzero. Nothing is
    created, rewritten or repaired: an unusable overlay is reported with the
    human's bytes left exactly as written.
    """
    factory_config = _load_config(config, data_dir)
    _require_prerequisites(require_gh=False, require_copilot=False)

    manager = _skill_manager(factory_config, repo)
    profile = _skill_profile(repo)
    generated_path = manager.generated_path(profile.dependency_fingerprint)

    from .repository_skills import (
        RepositorySkillError,
        RepositorySkillMergeError,
        merge_repository_skill,
        repository_skill_validation_error,
    )

    typer.echo(f"repository key: {manager.repository_key}")
    typer.echo(f"dependency fingerprint: {profile.dependency_fingerprint}")

    failed = False
    generated: RepositorySkill | None = None
    try:
        generated = manager.load_generated(profile.dependency_fingerprint)
    except RepositorySkillError as exc:
        typer.echo(f"generated skill: invalid ({generated_path})")
        typer.echo(f"  {exc}")
        failed = True
    else:
        if generated is None:
            typer.echo(f"generated skill: missing ({generated_path})")
            typer.echo("  run 'factory skill refresh' to generate guidance for this profile.")
            failed = True
        elif problem := repository_skill_validation_error(
            generated,
            profile,
            official_documentation_origins=factory_config.polish.official_documentation_origins,
            practice_reference_urls=factory_config.polish.practice_reference_urls,
        ):
            typer.echo(f"generated skill: invalid ({generated_path})")
            typer.echo(f"  {problem}")
            generated = None
            failed = True
        else:
            typer.echo(f"generated skill: valid ({generated_path})")

    read = manager.read_overlay()
    if not read.present:
        typer.echo(f"overlay: missing ({read.path})")
        typer.echo("  a missing overlay is normal; the factory never creates one.")
    elif read.error is not None:
        typer.echo(f"overlay: invalid ({read.path})")
        for line in read.error.problems:
            typer.echo(f"  {line}")
        typer.echo("  the factory only reads this file; edit it by hand or remove it.")
        failed = True
    else:
        typer.echo(f"overlay: valid ({read.path})")
        overlay = read.overlay
        if generated is not None and overlay is not None:
            try:
                merge_repository_skill(generated, overlay)
            except RepositorySkillMergeError as exc:
                typer.echo("effective skill: invalid (the overlay cannot be combined)")
                typer.echo(f"  {exc}")
                failed = True
            else:
                typer.echo("effective skill: valid (generated guidance plus the overlay)")

    if failed:
        raise typer.Exit(code=FAILURE_EXIT_CODE)


@skill_app.command("refresh")
def skill_refresh_command(
    repo: Path = typer.Option(..., "--repo", help="Path to the target Git repository."),
    runtime: RuntimeChoice = typer.Option(
        RuntimeChoice.FAKE,
        "--runtime",
        help="Agent runtime: 'fake' (default, no model calls) or 'copilot' (paid).",
    ),
    model_profile: str = typer.Option(
        "default",
        "--model-profile",
        help="Configured model profile to use (default: top-level models block).",
    ),
    config: Path = typer.Option(
        None, "--config", help="Path to a factory config YAML file (default: packaged config)."
    ),
    data_dir: Path = typer.Option(
        None, "--data-dir", help="Override the configured data directory."
    ),
) -> None:
    """Regenerate this repository's stored guidance on request.

    The only write path in ``factory skill``, and it happens because someone
    typed this command: it requires ``polish.enabled``, profiles the
    repository as currently checked out, and runs the configured researcher
    from a neutral directory under the data directory with the profile and
    the configured source allowlists as its entire input -- no work item, no
    plan, no diff, no changed files, no repository file access.

    No run, worktree or workspace is created. The human-owned overlay is
    never read, written or deleted here. Guidance that fails validation is
    refused and the previously stored file is left byte-for-byte unchanged.
    """
    factory_config = _load_config(config, data_dir, model_profile)
    if not factory_config.polish.enabled:
        raise _fail(
            "repository skill generation is disabled: set 'polish.enabled: true' in the "
            "factory configuration before running 'factory skill refresh'."
        )
    _require_prerequisites(require_gh=False, require_copilot=runtime is RuntimeChoice.COPILOT)
    _configure_logging(factory_config)

    manager = _skill_manager(factory_config, repo)
    profile = _skill_profile(repo)

    neutral_dir = factory_config.data_dir / SKILL_GENERATION_DIRNAME / manager.repository_key
    try:
        neutral_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _fail(f"cannot create the skill generation directory {neutral_dir}: {exc}") from None

    from .agents import (
        AgentRequest,
        runtime_exception_failure_reason,
    )
    from .models import AgentPurpose, AgentRole, InvocationRecord, utc_now
    from .repository_skills import (
        MAX_REPOSITORY_SKILL_GENERATION_ATTEMPTS,
        RepositorySkillError,
        repository_skill_correction_context,
        repository_skill_exhausted_warning,
        repository_skill_validation_error,
    )
    from .routing import ModelRouter
    from .writing_policy import apply_agent_result_writing_policy

    role_model = ModelRouter(factory_config).model_for_researcher()
    agent_runtime = _build_runtime(runtime)
    skill: RepositorySkill | None = None
    rejection: str | None = None
    initial_rejection: str | None = None
    rejected_skill: RepositorySkill | None = None
    for attempt in range(1, MAX_REPOSITORY_SKILL_GENERATION_ATTEMPTS + 1):
        request = AgentRequest(
            role=AgentRole.RESEARCHER,
            purpose=AgentPurpose.GENERATE_REPOSITORY_SKILL,
            model=role_model.model,
            reasoning=role_model.reasoning,
            context_tier=role_model.context_tier,
            work_item=_skill_generation_work_item(),
            repair_context=(
                repository_skill_correction_context(rejection, rejected_skill)
                if rejection is not None
                else None
            ),
            repository_profile=profile,
            official_documentation_origins=list(
                factory_config.polish.official_documentation_origins
            ),
            practice_reference_urls=list(factory_config.polish.practice_reference_urls),
            workspace_path=str(neutral_dir),
            attempt_number=attempt,
            timeout_seconds=factory_config.agent_timeout_seconds,
        )

        started_at = utc_now()
        try:
            result = apply_agent_result_writing_policy(
                agent_runtime.run(request),
                request.purpose,
            )
        except ValueError as exc:
            completed_at = utc_now()
            invocation = InvocationRecord(
                invocation_number=attempt,
                role=request.role,
                purpose=request.purpose,
                model=request.model,
                reasoning=request.reasoning,
                context_tier=request.context_tier,
                started_at=started_at,
                completed_at=completed_at,
                success=False,
                failure_reason=runtime_exception_failure_reason(exc),
                attempt_number=attempt,
            )
            try:
                _save_standalone_invocation(neutral_dir / "last-invocation.json", invocation)
            except OSError as persist_exc:
                raise _fail(
                    f"repository skill invocation telemetry could not be persisted: {persist_exc}",
                    code=FAILURE_EXIT_CODE,
                ) from None
            rejection = f"repository skill generation could not run: {exc}"
            if attempt == 1:
                initial_rejection = rejection
                continue
            assert initial_rejection is not None
            raise _fail(
                repository_skill_exhausted_warning(initial_rejection, rejection),
                code=FAILURE_EXIT_CODE,
            ) from None

        completed_at = utc_now()
        invocation = InvocationRecord(
            invocation_number=attempt,
            role=request.role,
            purpose=request.purpose,
            model=request.model,
            reasoning=request.reasoning,
            context_tier=request.context_tier,
            started_at=started_at,
            completed_at=completed_at,
            success=result.success,
            failure_reason=result.failure_reason,
            attempt_number=attempt,
            usage=result.usage,
        )
        try:
            _save_standalone_invocation(neutral_dir / "last-invocation.json", invocation)
        except OSError as exc:
            raise _fail(
                f"repository skill invocation telemetry could not be persisted: {exc}",
                code=FAILURE_EXIT_CODE,
            ) from None

        skill = result.repository_skill
        if not result.success:
            rejection = result.failure_reason or "the researcher produced no repository guidance"
            rejected_skill = result.repository_skill
            if attempt == 1:
                initial_rejection = rejection
                continue
            if initial_rejection is not None:
                rejection = repository_skill_exhausted_warning(initial_rejection, rejection)
            raise _fail(rejection, code=FAILURE_EXIT_CODE)
        if skill is None:
            rejection = "the researcher reported success without repository guidance"
        elif skill.dependency_fingerprint != profile.dependency_fingerprint:
            rejection = (
                "the researcher returned guidance for a different dependency fingerprint: "
                f"{skill.dependency_fingerprint} is not {profile.dependency_fingerprint}"
            )
            rejected_skill = skill
        elif problem := repository_skill_validation_error(
            skill,
            profile,
            official_documentation_origins=factory_config.polish.official_documentation_origins,
            practice_reference_urls=factory_config.polish.practice_reference_urls,
        ):
            rejection = f"refusing to store unverified repository guidance: {problem}"
            rejected_skill = skill
        else:
            break

        if attempt == 1:
            initial_rejection = rejection
            continue
        assert initial_rejection is not None
        raise _fail(
            repository_skill_exhausted_warning(initial_rejection, rejection),
            code=FAILURE_EXIT_CODE,
        )

    assert skill is not None

    # Stamped once, here, so the stored record says when this guidance was
    # produced rather than when the model claimed it was.
    skill = skill.model_copy(update={"generated_at": utc_now()})
    try:
        record = manager.refresh_generated(skill)
    except RepositorySkillError as exc:
        raise _fail(
            f"the generated repository skill could not be stored: {exc}",
            code=FAILURE_EXIT_CODE,
        ) from None

    typer.echo(f"repository key: {manager.repository_key}")
    typer.echo(f"dependency fingerprint: {skill.dependency_fingerprint}")
    typer.echo(f"{'created' if record.created else 'refreshed'} generated skill: {record.path}")
    typer.echo(f"overlay: untouched ({manager.overlay_path})")


if __name__ == "__main__":
    app()
