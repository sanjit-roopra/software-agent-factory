"""The ``factory`` command-line interface.

```bash
factory run --repo PATH --title TEXT --description TEXT [--runtime fake|copilot]
factory runs
factory show RUN_ID
factory start --repo PATH --github-repo OWNER/NAME [--once]
```

``--runtime`` defaults to ``fake`` so no command ever makes a paid model call
by accident; ``--runtime copilot`` opts in to the real
:class:`~software_agent_factory.copilot_runtime.CopilotAgentRuntime`.

Pull request creation, CI observation and the backlog daemon are all strictly
opt-in through configuration (``pull_request.enabled``, ``ci.enabled``,
``scheduler.enabled``). With the packaged defaults, ``factory run`` performs no
network access at all and finishes at ``PR_READY``.

``--data-dir`` overrides the configured data directory so tests and demos can
point the CLI at an isolated temporary directory without editing a config file.
"""

from __future__ import annotations

import json
import signal
import threading
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

import typer

from .agents import AgentRuntime, FakeAgentRuntime
from .config import FactoryConfig, load_config
from .copilot_runtime import CopilotAgentRuntime
from .models import ChangeSet, WorkflowState, WorkItem
from .service import FactoryService
from .store import FileRunStore
from .workflow import WorkflowController

app = typer.Typer(help="Local-first autonomous software engineering factory.")

#: States that mean "the factory finished this work item successfully".
SUCCESS_STATES = frozenset({WorkflowState.PR_READY, WorkflowState.DONE})

CONFIG_ERROR_EXIT_CODE = 2


class RuntimeChoice(StrEnum):
    FAKE = "fake"
    COPILOT = "copilot"


def _load_config(config: Path | None, data_dir: Path | None) -> FactoryConfig:
    loaded = load_config(config)
    if data_dir is not None:
        loaded = loaded.model_copy(
            update={
                "factory": loaded.factory.model_copy(
                    update={"data_dir": data_dir.expanduser()}
                )
            }
        )
    return loaded


def _build_runtime(choice: RuntimeChoice) -> AgentRuntime:
    if choice is RuntimeChoice.COPILOT:
        return CopilotAgentRuntime()
    return FakeAgentRuntime()


@app.command("run")
def run_command(
    repo: Path = typer.Option(..., "--repo", help="Path to the target Git repository."),
    title: str = typer.Option(..., "--title", help="Short title for the work item."),
    description: str = typer.Option(
        ..., "--description", help="Description of the work to perform."
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
    config: Path = typer.Option(
        None, "--config", help="Path to a factory config YAML file (default: packaged config)."
    ),
    data_dir: Path = typer.Option(
        None, "--data-dir", help="Override the configured data directory."
    ),
) -> None:
    """Run one work item synchronously through the factory workflow."""
    factory_config = _load_config(config, data_dir)
    store = FileRunStore(factory_config.data_dir)
    controller = WorkflowController(factory_config, store, _build_runtime(runtime))

    work_item = WorkItem(
        id=work_item_id or f"WI-{uuid4().hex[:12]}",
        title=title,
        description=description,
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
    if run.failure_reason is not None:
        typer.echo(f"reason: {run.failure_reason}")

    try:
        change_set = store.load_artifact(run.id, ChangeSet)
    except FileNotFoundError:
        change_set = None
    if change_set is not None:
        typer.echo(f"changed files: {', '.join(change_set.changed_files) or '(none)'}")

    if run.state not in SUCCESS_STATES:
        raise typer.Exit(code=1)


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
    factory_config = _load_config(config, data_dir)
    if not factory_config.scheduler.enabled:
        typer.echo(
            "scheduler is disabled: set 'scheduler.enabled: true' in the factory "
            "configuration before running 'factory start'.",
            err=True,
        )
        raise typer.Exit(code=CONFIG_ERROR_EXIT_CODE)

    store = FileRunStore(factory_config.data_dir)
    service = FactoryService(
        config=factory_config,
        store=store,
        runtime=_build_runtime(runtime),
        source_repo=repo,
        github_repo=github_repo,
    )

    if once:
        try:
            report = service.run_once()
            typer.echo(f"candidates: {report.candidates_fetched}")
            typer.echo(f"dispatched: {', '.join(report.dispatched) or '(none)'}")
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
        f"(concurrency {factory_config.scheduler.max_concurrent_tasks})"
    )
    service.run_forever(stop_event)


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
    factory_config = _load_config(config, data_dir)
    store = FileRunStore(factory_config.data_dir)

    try:
        run = store.load_run(run_id)
    except FileNotFoundError:
        typer.echo(f"no such run: {run_id}", err=True)
        raise typer.Exit(code=1) from None

    typer.echo(json.dumps(json.loads(run.model_dump_json()), indent=2))


if __name__ == "__main__":
    app()
