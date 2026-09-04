"""Shared, non-collected helpers for the factory's integration tests.

Deliberately named without a ``test_`` prefix so pytest imports it as a plain
module rather than collecting it. Everything here is hermetic: no network, no
model calls, and every remote-touching ``git``/``gh`` invocation goes through
:class:`ScriptedRunner`.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from software_agent_factory.agents import AgentRequest, AgentResult, FakeAgentRuntime
from software_agent_factory.config import FactoryConfig
from software_agent_factory.github import GitHubClient, GitPublisher
from software_agent_factory.models import (
    AgentRole,
    Complexity,
    RepairContext,
    Risk,
    TriageResult,
    WorkItem,
)
from software_agent_factory.publishing import CIObserver, PullRequestPublisher
from software_agent_factory.store import FileRunStore
from software_agent_factory.workflow import WorkflowController


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
            "refiner": {"model": "claude-opus-5", "reasoning": "high"},
            "researcher": {"model": "gpt-5.6-sol", "reasoning": "high"},
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
        "scope_drift": {"max_replans": max_replans},
        "risk": {
            "R0": {"human_approval": False},
            "R1": {"human_approval": False},
            "R2": {"human_approval": True},
            "R3": {"human_approval": True},
        },
    }
    if pull_request is not None:
        payload["pull_request"] = pull_request
    if ci is not None:
        payload["ci"] = ci
    if scheduler is not None:
        payload["scheduler"] = scheduler
    return FactoryConfig.model_validate(payload)


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
        return AgentResult(
            role=AgentRole.TRIAGE,
            success=True,
            triage_result=TriageResult(
                factory_eligible=True,
                complexity=complexity,
                risk=risk,
                requirements_quality="clear",
                needs_research=needs_research,
                dependencies=[],
                unknowns=[],
                confidence=0.8,
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
        if tail[:2] == ["remote", "get-url"]:
            if self.remote_missing:
                return FakeCompleted(returncode=128, stderr="error: No such remote 'origin'")
            return FakeCompleted(stdout=f"{self.remote_url}\n")
        if tail[:1] == ["rev-parse"] and "--abbrev-ref" in tail:
            return FakeCompleted(stdout=f"{self.base_branch}\n")
        if tail[:1] == ["rev-parse"]:
            return FakeCompleted(stdout=f"{self.commit_sha}\n")
        if tail[:3] == ["diff", "--cached", "--name-only"]:
            return FakeCompleted(stdout="".join(f"{name}\n" for name in self.changed_files))
        return FakeCompleted()

    def _gh(self, argv: list[str]) -> FakeCompleted:
        tail = argv[1:]
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
