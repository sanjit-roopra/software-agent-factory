#!/usr/bin/env python3
"""A/B report comparing the pi and Copilot runtimes on replayed issues.

The manifest lists closed issues of this repository with the commit each one
started from. Each task is replayed once per runtime with the same models and
reasoning. This module holds the manifest schema, the report built from stored
runs, and the go bar that decides whether pi is "recommended" or
"experimental" (see ``plans/pi-agent-runtime.md``, Risks). The driver replays
the tasks under a budget and writes a JSON and a Markdown report.

Usage (this spends money; the operator runs it by hand)::

    uv run --no-sync python scripts/performance/runtime_ab.py \\
        --manifest scripts/performance/runtime_ab_manifest.json --repo . \\
        --max-copilot-premium-requests 60 --max-pi-usd 20 --out /tmp/runtime_ab/report.json

Local-only replay:

* ``factory run`` is called without ``--config`` unless the operator passes one.
  The packaged default keeps ``pull_request``, ``ci``, ``merge``, ``escalation``
  and ``scheduler`` off, so a run stops at ``PR_READY`` with no push, no pull
  request and no issue comment.
* Any ``--config`` is loaded first and refused when one of those sections is
  enabled (``ensure_local_only``).
* The only GitHub call is the read-only ``gh issue view`` that fetches each
  issue's title and body, done for every task before the first paid run.

Budget: stop starting a new task once any given limit is reached by the running
total: Copilot premium requests, pi list-price USD estimate or wall seconds.
Both runtimes always finish the task they started, so the comparison stays fair.

Reporting rules:

* Token totals reuse the observability usage summing, so an unreported field
  stays ``None`` and a reported zero stays ``0``.
* Cache-read share is ``cache_read / (input + cache_read + cache_write)``. It
  is ``None`` (unavailable) when the runtime reported no cache-read count and
  ``0.0`` when it reported one that is zero.
* Each runtime keeps its own cost unit. Copilot reports premium requests and
  nano-AIU. pi reports a list-price USD estimate that is never spend. The two
  are never added together.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, ValidationError, model_validator

from software_agent_factory.config import FactoryConfig, load_config
from software_agent_factory.models import (
    AgentRole,
    AttemptTrigger,
    FactoryRun,
    InvocationRecord,
    ModelBase,
    VerificationReport,
    WorkflowState,
)

# Runtime-reported usage summing lives in observability; reuse it rather than
# re-implementing the "unreported stays None" rules.
from software_agent_factory.observability import UsageSummary
from software_agent_factory.observability import _usage_summary as summarize_usage
from software_agent_factory.store import FileRunStore

type Level = Literal["L0", "L1", "L2", "L3"]

SUCCESS_STATES = frozenset({WorkflowState.PR_READY, WorkflowState.DONE})

#: Go bar (plans/pi-agent-runtime.md, Risks). Shares are fractions of 1.
CACHE_SHARE_MARGIN = 0.15
CACHE_SHARE_FLOOR = 0.70
TOKEN_CEILING_PERCENT = 80
_SHARE_TOLERANCE = 1e-9

CRITERION_PASS_COUNT = "pass_count"
CRITERION_CACHE_SHARE = "cache_read_share"
CRITERION_TOKENS = "tokens"

_UNAVAILABLE = "unavailable"


class ManifestError(ValueError):
    """The manifest is unreadable or has an invalid entry."""


class Runtime(StrEnum):
    COPILOT = "copilot"
    PI = "pi"


class ManifestEntry(ModelBase):
    """One replayable closed issue and the commit it started from."""

    issue: int = Field(ge=1)
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    level: Level | None = None
    title: str | None = Field(default=None, min_length=1)


class Manifest(ModelBase):
    tasks: tuple[ManifestEntry, ...]


def _describe_entry(index: int, raw: object) -> str:
    issue = raw.get("issue") if isinstance(raw, dict) else None
    return f"manifest entry {index} (issue {issue})"


def _validate_entry(index: int, raw: object) -> ManifestEntry:
    if not isinstance(raw, dict):
        raise ManifestError(f"{_describe_entry(index, raw)} is not an object")
    try:
        return ManifestEntry.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        raise ManifestError(f"{_describe_entry(index, raw)} is invalid: {problems}") from None


def _read_manifest_tasks(path: Path) -> list[object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest {path} is not valid JSON: {exc}") from None
    tasks = payload.get("tasks") if isinstance(payload, dict) else None
    if not isinstance(tasks, list) or not tasks:
        raise ManifestError(f"manifest {path} has no tasks")
    return tasks


def load_manifest(path: Path) -> Manifest:
    """Load and validate the manifest, naming the first invalid entry.

    Runs before any benchmark run so a bad entry never costs a paid call.
    """
    entries: list[ManifestEntry] = []
    seen: set[int] = set()
    for index, raw in enumerate(_read_manifest_tasks(path), start=1):
        entry = _validate_entry(index, raw)
        if entry.issue in seen:
            raise ManifestError(f"manifest entry {index}: duplicate issue {entry.issue}")
        seen.add(entry.issue)
        entries.append(entry)
    return Manifest(tasks=tuple(entries))


class RunSample(ModelBase):
    """What one runtime produced for one task."""

    passed: bool
    wall_seconds: float = Field(ge=0.0)
    repair_rounds: int = Field(ge=0)
    invocations: tuple[InvocationRecord, ...] = ()
    error: str | None = None


class TaskOutcome(ModelBase):
    """One manifest task with its per-runtime samples. No samples means skipped."""

    entry: ManifestEntry
    samples: dict[Runtime, RunSample] = Field(default_factory=dict)


def sample_from_run(run: FactoryRun, *, verification_passed: bool | None) -> RunSample:
    """Summarise one stored run. ``verification_passed`` is ``None`` when unknown."""
    finished = run.completed_at or run.updated_at
    repair_rounds = sum(
        1
        for attempt in run.attempt_records
        if attempt.role is AgentRole.IMPLEMENTER
        and attempt.triggered_by is not AttemptTrigger.INITIAL
    )
    return RunSample(
        passed=run.state in SUCCESS_STATES and verification_passed is not False,
        wall_seconds=(finished - run.created_at).total_seconds(),
        repair_rounds=repair_rounds,
        invocations=tuple(run.invocation_records),
    )


def load_sample(store: FileRunStore, run_id: str) -> RunSample:
    """Build a :class:`RunSample` from a run persisted in ``store``."""
    run = store.load_run(run_id)
    try:
        verification_passed: bool | None = store.load_artifact(run_id, VerificationReport).passed
    except FileNotFoundError:
        verification_passed = None
    return sample_from_run(run, verification_passed=verification_passed)


class TokenCounts(ModelBase):
    input: int | None = None
    output: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None


class RuntimeCost(ModelBase):
    """A runtime's own cost unit. Fields of the other runtime stay ``None``."""

    premium_requests: float | None = None
    total_nano_aiu: int | None = None
    list_price_estimate_usd: float | None = None


class RuntimeMetrics(ModelBase):
    runs: int
    passes: int
    tokens: TokenCounts
    cache_read_share: float | None
    cost: RuntimeCost
    wall_seconds: float
    repair_rounds: int


class TaskReport(ModelBase):
    issue: int
    level: Level | None
    title: str | None
    skipped: bool
    runtimes: dict[Runtime, RuntimeMetrics] = Field(default_factory=dict)
    errors: dict[Runtime, str] = Field(default_factory=dict)


class CriterionResult(ModelBase):
    name: str
    met: bool
    detail: str


class GoBarVerdict(ModelBase):
    meets: bool
    recommendation: Literal["recommended", "experimental"]
    criteria: tuple[CriterionResult, ...]


class RuntimeAbReport(ModelBase):
    tasks: tuple[TaskReport, ...]
    totals: dict[Runtime, RuntimeMetrics]
    verdict: GoBarVerdict


def cache_read_share(tokens: TokenCounts) -> float | None:
    """``None`` when no cache-read count was reported; ``0.0`` when reported as zero."""
    if tokens.cache_read is None:
        return None
    denominator = (tokens.input or 0) + tokens.cache_read + (tokens.cache_write or 0)
    return tokens.cache_read / denominator if denominator else 0.0


def _tokens(usage: UsageSummary) -> TokenCounts:
    return TokenCounts(
        input=usage.input_tokens,
        output=usage.output_tokens,
        cache_read=usage.cache_read_tokens,
        cache_write=usage.cache_write_tokens,
    )


def _cost(runtime: Runtime, usage: UsageSummary) -> RuntimeCost:
    if runtime is Runtime.COPILOT:
        return RuntimeCost(
            premium_requests=usage.premium_requests, total_nano_aiu=usage.total_nano_aiu
        )
    return RuntimeCost(list_price_estimate_usd=usage.list_price_estimate_usd)


def _metrics(runtime: Runtime, samples: Sequence[RunSample]) -> RuntimeMetrics:
    invocations: Iterable[InvocationRecord] = (
        invocation for sample in samples for invocation in sample.invocations
    )
    usage = summarize_usage(invocations)
    tokens = _tokens(usage)
    return RuntimeMetrics(
        runs=len(samples),
        passes=sum(1 for sample in samples if sample.passed),
        tokens=tokens,
        cache_read_share=cache_read_share(tokens),
        cost=_cost(runtime, usage),
        wall_seconds=sum(sample.wall_seconds for sample in samples),
        repair_rounds=sum(sample.repair_rounds for sample in samples),
    )


def _percent(share: float | None) -> str:
    return _UNAVAILABLE if share is None else f"{share:.1%}"


def _pass_count_criterion(copilot: RuntimeMetrics, pi: RuntimeMetrics) -> CriterionResult:
    return CriterionResult(
        name=CRITERION_PASS_COUNT,
        met=pi.passes >= copilot.passes,
        detail=f"pi {pi.passes} passes vs Copilot {copilot.passes}",
    )


def _cache_share_criterion(copilot: RuntimeMetrics, pi: RuntimeMetrics) -> CriterionResult:
    pi_share = pi.cache_read_share
    copilot_share = copilot.cache_read_share
    if pi_share is None:
        return CriterionResult(
            name=CRITERION_CACHE_SHARE, met=False, detail="pi cache-read share unavailable"
        )
    if copilot_share is None:
        return CriterionResult(
            name=CRITERION_CACHE_SHARE,
            met=pi_share >= CACHE_SHARE_FLOOR - _SHARE_TOLERANCE,
            detail=(
                f"Copilot share unavailable; pi {_percent(pi_share)} "
                f"vs floor {CACHE_SHARE_FLOOR:.0%}"
            ),
        )
    return CriterionResult(
        name=CRITERION_CACHE_SHARE,
        met=pi_share - copilot_share >= CACHE_SHARE_MARGIN - _SHARE_TOLERANCE,
        detail=(
            f"pi {_percent(pi_share)} vs Copilot {_percent(copilot_share)} "
            f"plus {CACHE_SHARE_MARGIN * 100:.0f} points"
        ),
    )


def _input_and_cache_write(metrics: RuntimeMetrics) -> int | None:
    if metrics.tokens.input is None:
        return None
    return metrics.tokens.input + (metrics.tokens.cache_write or 0)


def _tokens_criterion(copilot: RuntimeMetrics, pi: RuntimeMetrics) -> CriterionResult:
    pi_total = _input_and_cache_write(pi)
    copilot_total = _input_and_cache_write(copilot)
    if pi_total is None or copilot_total is None:
        return CriterionResult(
            name=CRITERION_TOKENS, met=False, detail="input tokens unavailable for a runtime"
        )
    return CriterionResult(
        name=CRITERION_TOKENS,
        met=pi_total * 100 <= copilot_total * TOKEN_CEILING_PERCENT,
        detail=(
            f"pi input + cache write {pi_total} vs Copilot {copilot_total} "
            f"(ceiling {TOKEN_CEILING_PERCENT}%)"
        ),
    )


def evaluate_go_bar(copilot: RuntimeMetrics, pi: RuntimeMetrics) -> GoBarVerdict:
    """Apply the plan's go bar to the completed-task totals of both runtimes."""
    criteria = (
        _pass_count_criterion(copilot, pi),
        _cache_share_criterion(copilot, pi),
        _tokens_criterion(copilot, pi),
    )
    meets = pi.runs > 0 and all(item.met for item in criteria)
    return GoBarVerdict(
        meets=meets,
        recommendation="recommended" if meets else "experimental",
        criteria=criteria,
    )


def _task_report(outcome: TaskOutcome) -> TaskReport:
    entry = outcome.entry
    return TaskReport(
        issue=entry.issue,
        level=entry.level,
        title=entry.title,
        skipped=not outcome.samples,
        runtimes={
            runtime: _metrics(runtime, [sample]) for runtime, sample in outcome.samples.items()
        },
        errors={
            runtime: sample.error
            for runtime, sample in outcome.samples.items()
            if sample.error is not None
        },
    )


def build_report(outcomes: Sequence[TaskOutcome]) -> RuntimeAbReport:
    """Build the per-task and total report plus the go-bar verdict."""
    totals = {
        runtime: _metrics(
            runtime,
            [outcome.samples[runtime] for outcome in outcomes if runtime in outcome.samples],
        )
        for runtime in Runtime
    }
    return RuntimeAbReport(
        tasks=tuple(_task_report(outcome) for outcome in outcomes),
        totals=totals,
        verdict=evaluate_go_bar(totals[Runtime.COPILOT], totals[Runtime.PI]),
    )


def _count(value: int | None) -> str:
    return _UNAVAILABLE if value is None else str(value)


def _format_cost(runtime: Runtime, cost: RuntimeCost) -> str:
    if runtime is Runtime.COPILOT:
        if cost.premium_requests is None and cost.total_nano_aiu is None:
            return _UNAVAILABLE
        return (
            f"{cost.premium_requests or 0:g} premium requests / {cost.total_nano_aiu or 0} nano-AIU"
        )
    if cost.list_price_estimate_usd is None:
        return _UNAVAILABLE
    return f"${cost.list_price_estimate_usd:.4f} list-price estimate"


_TABLE_HEADER = (
    "| Task | Runtime | Pass | Input | Output | Cache read | Cache write | Cache share "
    "| Cost | Wall s | Repairs |\n"
    "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
)


def _metrics_row(label: str, runtime: Runtime, metrics: RuntimeMetrics) -> str:
    tokens = metrics.tokens
    cells = (
        label,
        runtime.value,
        f"{metrics.passes}/{metrics.runs}",
        _count(tokens.input),
        _count(tokens.output),
        _count(tokens.cache_read),
        _count(tokens.cache_write),
        _percent(metrics.cache_read_share),
        _format_cost(runtime, metrics.cost),
        f"{metrics.wall_seconds:.1f}",
        str(metrics.repair_rounds),
    )
    return "| " + " | ".join(cells) + " |"


def _task_label(task: TaskReport) -> str:
    level = f" {task.level}" if task.level else ""
    return f"#{task.issue}{level}"


def _task_rows(task: TaskReport) -> list[str]:
    if task.skipped:
        return [f"| {_task_label(task)} | skipped (budget reached) |" + " |" * 9]
    return [
        _metrics_row(_task_label(task), runtime, metrics)
        for runtime, metrics in task.runtimes.items()
    ]


def render_markdown(report: RuntimeAbReport) -> str:
    """Render the report as Markdown: verdict, criteria, totals, then per task."""
    verdict = report.verdict
    headline = (
        "pi meets the go bar: recommended"
        if verdict.meets
        else "pi does not meet the go bar: experimental, not recommended"
    )
    lines = [
        "# Runtime A/B report",
        "",
        f"Go bar: {headline}",
        "",
        *(
            f"- {'met' if item.met else 'not met'} {item.name}: {item.detail}"
            for item in verdict.criteria
        ),
        "",
        "Costs use each runtime's own unit. The pi list-price estimate is never spend and is "
        "never added to Copilot usage.",
        "",
        "## Totals",
        "",
        _TABLE_HEADER,
        *(_metrics_row("total", runtime, metrics) for runtime, metrics in report.totals.items()),
        "",
        "## Per task",
        "",
        _TABLE_HEADER,
        *(row for task in report.tasks for row in _task_rows(task)),
        "",
    ]
    errors = [
        f"- {_task_label(task)} {runtime.value}: {message}"
        for task in report.tasks
        for runtime, message in task.errors.items()
    ]
    return "\n".join([*lines, *(["## Errors", "", *errors, ""] if errors else [])])


# --- driver ---------------------------------------------------------------------------------


class UnsafeConfigError(RuntimeError):
    """The factory config could publish to GitHub, or could not be loaded."""


class IssueFetchError(RuntimeError):
    """An issue's title and body could not be read."""


#: Config sections whose ``enabled`` flag lets a run reach GitHub.
_GITHUB_SECTIONS = ("pull_request", "ci", "merge", "escalation", "scheduler")

_RUN_TIMEOUT_EXIT_CODE = 124
_COMMAND_NOT_FOUND_EXIT_CODE = 127
_USAGE_ERROR_EXIT_CODE = 2
_STDERR_TAIL_CHARS = 500

type CommandRunner = Callable[[Sequence[str], Path], subprocess.CompletedProcess[str]]


class IssueText(ModelBase):
    title: str
    body: str


class Budget(ModelBase):
    """Stop starting new tasks once any given limit is reached by the running total."""

    max_copilot_premium_requests: float | None = Field(default=None, gt=0.0)
    max_pi_usd: float | None = Field(default=None, gt=0.0)
    max_wall_seconds: float | None = Field(default=None, gt=0.0)

    @model_validator(mode="after")
    def _require_a_limit(self) -> Budget:
        if (
            self.max_copilot_premium_requests is None
            and self.max_pi_usd is None
            and self.max_wall_seconds is None
        ):
            raise ValueError("set at least one budget limit")
        return self


@dataclass
class BudgetTotals:
    """Running spend. Copilot premium requests and pi USD are kept apart."""

    premium_requests: float = 0.0
    pi_usd: float = 0.0
    wall_seconds: float = 0.0

    def add(self, runtime: Runtime, sample: RunSample) -> None:
        usage = summarize_usage(sample.invocations)
        self.wall_seconds += sample.wall_seconds
        if runtime is Runtime.COPILOT:
            self.premium_requests += usage.premium_requests or 0.0
        else:
            self.pi_usd += usage.list_price_estimate_usd or 0.0


def budget_reached(budget: Budget, totals: BudgetTotals) -> bool:
    pairs = (
        (budget.max_copilot_premium_requests, totals.premium_requests),
        (budget.max_pi_usd, totals.pi_usd),
        (budget.max_wall_seconds, totals.wall_seconds),
    )
    return any(limit is not None and used >= limit for limit, used in pairs)


@dataclass(frozen=True)
class ReplayRequest:
    entry: ManifestEntry
    runtime: Runtime
    title: str
    description: str


type Runner = Callable[[ReplayRequest], RunSample]
type IssueFetcher = Callable[[int], IssueText]


def _request(entry: ManifestEntry, runtime: Runtime, text: IssueText) -> ReplayRequest:
    return ReplayRequest(
        entry=entry,
        runtime=runtime,
        title=entry.title or text.title,
        description=text.body.strip() or text.title,
    )


def run_benchmark(
    manifest: Manifest, *, budget: Budget, runner: Runner, fetch_issue: IssueFetcher
) -> list[TaskOutcome]:
    """Replay each task once per runtime; skip the rest once the budget is reached.

    Issue text is fetched for every task first, so a fetch failure costs nothing.
    """
    texts = {entry.issue: fetch_issue(entry.issue) for entry in manifest.tasks}
    totals = BudgetTotals()
    outcomes: list[TaskOutcome] = []
    for entry in manifest.tasks:
        if budget_reached(budget, totals):
            outcomes.append(TaskOutcome(entry=entry))
            continue
        samples: dict[Runtime, RunSample] = {}
        for runtime in Runtime:
            samples[runtime] = runner(_request(entry, runtime, texts[entry.issue]))
            totals.add(runtime, samples[runtime])
        outcomes.append(TaskOutcome(entry=entry, samples=samples))
    return outcomes


def ensure_local_only(config: FactoryConfig) -> None:
    """Refuse a config under which a run could push, open a PR or comment on GitHub."""
    enabled = [name for name in _GITHUB_SECTIONS if getattr(config, name).enabled]
    if enabled:
        raise UnsafeConfigError(
            f"the benchmark must stay local; config enables: {', '.join(enabled)}"
        )


def _tail(text: str) -> str:
    return text.strip()[-_STDERR_TAIL_CHARS:]


def _failed_sample(message: str) -> RunSample:
    return RunSample(passed=False, wall_seconds=0.0, repair_rounds=0, error=message)


class CliReplayRunner:
    """Replays one task with ``factory run`` in a detached worktree at the base commit.

    Each runtime and task gets its own worktree and its own data directory, so
    runs never share state. Commands go through the injected ``run_command``.
    """

    def __init__(
        self,
        *,
        repo: Path,
        workdir: Path,
        model_profile: str,
        config: Path | None,
        run_command: CommandRunner,
    ) -> None:
        self._repo = repo
        self._workdir = workdir
        self._model_profile = model_profile
        self._config = config
        self._run = run_command

    def __call__(self, request: ReplayRequest) -> RunSample:
        task_dir = self._workdir / request.runtime.value / f"issue-{request.entry.issue}"
        worktree = task_dir / "repo"
        data_dir = task_dir / "data"
        added = self._git("worktree", "add", "--detach", str(worktree), request.entry.base_sha)
        if added.returncode != 0:
            return _failed_sample(f"git worktree add failed: {_tail(added.stderr)}")
        try:
            completed = self._run(self._factory_command(request, worktree, data_dir), worktree)
        finally:
            self._git("worktree", "remove", "--force", str(worktree))
        return self._sample(data_dir, completed)

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self._run(["git", "-C", str(self._repo), *args], self._repo)

    def _factory_command(self, request: ReplayRequest, worktree: Path, data_dir: Path) -> list[str]:
        command = [
            sys.executable,
            "-m",
            "software_agent_factory",
            "run",
            "--repo",
            str(worktree),
            "--title",
            request.title,
            "--description",
            request.description,
            "--runtime",
            request.runtime.value,
            "--model-profile",
            self._model_profile,
            "--data-dir",
            str(data_dir),
            "--work-item-id",
            f"runtime-ab-{request.runtime.value}-issue-{request.entry.issue}",
        ]
        if self._config is not None:
            command += ["--config", str(self._config)]
        return command

    @staticmethod
    def _sample(data_dir: Path, completed: subprocess.CompletedProcess[str]) -> RunSample:
        store = FileRunStore(data_dir)
        runs = store.list_runs()
        if not runs:
            return _failed_sample(
                f"no run stored (exit {completed.returncode}): {_tail(completed.stderr)}"
            )
        return load_sample(store, runs[-1].id)


def verify_base_commits(manifest: Manifest, repo: Path, run_command: CommandRunner) -> None:
    """Fail before any paid run when a base commit is missing from ``repo``."""
    for entry in manifest.tasks:
        found = run_command(
            ["git", "-C", str(repo), "cat-file", "-e", f"{entry.base_sha}^{{commit}}"], repo
        )
        if found.returncode != 0:
            raise ManifestError(f"issue {entry.issue}: base commit {entry.base_sha} not in {repo}")


class GhIssueFetcher:
    """Reads an issue's title and body with the read-only ``gh issue view``."""

    def __init__(self, repo: Path, run_command: CommandRunner) -> None:
        self._repo = repo
        self._run = run_command

    def __call__(self, issue: int) -> IssueText:
        completed = self._run(
            ["gh", "issue", "view", str(issue), "--json", "title,body"], self._repo
        )
        if completed.returncode != 0:
            raise IssueFetchError(f"issue {issue}: gh failed: {_tail(completed.stderr)}")
        try:
            return IssueText.model_validate_json(completed.stdout)
        except ValidationError as exc:
            raise IssueFetchError(f"issue {issue}: unexpected gh output: {exc}") from None


def subprocess_command_runner(timeout_seconds: float) -> CommandRunner:
    """Real command runner. A timeout or a missing program becomes a failed result."""

    def run(args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                list(args), cwd=cwd, capture_output=True, text=True, timeout=timeout_seconds
            )
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(
                list(args), _RUN_TIMEOUT_EXIT_CODE, "", f"timed out after {timeout_seconds:g}s"
            )
        except FileNotFoundError as exc:
            return subprocess.CompletedProcess(
                list(args), _COMMAND_NOT_FOUND_EXIT_CODE, "", str(exc)
            )

    return run


def _load_local_config(path: Path | None, model_profile: str) -> FactoryConfig:
    try:
        return load_config(path, model_profile=model_profile)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise UnsafeConfigError(f"config could not be loaded: {exc}") from None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay closed issues with Copilot and pi and compare the runtimes."
    )
    parser.add_argument("--manifest", type=Path, required=True, help="manifest JSON file")
    parser.add_argument("--repo", type=Path, default=Path("."), help="this repository's checkout")
    parser.add_argument(
        "--workdir", type=Path, help="worktrees and per-run data (default: a new temp directory)"
    )
    parser.add_argument("--out", type=Path, required=True, help="JSON report path; .md beside it")
    parser.add_argument("--model-profile", default="default", help="same profile for both runtimes")
    parser.add_argument("--config", type=Path, help="factory config; must not enable publishing")
    parser.add_argument("--max-copilot-premium-requests", type=float)
    parser.add_argument("--max-pi-usd", type=float, help="pi list-price estimate, not spend")
    parser.add_argument("--max-wall-seconds", type=float)
    parser.add_argument(
        "--run-timeout-seconds", type=float, default=3600.0, help="limit for each command"
    )
    return parser


def _write_reports(report: RuntimeAbReport, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    markdown = out.with_suffix(".md")
    markdown.write_text(render_markdown(report), encoding="utf-8")
    return markdown


def main(
    argv: Sequence[str] | None = None,
    *,
    run_command: CommandRunner | None = None,
    fetch_issue: IssueFetcher | None = None,
) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        budget = Budget(
            max_copilot_premium_requests=args.max_copilot_premium_requests,
            max_pi_usd=args.max_pi_usd,
            max_wall_seconds=args.max_wall_seconds,
        )
    except ValueError as exc:
        parser.error(str(exc))
    command = run_command or subprocess_command_runner(args.run_timeout_seconds)
    fetch = fetch_issue or GhIssueFetcher(args.repo, command)
    try:
        manifest = load_manifest(args.manifest)
        ensure_local_only(_load_local_config(args.config, args.model_profile))
        verify_base_commits(manifest, args.repo, command)
        workdir = args.workdir or Path(tempfile.mkdtemp(prefix="runtime_ab_"))
        runner = CliReplayRunner(
            repo=args.repo,
            workdir=workdir,
            model_profile=args.model_profile,
            config=args.config,
            run_command=command,
        )
        outcomes = run_benchmark(manifest, budget=budget, runner=runner, fetch_issue=fetch)
    except (ManifestError, UnsafeConfigError, IssueFetchError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _USAGE_ERROR_EXIT_CODE
    report = build_report(outcomes)
    markdown = _write_reports(report, args.out)
    print(f"{report.verdict.recommendation}: reports at {args.out} and {markdown}")
    print(f"runs and worktree data under {workdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
