"""Shared, non-collected helpers for the factory's integration tests.

Deliberately named without a ``test_`` prefix so pytest imports it as a plain
module rather than collecting it. Everything here is hermetic: no network, no
model calls, and every remote-touching ``git``/``gh`` invocation goes through
:class:`ScriptedRunner`.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Callable, Mapping, Sequence

from software_agent_factory.agents import AgentRequest, AgentResult, FakeAgentRuntime
from software_agent_factory.config import FactoryConfig
from software_agent_factory.escalation_protocol import ReplyPolicy
from software_agent_factory.github import GitHubClient, GitPublisher
from software_agent_factory.models import (
    AgentPurpose,
    AgentRole,
    Complexity,
    FactoryRun,
    ProjectPlan,
    ProjectTask,
    RepairContext,
    Risk,
    RiskRationale,
    TriageResult,
    WorkflowState,
    WorkItem,
)
from software_agent_factory.publishing import CIObserver, PullRequestPublisher
from software_agent_factory.store import FileRunStore
from software_agent_factory.workflow import WorkflowController

#: A reply policy for tests that do not care about it: escalation on, three reopens, a 24 hour
#: window. Change a field with ``dataclasses.replace``.
REPLY_POLICY = ReplyPolicy(
    max_reopens=3,
    reply_window_hours=24,
    escalation_enabled=True,
    allowed_hosts=("github.com",),
)


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout


def build_config(
    data_dir: Path,
    *,
    verify: list[str] | None = None,
    install: list[str] | None = None,
    build: list[str] | None = None,
    same_model_attempts: int = 2,
    max_total_attempts: int = 6,
    max_replans: int = 1,
    pull_request: dict[str, object] | None = None,
    ci: dict[str, object] | None = None,
    scheduler: dict[str, object] | None = None,
    max_changed_files: int = 100,
    polish_enabled: bool = False,
    approved_sensitive_files: list[str] | None = None,
    risk_assessment_enabled: bool = True,
) -> FactoryConfig:
    payload: dict[str, object] = {
        "factory": {
            "data_dir": str(data_dir),
            "retries": {
                "same_model_attempts": same_model_attempts,
                "max_total_attempts": max_total_attempts,
            },
        },
        "models": {
            "triage": {"model": "claude-sonnet-5", "reasoning": "medium"},
            "planner": {"model": "claude-opus-5", "reasoning": "high"},
            "workers": {
                "L0": {"model": "mai-code-1.1-flash", "reasoning": "medium"},
                "L1": {"model": "claude-sonnet-5", "reasoning": "medium"},
                "L2": {"model": "claude-opus-5", "reasoning": "high"},
                "L3": {"model": "claude-opus-5", "reasoning": "high"},
            },
            "tester": {"model": "claude-sonnet-5", "reasoning": "high"},
            "reviewer": {"model": "gpt-5.6-sol", "reasoning": "high"},
        },
        "repository": {
            "branch_prefix": "factory/",
            "command_timeout_seconds": 30,
            "commands": {
                "install": install or [],
                "verify": verify or [],
                "build": build or [],
            },
            "max_changed_files": max_changed_files,
        },
        "scope_drift": {
            "max_replans": max_replans,
            "approved_sensitive_files": approved_sensitive_files or [],
        },
        "polish": {"enabled": polish_enabled},
        "risk": {
            "R0": {"human_approval": False},
            "R1": {"human_approval": False},
            "R2": {"human_approval": True},
            "R3": {"human_approval": True},
        },
    }
    if not risk_assessment_enabled:
        payload["risk_assessment"] = {"enabled": False}
    if pull_request is not None:
        payload["pull_request"] = pull_request
    if ci is not None:
        payload["ci"] = ci
    if scheduler is not None:
        payload["scheduler"] = scheduler
    return FactoryConfig.model_validate(payload)


class CrashingRuntime:
    """Raises ``KeyboardInterrupt`` on one call of one role, as a killed factory process does."""

    def __init__(self, role: AgentRole, on_call: int = 1) -> None:
        self.role = role
        self.on_call = on_call
        self.requests: list[AgentRequest] = []
        self._delegate = FakeAgentRuntime()
        self._role_calls = 0

    @property
    def roles(self) -> list[AgentRole]:
        return [request.role for request in self.requests]

    def run(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        if request.role is self.role:
            self._role_calls += 1
            if self._role_calls == self.on_call:
                raise KeyboardInterrupt
        return self._delegate.run(request)


def work_item(work_item_id: str = "WI-1") -> WorkItem:
    return WorkItem(
        id=work_item_id,
        title="Reject empty customer names",
        description="Return HTTP 400 for empty or whitespace-only names.",
        acceptance_criteria=["Empty names are rejected with HTTP 400"],
    )


def triage_hook(
    complexity: Complexity = Complexity.L1,
    risk: Risk = Risk.R1,
    *,
    needs_research: bool = False,
):
    def hook(request: AgentRequest) -> AgentResult:
        rationale = (
            RiskRationale(
                intended_outcome="Update deployment credentials safely.",
                sensitive_boundary="Production deployment configuration.",
                necessity="Task requires modifying production deployment keys.",
                credible_scenario="Misconfiguration could cause service outage.",
                known_mitigations=["Validate syntax before deployment."],
                residual_risk="Manual operator review required before release.",
            )
            if risk in {Risk.R2, Risk.R3}
            else None
        )
        return AgentResult(
            role=AgentRole.TRIAGE,
            success=True,
            triage_result=TriageResult(
                factory_eligible=True,
                complexity=complexity,
                risk=risk,
                needs_research=needs_research,
                dependencies=[],
                unknowns=[],
                confidence=0.8,
                risk_rationale=rationale,
            ),
        )

    return hook


def repair_contexts(requests: Sequence[AgentRequest]) -> list[RepairContext]:
    return [r.repair_context for r in requests if isinstance(r.repair_context, RepairContext)]


@dataclass
class FakeCompleted:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


#: Local, non-network ``git`` commands that a fake runner must execute for
#: real: publication binds tree, parent and ref identity, which only git can
#: compute honestly. ``push``/``ls-remote``/``remote`` stay faked.
_LOCAL_GIT_COMMANDS: frozenset[str] = frozenset(
    {
        "write-tree",
        "rev-parse",
        "symbolic-ref",
        "commit-tree",
        "rev-list",
        "update-ref",
        "diff",
        "cat-file",
        "show",
        "log",
        "status",
        "add",
    }
)


@dataclass
class ScriptedRunner:
    """Fake ``CommandRunner`` for every remote-touching ``git``/``gh`` call.

    Responses are keyed by command shape rather than call order so one runner
    can serve repeated publish cycles (a CI repair pushes again).
    """

    remote_url: str = "https://github.com/acme/repo.git"
    changed_files: tuple[str, ...] = ("FACTORY_NOTES.md",)
    commit_sha: str = "0123456789abcdef0123456789abcdef01234567"
    base_branch: str = "main"
    pr_url: str = "https://github.com/acme/repo/pull/42"
    active_host: str = "github.com"
    check_responses: list[list[dict[str, str]]] = field(default_factory=list)
    run_log: str = ""
    remote_missing: bool = False
    calls: list[tuple[list[str], Path | None, Mapping[str, str] | None]] = field(
        default_factory=list
    )
    _checks_index: int = 0

    def __call__(
        self,
        args: Sequence[str],
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> FakeCompleted:
        argv = list(args)
        self.calls.append((argv, cwd, env))
        if argv and argv[0] == "git":
            return self._git(argv)
        if argv and argv[0].endswith("gh"):
            return self._gh(argv)
        return FakeCompleted()

    def _git(self, argv: list[str]) -> FakeCompleted:
        tail = argv[3:] if argv[1:2] == ["-C"] else argv[1:]
        repo = Path(argv[2]) if argv[1:2] == ["-C"] else None
        real_repo = repo is not None and (repo / ".git").exists()
        if tail[:2] == ["remote", "get-url"]:
            if self.remote_missing:
                return FakeCompleted(returncode=128, stderr="error: No such remote 'origin'")
            return FakeCompleted(stdout=f"{self.remote_url}\n")
        if tail[:1] == ["rev-parse"] and "--abbrev-ref" in tail:
            return FakeCompleted(stdout=f"{self.base_branch}\n")
        if real_repo and tail[:1] and tail[0] in _LOCAL_GIT_COMMANDS:
            # Only the network is faked. Commit identity (tree, parent, ref
            # advancement) is produced by real git so a publication cannot
            # appear valid against a mock while being wrong against git.
            assert repo is not None
            result = subprocess.run(["git", "-C", str(repo), *tail], capture_output=True, text=True)
            return FakeCompleted(
                returncode=result.returncode, stdout=result.stdout, stderr=result.stderr
            )
        if tail == ["write-tree"] or (
            tail[:1] == ["rev-parse"] and any(arg.endswith("^{tree}") for arg in tail)
        ):
            return FakeCompleted(stdout=f"{self.commit_sha}\n")
        if tail[:1] == ["rev-parse"]:
            return FakeCompleted(stdout=f"{self.commit_sha}\n")
        if tail[:3] == ["diff", "--cached", "--name-only"]:
            return FakeCompleted(stdout="".join(f"{name}\n" for name in self.changed_files))
        return FakeCompleted()

    def _gh(self, argv: list[str]) -> FakeCompleted:
        tail = argv[1:]
        if tail[:2] == ["auth", "status"]:
            return FakeCompleted(
                stdout=json.dumps(
                    {
                        "hosts": {
                            self.active_host: [
                                {
                                    "active": True,
                                    "host": self.active_host,
                                    "state": "success",
                                }
                            ]
                        }
                    }
                )
            )
        if tail[:2] == ["pr", "create"]:
            return FakeCompleted(stdout=f"{self.pr_url}\n")
        if tail[:2] == ["pr", "checks"]:
            index = min(self._checks_index, len(self.check_responses) - 1)
            payload = self.check_responses[index] if self.check_responses else []
            self._checks_index += 1
            return FakeCompleted(stdout=json.dumps(payload))
        if tail[:2] == ["run", "view"]:
            return FakeCompleted(stdout=self.run_log)
        return FakeCompleted()

    def commands(self, executable: str) -> list[list[str]]:
        return [argv for argv, _cwd, _env in self.calls if argv and argv[0].endswith(executable)]

    def pushes(self) -> list[list[str]]:
        return [argv for argv in self.commands("git") if "push" in argv]

    @property
    def pr_bodies(self) -> list[str]:
        bodies: list[str] = []
        for argv, _cwd, _env in self.calls:
            if argv[1:3] == ["pr", "create"] and "--body" in argv:
                bodies.append(argv[argv.index("--body") + 1])
        return bodies


def check_payload(
    name: str, bucket: str, *, description: str = "", link: str = ""
) -> dict[str, str]:
    return {
        "name": name,
        "bucket": bucket,
        "state": bucket,
        "description": description,
        "link": link,
    }


def build_controller(
    config: FactoryConfig,
    store: FileRunStore,
    runtime: FakeAgentRuntime,
    runner: ScriptedRunner | None = None,
) -> WorkflowController:
    """Controller wired to fake ``git``/``gh`` boundaries when ``runner`` is given."""
    if runner is None:
        return WorkflowController(config, store, runtime)
    publisher = PullRequestPublisher(
        config,
        publisher=GitPublisher(
            runner=runner,
            remote=config.pull_request.remote,
            branch_prefix=config.repository.branch_prefix,
            base_branch=runner.base_branch,
            max_changed_files=config.repository.max_changed_files,
            allowed_hosts=frozenset(config.pull_request.allowed_hosts),
        ),
        client=GitHubClient(runner=runner),
        token=None,
        runner=runner,
    )
    observer = CIObserver(
        config, client=GitHubClient(runner=runner), token=None, sleep=lambda _seconds: None
    )
    return WorkflowController(config, store, runtime, publisher=publisher, ci_observer=observer)


_open_fake_pi_processes: list[FakePiProcess] = []


# double-waiver: B1 — out-of-process pi subprocess handle
class FakePiProcess:
    """``PiProcessHandle``-shaped double backed by real ``os.pipe()`` fds.

    Real pipe fds let ``PiRpcClient``'s raw-fd ``select``/``os.read`` loop run
    against real, deterministic file descriptors: a test writes scripted JSONL
    records into the end the client reads, exactly as a real ``pi`` process
    would. ``stdin``/``stdout``/``stderr`` are the client's ends; the
    ``_*_write``/``_*_read`` counterparts are the test's ends. ``stderr=False``
    mirrors a real ``Popen(stderr=subprocess.DEVNULL)`` handle
    (``.stderr is None``), as ``scripts/performance/pi_cache_probe.py`` uses.

    Every instance registers itself so the autouse ``conftest`` fixture can
    :meth:`close` it after the test: the pipes are real fds and would
    otherwise leak until garbage collection.
    """

    def __init__(self, *, stderr: bool = True) -> None:
        stdin_read_fd, stdin_write_fd = os.pipe()
        stdout_read_fd, stdout_write_fd = os.pipe()

        self.stdin: IO[str] | None = os.fdopen(stdin_write_fd, "w")
        self._stdin_read = os.fdopen(stdin_read_fd, "r")
        self.stdout: IO[str] | None = os.fdopen(stdout_read_fd, "r")
        self._stdout_write = os.fdopen(stdout_write_fd, "w")

        self.stderr: IO[str] | None = None
        self._stderr_write: IO[str] | None = None
        if stderr:
            stderr_read_fd, stderr_write_fd = os.pipe()
            self.stderr = os.fdopen(stderr_read_fd, "r")
            self._stderr_write = os.fdopen(stderr_write_fd, "w")

        self.pid = 999_999
        self._returncode: int | None = None
        self._wait_returncode: int | None = None
        #: Raised by :meth:`communicate`, e.g. a ``UnicodeDecodeError`` as a
        #: real text-mode ``Popen`` raises on undecodable leftover output.
        self.communicate_error: Exception | None = None
        _open_fake_pi_processes.append(self)

    def write_records(self, *records: Mapping[str, Any]) -> None:
        for record in records:
            self._stdout_write.write(json.dumps(record) + "\n")
        self._stdout_write.flush()

    def write_raw_stdout(self, text: str) -> None:
        self._stdout_write.write(text)
        self._stdout_write.flush()

    def write_stdout_bytes(self, data: bytes) -> None:
        self._stdout_write.buffer.write(data)
        self._stdout_write.flush()

    def write_stderr(self, text: str) -> None:
        assert self._stderr_write is not None
        self._stderr_write.write(text)
        self._stderr_write.flush()

    def write_stderr_bytes(self, data: bytes) -> None:
        assert self._stderr_write is not None
        self._stderr_write.buffer.write(data)
        self._stderr_write.flush()

    def close_stdout(self) -> None:
        self._stdout_write.close()

    def exit(self, returncode: int) -> None:
        self._returncode = returncode
        self._wait_returncode = returncode

    def exit_pending_reap(self, returncode: int) -> None:
        """Simulate a process that has exited but not yet been reaped by ``poll()``.

        ``poll()`` still reports ``None`` (not yet observed), while ``wait()``
        successfully reaps it and returns ``returncode``.
        """
        self._wait_returncode = returncode

    def poll(self) -> int | None:
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        if self._wait_returncode is None:
            raise subprocess.TimeoutExpired(cmd="fake-pi", timeout=timeout or 0)
        return self._wait_returncode

    def communicate(self, *, timeout: float | None = None) -> tuple[str, str]:
        if self.communicate_error is not None:
            raise self.communicate_error
        return ("", "")

    def sent_commands(self) -> list[dict[str, Any]]:
        """Read back what ``PiRpcClient`` wrote to stdin so far (non-blocking)."""
        os.set_blocking(self._stdin_read.fileno(), False)
        commands: list[dict[str, Any]] = []
        try:
            for line in self._stdin_read:
                stripped = line.strip()
                if stripped:
                    commands.append(json.loads(stripped))
        except BlockingIOError:
            pass
        return commands

    def close(self) -> None:
        """Close every pipe end this double still holds. Safe to call twice."""
        for stream in (
            self.stdin,
            self._stdin_read,
            self.stdout,
            self._stdout_write,
            self.stderr,
            self._stderr_write,
        ):
            if stream is None:
                continue
            try:
                stream.close()
            except OSError:
                pass


def close_open_fake_pi_processes() -> None:
    """Close and forget every :class:`FakePiProcess` created so far."""
    while _open_fake_pi_processes:
        _open_fake_pi_processes.pop().close()


class FakePiClock:
    """Deterministic monotonic clock for ``PiRpcClient``/``PiAgentRuntime`` timeout tests.

    Time only moves when the code under test would block: the ``pi_fake_clock``
    fixture swaps ``select.select`` for a poll that advances :attr:`now` by the
    requested timeout when nothing is ready, so a "1 second" timeout elapses
    instantly and deterministically.
    """

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def deadline(self, seconds: float) -> float:
        return self.now + seconds


def planner_with_dependencies(
    fallback: Callable[[AgentRequest], AgentResult], *dependencies: tuple[int, ...]
) -> Callable[[AgentRequest], AgentResult]:
    """A planner whose project plan has one task per entry, each with those dependencies.

    ``fallback`` answers every other request, for example the task execution plans.
    """

    def planner(request: AgentRequest) -> AgentResult:
        if request.purpose is not AgentPurpose.DECOMPOSE_PROJECT:
            return fallback(request)
        assert request.project_brief is not None
        return AgentResult(
            role=AgentRole.PLANNER,
            success=True,
            project_plan=ProjectPlan(
                project_id=request.project_brief.id,
                summary="Planned outcomes.",
                delivery_approach="Each task is one independently verifiable outcome.",
                tasks=tuple(
                    ProjectTask(
                        id=number,
                        title=f"Outcome number {number}",
                        description=f"Implement outcome number {number}.",
                        acceptance_criteria=(f"Outcome number {number} works.",),
                        dependencies=task_dependencies,
                    )
                    for number, task_dependencies in enumerate(dependencies, start=1)
                ),
            ),
        )

    return planner


class ScriptedController:
    """Runs the real controller, except for tasks told to end another way.

    A ``WorkflowState`` ends the child run there, an ``Exception`` is raised
    from the dispatch, and a tuple of text lets the child run finish but gives
    it those ``needs_look`` reasons.
    """

    def __init__(
        self,
        delegate: WorkflowController,
        store: FileRunStore,
        outcomes: Mapping[int, WorkflowState | Exception | tuple[str, ...]],
    ) -> None:
        self._delegate = delegate
        self._store = store
        self._outcomes = outcomes

    def run(
        self, work_item: WorkItem, source_repo: Path, *, run_id: str | None = None
    ) -> FactoryRun:
        outcome = self._outcomes.get(work_item.project_task_id or 0)
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, WorkflowState):
            run = FactoryRun(
                id=run_id or "run-scripted",
                work_item_id=work_item.id,
                state=outcome,
                failure_reason="scripted rejection",
            )
            self._store.save_run(run)
            return run
        run = self._delegate.run(work_item, source_repo, run_id=run_id)
        if outcome is not None:
            run = run.model_copy(update={"needs_look": list(outcome)})
            self._store.save_run(run)
        return run

    def resume(self, run_id: str, source_repo: Path) -> FactoryRun:
        return self._delegate.resume(run_id, source_repo)
