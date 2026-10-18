"""The ``factory`` command-line interface (``PLAN.md`` Phases 1-15).

```bash
factory --version
factory run --repo PATH --title TEXT --description TEXT [--runtime fake|copilot|pi]
factory project --repo PATH --title TEXT --description TEXT [--runtime fake|copilot|pi]
factory runs
factory show RUN_ID
factory start --repo PATH --github-repo OWNER/NAME [--once] [--runtime fake|copilot|pi]
factory doctor [--runtime fake|copilot|pi|claude-code] [--json]
factory status [--json]
factory dashboard [--port 8765] [--open-browser]
factory service install|status|uninstall
```

``--runtime`` defaults to ``fake`` so no command ever makes a paid model call
by accident; ``--runtime copilot`` opts in to the real
:class:`~software_agent_factory.copilot_runtime.CopilotAgentRuntime`, and
``--runtime pi`` opts in to the pi agent runtime (requires ``pi``, Node and a
provider credential -- see ``factory doctor --runtime pi``).

Pull request creation, CI observation, the backlog daemon, the dashboard and
the launchd service are all strictly opt-in (``pull_request.enabled``,
``ci.enabled``, ``scheduler.enabled``, and an explicit ``factory dashboard`` /
``factory service install`` command). With the packaged defaults, ``factory
run`` performs no network access at all and finishes at ``PR_READY``.

``--data-dir`` overrides the configured data directory so tests and demos can
point the CLI at an isolated temporary directory without editing a config
file.

Three conventions hold across every command here:

- **Fail before you work.** Configuration problems and missing external
  prerequisites (``git``, and ``gh``/``copilot``/``pi`` only when the
  requested feature set needs them) exit with :data:`CONFIG_ERROR_EXIT_CODE`
  and one explicit line, never a traceback from deep inside a workspace or
  tracker.
- **Read-only stays read-only.** ``runs``, ``show`` and ``status`` derive
  everything from persisted artifacts
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
from typing import TYPE_CHECKING, Any, Literal

import typer

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime, timedelta

    from .agents import AgentRuntime
    from .config import FactoryConfig
    from .dashboard.snapshot import (
        EscalateRequester,
        EscalateRequestResult,
        ResumeRequester,
        ResumeRequestReader,
        ResumeRequestResult,
        ResumeRunReader,
        SteerRequester,
        StopRequester,
        StopRequestResult,
    )
    from .models import (
        DashboardResumeRequest,
        FactoryRun,
        InvocationRecord,
        RunLaunch,
        RuntimeName,
        ToolchainSetupPlan,
    )
    from .store import FileRunStore

app = typer.Typer(help="Local-first autonomous software engineering factory.")
service_app = typer.Typer(
    help="Manage the opt-in per-user macOS launchd service (never automatic)."
)
app.add_typer(service_app, name="service")

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

#: Shared ``--runtime`` help text for every command whose runtime choice
#: builds an :class:`~software_agent_factory.agents.AgentRuntime` (``run``,
#: ``project``, ``start``). One source keeps the three
#: runtime names here from drifting out of sync with :class:`RuntimeChoice`.
#: Shared ``--no-risk-assessment`` option for the commands that start runs
#: (``run``, ``project``, ``start``, ``service install``).
NO_RISK_ASSESSMENT_FLAG = "--no-risk-assessment"
NO_RISK_ASSESSMENT_HELP = (
    "Turn off risk assessment for this invocation: no risk level asks for human "
    "approval and triage writes no risk rationale."
)

RUNTIME_OPTION_HELP = (
    "Agent runtime: 'fake' (default, no model calls), 'copilot' (paid), 'pi' (paid) or "
    "'claude-code' (Claude subscription)."
)

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
    "PiAgentRuntime": (".pi_runtime", "PiAgentRuntime"),
    "ClaudeCodeAgentRuntime": (".claude_code_runtime", "ClaudeCodeAgentRuntime"),
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
    PI = "pi"
    CLAUDE_CODE = "claude-code"


def _current_system() -> str:
    """The host OS name. A function (not an inline ``platform.system()``
    call) so macOS-only commands can be exercised deterministically from any
    development platform."""
    return platform.system()


class _Failure(typer.Exit):
    """The ``Exit`` of :func:`_fail`, which keeps its message."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(code=code)
        self.message = message


def _fail(message: str, *, code: int = CONFIG_ERROR_EXIT_CODE) -> typer.Exit:
    """Print ``message`` to stderr and return an ``Exit`` for the caller to
    raise. Returning rather than raising keeps ``raise _fail(...)`` explicit
    at the call site, so a reader always sees the control flow."""
    typer.echo(message, err=True)
    return _Failure(message, code)


def _load_config(
    config: Path | None,
    data_dir: Path | None,
    model_profile: str | None = None,
    no_risk_assessment: bool = False,
) -> FactoryConfig:
    """Load configuration, applying optional ``--data-dir`` and ``--no-risk-assessment`` overrides.

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
    if no_risk_assessment:
        loaded = loaded.model_copy(
            update={"risk_assessment": loaded.risk_assessment.model_copy(update={"enabled": False})}
        )
    return loaded


def _require_prerequisites(
    *,
    require_gh: bool,
    runtimes: frozenset[RuntimeName] = frozenset(),
    pi_executable: str = "pi",
) -> None:
    """Refuse to start work when a required external executable is absent.

    Uses the same ``PATH`` lookup ``factory doctor`` uses
    (:func:`~software_agent_factory.doctor.missing_prerequisites`), so the
    two can never disagree, and runs before any workspace, tracker or agent
    code -- the alternative is a traceback from a failed ``git`` exec several
    layers down. ``runtimes`` comes from :meth:`FactoryConfig.runtimes_for`.
    ``pi_executable`` should be the configured
    ``factory_config.pi.executable`` whenever a loaded config is available, so
    a custom executable name is looked up instead of the literal ``"pi"``.
    """
    missing_checker = _seam("missing_prerequisites")
    missing = missing_checker(
        require_gh=require_gh,
        runtimes=runtimes,
        pi_executable=pi_executable,
    )
    if not missing:
        return
    raise _fail(
        f"missing required executable(s) on PATH: {', '.join(missing)}. "
        "Install them and retry; 'factory doctor' explains each requirement."
    )


def _default_runtime(choice: RuntimeChoice) -> RuntimeName | None:
    """The runtime for roles without ``runtime``; ``None`` for the fake runtime."""
    from .models import RuntimeName

    return None if choice is RuntimeChoice.FAKE else RuntimeName(choice.value)


def _require_claude_code_effort(runtime: RuntimeChoice, config: FactoryConfig) -> None:
    """Refuse a ``claude-code`` run whose models use a reasoning level ``--effort``
    does not accept, before any work starts (ADR-045). Route options can swap
    in another model profile, so those profiles are checked too. A role that
    names its own runtime was already checked when the configuration loaded."""
    if runtime is not RuntimeChoice.CLAUDE_CODE:
        return
    from .models import CLAUDE_CODE_EFFORT_LEVELS

    invalid = [
        f"{role}={role_config.reasoning}"
        for role, role_config in config.reachable_roles().items()
        if role_config.runtime is None and role_config.reasoning not in CLAUDE_CODE_EFFORT_LEVELS
    ]
    if invalid:
        raise _fail(
            f"--runtime claude-code accepts reasoning {', '.join(CLAUDE_CODE_EFFORT_LEVELS)}; "
            f"the configured models use {', '.join(invalid)}. "
            "Use --model-profile claude or change the reasoning values."
        )


def _configure_logging(config: FactoryConfig) -> None:
    """Attach the bounded structured log under ``<data_dir>/logs``.

    A logging destination that cannot be created is reported as a warning
    rather than aborting the command: losing the on-disk log copy must never
    stop the factory (or the dashboard) from running.
    """
    try:
        from .observability import configure_factory_logging

        configure_factory_logging(config.data_dir)
    except OSError as exc:
        typer.echo(f"warning: could not open the structured log: {exc}", err=True)


def _build_runtime(choice: RuntimeChoice, config: FactoryConfig) -> AgentRuntime:
    """The fake runtime serves every call, so a dry run never makes a paid call.
    Otherwise each role goes to its own runtime, built on first use (ADR-045)."""
    default = _default_runtime(choice)
    if default is None:
        return _seam("FakeAgentRuntime")()  # type: ignore[no-any-return]
    from .models import RuntimeName
    from .runtime_router import RoutingAgentRuntime

    factories: dict[RuntimeName, Callable[[], AgentRuntime]] = {
        RuntimeName.COPILOT: lambda: _seam("CopilotAgentRuntime")(),
        RuntimeName.PI: lambda: _seam("PiAgentRuntime")(config.pi, config.data_dir),
        RuntimeName.CLAUDE_CODE: lambda: _seam("ClaudeCodeAgentRuntime")(),
    }
    return RoutingAgentRuntime(default, factories)


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
        help=RUNTIME_OPTION_HELP,
    ),
    model_profile: str = typer.Option(
        "default",
        "--model-profile",
        help="Configured model profile to use (default: top-level models block).",
    ),
    no_risk_assessment: bool = typer.Option(
        False, NO_RISK_ASSESSMENT_FLAG, help=NO_RISK_ASSESSMENT_HELP
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

    from .models import WorkItem

    factory_config = _load_config(config, data_dir, model_profile, no_risk_assessment)
    store, controller = _synchronous_controller(
        runtime, factory_config, _launch(runtime, model_profile, config)
    )

    work_item = WorkItem(
        id=work_item_id or f"WI-{uuid4().hex[:12]}",
        title=title,
        description=description,
        acceptance_criteria=acceptance_criteria or [],
        constraints=constraints or [],
    )

    _report_run(store, controller.run(work_item, repo))


def _launch(runtime: RuntimeChoice, model_profile: str, config: Path | None) -> RunLaunch:
    """The command settings a new run records, for ``factory escalate`` (ADR-055)."""
    from .models import RunLaunch

    return RunLaunch(
        runtime=runtime.value,
        model_profile=model_profile,
        config_path=None if config is None else str(config.expanduser().resolve()),
    )


def _synchronous_controller(
    runtime: RuntimeChoice, factory_config: FactoryConfig, launch: RunLaunch
) -> tuple[FileRunStore, Any]:
    """The store and controller of a command that drives a run in this process."""
    from .store import FileRunStore

    # A manual run needs ``gh`` only for the publishing/CI features it would
    # actually reach; the scheduler is irrelevant here, so an offline default
    # run requires nothing but ``git``.
    _require_prerequisites(
        require_gh=factory_config.pull_request.enabled or factory_config.ci.enabled,
        runtimes=factory_config.runtimes_for(_default_runtime(runtime)),
        pi_executable=factory_config.pi.executable,
    )
    _require_claude_code_effort(runtime, factory_config)
    _configure_logging(factory_config)
    store = FileRunStore(factory_config.data_dir)
    controller_cls = _seam("WorkflowController")
    return store, controller_cls(
        factory_config, store, _build_runtime(runtime, factory_config), launch=launch
    )


def _report_run(store: FileRunStore, run: FactoryRun) -> None:
    """Print where a synchronous run ended; exit with failure unless it succeeded."""
    from .models import ChangeSet

    typer.echo(f"run id: {run.id}")
    typer.echo(f"state: {run.state}")
    if run.effective_route is not None:
        typer.echo(f"route: {run.effective_route}")
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
    repo: Path = typer.Option(  # NOSONAR(S107) - Typer maps each CLI option to one parameter.
        ...,
        "--repo",
        help="Path to the target Git repository.",
    ),
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
        help=RUNTIME_OPTION_HELP,
    ),
    model_profile: str = typer.Option(
        "default",
        "--model-profile",
        help="Configured model profile to use (default: top-level models block).",
    ),
    no_risk_assessment: bool = typer.Option(
        False, NO_RISK_ASSESSMENT_FLAG, help=NO_RISK_ASSESSMENT_HELP
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

    factory_config = _load_config(config, data_dir, model_profile, no_risk_assessment)
    _require_prerequisites(
        require_gh=(
            github_repo is not None
            or factory_config.pull_request.enabled
            or factory_config.ci.enabled
            or factory_config.merge.enabled
        ),
        runtimes=factory_config.runtimes_for(_default_runtime(runtime)),
        pi_executable=factory_config.pi.executable,
    )
    _require_claude_code_effort(runtime, factory_config)
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
            _build_runtime(runtime, factory_config),
            launch=_launch(runtime, model_profile, config),
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
    for reason in execution.needs_look:
        typer.echo(f"needs look: {reason}")
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
        help=RUNTIME_OPTION_HELP,
    ),
    model_profile: str = typer.Option(
        "default",
        "--model-profile",
        help="Configured model profile to use (default: top-level models block).",
    ),
    no_risk_assessment: bool = typer.Option(
        False, NO_RISK_ASSESSMENT_FLAG, help=NO_RISK_ASSESSMENT_HELP
    ),
    once: bool = typer.Option(
        False, "--once", help="Run one bounded scheduler tick instead of polling forever."
    ),
    removed_performance_mode: str | None = typer.Option(
        None,
        "--performance-mode",
        hidden=True,
        help="Removed (ADR-036). Accepted and ignored so services installed with it still start.",
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
    factory_config = _load_config(config, data_dir, model_profile, no_risk_assessment)
    if not factory_config.scheduler.enabled:
        raise _fail(
            "scheduler is disabled: set 'scheduler.enabled: true' in the factory "
            "configuration before running 'factory start'."
        )
    # Polling the backlog is a GitHub operation, so ``gh`` is required here
    # even when publishing and CI observation are both disabled.
    _require_prerequisites(
        require_gh=True,
        runtimes=factory_config.runtimes_for(_default_runtime(runtime)),
        pi_executable=factory_config.pi.executable,
    )
    _require_claude_code_effort(runtime, factory_config)
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
        runtime=_build_runtime(runtime, factory_config),
        source_repo=repo,
        github_repo=github_repo,
        launch=_launch(runtime, model_profile, config),
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

    runs = store.list_runs(skip_invalid=True)
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


_STOP_MESSAGES: dict[str, str] = {
    "created": "stop requested for {run_id}. the factory stops the agent within seconds, "
    "keeps the commits and halts the run for you.",
    "exists": "a stop is already waiting for {run_id}.",
    "finished": "run {run_id} has already finished; there is nothing to stop.",
    "run_missing": "no such run: {run_id}",
}


@app.command("cancel")
def cancel_command(
    run_id: str = typer.Argument(..., help="The run id to stop."),
    config: Path = typer.Option(
        None, "--config", help="Path to a factory config YAML file (default: packaged config)."
    ),
    data_dir: Path = typer.Option(
        None, "--data-dir", help="Override the configured data directory."
    ),
) -> None:
    """Ask the factory to stop a run. It keeps the commits and halts the run for you."""
    from .models import utc_now
    from .store import FileRunStore

    factory_config = _load_config(config, data_dir)
    store = FileRunStore(factory_config.data_dir)
    result = request_run_stop(store, run_id, "cli", utc_now())
    message = _STOP_MESSAGES[result].format(run_id=run_id)
    if result in ("finished", "run_missing"):
        raise _fail(message, code=FAILURE_EXIT_CODE)
    typer.echo(message)


_STEER_MESSAGES: dict[str, str] = {
    "created": "note saved for {run_id}. the agent reads it at its next call.",
    "exists": "a note is already waiting for {run_id}; it goes with the next call.",
    "finished": "run {run_id} has finished or cannot resume; there is nothing to steer.",
    "run_missing": "no such run: {run_id}",
}


@app.command("steer")
def steer_command(
    run_id: str = typer.Argument(..., help="The run id to steer."),
    note: str = typer.Argument(..., help="The note for the next agent call, at most 2000 chars."),
    config: Path = typer.Option(
        None, "--config", help="Path to a factory config YAML file (default: packaged config)."
    ),
    data_dir: Path = typer.Option(
        None, "--data-dir", help="Override the configured data directory."
    ),
) -> None:
    """Send a note to the next agent call of a run. A halted run reads it when it resumes."""
    from .models import utc_now
    from .resume import clean_steer_note
    from .store import FileRunStore

    cleaned = clean_steer_note(note)
    if cleaned is None:
        raise _fail("the note is empty, too long or holds a secret", code=FAILURE_EXIT_CODE)
    factory_config = _load_config(config, data_dir)
    store = FileRunStore(factory_config.data_dir)
    result = request_run_steer(store, run_id, cleaned, "cli", utc_now())
    message = _STEER_MESSAGES[result].format(run_id=run_id)
    if result != "created":
        raise _fail(message, code=FAILURE_EXIT_CODE)
    typer.echo(message)


@app.command("escalate")
def escalate_command(
    run_id: str = typer.Argument(..., help="The run id to redo one route higher."),
    note: str = typer.Option(
        ..., "--note", help="What to change, for the agents. At most 2000 chars."
    ),
    config: Path = typer.Option(
        None, "--config", help="Path to a factory config YAML file (default: packaged config)."
    ),
    data_dir: Path = typer.Option(
        None, "--data-dir", help="Override the configured data directory."
    ),
) -> None:
    """Redo a run one route higher with your note, on the same branch and pull request.

    The run's repository, runtime, model profile and config come from the run itself. The
    run's ``operator-escalation.json`` says how it went, for the dashboard.
    """
    import os

    from .operator_escalation import save_escalation_start
    from .redaction import redact_secrets
    from .resume import clean_steer_note
    from .store import OPERATOR_ESCALATION_FILENAME, FileRunStore

    cleaned = clean_steer_note(note)
    if cleaned is None:
        raise _fail("the note is empty, too long or holds a secret", code=FAILURE_EXIT_CODE)
    run_data_dir = _load_config(config, data_dir).data_dir
    store = FileRunStore(run_data_dir)
    try:
        record = save_escalation_start(store, run_id, cleaned, os.getpid())
    except FileNotFoundError:
        raise _fail(f"no such run: {run_id}", code=FAILURE_EXIT_CODE)
    if record is None:
        raise _fail(
            f"cannot escalate run {run_id}: another escalation of it still runs",
            code=FAILURE_EXIT_CODE,
        )
    try:
        run = _escalate(store, run_data_dir, run_id, cleaned)
    except Exception as exc:
        # The page names the run, so the record keeps only why.
        error = (
            exc.message.removeprefix(f"cannot escalate run {run_id}: ")
            if isinstance(exc, _Failure)
            else f"{type(exc).__name__}: {exc}"
        )
        failed = record.model_copy(update={"status": "failed", "error": redact_secrets(error)})
        store.save_artifact(run_id, failed, OPERATOR_ESCALATION_FILENAME)
        raise
    done = record.model_copy(update={"status": "done"})
    store.save_artifact(run_id, done, OPERATOR_ESCALATION_FILENAME)
    _report_run(store, run)


def _escalate(store: FileRunStore, data_dir: Path, run_id: str, note: str) -> FactoryRun:
    """Build the controller the run started with and escalate the run."""
    from .workspace import WorkspaceError

    refused = f"cannot escalate run {run_id}"
    launch = store.load_run(run_id).launch
    if launch is None:
        raise _fail(
            f"{refused}: it was started before runs recorded their settings",
            code=FAILURE_EXIT_CODE,
        )
    launch_config = None if launch.config_path is None else Path(launch.config_path)
    factory_config = _load_config(launch_config, data_dir, launch.model_profile)
    _, controller = _synchronous_controller(RuntimeChoice(launch.runtime), factory_config, launch)
    try:
        run: FactoryRun = controller.escalate(run_id, note)
    except FileNotFoundError:
        raise _fail(f"{refused}: it has no saved plan", code=FAILURE_EXIT_CODE)
    except (ValueError, WorkspaceError) as exc:
        raise _fail(f"{refused}: {exc}", code=FAILURE_EXIT_CODE)
    return run


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
        help=(
            "Check prerequisites for this runtime ('copilot' additionally requires copilot; "
            "'pi' additionally requires pi, Node and a provider credential)."
        ),
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
        requested_runtime=_default_runtime(runtime),
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


def build_resume_run_reader(store: FileRunStore) -> ResumeRunReader:
    """The run reader of the dashboard's approve and answer routes. Read only.

    A run that is missing or cannot be read is an unknown run: the route answers ``404``.
    """

    def read(run_id: str) -> FactoryRun | None:
        try:
            return store.load_run(run_id)
        except (OSError, ValueError):
            return None

    return read


def build_resume_requester(store: FileRunStore) -> ResumeRequester:
    """The requester of the dashboard's approve and answer routes.

    The one write the dashboard makes: a create-only request file. The service reads it
    later and is the only writer of the run.
    """

    def create(run_id: str, request: DashboardResumeRequest) -> ResumeRequestResult:
        try:
            created = store.create_dashboard_request(run_id, request)
        except FileNotFoundError:
            return "run_missing"
        return "created" if created else "exists"

    return create


def request_run_stop(
    store: FileRunStore, run_id: str, source: Literal["dashboard", "cli"], created_at: datetime
) -> StopRequestResult:
    """Store a stop request for a run that has not finished (ADR-047). Create-only.

    The controller that drives the run reads it, kills the agent call and halts the run.
    """
    from .lifecycle import is_run_finished
    from .models import RunStopRequest

    try:
        run = store.load_run(run_id)
    except (FileNotFoundError, ValueError):
        return "run_missing"
    if is_run_finished(run):
        return "finished"
    request = RunStopRequest(run_id=run_id, source=source, created_at=created_at)
    try:
        created = store.create_stop_request(request)
    except FileNotFoundError:
        return "run_missing"
    return "created" if created else "exists"


def _can_take_steer_note(run: FactoryRun) -> bool:
    """Whether a later agent call of ``run`` can read a steer note.

    A working run can. A halted run can only when its halt can resume: a note on any other
    halt would wait for a call that never comes.
    """
    from .lifecycle import is_run_finished
    from .models import ResumeClassification, WorkflowState

    if not is_run_finished(run):
        return True
    return (
        run.state is WorkflowState.NEEDS_HUMAN
        and run.escalation is not None
        and run.escalation.resume_classification is not ResumeClassification.NOT_RESUMABLE
    )


def request_run_steer(
    store: FileRunStore,
    run_id: str,
    note: str,
    source: Literal["dashboard", "cli"],
    created_at: datetime,
) -> StopRequestResult:
    """Store a steer note for a run that has not finished (ADR-047). Create-only.

    ``note`` is already clean. The controller adds it to the next agent prompt.
    """
    from .models import RunSteerNote

    try:
        run = store.load_run(run_id)
    except (FileNotFoundError, ValueError):
        return "run_missing"
    if not _can_take_steer_note(run):
        return "finished"
    request = RunSteerNote(run_id=run_id, source=source, note=note, created_at=created_at)
    try:
        created = store.create_steer_note(request)
    except FileNotFoundError:
        return "run_missing"
    return "created" if created else "exists"


def build_steer_requester(store: FileRunStore) -> SteerRequester:
    """The requester of the dashboard's steer route."""

    def steer(run_id: str, note: str, created_at: datetime) -> StopRequestResult:
        return request_run_steer(store, run_id, note, "dashboard", created_at)

    return steer


def build_escalator(store: FileRunStore, data_dir: Path) -> EscalateRequester:
    """The requester of the dashboard's escalate route: a detached ``factory escalate``.

    The dashboard never drives a run, so the escalation runs as its own process that outlives
    the request. It records the escalation as running at once, so the page shows it and offers
    no second one. The command records how it went (ADR-055).
    """
    import subprocess
    import threading

    from .models import OperatorEscalation
    from .observability import load_operator_escalation
    from .operator_escalation import save_escalation_start
    from .redaction import redact_secrets
    from .resume import escalation_route
    from .store import OPERATOR_ESCALATION_FILENAME

    one_at_a_time = threading.Lock()

    def escalate(run_id: str, note: str) -> EscalateRequestResult:
        with one_at_a_time:
            try:
                last = load_operator_escalation(store, run_id)
                if escalation_route(store.load_run(run_id)) is None or (
                    last is not None and last.status == "running"
                ):
                    return "not_escalatable"
            except (FileNotFoundError, ValueError):
                return "run_missing"
            # ``--note=`` keeps a note that starts with a dash from reading as an option.
            command = [sys.executable, "-m", "software_agent_factory", "escalate", run_id]
            command += [f"--note={note}", "--data-dir", str(data_dir)]
            try:
                child = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError as exc:
                failed = OperatorEscalation(
                    note=note, status="failed", error=redact_secrets(str(exc))
                )
                store.save_artifact(run_id, failed, OPERATOR_ESCALATION_FILENAME)
                return "started"
            save_escalation_start(store, run_id, note, child.pid)
        # Reap the child when it ends, so its pid reads as gone.
        threading.Thread(target=child.wait, daemon=True).start()
        return "started"

    return escalate


def build_stop_requester(store: FileRunStore) -> StopRequester:
    """The requester of the dashboard's stop route."""

    def stop(run_id: str, created_at: datetime) -> StopRequestResult:
        return request_run_stop(store, run_id, "dashboard", created_at)

    return stop


def build_resume_request_reader(store: FileRunStore) -> ResumeRequestReader:
    """The request reader of the run detail. Read only, and it leaves the answers out.

    The page shows when and whether a request was queued, never what was answered.
    """

    def read(run_id: str, episode_id: str) -> list[dict[str, object]]:
        return [
            request.model_dump(mode="json", exclude={"answers"})
            for request in store.list_dashboard_requests(run_id, episode_id)
        ]

    return read


@app.command("setup")
def setup_command(
    repo: Path = typer.Option(..., "--repo", help="Path to the target Git repository."),
    config: Path = typer.Option(
        None, "--config", help="Path to a factory config YAML file (default: packaged config)."
    ),
    data_dir: Path = typer.Option(
        None, "--data-dir", help="Override the configured data directory."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the setup plan without changing anything."
    ),
    publish: bool = typer.Option(
        False,
        "--publish",
        help="Commit the setup worktree, push its branch and open a pull request.",
    ),
) -> None:
    """Add the missing development tools to a repository (ADR-034).

    The factory detects the stack and the tools the repository already has,
    and plans only the missing ones: a formatter, a linter, a type checker,
    a test runner and the mutation tool. It never replaces an existing tool.
    Without ``--dry-run``, it changes the manifest and the lockfile in a
    factory worktree at the source HEAD, on its own branch, and records the
    plan in ``.factory/setup.json``. It installs nothing, and the JavaScript
    commands run no package scripts. Python locking can run the project's
    build backend. The source checkout is never changed. Nothing is
    committed or pushed unless ``--publish`` is given, and the factory never
    merges a setup pull request.
    """
    from .command_probe import ProbeLimits
    from .publishing import PullRequestPublisher
    from .setup_run import (
        SetupError,
        plan_setup,
        publish_setup,
        run_toolchain_setup,
        source_state,
    )
    from .verification import DeterministicVerifier

    factory_config = _load_config(config, data_dir)
    repo = repo.expanduser()
    try:
        head, dirty = source_state(repo)
    except SetupError as exc:
        raise _fail(str(exc)) from None
    if dry_run:
        try:
            plan, _files = plan_setup(repo)
        except (OSError, ValueError) as exc:
            raise _fail(f"cannot plan the setup: {exc}") from None
        notes = plan.notes
        if dirty:
            notes = (*notes, "uncommitted changes in the checkout; a setup run uses HEAD")
        _echo_setup_plan(plan, notes)
        return
    if publish and not factory_config.pull_request.enabled:
        raise _fail("--publish needs pull_request.enabled in the configuration")
    try:
        result = run_toolchain_setup(
            repo,
            factory_config.factory.data_dir,
            factory_config.repository.branch_prefix,
            DeterministicVerifier(),
            ProbeLimits.from_repository(factory_config.repository),
            head,
        )
    except SetupError as exc:
        raise _fail(f"setup could not run: {exc}", code=1) from None
    _echo_setup_plan(result.plan, result.plan.notes)
    if result.plan.is_empty:
        return
    if not result.outcome.succeeded:
        raise _fail(
            f"setup command failed: {result.outcome.failed_command} "
            f"({result.outcome.failure_reason}); worktree kept at {result.worktree}",
            code=1,
        )
    typer.echo(f"worktree: {result.worktree}")
    typer.echo(f"branch: {result.branch}")
    if publish:
        try:
            published = publish_setup(result, PullRequestPublisher(factory_config), repo)
        except SetupError as exc:
            raise _fail(f"setup could not publish: {exc}", code=1) from None
        typer.echo(f"pull request: {published.pull_request_url}")


def _echo_setup_plan(plan: ToolchainSetupPlan, notes: tuple[str, ...]) -> None:
    for note in notes:
        typer.echo(f"note: {note}")
    if plan.is_empty:
        typer.echo("nothing to add")
    for command in plan.commands:
        typer.echo(f"add: {command}")
    for path in plan.files:
        typer.echo(f"write: {path}")


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
    """Serve the local dashboard until interrupted (ADR-016, ADR-033).

    Blocks in the foreground and is the *only* thing that ever starts a
    dashboard: nothing in ``factory run`` or ``factory start`` opens a
    socket. The server binds ``127.0.0.1`` and nothing else. It answers ``GET``
    and six ``POST`` routes that queue an approval, plan answers, a
    publish retry, a stop or a steer note for the factory, or start an escalation. It is
    protected by a token generated
    for this process; the tokenized URL is printed to stdout once and never
    written to the log.
    Ctrl-C stops it and closes the socket.
    """
    factory_config = _load_config(config, data_dir)
    _configure_logging(factory_config)

    from .dashboard import LOOPBACK_HOST, DashboardConfig
    from .dashboard.actions import ResumeActions
    from .escalation_protocol import ReplyPolicy
    from .models import RunListFilter
    from .observability import (
        RunScanCache,
        build_active_invocation_summary,
        build_monitoring_snapshot,
        build_operational_health,
        build_run_detail,
        build_run_diff,
        build_run_evidence,
        resolve_usage,
    )
    from .projects import FileProjectStore
    from .store import FileRunStore

    store = FileRunStore(factory_config.data_dir)
    stale_after = _stale_after(factory_config, None)
    reply_policy = ReplyPolicy.from_config(factory_config.escalation)
    scan_cache = RunScanCache(store)

    def snapshot_provider(
        *, limit: int, offset: int, filters: RunListFilter | None = None
    ) -> object:
        return build_monitoring_snapshot(
            store,
            stale_after=stale_after,
            limit=limit,
            offset=offset,
            max_scanned_runs=max_scanned_runs,
            scan=scan_cache.get_scan(max_scanned_runs),
            reply_policy=reply_policy,
            run_filter=filters,
        )

    def run_detail_provider(run_id: str) -> object | None:
        # Returns None (rendered as 404) for a run that does not exist or
        # cannot be read. The detail carries raw failure reasons and the
        # escalation text, so ``dashboard.view.run_detail_view`` must redact and
        # bound them before they reach a client. It never carries a log, a
        # diff, a prompt or a raw artifact body.
        return build_run_detail(
            store,
            run_id,
            stale_after=stale_after,
            reply_policy=reply_policy,
        )

    def health_provider() -> object:
        return build_operational_health(
            store,
            data_dir=factory_config.data_dir,
            stale_after=stale_after,
            max_scanned_runs=max_scanned_runs,
            scan=scan_cache.get_scan(max_scanned_runs),
        )

    def invocation_row(invocation: InvocationRecord) -> dict[str, object]:
        # Resolve per-model usage so the project totals count it like a run's.
        row = invocation.model_dump(mode="json")
        if invocation.usage is not None:
            row["usage"] = resolve_usage(invocation.usage).model_dump(mode="json")
        return row

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
                        **invocation_row(invocation),
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
                            **invocation_row(invocation),
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
                    "needs_look": execution_data["needs_look"],
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
                run_evidence_provider=lambda run_id: build_run_evidence(store, run_id),
                run_diff_provider=lambda run_id, attempt: build_run_diff(store, run_id, attempt),
                resume_request_reader=build_resume_request_reader(store),
                resume_actions=ResumeActions(
                    run_reader=build_resume_run_reader(store),
                    requester=build_resume_requester(store),
                    reply_policy=reply_policy,
                    stopper=build_stop_requester(store),
                    steerer=build_steer_requester(store),
                    escalator=build_escalator(store, factory_config.data_dir),
                ),
                host=LOOPBACK_HOST,
                port=port,
            )
        )
    except OSError as exc:
        raise _fail(f"could not bind the dashboard to {LOOPBACK_HOST}:{port}: {exc}") from None

    typer.echo(f"dashboard: {server.dashboard_url}")
    typer.echo(
        "loopback only. it can queue an approval, answers, a retry, a stop or a note, "
        "or escalate a run. "
        "press Ctrl-C to quit."
    )
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
    no_risk_assessment: bool = typer.Option(
        False,
        NO_RISK_ASSESSMENT_FLAG,
        help="Turn off risk assessment for every run the service dispatches.",
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

    factory_config = _load_config(config, data_dir, model_profile, no_risk_assessment)
    _require_claude_code_effort(runtime, factory_config)
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
        requested_runtime=_default_runtime(runtime),
        # An env-var pi credential lives in the operator's shell and never
        # reaches the launchd service (only the plist's own
        # EnvironmentVariables does), so it must not satisfy this preflight.
        accept_pi_env_credentials=False,
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
        import os

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
            risk_assessment_disabled=no_risk_assessment,
            label=label,
            allow_source_dev=allow_source_dev,
            # A path, not a secret: carried into the plist so the service can
            # find the same ~/.pi/agent override the operator's shell uses
            # (the launchd environment otherwise only inherits PATH).
            pi_coding_agent_dir=os.environ.get("PI_CODING_AGENT_DIR"),
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


if __name__ == "__main__":
    app()
