from __future__ import annotations

import json
import os
import signal
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from factory_testing import FakePiClock, FakePiProcess
from prompt_fixtures import (
    ACCEPTED_FINDING_MESSAGE,
    BRIEF_TEXT,
    DEBT_DIFF,
    DIFF,
    FIRST_CALL_TEXT,
    FIRST_REVIEW_TESTER_FINDING,
    OPENING_TEXT,
    OUTPUT_REJECTION,
    POLISH_SUMMARY,
    PRIOR_FINDING_MESSAGE,
    RE_REVIEW_TESTER_FINDING,
    REPAIR_DIFF,
    REPAIRED_DIFF,
    SKILL_GUIDANCE,
    VERIFICATION_FAILURE,
    accepted_debt_review_request,
    change_set_correction_request,
    first_implementer_request,
    first_review_request,
    make_request,
    polish_request,
    re_review_request,
    verification_repair_request,
    with_output_rejection,
    work_item,
)

from software_agent_factory.agent_artifact import parse_agent_artifact
from software_agent_factory.agents import (
    RUNTIME_FAILURE_REASON_LIMIT,
    AgentRequest,
    AgentResult,
    is_retryable_typed_artifact_failure,
)
from software_agent_factory.config import PiConfig
from software_agent_factory.copilot_runtime import parse_copilot_artifact
from software_agent_factory.models import (
    AgentPurpose,
    AgentRole,
    ChangeSet,
    RepositoryProfile,
    TriageResult,
)
from software_agent_factory.pi_rpc import PiRpcClient
from software_agent_factory.pi_runtime import (
    _BEST_EFFORT_USAGE_DEADLINE_SECONDS,
    PiAgentRuntime,
    ProcessFactory,
    _default_process_factory,
    usage_from_pi_messages,
)
from software_agent_factory.prompts import build_prompt, build_prompt_sections, section_hashes
from software_agent_factory.subprocess_utils import sanitize_output


def _correction_request(**overrides: object) -> AgentRequest:
    defaults: dict[str, object] = {
        "purpose": AgentPurpose.CORRECT_CHANGE_SET,
        "change_set": ChangeSet(summary="Fix output shape"),
        "workspace_path": "/workspaces/wi-1",
    }
    defaults.update(overrides)
    return make_request(AgentRole.IMPLEMENTER, **defaults)


def _skill_request(**overrides: object) -> AgentRequest:
    defaults: dict[str, object] = {
        "purpose": AgentPurpose.GENERATE_REPOSITORY_SKILL,
        "repository_profile": RepositoryProfile(
            manifest_fingerprint="a" * 64,
            dependency_fingerprint="b" * 64,
        ),
        "official_documentation_origins": ["https://react.dev"],
        "practice_reference_urls": ["https://example.com/review.md"],
        "workspace_path": "/runs/RUN-1",
    }
    defaults.update(overrides)
    return make_request(AgentRole.RESEARCHER, **defaults)


_SESSIONS_DIRECTORY = "pi-sessions"
_IMPLEMENTER_SIDECAR = "implementer.meta.json"

#: Where ``_runtime`` puts the pi session store; ``_isolated_data_dir`` points it at a temp dir.
_DATA_DIR = [Path("/nonexistent-data-dir")]


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path: Path) -> None:
    """Keep the session store of every runtime under the test's own temp dir."""
    _DATA_DIR[0] = tmp_path / "factory-data"


def _runtime(
    *, process_factory: ProcessFactory | None = None, **overrides: object
) -> PiAgentRuntime:
    config = PiConfig(**overrides)
    kwargs: dict[str, object] = {}
    if process_factory is not None:
        kwargs["process_factory"] = process_factory
    return PiAgentRuntime(config, data_dir=_DATA_DIR[0], **kwargs)


class StdinEofExitProcess(FakePiProcess):
    """A pi-like fake that exits when its stdin is closed, not in response to abort.

    Real ``pi`` shuts down on stdin EOF, independent of whether it ever
    receives or acts on an RPC ``abort`` command. Wraps ``self.stdin``'s
    ``close`` so closing it (as :meth:`PiAgentRuntime._abort_and_kill` now
    does) flips this fake to "exited", the same way a real pi process would
    without needing to model actual EOF detection over the pipe.
    """

    def __init__(self) -> None:
        super().__init__()
        assert self.stdin is not None
        real_close = self.stdin.close

        def _close_and_exit() -> None:
            real_close()
            self.exit(0)

        self.stdin.close = _close_and_exit  # type: ignore[method-assign]


def _get_messages_response(
    stop_reason: str | None = None,
    error_message: str | None = None,
    *,
    usage: dict[str, Any] | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """The ``get_messages`` response :meth:`PiAgentRuntime.run` reads for ``c2``.

    ``stop_reason=None`` mirrors a message that settled normally (no
    ``stopReason`` field carried at all). ``usage``/``model`` attach a
    per-message ``usage`` mapping and a ``model`` id, the shape
    :func:`~software_agent_factory.pi_runtime.usage_from_pi_messages` reads;
    passing either includes the message in the response even when
    ``stop_reason`` is ``None`` (a settled call still has a message pi
    reports usage against).
    """
    message: dict[str, Any] = {"role": "assistant"}
    if stop_reason is not None:
        message["stopReason"] = stop_reason
    if error_message is not None:
        message["errorMessage"] = error_message
    if usage is not None:
        message["usage"] = usage
    if model is not None:
        message["model"] = model
    include_message = stop_reason is not None or usage is not None
    return {
        "type": "response",
        "id": "c2",
        "success": True,
        "data": {"messages": [message] if include_message else []},
    }


def _scripted_process(
    final_assistant_text: str,
    *,
    stop_reason: str | None = None,
    usage: dict[str, Any] | None = None,
    model: str | None = None,
) -> FakePiProcess:
    """A ``FakePiProcess`` that answers ``prompt``, ``get_messages``, then
    ``get_last_assistant_text``.

    Matches the exchange :meth:`PiAgentRuntime.run` drives: a ``prompt``
    command (id ``c1``), settling via one ``agent_settled`` event, a
    ``get_messages`` command (id ``c2``) whose final message carries
    ``stop_reason``/``usage``/``model`` (all ``None`` by default -- a
    normally-settled call with no usage to report), then a
    ``get_last_assistant_text`` command (id ``c3``) answering with
    ``final_assistant_text``.
    """
    process = FakePiProcess()
    process.write_records(
        {"type": "response", "id": "c1", "success": True},
        {"type": "agent_settled"},
        _get_messages_response(stop_reason=stop_reason, usage=usage, model=model),
        {
            "type": "response",
            "id": "c3",
            "success": True,
            "data": {"text": final_assistant_text},
        },
    )
    process.exit(0)
    return process


# ---------------------------------------------------------------------------
# run: what pi is launched with (command, cwd) per role/purpose
# ---------------------------------------------------------------------------


class _Launch(NamedTuple):
    command: list[str]
    cwd: Path
    env: dict[str, str]


def _launch(request: AgentRequest | None = None, **pi_config: object) -> _Launch:
    """Run ``request`` (default: a triage call) and return what pi was launched with.

    ``pi_config`` overrides :class:`PiConfig` fields (``provider``, ...).

    The process factory is the seam: it records the ``(command, cwd, env)``
    the runtime hands it, then answers with a process that settles normally.
    Whether the call then succeeds depends on the role's artifact, which these
    launch assertions do not care about.
    """
    launches: list[_Launch] = []
    process = _scripted_process(json.dumps(_TRIAGE_JSON))

    def factory(command: Sequence[str], cwd: Path, env: dict[str, str]) -> FakePiProcess:
        launches.append(_Launch(list(command), cwd, env))
        return process

    _runtime(process_factory=factory, **pi_config).run(request or make_request(AgentRole.TRIAGE))
    assert len(launches) == 1
    return launches[0]


def _tools_of(command: list[str]) -> str:
    return command[command.index("--tools") + 1]


def test_run_implementer_launches_pi_with_write_tools_in_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    request = make_request(AgentRole.IMPLEMENTER, workspace_path=str(tmp_path))

    launch = _launch(request)

    assert launch.command == [
        "pi",
        "--mode",
        "rpc",
        "--provider",
        "github-copilot",
        "--model",
        "claude-sonnet-5",
        "--thinking",
        "high",
        "--tools",
        "read,bash,edit,write,grep,find,ls",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-context-files",
        "--no-approve",
        "--session",
        str(_DATA_DIR[0] / _SESSIONS_DIRECTORY / "+w+i-1" / "implementer.jsonl"),
    ]
    assert launch.cwd == tmp_path.resolve()
    assert "GITHUB_TOKEN" not in launch.env


@pytest.mark.parametrize(
    "role",
    [
        AgentRole.TRIAGE,
        AgentRole.REFINER,
        AgentRole.RESEARCHER,
        AgentRole.PLANNER,
        AgentRole.TESTER,
        AgentRole.REVIEWER,
    ],
)
def test_run_read_only_roles_launch_pi_with_read_only_tools_in_the_process_cwd(
    role: AgentRole, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    launch = _launch(make_request(role))

    assert _tools_of(launch.command) == "read,grep,find,ls"
    assert "--no-tools" not in launch.command
    assert launch.cwd == tmp_path.resolve()


def test_run_read_only_role_uses_its_workspace_when_the_request_has_one(tmp_path: Path) -> None:
    launch = _launch(make_request(AgentRole.REVIEWER, workspace_path=str(tmp_path)))

    assert launch.cwd == tmp_path.resolve()


def test_run_change_set_correction_launches_pi_without_tools_in_the_workspace(
    tmp_path: Path,
) -> None:
    launch = _launch(_correction_request(workspace_path=str(tmp_path)))

    assert "--no-tools" in launch.command
    assert "--tools" not in launch.command
    assert launch.cwd == tmp_path.resolve()


def test_run_rejects_skill_generation_before_starting_pi() -> None:
    started: list[Sequence[str]] = []

    def factory(command: Sequence[str], cwd: Path, env: dict[str, str]) -> FakePiProcess:
        started.append(command)
        raise AssertionError("pi must not be started for a rejected request")

    runtime = _runtime(process_factory=factory)

    request = _skill_request()

    with pytest.raises(ValueError, match="not supported on pi"):
        runtime.run(request)

    assert started == []


def test_run_launches_the_configured_executable_and_provider() -> None:
    launch = _launch(executable="pi-beta", provider="anthropic")

    assert launch.command[0] == "pi-beta"
    assert launch.command[launch.command.index("--provider") + 1] == "anthropic"


def test_run_launches_the_requested_model_and_reasoning_level() -> None:
    launch = _launch(make_request(AgentRole.TRIAGE, model="gpt-5", reasoning="high"))

    assert launch.command[launch.command.index("--model") + 1] == "gpt-5"
    assert launch.command[launch.command.index("--thinking") + 1] == "high"


# ---------------------------------------------------------------------------
# run: session continuation (Slice 6)
# ---------------------------------------------------------------------------


_SESSION_START = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
_SESSION_MAX_AGE = 600  # not the config default, so the wiring is proven
_REVIEW_TEXT = json.dumps({"approved": True})
_CHANGE_SET_TEXT = json.dumps({"summary": "Reject empty customer names."})
_TEXT_BY_ROLE = {
    AgentRole.IMPLEMENTER: _CHANGE_SET_TEXT,
    AgentRole.REVIEWER: _REVIEW_TEXT,
}


class _SessionRig:
    """A pi runtime on a hand-moved session clock that records each launch.

    The process factory plays pi: it records the command, creates the session
    file named by ``--session`` (as pi does), and answers with a settled process.
    """

    def __init__(self, tmp_path: Path, **pi_config: object) -> None:
        self.data_dir = tmp_path / "factory-data"
        self.now = _SESSION_START
        self.commands: list[list[str]] = []
        self.prompts: list[str] = []
        self._process: FakePiProcess | None = None
        self.runtime = self._runtime_for(PiConfig(**pi_config))

    def _runtime_for(self, config: PiConfig) -> PiAgentRuntime:
        return PiAgentRuntime(
            config, self.data_dir, process_factory=self._start, clock=lambda: self.now
        )

    def switch_provider(self, provider: str) -> None:
        """Run later calls with another configured provider, on the same session store."""
        self.runtime = self._runtime_for(PiConfig(provider=provider))

    def _start(self, command: Sequence[str], _cwd: Path, _env: dict[str, str]) -> FakePiProcess:
        self.commands.append(list(command))
        session_path = _session_path_of(command)
        if session_path is not None:
            session_path.parent.mkdir(parents=True, exist_ok=True)
            session_path.touch()
            os.utime(session_path, (self.now.timestamp(), self.now.timestamp()))
        assert self._process is not None
        return self._process

    def run(self, request: AgentRequest, *, process: FakePiProcess | None = None) -> AgentResult:
        self._process = process or _scripted_process(
            _TEXT_BY_ROLE.get(request.role, json.dumps(_TRIAGE_JSON))
        )
        result = self.runtime.run(request)
        self.prompts.append(_sent_prompt(self._process))
        return result

    def session_paths(self) -> list[Path | None]:
        """The session file each launch used, in order (``None`` for ``--no-session``)."""
        return [_session_path_of(command) for command in self.commands]

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def _sent_prompt(process: FakePiProcess) -> str:
    [prompt] = [command for command in process.sent_commands() if command["type"] == "prompt"]
    return str(prompt["message"])


def _session_path_of(command: Sequence[str]) -> Path | None:
    if "--session" not in command:
        return None
    return Path(command[list(command).index("--session") + 1])


def _implementer(work_item_id: str = "WI-1", **overrides: object) -> AgentRequest:
    return make_request(
        AgentRole.IMPLEMENTER, work_item=work_item(work_item_id), workspace_path="/w", **overrides
    )


def _reviewer(**overrides: object) -> AgentRequest:
    return make_request(AgentRole.REVIEWER, **overrides)


def _missing_from(prompt: str, texts: Sequence[str]) -> list[str]:
    return [text for text in texts if text not in prompt]


def _found_in(prompt: str, texts: Sequence[str]) -> list[str]:
    return [text for text in texts if text in prompt]


@pytest.mark.parametrize(
    "role",
    [
        AgentRole.TRIAGE,
        AgentRole.REFINER,
        AgentRole.RESEARCHER,
        AgentRole.PLANNER,
        AgentRole.TESTER,
    ],
)
def test_run_other_roles_never_use_a_persisted_session(role: AgentRole, tmp_path: Path) -> None:
    rig = _SessionRig(tmp_path)

    rig.run(make_request(role))
    rig.run(make_request(role))

    assert [command[-1] for command in rig.commands] == ["--no-session", "--no-session"]
    assert "--session" not in rig.commands[0] + rig.commands[1]
    assert not (rig.data_dir / _SESSIONS_DIRECTORY).exists()


def test_run_first_implementer_call_starts_its_session_file_under_the_data_dir(
    tmp_path: Path,
) -> None:
    rig = _SessionRig(tmp_path)

    rig.run(_implementer())

    [path] = rig.session_paths()
    assert path is not None
    assert path.parent.parent == rig.data_dir / _SESSIONS_DIRECTORY
    assert path.name == "implementer.jsonl"


def test_run_repair_round_within_the_limit_resumes_the_same_session_file(tmp_path: Path) -> None:
    rig = _SessionRig(tmp_path)
    rig.run(first_implementer_request())
    rig.advance(minutes=10)

    rig.run(verification_repair_request())

    first, second = rig.session_paths()
    assert first is not None
    assert second == first


def test_run_reviewer_resumes_its_own_session_not_the_implementers(tmp_path: Path) -> None:
    rig = _SessionRig(tmp_path)
    rig.run(_implementer())
    rig.run(first_review_request())
    rig.advance(minutes=5)

    rig.run(re_review_request())

    implementer, reviewer, re_review = rig.session_paths()
    assert reviewer is not None
    assert reviewer.name == "reviewer.jsonl"
    assert re_review == reviewer
    assert implementer != reviewer


def test_run_interleaved_work_items_use_only_their_own_session_files(tmp_path: Path) -> None:
    rig = _SessionRig(tmp_path)
    rig.run(first_implementer_request("W1"))
    rig.run(first_implementer_request("W2"))

    rig.run(verification_repair_request("W1"))
    rig.run(verification_repair_request("W2"))

    w1, w2, w1_again, w2_again = rig.session_paths()
    assert w1 != w2
    assert (w1_again, w2_again) == (w1, w2)


@pytest.mark.parametrize(
    ("elapsed_seconds", "resumed"), [(_SESSION_MAX_AGE - 1, True), (_SESSION_MAX_AGE, False)]
)
def test_run_session_age_limit_comes_from_the_pi_config(
    tmp_path: Path, elapsed_seconds: int, resumed: bool
) -> None:
    rig = _SessionRig(tmp_path, session_reuse_max_age_seconds=_SESSION_MAX_AGE)
    rig.run(first_implementer_request())
    rig.advance(seconds=elapsed_seconds)

    rig.run(verification_repair_request())

    first, second = rig.session_paths()
    assert (second == first) is resumed


def test_run_new_session_removes_the_expired_file_of_the_previous_one(tmp_path: Path) -> None:
    rig = _SessionRig(tmp_path, session_reuse_max_age_seconds=_SESSION_MAX_AGE)
    rig.run(_implementer())
    rig.advance(seconds=_SESSION_MAX_AGE)

    rig.run(_implementer())

    first, second = rig.session_paths()
    assert first is not None
    assert second is not None
    assert not first.exists()
    assert second.exists()


@pytest.mark.parametrize(
    "changed", [{"model": "claude-opus-5"}, {"reasoning": "high"}], ids=["model", "reasoning"]
)
def test_run_changed_call_settings_start_a_new_session_and_keep_the_old_file(
    tmp_path: Path, changed: dict[str, str]
) -> None:
    rig = _SessionRig(tmp_path)
    rig.run(_implementer())

    rig.run(_implementer(**changed))

    first, second = rig.session_paths()
    assert first is not None
    assert second != first
    assert first.exists()


def test_run_changed_provider_starts_a_new_session(tmp_path: Path) -> None:
    rig = _SessionRig(tmp_path)
    rig.run(_implementer())
    rig.switch_provider("anthropic")

    rig.run(_implementer())

    first, second = rig.session_paths()
    assert second != first


def test_run_change_set_correction_resumes_the_implementer_session_without_tools(
    tmp_path: Path,
) -> None:
    rig = _SessionRig(tmp_path)
    rig.run(_implementer())
    rig.advance(minutes=2)

    rig.run(_correction_request(workspace_path="/w"))

    first, second = rig.session_paths()
    assert second == first
    assert "--no-tools" in rig.commands[1]
    assert "--tools" not in rig.commands[1]


def test_run_correction_resumes_the_session_of_a_call_whose_output_did_not_parse(
    tmp_path: Path,
) -> None:
    rig = _SessionRig(tmp_path)
    unparsed = rig.run(_implementer(), process=_scripted_process("not a change set"))

    rig.run(_correction_request(workspace_path="/w"))

    first, second = rig.session_paths()
    assert unparsed.success is False
    assert second == first


@pytest.mark.usefixtures("pi_fake_clock")
def test_run_timed_out_call_does_not_make_the_session_reusable(tmp_path: Path) -> None:
    rig = _SessionRig(tmp_path)
    hung = FakePiProcess()
    hung.write_records({"type": "response", "id": "c1", "success": True})
    timed_out = rig.run(_implementer(timeout_seconds=1), process=hung)

    rig.run(_implementer())

    first, second = rig.session_paths()
    assert timed_out.success is False
    assert timed_out.failure_reason == "pi timed out after 1 seconds"
    assert first is not None
    assert second is not None
    assert second.name == "implementer-2.jsonl"
    assert first.exists()


def test_run_assistant_error_does_not_make_the_session_reusable(tmp_path: Path) -> None:
    rig = _SessionRig(tmp_path)
    result = rig.run(_implementer(), process=_scripted_process("", stop_reason="error"))

    rig.run(_implementer())

    first, second = rig.session_paths()
    assert result.success is False
    assert second != first


def test_run_success_after_a_failed_call_makes_the_new_session_reusable(tmp_path: Path) -> None:
    rig = _SessionRig(tmp_path)
    rig.run(_implementer(), process=_scripted_process("", stop_reason="error"))
    rig.run(first_implementer_request())

    rig.run(verification_repair_request())

    _failed, fresh, resumed = rig.session_paths()
    assert resumed == fresh


def test_run_keeps_session_files_and_directories_private_to_the_owner(tmp_path: Path) -> None:
    rig = _SessionRig(tmp_path)

    rig.run(_implementer())

    [path] = rig.session_paths()
    assert path is not None
    root = rig.data_dir / _SESSIONS_DIRECTORY
    private = [root, path.parent, path, path.parent / _IMPLEMENTER_SIDECAR]
    assert [entry.stat().st_mode & 0o077 for entry in private] == [0, 0, 0, 0]


def test_run_keeps_the_result_when_the_session_record_cannot_be_written(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rig = _SessionRig(tmp_path)
    rig.run(_implementer())
    [first] = rig.session_paths()
    assert first is not None
    (first.parent / _IMPLEMENTER_SIDECAR).unlink()
    (first.parent / _IMPLEMENTER_SIDECAR).mkdir()

    result = rig.run(_implementer())

    assert "could not record the pi session outcome" in caplog.text
    assert result.success is True
    assert result.change_set == ChangeSet(summary="Reject empty customer names.")


def _recorded_sections(rig: _SessionRig, role_stem: str) -> dict[str, str]:
    """The ``sent_sections`` map in the sidecar of the session the last call used."""
    [*_, last] = rig.session_paths()
    assert last is not None
    sidecar = json.loads((last.parent / f"{role_stem}.meta.json").read_text(encoding="utf-8"))
    return dict(sidecar["sent_sections"])


def _sections_of(*requests: AgentRequest) -> dict[str, str]:
    merged: dict[str, str] = {}
    for request in requests:
        merged.update(section_hashes(build_prompt_sections(request)))
    return merged


def _assert_sent_only_what_changed(
    rig: _SessionRig, result: AgentResult, request: AgentRequest, unwanted: Sequence[str]
) -> None:
    """The last call sent less than the full prompt and none of ``unwanted``.

    Each of ``unwanted`` must really be in an earlier prompt of the session: the
    positive control that makes its absence from the last prompt mean something.
    """
    *earlier, sent = rig.prompts
    assert _missing_from("\n".join(earlier), unwanted) == []
    assert _found_in(sent, unwanted) == []
    assert len(sent) < len(build_prompt(request))
    assert result.performance is not None
    assert result.performance.prompt_chars == len(sent)


def test_run_first_call_of_a_session_sends_the_full_prompt(tmp_path: Path) -> None:
    rig = _SessionRig(tmp_path)
    request = first_implementer_request()

    result = rig.run(request)

    [prompt] = rig.prompts
    assert prompt == build_prompt(request)
    assert _missing_from(prompt, FIRST_CALL_TEXT) == []
    assert result.performance is not None
    assert result.performance.prompt_chars == len(prompt)


def test_run_first_call_records_every_section_of_the_full_prompt(tmp_path: Path) -> None:
    rig = _SessionRig(tmp_path)
    request = first_implementer_request()

    rig.run(request)

    assert _recorded_sections(rig, "implementer") == _sections_of(request)


def test_run_continued_call_records_the_earlier_sections_with_its_own(tmp_path: Path) -> None:
    rig = _SessionRig(tmp_path)
    first = first_implementer_request()
    repair = verification_repair_request()
    rig.run(first)
    rig.advance(minutes=1)

    rig.run(repair)

    recorded = _recorded_sections(rig, "implementer")
    assert recorded == _sections_of(first, repair)
    assert "Repair context" in recorded


def test_run_implementer_repair_sends_only_the_repair_round_into_the_same_session(
    tmp_path: Path,
) -> None:
    rig = _SessionRig(tmp_path)
    rig.run(first_implementer_request())
    rig.advance(minutes=1)

    repair = verification_repair_request()

    result = rig.run(repair)

    first, second = rig.session_paths()
    repair_prompt = rig.prompts[1]
    assert second == first
    assert _missing_from(repair_prompt, [VERIFICATION_FAILURE, DIFF.strip()]) == []
    _assert_sent_only_what_changed(rig, result, repair, FIRST_CALL_TEXT)


def test_run_polish_round_sends_the_repository_skill_that_first_appears_there(
    tmp_path: Path,
) -> None:
    rig = _SessionRig(tmp_path)
    rig.run(first_implementer_request())
    rig.advance(minutes=1)
    polish = polish_request()

    result = rig.run(polish)

    first, second = rig.session_paths()
    first_prompt, polish_prompt = rig.prompts
    assert second == first
    assert SKILL_GUIDANCE not in first_prompt
    assert _missing_from(polish_prompt, [SKILL_GUIDANCE, POLISH_SUMMARY, DIFF.strip()]) == []
    _assert_sent_only_what_changed(rig, result, polish, FIRST_CALL_TEXT)


def test_run_re_review_sends_the_new_evidence_findings_and_rules(tmp_path: Path) -> None:
    rig = _SessionRig(tmp_path)
    rig.run(first_review_request())
    rig.advance(minutes=1)
    re_review = re_review_request()

    result = rig.run(re_review)

    first, second = rig.session_paths()
    first_prompt, re_review_prompt = rig.prompts
    assert second == first
    assert (
        _missing_from(
            re_review_prompt,
            [
                PRIOR_FINDING_MESSAGE,
                REPAIR_DIFF.strip(),
                REPAIRED_DIFF.strip(),
                RE_REVIEW_TESTER_FINDING,
                "Return one disposition for each prior finding id",
            ],
        )
        == []
    )
    unwanted = (*FIRST_CALL_TEXT, FIRST_REVIEW_TESTER_FINDING, "Leave prior_finding_dispositions")
    _assert_sent_only_what_changed(rig, result, re_review, unwanted)
    assert _found_in(first_prompt, [FIRST_REVIEW_TESTER_FINDING]) != []


def test_run_review_after_accepted_debt_sends_the_debt_the_rules_and_the_new_diff(
    tmp_path: Path,
) -> None:
    rig = _SessionRig(tmp_path)
    rig.run(first_review_request())
    rig.advance(minutes=1)
    rig.run(re_review_request())
    rig.advance(minutes=1)
    debt = accepted_debt_review_request()

    result = rig.run(debt)

    first, _second, third = rig.session_paths()
    debt_prompt = rig.prompts[2]
    assert third == first
    assert (
        _missing_from(
            debt_prompt,
            [
                ACCEPTED_FINDING_MESSAGE,
                "Do not report an unchanged accepted finding again",
                DEBT_DIFF.strip(),
                "Leave prior_finding_dispositions and repair_regressions empty",
            ],
        )
        == []
    )
    unwanted = (*FIRST_CALL_TEXT, PRIOR_FINDING_MESSAGE, "Return one disposition for each prior")
    _assert_sent_only_what_changed(rig, result, debt, unwanted)


def test_run_retry_inside_one_reviewer_round_sends_only_the_output_rejection(
    tmp_path: Path,
) -> None:
    rig = _SessionRig(tmp_path)
    first = first_review_request()
    retry = with_output_rejection(first)
    rig.run(first)

    result = rig.run(retry)

    first_path, retry_path = rig.session_paths()
    _first_prompt, retry_prompt = rig.prompts
    assert retry_path == first_path
    assert OUTPUT_REJECTION in retry_prompt
    unwanted = (*FIRST_CALL_TEXT, DIFF.strip(), FIRST_REVIEW_TESTER_FINDING)
    _assert_sent_only_what_changed(rig, result, retry, unwanted)


def test_run_round_without_the_earlier_repair_says_it_no_longer_applies(tmp_path: Path) -> None:
    rig = _SessionRig(tmp_path)
    rig.run(first_implementer_request())
    rig.advance(minutes=1)
    rig.run(verification_repair_request())
    rig.advance(minutes=1)

    rig.run(first_implementer_request(attempt_number=3))

    first, _second, third = rig.session_paths()
    later_prompt = rig.prompts[2]
    assert third == first
    assert "These earlier sections no longer apply: Current diff, Repair context." in later_prompt
    assert _found_in(later_prompt, [VERIFICATION_FAILURE]) == []
    recorded = _recorded_sections(rig, "implementer")
    assert "Repair context" not in recorded
    assert recorded == _sections_of(first_implementer_request(attempt_number=3))


def test_run_repair_after_a_change_set_correction_does_not_send_the_specification_again(
    tmp_path: Path,
) -> None:
    rig = _SessionRig(tmp_path)
    first = first_implementer_request()
    rig.run(first)
    rig.advance(minutes=1)
    rig.run(change_set_correction_request(first))
    rig.advance(minutes=1)
    repair = verification_repair_request()

    result = rig.run(repair)

    first_path, _correction_path, repair_path = rig.session_paths()
    repair_prompt = rig.prompts[2]
    assert repair_path == first_path
    assert _missing_from(repair_prompt, [VERIFICATION_FAILURE, DIFF.strip()]) == []
    assert (
        "These earlier sections no longer apply: Correction context, Supplied ChangeSet to correct."
        in repair_prompt
    )
    _assert_sent_only_what_changed(rig, result, repair, FIRST_CALL_TEXT)


def test_run_change_set_correction_sends_only_the_change_set_and_its_context(
    tmp_path: Path,
) -> None:
    rig = _SessionRig(tmp_path)
    first = first_implementer_request()
    rig.run(first)
    rig.advance(minutes=1)
    correction = change_set_correction_request(first)

    result = rig.run(correction)

    first_path, second = rig.session_paths()
    first_prompt, correction_prompt = rig.prompts
    assert second == first_path
    assert _missing_from(correction_prompt, ["Fix the output shape.", "Correction context"]) == []
    carried_over = (*BRIEF_TEXT, *OPENING_TEXT)
    _assert_sent_only_what_changed(rig, result, correction, carried_over)


def test_run_continued_call_with_nothing_new_starts_a_new_session_with_the_full_prompt(
    tmp_path: Path,
) -> None:
    rig = _SessionRig(tmp_path)
    request = first_implementer_request()
    rig.run(request)
    rig.advance(minutes=1)

    result = rig.run(request)

    first, second = rig.session_paths()
    assert first is not None
    assert second is not None
    assert second.name == "implementer-2.jsonl"
    assert first.exists()
    assert rig.prompts == [build_prompt(request)] * 2
    assert result.performance is not None
    assert result.performance.prompt_chars == len(rig.prompts[1])
    assert _recorded_sections(rig, "implementer") == _sections_of(request)


def test_run_repair_after_a_fallback_continues_the_new_session(tmp_path: Path) -> None:
    rig = _SessionRig(tmp_path)
    rig.run(first_implementer_request())
    rig.advance(minutes=1)
    rig.run(first_implementer_request())
    rig.advance(minutes=1)

    rig.run(verification_repair_request())

    _first, fallback, repair = rig.session_paths()
    assert repair == fallback
    assert _missing_from(rig.prompts[1], FIRST_CALL_TEXT) == []
    assert _found_in(rig.prompts[2], FIRST_CALL_TEXT) == []


def test_run_call_that_did_not_settle_records_no_sections_and_the_next_call_sends_all(
    tmp_path: Path,
) -> None:
    rig = _SessionRig(tmp_path)
    rig.run(first_implementer_request(), process=_scripted_process("", stop_reason="error"))
    assert _recorded_sections(rig, "implementer") == {}
    repair = verification_repair_request()

    rig.run(repair)

    first, second = rig.session_paths()
    assert second != first
    assert rig.prompts[1] == build_prompt(repair)


def test_run_sidecar_without_sections_starts_a_new_session_with_the_full_prompt(
    tmp_path: Path,
) -> None:
    """A sidecar written before ``sent_sections`` existed: what the session holds is unknown."""
    rig = _SessionRig(tmp_path)
    rig.run(first_implementer_request())
    [first] = rig.session_paths()
    assert first is not None
    sidecar_path = first.parent / _IMPLEMENTER_SIDECAR
    old_format = json.loads(sidecar_path.read_text(encoding="utf-8"))
    del old_format["sent_sections"]
    sidecar_path.write_text(json.dumps(old_format), encoding="utf-8")
    repair = verification_repair_request()

    rig.run(repair)

    _first, second = rig.session_paths()
    assert second is not None
    assert second.name == "implementer-2.jsonl"
    assert rig.prompts[1] == build_prompt(repair)
    assert _recorded_sections(rig, "implementer") == _sections_of(repair)


def test_run_repair_in_a_new_session_sends_the_full_prompt(tmp_path: Path) -> None:
    rig = _SessionRig(tmp_path)
    rig.run(first_implementer_request())
    request = verification_repair_request(model="claude-opus-5")

    rig.run(request)

    first, second = rig.session_paths()
    assert second != first
    assert rig.prompts[1] == build_prompt(request)


def test_run_role_without_a_session_sends_the_full_prompt_even_with_a_rejection(
    tmp_path: Path,
) -> None:
    rig = _SessionRig(tmp_path)
    request = make_request(AgentRole.TESTER, repair_context=OUTPUT_REJECTION)

    rig.run(request)

    assert rig.prompts == [build_prompt(request)]


# ---------------------------------------------------------------------------
# run: child environment (credential scrub, provider key allowlist, cache retention)
# ---------------------------------------------------------------------------


#: Provider -> the API-key environment variable pi authenticates it from.
_PROVIDER_KEY_ENV_VARS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

#: Credential variables pi reads whose names do not end in ``_API_KEY``.
_NON_API_KEY_CREDENTIAL_ENV_VARS = (
    "ANTHROPIC_OAUTH_TOKEN",
    "ANTHROPIC_AUTH_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SECRET_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "HF_TOKEN",
    "GOOGLE_APPLICATION_CREDENTIALS",
)

#: Deliberately not ``ghp_``-shaped, so only exact-value redaction can hide it.
_PLAIN_SECRET = "plainsecretvalue1234"


@pytest.fixture(autouse=True)
def killpg_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Record ``os.killpg`` calls instead of signalling the fake pid's process group.

    ``FakePiProcess.pid`` is not a real process group: without this, a
    timeout test would send SIGTERM to whatever group has that id. The fake
    never "dies", so ``PiRpcClient.close`` repeats the kill after the runtime's
    own: tests compare ``set(killpg_calls)``, not the list.
    """
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "software_agent_factory.subprocess_utils.os.killpg",
        lambda pid, sig: calls.append((pid, sig)),
    )
    return calls


@pytest.fixture(autouse=True)
def _isolated_credential_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test with no credential variable from the developer's shell."""
    for name in (
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "COPILOT_GITHUB_TOKEN",
        "JEV_API_KEY",
        "XAI_API_KEY",
        "ACME_LABS_API_KEY",
        "KIMI_API_KEY",
        "KIMI_CODING_API_KEY",
        *_PROVIDER_KEY_ENV_VARS.values(),
        *_NON_API_KEY_CREDENTIAL_ENV_VARS,
    ):
        monkeypatch.delenv(name, raising=False)


def _failure_reason_when_pi_writes(stderr: str, **runtime_kwargs: object) -> str:
    process = FakePiProcess()
    process.write_records({"type": "response", "id": "c1", "success": True})
    process.write_stderr(stderr)
    process.close_stdout()
    process.exit(1)
    runtime = _runtime(process_factory=lambda command, cwd, env: process, **runtime_kwargs)
    result = runtime.run(make_request(AgentRole.TRIAGE))
    assert result.success is False
    assert result.failure_reason is not None
    return result.failure_reason


def test_run_redacts_a_secret_the_stderr_window_would_cut_in_half(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pi's 4 KB stderr window cuts inside the secret: without redaction before
    truncation, its last five characters would open the failure reason."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", _PLAIN_SECRET)

    reason = _failure_reason_when_pi_writes(
        "A" * 100 + _PLAIN_SECRET + "B" * (4096 - 5), provider="anthropic"
    )

    assert _PLAIN_SECRET[-5:] not in reason


def test_run_failure_reason_keeps_the_final_stderr_line_of_a_long_stderr() -> None:
    """The last lines of stderr hold the actual error; the reason must not keep
    only the noisy front of the 4 KB tail."""
    stderr = "startup noise line\n" * 200 + "Error: quota exceeded for model"

    reason = _failure_reason_when_pi_writes(stderr)

    assert reason.endswith("Error: quota exceeded for model")


def test_run_child_env_scrubs_github_credential_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("GH_TOKEN", "ghp_other_secret")

    env = _launch().env

    assert "GITHUB_TOKEN" not in env
    assert "GH_TOKEN" not in env


def test_run_child_env_keeps_copilot_github_token_for_headless_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "copilot-pat")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")

    env = _launch().env

    assert env["COPILOT_GITHUB_TOKEN"] == "copilot-pat"
    assert "GITHUB_TOKEN" not in env


def test_run_child_env_sets_pi_cache_retention_from_config() -> None:
    assert _launch(cache_retention="short").env["PI_CACHE_RETENTION"] == "short"


def test_run_child_env_defaults_pi_cache_retention_to_long() -> None:
    assert _launch().env["PI_CACHE_RETENTION"] == "long"


def test_run_child_env_drops_copilot_github_token_for_non_copilot_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "copilot-pat")

    env = _launch(provider="anthropic").env

    assert "COPILOT_GITHUB_TOKEN" not in env


@pytest.mark.parametrize(("provider", "own_var"), sorted(_PROVIDER_KEY_ENV_VARS.items()))
def test_run_child_env_keeps_only_the_configured_providers_api_key(
    monkeypatch: pytest.MonkeyPatch, provider: str, own_var: str
) -> None:
    for name in _PROVIDER_KEY_ENV_VARS.values():
        monkeypatch.setenv(name, f"value-of-{name}")
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "copilot-pat")

    env = _launch(provider=provider).env

    assert env[own_var] == f"value-of-{own_var}"
    for name in _PROVIDER_KEY_ENV_VARS.values():
        if name != own_var:
            assert name not in env
    assert "COPILOT_GITHUB_TOKEN" not in env


def test_run_child_env_drops_every_provider_api_key_for_github_copilot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in _PROVIDER_KEY_ENV_VARS.values():
        monkeypatch.setenv(name, f"value-of-{name}")

    env = _launch().env

    assert not set(_PROVIDER_KEY_ENV_VARS.values()) & set(env)


def test_run_child_env_drops_any_other_api_key_variable_pi_could_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XAI_API_KEY", "xai-secret")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")

    env = _launch(provider="anthropic").env

    assert "XAI_API_KEY" not in env
    assert "AZURE_OPENAI_API_KEY" not in env
    assert env["ANTHROPIC_API_KEY"] == "anthropic-secret"


def test_run_child_env_drops_the_default_routing_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JEV_API_KEY", "routing-secret")

    assert "JEV_API_KEY" not in _launch().env


def test_run_child_env_drops_the_configured_routing_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CUSTOM_ROUTING_KEY", "routing-secret")
    launches: list[_Launch] = []
    process = _scripted_process(json.dumps(_TRIAGE_JSON))

    def factory(command: Sequence[str], cwd: Path, env: dict[str, str]) -> FakePiProcess:
        launches.append(_Launch(list(command), cwd, env))
        return process

    runtime = PiAgentRuntime(
        PiConfig(),
        data_dir=Path("/data"),
        process_factory=factory,
        routing_api_key_env_var="CUSTOM_ROUTING_KEY",
    )

    runtime.run(make_request(AgentRole.TRIAGE))

    assert "CUSTOM_ROUTING_KEY" not in launches[0].env


@pytest.mark.parametrize("env_var", _NON_API_KEY_CREDENTIAL_ENV_VARS)
def test_run_child_env_drops_credentials_without_an_api_key_suffix_for_github_copilot(
    monkeypatch: pytest.MonkeyPatch, env_var: str
) -> None:
    monkeypatch.setenv(env_var, f"value-of-{env_var}")

    assert env_var not in _launch(provider="github-copilot").env


def test_run_child_env_keeps_every_credential_variable_of_the_configured_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_OAUTH_TOKEN", "oauth-secret")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "auth-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("HF_TOKEN", "hf-secret")

    env = _launch(provider="anthropic").env

    assert env["ANTHROPIC_OAUTH_TOKEN"] == "oauth-secret"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "auth-secret"
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "HF_TOKEN" not in env


def test_run_child_env_keeps_the_aws_variables_for_amazon_bedrock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "aws-id")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("ANTHROPIC_OAUTH_TOKEN", "oauth-secret")

    env = _launch(provider="amazon-bedrock").env

    assert env["AWS_ACCESS_KEY_ID"] == "aws-id"
    assert env["AWS_SECRET_ACCESS_KEY"] == "aws-secret"
    assert "ANTHROPIC_OAUTH_TOKEN" not in env


def test_run_child_env_keeps_the_api_key_of_a_provider_missing_from_the_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``acme-labs`` is not in the provider map; its own key is derived from the
    name and must survive the ``*_API_KEY`` sweep."""
    monkeypatch.setenv("ACME_LABS_API_KEY", "acme-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")

    env = _launch(provider="acme-labs").env

    assert env["ACME_LABS_API_KEY"] == "acme-secret"
    assert "ANTHROPIC_API_KEY" not in env


def test_run_child_env_keeps_the_key_variable_pi_reads_for_a_provider_named_differently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``kimi-coding`` reads ``KIMI_API_KEY``, not the name-derived ``KIMI_CODING_API_KEY``."""
    monkeypatch.setenv("KIMI_API_KEY", "kimi-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")

    env = _launch(provider="kimi-coding").env

    assert env["KIMI_API_KEY"] == "kimi-secret"
    assert "ANTHROPIC_API_KEY" not in env


def test_run_redacts_the_kept_api_key_of_a_provider_missing_from_the_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ACME_LABS_API_KEY", _PLAIN_SECRET)

    reason = _failure_reason_when_pi_writes(
        f"auth failed for {_PLAIN_SECRET}", provider="acme-labs"
    )

    assert _PLAIN_SECRET not in reason


def test_run_redacts_credentials_without_an_api_key_suffix_from_the_failure_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_OAUTH_TOKEN", "oauth-secret-value")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret-value")

    reason = _failure_reason_when_pi_writes(
        "oauth-secret-value and aws-secret-value", provider="github-copilot"
    )

    assert "oauth-secret-value" not in reason
    assert "aws-secret-value" not in reason
    assert "[REDACTED] and [REDACTED]" in reason


@pytest.mark.parametrize(
    "env_var",
    [
        "GITHUB_TOKEN",
        "COPILOT_GITHUB_TOKEN",
        "JEV_API_KEY",
        "XAI_API_KEY",
        *sorted(_PROVIDER_KEY_ENV_VARS.values()),
    ],
)
@pytest.mark.parametrize("provider", ["github-copilot", "anthropic", "openai"])
def test_run_redacts_every_known_credential_value_from_failure_reason(
    monkeypatch: pytest.MonkeyPatch, env_var: str, provider: str
) -> None:
    monkeypatch.setenv(env_var, _PLAIN_SECRET)

    reason = _failure_reason_when_pi_writes(f"auth failed for {_PLAIN_SECRET}", provider=provider)

    assert _PLAIN_SECRET not in reason
    assert "auth failed for [REDACTED]" in reason


# ---------------------------------------------------------------------------
# run: request validation shared with Copilot (validate_runtime_request)
# ---------------------------------------------------------------------------


def test_run_implementer_without_workspace_path_raises() -> None:
    runtime = _runtime()
    request = make_request(AgentRole.IMPLEMENTER)

    with pytest.raises(ValueError, match="IMPLEMENTER requests require workspace_path"):
        runtime.run(request)


def test_run_change_set_correction_without_workspace_path_raises_correction_wording() -> None:
    runtime = _runtime()
    request = _correction_request(workspace_path=None)

    with pytest.raises(ValueError, match="ChangeSet correction requires workspace_path"):
        runtime.run(request)


def test_run_timeout_seconds_below_one_raises() -> None:
    runtime = _runtime()
    request = make_request(AgentRole.TRIAGE, timeout_seconds=0)

    with pytest.raises(ValueError, match="timeout_seconds must be at least 1"):
        runtime.run(request)


# ---------------------------------------------------------------------------
# run: happy path against a scripted process (Step 3.3)
# ---------------------------------------------------------------------------


_TRIAGE_JSON = {
    "factory_eligible": True,
    "complexity": "L1",
    "risk": "R1",
    "requirements_quality": "clear",
    "needs_research": False,
    "confidence": 0.9,
}


def test_run_implementer_happy_path_returns_change_set(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    final_text = json.dumps({"summary": "Reject empty customer names."})
    process = _scripted_process(final_text)
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = make_request(AgentRole.IMPLEMENTER, workspace_path=str(workspace))

    result = runtime.run(request)

    assert result.success is True
    assert result.role is AgentRole.IMPLEMENTER
    assert result.change_set == ChangeSet(summary="Reject empty customer names.")
    assert result.performance is not None
    assert result.performance.response_chars == len(final_text)


def test_run_read_only_role_happy_path_returns_triage_result() -> None:
    final_text = json.dumps(_TRIAGE_JSON)
    process = _scripted_process(final_text)
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = make_request(AgentRole.TRIAGE)

    result = runtime.run(request)

    assert result.success is True
    assert result.role is AgentRole.TRIAGE
    assert result.triage_result == TriageResult(**_TRIAGE_JSON)


def test_run_sends_prompt_built_from_the_request() -> None:
    process = _scripted_process(json.dumps(_TRIAGE_JSON))
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = make_request(AgentRole.TRIAGE)

    runtime.run(request)

    command = process.sent_commands()[0]
    assert command["type"] == "prompt"
    assert request.work_item.title in command["message"]


# ---------------------------------------------------------------------------
# run: output parity with Copilot (AC4)
# ---------------------------------------------------------------------------


def test_output_parity_with_copilot_for_identical_final_text() -> None:
    final_text = json.dumps(_TRIAGE_JSON)

    pi_artifact = parse_agent_artifact(
        AgentRole.TRIAGE, text=final_text, purpose=AgentPurpose.STANDARD
    )
    copilot_artifact = parse_copilot_artifact(
        AgentRole.TRIAGE, stdout=final_text, purpose=AgentPurpose.STANDARD
    )

    assert pi_artifact == copilot_artifact == TriageResult(**_TRIAGE_JSON)


# ---------------------------------------------------------------------------
# run: malformed output is retryable, same wording as Copilot
# ---------------------------------------------------------------------------


def test_run_malformed_output_is_retryable_like_copilot(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    process = _scripted_process("not a JSON object at all")
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = make_request(AgentRole.IMPLEMENTER, workspace_path=str(workspace))

    result = runtime.run(request)

    assert result.success is False
    assert is_retryable_typed_artifact_failure(result, ChangeSet) is True


@pytest.mark.parametrize(
    "text",
    [
        "not a JSON object at all",
        json.dumps({"complexity": "L1"}),
        json.dumps({"factory_eligible": True, "complexity": "L9"}),
        "",
    ],
)
def test_run_malformed_output_fails_with_the_same_wording_as_copilot(text: str) -> None:
    with pytest.raises(ValueError) as copilot_error:
        parse_copilot_artifact(AgentRole.TRIAGE, stdout=text, purpose=AgentPurpose.STANDARD)
    process = _scripted_process(text)
    runtime = _runtime(process_factory=lambda command, cwd, env: process)

    result = runtime.run(make_request(AgentRole.TRIAGE))

    assert result.success is False
    assert result.failure_reason == sanitize_output(str(copilot_error.value), set())


# ---------------------------------------------------------------------------
# run: pi failures yield sanitized failed results (Step 3.4)
# ---------------------------------------------------------------------------


def test_run_assistant_error_yields_failed_result() -> None:
    process = FakePiProcess()
    process.write_records(
        {"type": "response", "id": "c1", "success": True},
        {"type": "agent_settled"},
        _get_messages_response(stop_reason="error", error_message="boom"),
    )
    process.exit(0)
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = make_request(AgentRole.TRIAGE)

    result = runtime.run(request)

    assert result.success is False
    assert result.failure_reason == "pi assistant error: boom"


def test_run_assistant_error_without_error_message_uses_default_wording() -> None:
    process = FakePiProcess()
    process.write_records(
        {"type": "response", "id": "c1", "success": True},
        {"type": "agent_settled"},
        _get_messages_response(stop_reason="error"),
    )
    process.exit(0)
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = make_request(AgentRole.TRIAGE)

    result = runtime.run(request)

    assert result.success is False
    assert result.failure_reason == "pi assistant error: (no error message)"


def test_run_assistant_aborted_yields_failed_result() -> None:
    process = FakePiProcess()
    process.write_records(
        {"type": "response", "id": "c1", "success": True},
        {"type": "agent_settled"},
        _get_messages_response(stop_reason="aborted"),
    )
    process.exit(0)
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = make_request(AgentRole.TRIAGE)

    result = runtime.run(request)

    assert result.success is False
    assert result.failure_reason == "pi assistant call was aborted"


def test_run_process_exits_before_settling_yields_failed_result() -> None:
    process = FakePiProcess()
    process.write_records({"type": "response", "id": "c1", "success": True})
    process.close_stdout()
    process.exit(7)
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = make_request(AgentRole.TRIAGE)

    result = runtime.run(request)

    assert result.success is False
    assert result.failure_reason == "pi process exited with code 7 before settling:"


def test_run_invalid_protocol_line_yields_failed_result() -> None:
    process = FakePiProcess()
    process.write_records({"type": "response", "id": "c1", "success": True})
    process.write_raw_stdout("not json at all\n")
    process.exit(0)
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = make_request(AgentRole.TRIAGE)

    result = runtime.run(request)

    assert result.success is False
    assert result.failure_reason == "pi wrote an invalid protocol record: not json at all"


def test_run_command_error_yields_failed_result() -> None:
    process = FakePiProcess()
    process.write_records(
        {"type": "response", "id": "c1", "success": True},
        {"type": "agent_settled"},
        {"type": "response", "id": "c2", "success": False, "error": "not supported"},
    )
    process.exit(0)
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = make_request(AgentRole.TRIAGE)

    result = runtime.run(request)

    assert result.success is False
    assert result.failure_reason == "pi command 'get_messages' failed: not supported"


def test_run_get_messages_non_mapping_data_yields_failed_result() -> None:
    process = FakePiProcess()
    process.write_records(
        {"type": "response", "id": "c1", "success": True},
        {"type": "agent_settled"},
        {"type": "response", "id": "c2", "success": True, "data": ["oops"]},
    )
    process.exit(0)
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = make_request(AgentRole.TRIAGE)

    result = runtime.run(request)

    assert result.success is False
    assert result.failure_reason == "pi returned an unexpected response for get_messages"


def test_run_get_last_assistant_text_non_mapping_data_yields_failed_result() -> None:
    process = FakePiProcess()
    process.write_records(
        {"type": "response", "id": "c1", "success": True},
        {"type": "agent_settled"},
        _get_messages_response(),
        {"type": "response", "id": "c3", "success": True, "data": "not-a-mapping"},
    )
    process.exit(0)
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = make_request(AgentRole.TRIAGE)

    result = runtime.run(request)

    assert result.success is False
    assert result.failure_reason == "pi returned an unexpected response for get_last_assistant_text"


def test_run_get_last_assistant_text_null_text_is_treated_as_empty() -> None:
    process = FakePiProcess()
    process.write_records(
        {"type": "response", "id": "c1", "success": True},
        {"type": "agent_settled"},
        _get_messages_response(),
        {"type": "response", "id": "c3", "success": True, "data": {"text": None}},
    )
    process.exit(0)
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = make_request(AgentRole.TRIAGE)

    result = runtime.run(request)

    # A ``null`` text must be treated as empty text (``response_chars == 0``),
    # not stringified into the four-character literal "None".
    assert result.success is False
    assert result.performance is not None
    assert result.performance.response_chars == 0


def test_run_missing_executable_yields_failed_result() -> None:
    def factory(command: list[str], cwd: Path, env: dict[str, str]) -> FakePiProcess:
        raise FileNotFoundError(command[0])

    runtime = _runtime(process_factory=factory, executable="pi-missing")
    request = make_request(AgentRole.TRIAGE)

    result = runtime.run(request)

    assert result.success is False
    assert result.failure_reason == "pi could not be started (FileNotFoundError): pi-missing"


def test_run_failure_reason_stays_within_shared_runtime_limit() -> None:
    process = FakePiProcess()
    process.write_records({"type": "response", "id": "c1", "success": True})
    process.write_stderr("boom " * 5000)
    process.close_stdout()
    process.exit(1)
    runtime = _runtime(process_factory=lambda command, cwd, env: process)

    result = runtime.run(make_request(AgentRole.TRIAGE))

    assert result.failure_reason is not None
    assert result.failure_reason.startswith("pi process exited with code 1")
    assert len(result.failure_reason) <= RUNTIME_FAILURE_REASON_LIMIT


# ---------------------------------------------------------------------------
# run: timeout aborts, then kills if still alive (Step 3.4)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("pi_fake_clock")
def test_run_timeout_sends_abort_then_kills_process_group(
    killpg_calls: list[tuple[int, int]],
) -> None:
    process = FakePiProcess()
    process.write_records({"type": "response", "id": "c1", "success": True})
    # No agent_settled event is ever written, and the process never exits --
    # simulates pi not settling within the request timeout.
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = make_request(AgentRole.TRIAGE, timeout_seconds=1)

    result = runtime.run(request)

    assert result.success is False
    assert result.failure_reason == "pi timed out after 1 seconds"
    sent_types = [command.get("type") for command in process.sent_commands()]
    assert "abort" in sent_types
    assert set(killpg_calls) == {(process.pid, signal.SIGTERM)}
    # No get_messages call ever completed (wait_for_settled itself never
    # settled), so the best-effort get_messages retry (Step 4.2) is attempted
    # but also gets nothing back -- usage stays unknown, not zeroed out.
    assert "get_messages" in sent_types
    assert result.usage is None


def test_run_timeout_gives_best_effort_usage_read_its_own_short_deadline(
    pi_fake_clock: FakePiClock,
) -> None:
    """The request's deadline has already passed when the best-effort
    ``get_messages`` runs, so it gets its own bounded window, not the request's."""
    process = FakePiProcess()
    process.write_records({"type": "response", "id": "c1", "success": True})
    runtime = _runtime(process_factory=lambda command, cwd, env: process)

    runtime.run(make_request(AgentRole.TRIAGE, timeout_seconds=1))

    # 1 s request timeout, then the 2 s best-effort window, then nothing more.
    assert pi_fake_clock.now == pytest.approx(1000.0 + 1 + _BEST_EFFORT_USAGE_DEADLINE_SECONDS)


def test_best_effort_usage_of_a_resumed_session_counts_only_its_own_round() -> None:
    """A timed-out call reads ``get_messages`` on a session that also holds earlier rounds.

    The read runs after ``wait_for_settled`` gave up, which drops any response
    already in the pipe, so the test drives the read itself.
    """
    process = FakePiProcess()
    process.write_records(
        {
            "type": "response",
            "id": "c1",
            "success": True,
            "data": {
                "messages": [
                    {"role": "user", "content": "round 1 prompt"},
                    _assistant_message({"input": 100, "output": 20}),
                    {"role": "user", "content": "round 2 prompt"},
                    _assistant_message({"input": 7, "output": 3}),
                ]
            },
        }
    )

    usage = _runtime()._best_effort_usage(PiRpcClient(process))

    assert usage is not None
    assert (usage.total_user_requests, usage.input_tokens) == (1, 7)


@pytest.mark.usefixtures("pi_fake_clock")
def test_run_timeout_after_malformed_usage_still_aborts_and_kills(
    killpg_calls: list[tuple[int, int]],
) -> None:
    """A malformed value (e.g. a negative token count) in the settled call's
    own ``get_messages`` read must not raise out of ``run()`` before it
    reaches the timeout path -- it is treated as unreported usage instead,
    and pi is still aborted and killed when the subsequent
    ``get_last_assistant_text`` call times out."""
    process = FakePiProcess()
    process.write_records(
        {"type": "response", "id": "c1", "success": True},
        {"type": "agent_settled"},
        _get_messages_response(usage={"input": -5}, model="claude-sonnet-5"),
    )
    # No response ever arrives for get_last_assistant_text (c3), and the
    # process never exits on its own.
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = make_request(AgentRole.TRIAGE, timeout_seconds=1)

    result = runtime.run(request)

    assert result.success is False
    assert result.failure_reason == "pi timed out after 1 seconds"
    assert result.usage is not None
    assert result.usage.input_tokens is None
    sent_types = [command.get("type") for command in process.sent_commands()]
    assert "abort" in sent_types
    assert set(killpg_calls) == {(process.pid, signal.SIGTERM)}


@pytest.mark.usefixtures("pi_fake_clock")
def test_run_timeout_with_undecodable_leftover_output_keeps_partial_usage(
    killpg_calls: list[tuple[int, int]],
) -> None:
    """AC6: killing a wedged pi reads its leftover output, and a text-mode
    ``Popen`` raises ``UnicodeDecodeError`` on a split UTF-8 character there.
    That must not replace the failed result or lose the usage read so far."""
    process = FakePiProcess()
    process.write_records(
        {"type": "response", "id": "c1", "success": True},
        {"type": "agent_settled"},
        _get_messages_response(usage={"input": 40, "output": 10}, model="claude-sonnet-5"),
    )
    process.communicate_error = UnicodeDecodeError("utf-8", b"\xe2\x82", 0, 2, "unexpected end")
    runtime = _runtime(process_factory=lambda command, cwd, env: process)

    result = runtime.run(make_request(AgentRole.TRIAGE, timeout_seconds=1))

    assert result.success is False
    assert result.failure_reason == "pi timed out after 1 seconds"
    assert result.usage is not None
    assert result.usage.input_tokens == 40
    assert set(killpg_calls) == {(process.pid, signal.SIGTERM)}


def test_default_process_factory_tolerates_a_split_utf8_character_when_killed() -> None:
    """The real ``Popen`` the runtime starts must decode leniently: a child
    killed mid-character leaves bytes ``communicate()`` cannot strictly decode."""
    script = (
        "import sys, time\n"
        "sys.stdout.buffer.write(b'\\xe2\\x82')\n"
        "sys.stdout.buffer.flush()\n"
        "sys.stderr.write('ready\\n')\n"
        "sys.stderr.flush()\n"
        "time.sleep(60)\n"
    )
    process = _default_process_factory([sys.executable, "-c", script], Path.cwd(), dict(os.environ))
    assert process.stderr is not None
    assert process.stderr.readline() == "ready\n"

    process.kill()
    stdout, _stderr = process.communicate()

    assert stdout == "\ufffd"


@pytest.mark.usefixtures("pi_fake_clock")
def test_run_does_not_kill_a_pi_that_exits_on_stdin_eof_after_a_timeout(
    killpg_calls: list[tuple[int, int]],
) -> None:
    """pi shuts down on stdin EOF, not necessarily in response to ``abort``.

    On timeout the runtime must close stdin after sending the best-effort
    ``abort``: a pi that only exits once its stdin closes
    (:class:`StdinEofExitProcess`) must not be escalated to a process-group kill.
    """
    process = StdinEofExitProcess()
    process.write_records({"type": "response", "id": "c1", "success": True})
    runtime = _runtime(process_factory=lambda command, cwd, env: process)

    result = runtime.run(make_request(AgentRole.TRIAGE, timeout_seconds=1))

    assert result.failure_reason == "pi timed out after 1 seconds"
    assert "abort" in [command.get("type") for command in process.sent_commands()]
    assert process.poll() == 0
    assert killpg_calls == []


def test_run_process_exits_before_settling_reports_unknown_usage() -> None:
    process = FakePiProcess()
    process.write_records({"type": "response", "id": "c1", "success": True})
    process.close_stdout()
    process.exit(7)
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = make_request(AgentRole.TRIAGE)

    result = runtime.run(request)

    assert result.success is False
    assert result.usage is None


# ---------------------------------------------------------------------------
# run: usage recorded per model (Step 4.2)
# ---------------------------------------------------------------------------


def test_run_success_carries_usage_from_get_messages(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    final_text = json.dumps({"summary": "Reject empty customer names."})
    process = _scripted_process(
        final_text,
        usage={
            "input": 100,
            "output": 20,
            "cacheRead": 80,
            "cacheWrite": 5,
            "cost": {"total": 0.05},
        },
        model="claude-sonnet-5",
    )
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = make_request(AgentRole.IMPLEMENTER, workspace_path=str(workspace))

    result = runtime.run(request)

    assert result.success is True
    assert result.usage is not None
    assert result.usage.input_tokens == 100
    assert result.usage.output_tokens == 20
    assert result.usage.cache_read_tokens == 80
    assert result.usage.cache_write_tokens == 5
    assert result.usage.list_price_estimate_usd == pytest.approx(0.05)
    assert result.usage.total_user_requests == 1
    assert result.usage.current_model == "claude-sonnet-5"


def test_run_resumed_call_reports_only_the_usage_of_its_own_round(tmp_path: Path) -> None:
    """``get_messages`` returns the whole session, so the earlier round must not be counted."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    round_one = [
        {"role": "user", "content": "round 1 prompt"},
        _assistant_message({"input": 100, "output": 20, "cacheWrite": 60, "cost": {"total": 0.4}}),
        {"role": "toolResult", "content": "round 1 tool output"},
        _assistant_message({"input": 30, "output": 10, "cacheRead": 90, "cost": {"total": 0.1}}),
    ]
    round_two = [
        {"role": "user", "content": "round 2 prompt"},
        _assistant_message({"input": 7, "output": 3, "cacheRead": 120, "cost": {"total": 0.02}}),
    ]
    process = FakePiProcess()
    process.write_records(
        {"type": "response", "id": "c1", "success": True},
        {"type": "agent_settled"},
        {
            "type": "response",
            "id": "c2",
            "success": True,
            "data": {"messages": [*round_one, *round_two]},
        },
        {
            "type": "response",
            "id": "c3",
            "success": True,
            "data": {"text": json.dumps({"summary": "Done."})},
        },
    )
    process.exit(0)
    runtime = _runtime(process_factory=lambda command, cwd, env: process)

    result = runtime.run(make_request(AgentRole.IMPLEMENTER, workspace_path=str(workspace)))

    assert result.usage is not None
    assert result.usage.total_user_requests == 1
    assert result.usage.input_tokens == 7
    assert result.usage.output_tokens == 3
    assert result.usage.cache_read_tokens == 120
    assert result.usage.cache_write_tokens is None
    assert result.usage.list_price_estimate_usd == pytest.approx(0.02)
    assert result.usage.model_usage[0].requests == 1


def test_run_timeout_after_settling_keeps_partial_usage(pi_fake_clock: FakePiClock) -> None:
    """AC6: a timeout after the call settled still records usage read so far.

    The settled call's own ``get_messages`` read already captured usage
    before ``get_last_assistant_text`` stalls past the timeout -- no
    best-effort retry is needed (or attempted) here.
    """
    process = FakePiProcess()
    process.write_records(
        {"type": "response", "id": "c1", "success": True},
        {"type": "agent_settled"},
        _get_messages_response(usage={"input": 40, "output": 10}, model="claude-sonnet-5"),
    )
    # No response ever arrives for get_last_assistant_text (c3).
    runtime = _runtime(process_factory=lambda command, cwd, env: process)
    request = make_request(AgentRole.TRIAGE, timeout_seconds=1)

    result = runtime.run(request)

    assert result.success is False
    assert result.failure_reason == "pi timed out after 1 seconds"
    assert result.usage is not None
    assert result.usage.input_tokens == 40
    assert result.usage.output_tokens == 10
    # Usage was already read, so no best-effort retry extends the wait.
    assert pi_fake_clock.now == pytest.approx(1000.0 + 1)


# ---------------------------------------------------------------------------
# usage_from_pi_messages: pure per-model usage mapping (Step 4.2)
# ---------------------------------------------------------------------------


def _assistant_message(usage: dict[str, Any], *, model: str = "claude-sonnet-5") -> dict[str, Any]:
    return {"role": "assistant", "model": model, "usage": usage}


def test_usage_from_pi_messages_sums_per_model() -> None:
    messages = [
        _assistant_message(
            {"input": 100, "output": 20, "cacheRead": 80, "cacheWrite": 5, "cost": {"total": 0.03}}
        ),
        _assistant_message(
            {"input": 50, "output": 10, "cacheRead": 40, "cacheWrite": 2, "cost": {"total": 0.02}}
        ),
    ]

    usage = usage_from_pi_messages(messages)

    assert usage is not None
    assert usage.total_user_requests == 2
    assert usage.input_tokens == 150
    assert usage.output_tokens == 30
    assert usage.cache_read_tokens == 120
    assert usage.cache_write_tokens == 7
    assert usage.list_price_estimate_usd == pytest.approx(0.05)
    assert usage.current_model == "claude-sonnet-5"
    assert len(usage.model_usage) == 1
    model_usage = usage.model_usage[0]
    assert model_usage.model == "claude-sonnet-5"
    assert model_usage.requests == 2
    assert model_usage.input_tokens == 150
    assert model_usage.output_tokens == 30
    assert model_usage.cache_read_tokens == 120
    assert model_usage.cache_write_tokens == 7
    assert model_usage.list_price_estimate_usd == pytest.approx(0.05)


def test_usage_from_pi_messages_maps_reasoning_to_reasoning_tokens() -> None:
    messages = [_assistant_message({"input": 10, "output": 5, "reasoning": 7})]

    usage = usage_from_pi_messages(messages)

    assert usage.reasoning_tokens == 7
    assert usage.model_usage[0].reasoning_tokens == 7


def test_usage_from_pi_messages_groups_messages_by_model_in_first_seen_order() -> None:
    messages = [
        _assistant_message({"input": 100, "output": 20, "cost": {"total": 0.03}}, model="model-a"),
        _assistant_message({"input": 50, "output": 10, "cost": {"total": 0.5}}, model="model-b"),
        _assistant_message({"input": 10, "output": 5, "cost": {"total": 0.01}}, model="model-a"),
    ]

    usage = usage_from_pi_messages(messages)

    assert [entry.model for entry in usage.model_usage] == ["model-a", "model-b"]
    first, second = usage.model_usage
    assert (first.requests, first.input_tokens, first.output_tokens) == (2, 110, 25)
    assert first.list_price_estimate_usd == pytest.approx(0.04)
    assert (second.requests, second.input_tokens, second.output_tokens) == (1, 50, 10)
    assert second.list_price_estimate_usd == pytest.approx(0.5)
    assert usage.total_user_requests == 3
    assert usage.input_tokens == 160
    assert usage.output_tokens == 35
    assert usage.list_price_estimate_usd == pytest.approx(0.54)
    assert usage.current_model == "model-a"


def test_usage_from_pi_messages_unreported_fields_stay_unknown() -> None:
    messages = [_assistant_message({"input": 100, "output": 20})]

    usage = usage_from_pi_messages(messages)

    assert usage is not None
    assert usage.cache_write_tokens is None
    assert usage.reasoning_tokens is None
    assert usage.input_tokens == 100
    assert usage.output_tokens == 20


def test_usage_from_pi_messages_reported_zero_stays_zero_not_unknown() -> None:
    messages = [_assistant_message({"input": 10, "cacheRead": 0})]

    usage = usage_from_pi_messages(messages)

    assert usage is not None
    assert usage.cache_read_tokens == 0


def test_usage_from_pi_messages_counts_cache_write_1h_as_part_of_cache_write() -> None:
    """pi 0.84.4 reports the one-hour writes as a subset of ``cacheWrite``."""
    messages = [_assistant_message({"input": 10, "cacheWrite": 706, "cacheWrite1h": 706})]

    usage = usage_from_pi_messages(messages)

    assert usage is not None
    assert usage.cache_write_tokens == 706
    assert usage.model_usage[0].cache_write_tokens == 706


def test_usage_from_pi_messages_falls_back_to_cache_write_1h_without_cache_write() -> None:
    messages = [
        _assistant_message({"input": 10, "cacheWrite1h": 4}),
        _assistant_message({"input": 10, "cacheWrite": 3, "cacheWrite1h": 3}),
    ]

    usage = usage_from_pi_messages(messages)

    assert usage is not None
    assert usage.cache_write_tokens == 7


def test_usage_from_pi_messages_copilot_only_units_stay_unknown() -> None:
    messages = [_assistant_message({"input": 10, "output": 5})]

    usage = usage_from_pi_messages(messages)

    assert usage is not None
    assert usage.total_premium_request_cost is None
    assert usage.total_nano_aiu is None
    assert usage.model_usage[0].premium_request_cost is None
    assert usage.model_usage[0].total_nano_aiu is None


def test_usage_from_pi_messages_no_assistant_messages_reports_zero_requests() -> None:
    usage = usage_from_pi_messages([])

    assert usage is not None
    assert usage.total_user_requests == 0
    assert usage.input_tokens is None
    assert usage.output_tokens is None
    assert usage.reasoning_tokens is None
    assert usage.cache_read_tokens is None
    assert usage.cache_write_tokens is None
    assert usage.list_price_estimate_usd is None
    assert usage.current_model is None
    assert usage.model_usage == ()


def test_usage_from_pi_messages_ignores_non_assistant_messages() -> None:
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "tool", "usage": "not-a-mapping"},
        _assistant_message({"input": 5}),
    ]

    usage = usage_from_pi_messages(messages)

    assert usage is not None
    assert usage.total_user_requests == 1
    assert usage.input_tokens == 5


def test_usage_from_pi_messages_message_without_model_skips_model_usage_entry() -> None:
    """A modelless assistant message counts toward the aggregate but gets no
    ``model_usage`` entry -- this pure function has no request-supplied model
    to fall back on (see its docstring)."""
    messages = [{"role": "assistant", "usage": {"input": 5}}]

    usage = usage_from_pi_messages(messages)

    assert usage is not None
    assert usage.total_user_requests == 1
    assert usage.input_tokens == 5
    assert usage.current_model is None
    assert usage.model_usage == ()


def test_usage_from_pi_messages_non_numeric_field_value_stays_unknown() -> None:
    """A ``usage`` field carrying a non-numeric value (a malformed record) is
    treated as not reported, not coerced or crashed on."""
    messages = [_assistant_message({"input": "not-a-number", "output": 5})]

    usage = usage_from_pi_messages(messages)

    assert usage is not None
    assert usage.input_tokens is None
    assert usage.output_tokens == 5


def test_usage_from_pi_messages_fractional_token_count_stays_unknown() -> None:
    """Token counts are whole numbers -- a fractional one is malformed, as in
    the Copilot runtime, and is not truncated or rounded."""
    messages = [_assistant_message({"input": 10.5, "output": 5.0, "cacheWrite": 2.5})]

    usage = usage_from_pi_messages(messages)

    assert usage.input_tokens is None
    assert usage.output_tokens == 5
    assert usage.cache_write_tokens is None
    assert usage.model_usage[0].input_tokens is None


def test_usage_from_pi_messages_negative_field_value_stays_unknown() -> None:
    """A negative token count is malformed -- pi never legitimately reports
    one, and :class:`~software_agent_factory.models.ModelUsage`/
    :class:`~software_agent_factory.models.UsageMetrics` reject it outright
    -- so it must be treated as not reported, not crash the mapping."""
    messages = [_assistant_message({"input": -5, "output": 5})]

    usage = usage_from_pi_messages(messages)

    assert usage is not None
    assert usage.input_tokens is None
    assert usage.output_tokens == 5


def test_usage_from_pi_messages_nan_field_value_stays_unknown() -> None:
    messages = [_assistant_message({"input": float("nan"), "output": 5})]

    usage = usage_from_pi_messages(messages)

    assert usage is not None
    assert usage.input_tokens is None
    assert usage.output_tokens == 5


def test_usage_from_pi_messages_infinite_field_value_stays_unknown() -> None:
    messages = [_assistant_message({"input": float("inf"), "output": 5})]

    usage = usage_from_pi_messages(messages)

    assert usage is not None
    assert usage.input_tokens is None
    assert usage.output_tokens == 5


def test_usage_from_pi_messages_negative_cost_stays_unknown() -> None:
    messages = [_assistant_message({"input": 5, "cost": {"total": -0.5}})]

    usage = usage_from_pi_messages(messages)

    assert usage is not None
    assert usage.list_price_estimate_usd is None
    assert usage.input_tokens == 5


def test_usage_from_pi_messages_current_model_is_last_assistant_messages_own_model() -> None:
    """``current_model`` is the *last* assistant message's own ``model`` --
    ``None`` when that specific message did not report one, even when an
    earlier assistant message did (matches the corrected docstring; the
    prior implementation incorrectly kept the earlier model)."""
    messages = [
        _assistant_message({"input": 100}, model="claude-sonnet-5"),
        {"role": "assistant", "usage": {"input": 5}},
    ]

    usage = usage_from_pi_messages(messages)

    assert usage is not None
    assert usage.current_model is None
