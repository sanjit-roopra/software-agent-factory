"""Tests for the dashboard's approve and answer routes (ADR-033, #80 slice 4).

A real ``ThreadingHTTPServer`` on loopback and a real ``FileRunStore`` in ``tmp_path``: the
only fakes are the server's own boundaries. Every refusal is proven to leave the data
directory byte for byte as it was.
"""

from __future__ import annotations

import ast
import http.client
import json
import logging
import socket
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from software_agent_factory import dashboard
from software_agent_factory.cli import (
    build_resume_request_reader,
    build_resume_requester,
    build_resume_run_reader,
)
from software_agent_factory.dashboard import DashboardConfig, DashboardServer, create_server
from software_agent_factory.dashboard.actions import ResumeActions
from software_agent_factory.dashboard.handler import MAX_BODY_BYTES
from software_agent_factory.dashboard.security import TOKEN_HEADER
from software_agent_factory.dashboard.server import DEFAULT_REQUEST_TIMEOUT_SECONDS
from software_agent_factory.dashboard.snapshot import ResumeRequestResult
from software_agent_factory.models import (
    Complexity,
    DashboardResumeRequest,
    EscalationRecord,
    EscalationStatus,
    FactoryRun,
    PlanDecisionAnswer,
    PlanDecisionContext,
    ResumeClassification,
    Risk,
    RiskApprovalContext,
    RiskRationale,
    WorkflowState,
)
from software_agent_factory.resume import (
    MAX_PLAN_DECISION_ANSWER_CHARS,
    compute_approval_context_fingerprint,
    compute_plan_decision_context_fingerprint,
)
from software_agent_factory.store import FileRunStore

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
RUN_ID = "run-action"
EPISODE = "ep-action"
PLAN_FINGERPRINT = "a" * 64
DECISIONS = ["Pick a storage format.", "Pick a cache size."]
RISK = ResumeClassification.RISK_APPROVAL
PLAN = ResumeClassification.PLAN_DECISION
ORIGIN_HEADER = "Origin"
CONTENT_TYPE = "Content-Type"
HOST_HEADER = "Host"
JSON_TYPE = "application/json"
TEXT_TYPE = "text/plain"
READ_ONLY_METHODS = "GET, HEAD"
WORK_ITEM_ID = "task-1"
LOOPBACK = "127.0.0.1"
WRONG_TOKEN = "wrong"
EVIL_HOST = "evil.example:80"
EVIL_ORIGIN = "http://evil.example"
OLD_EPISODE = "ep-old"
OTHER_FINGERPRINT = "b" * 64
ANSWER_FORMAT = "JSON files."
ANSWER_CACHE = "Ten entries."
ANSWER_FINE = "Fine."
REASON = "reason"
EPISODE_FIELD = "episode_id"
FINGERPRINT_FIELD = "context_fingerprint"
NOT_WAITING = "not_waiting"
CLOSE = "close"
MISSING_RUN = "run-missing"
STALE_EPISODE = "stale_episode"
WRONG_ACTION = "wrong_action"
EXPIRED = "expired"
ENCODING = "utf-8"
RESUME = "resume"
APPROVE = "approve"
ANSWER = "answer"


# -- runs ---------------------------------------------------------------------------------


def _risk_context() -> RiskApprovalContext:
    rationale = RiskRationale(
        intended_outcome="Update the schema safely.",
        sensitive_boundary="Production database.",
        necessity="The work item migrates customer records.",
        credible_scenario="A bad migration could corrupt accounts.",
        known_mitigations=["Run the migration in one transaction."],
        residual_risk="A short table lock.",
    )
    decision = f"Approve advancing run {RUN_ID} to REFINING."
    authorized = ["Move the run from NEEDS_HUMAN to REFINING."]
    unauthorized = ["Approval does not change the task scope."]
    conditions = ["Quality gates must pass before a pull request."]
    return RiskApprovalContext(
        risk=Risk.R2,
        complexity=Complexity.L1,
        work_item_id=WORK_ITEM_ID,
        work_item_title="Task",
        risk_rationale=rationale,
        decision_requested=decision,
        next_state=WorkflowState.REFINING,
        authorized_actions=authorized,
        unauthorized_actions=unauthorized,
        conditions_in_force=conditions,
        context_fingerprint=compute_approval_context_fingerprint(
            run_id=RUN_ID,
            episode_id=EPISODE,
            work_item_id=WORK_ITEM_ID,
            work_item_title="Task",
            risk=Risk.R2.value,
            complexity=Complexity.L1.value,
            rationale=rationale,
            decision_requested=decision,
            next_state=WorkflowState.REFINING.value,
            authorized_actions=authorized,
            unauthorized_actions=unauthorized,
            conditions_in_force=conditions,
        ),
    )


def _plan_context() -> PlanDecisionContext:
    return PlanDecisionContext(
        plan_fingerprint=PLAN_FINGERPRINT,
        decisions=DECISIONS,
        context_fingerprint=compute_plan_decision_context_fingerprint(
            run_id=RUN_ID,
            episode_id=EPISODE,
            plan_fingerprint=PLAN_FINGERPRINT,
            decisions=DECISIONS,
        ),
    )


def _run(
    kind: ResumeClassification = RISK,
    *,
    state: WorkflowState = WorkflowState.NEEDS_HUMAN,
    **record: object,
) -> FactoryRun:
    fields: dict[str, object] = {
        EPISODE_FIELD: EPISODE,
        "status": EscalationStatus.NOTIFIED,
        "resume_classification": kind,
        "created_at": NOW - timedelta(hours=1),
        "approval_context": _risk_context() if kind is RISK else None,
        "plan_decision_context": _plan_context() if kind is PLAN else None,
    }
    escalation = EscalationRecord.model_validate({**fields, **record})
    return FactoryRun(id=RUN_ID, work_item_id=WORK_ITEM_ID, state=state, escalation=escalation)


def _fingerprint(run: FactoryRun) -> str:
    assert run.escalation is not None
    context = run.escalation.approval_context or run.escalation.plan_decision_context
    assert context is not None
    return context.context_fingerprint


RUN_PATH = f"/api/runs/{RUN_ID}"


def _path(action: str, run_id: str = RUN_ID) -> str:
    return f"/api/runs/{run_id}/{action}"


def _tree(root: Path) -> dict[str, bytes]:
    """Every file under ``root`` with its bytes, to prove a refusal wrote nothing."""
    return {
        str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


# -- server and client --------------------------------------------------------------------


@dataclass
class Rig:
    server: DashboardServer
    store: FileRunStore
    data_dir: Path
    run: FactoryRun

    @property
    def port(self) -> int:
        return self.server.address[1]

    def body(self, **overrides: object) -> dict[str, object]:
        """A valid body for the run, with ``overrides`` replacing or (as ``None``) dropping keys."""
        body: dict[str, object] = {
            EPISODE_FIELD: EPISODE,
            FINGERPRINT_FIELD: _fingerprint(self.run),
        }
        if self.run.escalation is not None and self.run.escalation.resume_classification is PLAN:
            body["answers"] = [ANSWER_FORMAT, ANSWER_CACHE]
        body.update(overrides)
        return {key: value for key, value in body.items() if value is not None}

    def headers(self, **overrides: str | None) -> dict[str, str]:
        """The headers of a valid write, with ``overrides`` replacing or (as ``None``) dropping."""
        headers: dict[str, str | None] = {
            TOKEN_HEADER: self.server.token,
            ORIGIN_HEADER: f"http://127.0.0.1:{self.port}",
            CONTENT_TYPE: JSON_TYPE,
        }
        headers.update(overrides)
        return {name: value for name, value in headers.items() if value is not None}

    def post(
        self,
        path: str,
        body: object = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        """POST ``body`` (bytes as is, anything else as JSON) and return status and JSON."""
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        conn = http.client.HTTPConnection(LOOPBACK, self.port, timeout=5)
        try:
            conn.request("POST", path, body=payload, headers=headers or self.headers())
            response = conn.getresponse()
            return response.status, json.loads(response.read() or b"null")
        finally:
            conn.close()

    def approve(self, body: object = None, **header_overrides: str | None) -> tuple[int, Any]:
        return self.post(
            _path(APPROVE),
            self.body() if body is None else body,
            headers=self.headers(**header_overrides),
        )

    def answer(self, body: object = None, **header_overrides: str | None) -> tuple[int, Any]:
        return self.post(
            _path(ANSWER),
            self.body() if body is None else body,
            headers=self.headers(**header_overrides),
        )

    def requests(self) -> list[DashboardResumeRequest]:
        return self.store.list_dashboard_requests(RUN_ID, EPISODE)

    @contextmanager
    def assert_writes_nothing(self) -> Iterator[None]:
        """Fail if anything under the data directory changes, in bytes or in names."""
        before = _tree(self.data_dir)
        yield
        assert _tree(self.data_dir) == before


RigFactory = Callable[..., Rig]


@pytest.fixture
def make_rig(tmp_path: Path) -> Iterator[RigFactory]:
    started: list[tuple[DashboardServer, threading.Thread]] = []

    def build(
        run: FactoryRun | None = None,
        *,
        max_reopens: int | None = 3,
        reply_window_hours: float | None = 24,
        requester: Callable[[str, DashboardResumeRequest], ResumeRequestResult] | None = None,
        actions: bool = True,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> Rig:
        stored = run if run is not None else _run()
        store = FileRunStore(tmp_path)
        store.save_run(stored)

        server = create_server(
            DashboardConfig(
                snapshot_provider=lambda *, limit, offset: {},
                run_detail_provider=lambda run_id: None,
                resume_actions=ResumeActions(
                    run_reader=build_resume_run_reader(store),
                    requester=requester or build_resume_requester(store),
                    reply_window_hours=reply_window_hours,
                    max_reopens=max_reopens,
                    clock=lambda: NOW,
                )
                if actions
                else None,
                request_timeout_seconds=request_timeout_seconds,
            )
        )
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        started.append((server, thread))
        return Rig(server=server, store=store, data_dir=tmp_path, run=stored)

    yield build
    for server, thread in started:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# -- accepted -----------------------------------------------------------------------------


def test_an_approval_is_accepted_and_stored_with_the_server_clock(make_rig: RigFactory) -> None:
    rig = make_rig()

    status, payload = rig.approve(rig.body(created_at="2000-01-01T00:00:00Z"))

    assert (status, payload["status"]) == (202, "accepted")
    assert payload["requested_at"] == NOW.isoformat()
    (stored,) = rig.requests()
    assert stored.action is RISK
    assert stored.status == "pending"
    assert stored.created_at == NOW
    assert stored.context_fingerprint == _fingerprint(rig.run)


def test_answers_are_accepted_and_stored_in_order(make_rig: RigFactory) -> None:
    rig = make_rig(_run(PLAN))

    status, _ = rig.answer()

    assert status == 202
    (stored,) = rig.requests()
    assert stored.action is PLAN
    assert [(a.decision_number, a.answer) for a in stored.answers] == [
        (1, ANSWER_FORMAT),
        (2, ANSWER_CACHE),
    ]


def test_a_run_is_not_changed_by_an_accepted_request(make_rig: RigFactory) -> None:
    rig = make_rig()
    before = {name: data for name, data in _tree(rig.data_dir).items() if name.endswith("run.json")}

    assert rig.approve()[0] == 202

    after = {name: data for name, data in _tree(rig.data_dir).items() if name.endswith("run.json")}
    assert after == before


@pytest.mark.parametrize(
    "content_type", ["application/json; charset=utf-8", "Application/JSON", "application/json;x=y"]
)
def test_a_json_content_type_may_carry_parameters(make_rig: RigFactory, content_type: str) -> None:
    rig = make_rig()

    assert rig.approve(**{CONTENT_TYPE: content_type})[0] == 202


def test_a_body_of_exactly_16_kb_is_accepted(make_rig: RigFactory) -> None:
    rig = make_rig()
    body = json.dumps(rig.body()).encode()
    padded = body + b" " * (MAX_BODY_BYTES - len(body))
    assert len(padded) == 16 * 1024

    assert rig.approve(padded)[0] == 202


def test_a_plan_answer_of_the_longest_length_is_accepted(make_rig: RigFactory) -> None:
    rig = make_rig(_run(PLAN))
    longest = "x" * MAX_PLAN_DECISION_ANSWER_CHARS

    assert rig.answer(rig.body(answers=[longest, ANSWER_CACHE]))[0] == 202
    assert rig.requests()[0].answers[0].answer == longest


# -- rejected: transport ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("problem", "overrides", "status"),
    [
        ("no token header", {TOKEN_HEADER: None}, 401),
        ("a wrong token header", {TOKEN_HEADER: WRONG_TOKEN}, 401),
        ("a non-ASCII token header", {TOKEN_HEADER: "caf\u00e9"}, 401),
        ("no Origin header", {ORIGIN_HEADER: None}, 403),
        ("a foreign Origin", {ORIGIN_HEADER: EVIL_ORIGIN}, 403),
        ("the localhost alias as Origin", {ORIGIN_HEADER: "http://localhost:1"}, 403),
        ("a form-encoded body", {CONTENT_TYPE: "application/x-www-form-urlencoded"}, 415),
        ("no content type", {CONTENT_TYPE: None}, 415),
        ("a look-alike content type", {CONTENT_TYPE: "text/json"}, 415),
        (
            "a parameter that hides the type",
            {CONTENT_TYPE: "text/plain; x=application/json"},
            415,
        ),
        ("a wrong Host", {HOST_HEADER: EVIL_HOST}, 400),
    ],
)
def test_a_rejected_request_changes_nothing(
    make_rig: RigFactory, problem: str, overrides: dict[str, str | None], status: int
) -> None:
    rig = make_rig()
    with rig.assert_writes_nothing():
        assert rig.approve(**overrides)[0] == status, problem


def test_the_token_only_in_the_query_string_is_not_enough(make_rig: RigFactory) -> None:
    rig = make_rig()
    with rig.assert_writes_nothing():
        status, _ = rig.post(
            f"{_path(APPROVE)}?token={rig.server.token}",
            rig.body(),
            headers=rig.headers(**{TOKEN_HEADER: None}),
        )

    assert status == 401


def test_a_body_over_16_kb_is_413(make_rig: RigFactory) -> None:
    rig = make_rig()
    body = json.dumps(rig.body()).encode()

    with rig.assert_writes_nothing():
        status, _ = rig.approve(body + b" " * (MAX_BODY_BYTES - len(body) + 1))

        assert status == 413


#: The header faults in the order the handler checks them, each with the status it gets.
_HEADER_FAULTS: tuple[tuple[str, str, int], ...] = (
    (HOST_HEADER, EVIL_HOST, 400),
    (ORIGIN_HEADER, EVIL_ORIGIN, 403),
    (TOKEN_HEADER, WRONG_TOKEN, 401),
    (CONTENT_TYPE, TEXT_TYPE, 415),
)


def _cumulative_header_faults() -> list[Any]:
    """Every fault at once, then without the first, and so on. The last row has none left."""
    rows = [
        pytest.param(
            {name: value for name, value, _ in _HEADER_FAULTS[first:]},
            _HEADER_FAULTS[first][2],
            id=" + ".join(name for name, _, _ in _HEADER_FAULTS[first:]) + " wrong",
        )
        for first in range(len(_HEADER_FAULTS))
    ]
    return [*rows, pytest.param({}, 400, id="only the body wrong")]


@pytest.mark.parametrize(("faults", "status"), _cumulative_header_faults())
def test_the_checks_run_in_the_documented_order(
    make_rig: RigFactory, faults: dict[str, str], status: int
) -> None:
    rig = make_rig()

    assert rig.approve(b"not json", **faults)[0] == status


@pytest.mark.parametrize(
    ("length", "status"), [("abc", 400), ("-1", 400), ("", 400), ("9" * 40, 413)]
)
def test_a_content_length_that_is_not_a_size(
    make_rig: RigFactory, length: str, status: int
) -> None:
    rig = make_rig()
    conn = http.client.HTTPConnection(LOOPBACK, rig.port, timeout=5)
    conn.putrequest("POST", _path(APPROVE))
    for name, value in rig.headers().items():
        conn.putheader(name, value)
    conn.putheader("Content-Length", length)
    conn.endheaders()
    response = conn.getresponse()
    response.read()
    conn.close()

    assert response.status == status
    assert response.getheader("Connection") == CLOSE


def test_a_refused_write_leaves_the_connection_usable(make_rig: RigFactory) -> None:
    rig = make_rig()
    conn = http.client.HTTPConnection(LOOPBACK, rig.port, timeout=5)
    body = json.dumps(rig.body()).encode()
    conn.request(
        "POST",
        _path(APPROVE),
        body=body,
        headers=rig.headers(**{TOKEN_HEADER: WRONG_TOKEN}),
    )
    refused = conn.getresponse()
    refused.read()
    assert refused.status == 401

    conn.request("GET", "/healthz", headers={TOKEN_HEADER: rig.server.token})
    follow_up = conn.getresponse()
    follow_up.read()
    conn.close()

    assert follow_up.status == 200


SHORT_TIMEOUT = 0.3


def _declare_a_body_and_send_none(rig: Rig, **header_overrides: str | None) -> bytes:
    """Declare 100 body bytes, send none, and read until the server closes the connection."""
    lines = [f"POST {_path(APPROVE)} HTTP/1.1", f"{HOST_HEADER}: {LOOPBACK}:{rig.port}"]
    lines += [f"{name}: {value}" for name, value in rig.headers(**header_overrides).items()]
    lines.append("Content-Length: 100")
    received = b""
    with socket.create_connection((LOOPBACK, rig.port), timeout=5) as sock:
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        while chunk := sock.recv(4096):
            received += chunk
    return received


def test_a_body_that_never_arrives_is_408_and_the_connection_closes(
    make_rig: RigFactory,
) -> None:
    rig = make_rig(request_timeout_seconds=SHORT_TIMEOUT)
    with rig.assert_writes_nothing():
        received = _declare_a_body_and_send_none(rig)

    assert received.startswith(b"HTTP/1.1 408 ")


def test_a_refused_write_that_never_sends_its_body_closes_the_connection(
    make_rig: RigFactory,
) -> None:
    rig = make_rig(request_timeout_seconds=SHORT_TIMEOUT)

    received = _declare_a_body_and_send_none(rig, **{TOKEN_HEADER: WRONG_TOKEN})

    assert received.startswith(b"HTTP/1.1 401 ")


# -- rejected: body -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        b"{not json",
        b"",
        b"\xff\xfe",
        b"[" * 10_000,
        b"[]",
        b'"text"',
        b"null",
    ],
    ids=["malformed", "empty", "not utf-8", "deeply nested", "list", "string", "null"],
)
def test_a_body_that_is_not_a_json_object_is_400(make_rig: RigFactory, body: bytes) -> None:
    rig = make_rig()
    with rig.assert_writes_nothing():
        assert rig.approve(body)[0] == 400


@pytest.mark.parametrize(
    "overrides",
    [
        {EPISODE_FIELD: None},
        {EPISODE_FIELD: 7},
        {EPISODE_FIELD: "has space"},
        {FINGERPRINT_FIELD: None},
        {FINGERPRINT_FIELD: "short"},
        {FINGERPRINT_FIELD: 7},
        {FINGERPRINT_FIELD: "A" * 64},
        {FINGERPRINT_FIELD: "Z" * 64},
    ],
    ids=[
        "no episode",
        "number episode",
        "bad episode",
        "no print",
        "short print",
        "number print",
        "upper-case hex print",
        "non-hex print",
    ],
)
def test_a_missing_or_malformed_field_is_400(
    make_rig: RigFactory, overrides: dict[str, object]
) -> None:
    rig = make_rig()
    with rig.assert_writes_nothing():
        assert rig.approve(rig.body(**overrides))[0] == 400


@pytest.mark.parametrize(
    ("answers", "number"),
    [
        ([ANSWER_FINE, "   "], 2),
        (["x" * (MAX_PLAN_DECISION_ANSWER_CHARS + 1), ANSWER_FINE], 1),
        ([ANSWER_FINE, "two\nlines"], 2),
        ([7, ANSWER_FINE], 1),
        (["https://example.com/leak", ANSWER_FINE], 1),
    ],
    ids=["blank", "one character too long", "two lines", "not text", "a link"],
)
def test_an_invalid_answer_is_400_and_names_its_decision(
    make_rig: RigFactory, answers: list[object], number: int
) -> None:
    rig = make_rig(_run(PLAN))
    with rig.assert_writes_nothing():
        status, payload = rig.answer(rig.body(answers=answers))

    assert (status, payload["decision"]) == (400, number)


@pytest.mark.parametrize(
    "answers", [None, [], "text", ["Only one."], ["a", "b", "c"]], ids=lambda value: repr(value)
)
def test_answers_that_do_not_cover_the_decisions_are_400(
    make_rig: RigFactory, answers: object
) -> None:
    rig = make_rig(_run(PLAN))
    with rig.assert_writes_nothing():
        status, payload = rig.answer(rig.body(answers=answers))

    assert status == 400
    assert "decision" not in payload


# -- rejected: run ------------------------------------------------------------------------


def test_an_unknown_run_is_404(make_rig: RigFactory) -> None:
    rig = make_rig()
    with rig.assert_writes_nothing():
        status, _ = rig.post(_path(APPROVE, MISSING_RUN), rig.body(), headers=rig.headers())

    assert status == 404


@pytest.mark.parametrize("run_id", ["../etc", "..%2Fetc", "a%2Fb", "run id", "x" * 129])
def test_a_run_id_that_is_not_shaped_like_one_is_400(make_rig: RigFactory, run_id: str) -> None:
    rig = make_rig()
    with rig.assert_writes_nothing():
        status, _ = rig.post(_path(APPROVE, run_id).replace(" ", "%20"), rig.body())

    assert status == 400


# -- rejected: 409 ------------------------------------------------------------------------


def _conflict_cases() -> list[Any]:
    other_episode = OLD_EPISODE
    return [
        pytest.param(
            lambda rig: rig.approve(rig.body(episode_id=other_episode)),
            _run(),
            STALE_EPISODE,
            id="an old episode id",
        ),
        pytest.param(
            lambda rig: rig.approve(rig.body(context_fingerprint=OTHER_FINGERPRINT)),
            _run(),
            "stale_fingerprint",
            id="a changed context fingerprint",
        ),
        pytest.param(
            lambda rig: rig.answer(rig.body(answers=["One.", "Two."])),
            _run(RISK),
            WRONG_ACTION,
            id="the answer action on a risk approval",
        ),
        pytest.param(
            lambda rig: rig.approve(),
            _run(PLAN),
            WRONG_ACTION,
            id="the approve action on a plan decision",
        ),
        pytest.param(
            lambda rig: rig.approve(),
            _run(reopen_count=3),
            "reopen_limit",
            id="the reopen limit reached",
        ),
        pytest.param(
            lambda rig: rig.approve(),
            _run(created_at=NOW - timedelta(hours=25)),
            EXPIRED,
            id="the reply window ended",
        ),
        pytest.param(
            lambda rig: rig.approve(),
            _run(state=WorkflowState.IMPLEMENTING),
            NOT_WAITING,
            id="a run in IMPLEMENTING",
        ),
        pytest.param(
            lambda rig: rig.approve(),
            _run(status=EscalationStatus.REOPENED),
            NOT_WAITING,
            id="a run already reopened",
        ),
    ]


@pytest.mark.parametrize(("send", "run", REASON), _conflict_cases())
def test_a_request_the_service_would_refuse_is_409_with_its_reason(
    make_rig: RigFactory, send: Callable[[Rig], tuple[int, Any]], run: FactoryRun, reason: str
) -> None:
    rig = make_rig(run)
    with rig.assert_writes_nothing():
        status, payload = send(rig)

    assert (status, payload[REASON]) == (409, reason)


def _oversized(rig: Rig) -> bytes:
    return json.dumps(rig.body()).encode() + b" " * MAX_BODY_BYTES


def _unknown_run_with(rig: Rig, **overrides: object) -> tuple[int, Any]:
    return rig.post(_path(APPROVE, MISSING_RUN), rig.body(**overrides), headers=rig.headers())


def _precedence_cases() -> list[Any]:
    two_answers = [ANSWER_FORMAT, ANSWER_CACHE]
    window_ended = {"created_at": NOW - timedelta(hours=25)}
    # Each row has two faults. The first one in the documented order is the one reported:
    # transport (415, 413) before the body (400), the body before the run (404), the run
    # before the conflicts, the conflicts in the service's order -- not waiting, window,
    # reopen limit, episode, fingerprint, action -- and the answer count after them all.
    return [
        pytest.param(
            _run(),
            lambda rig: rig.approve(
                rig.body(episode_id=OLD_EPISODE, context_fingerprint=OTHER_FINGERPRINT)
            ),
            (409, {REASON: STALE_EPISODE}),
            id="stale episode and stale fingerprint: the episode",
        ),
        pytest.param(
            _run(),
            lambda rig: rig.answer(
                rig.body(answers=two_answers, context_fingerprint=OTHER_FINGERPRINT)
            ),
            (409, {REASON: "stale_fingerprint"}),
            id="wrong action and stale fingerprint: the fingerprint",
        ),
        pytest.param(
            _run(**window_ended),
            lambda rig: rig.answer(rig.body(answers=two_answers)),
            (409, {REASON: EXPIRED}),
            id="wrong action and ended window: the window",
        ),
        pytest.param(
            _run(**window_ended),
            lambda rig: rig.approve(rig.body(episode_id=OLD_EPISODE)),
            (409, {REASON: EXPIRED}),
            id="stale episode and ended window: the window",
        ),
        pytest.param(
            _run(reopen_count=3),
            lambda rig: rig.approve(rig.body(episode_id=OLD_EPISODE)),
            (409, {REASON: "reopen_limit"}),
            id="stale episode and reopen limit: the reopen limit",
        ),
        pytest.param(
            _run(reopen_count=3, **window_ended),
            lambda rig: rig.approve(),
            (409, {REASON: EXPIRED}),
            id="ended window and reopen limit: the window",
        ),
        pytest.param(
            _run(PLAN),
            lambda rig: rig.answer(rig.body(episode_id=OLD_EPISODE, answers=[ANSWER_FINE])),
            (409, {REASON: STALE_EPISODE}),
            id="plan: stale episode and wrong answer count: the episode",
        ),
        pytest.param(
            _run(PLAN),
            lambda rig: rig.answer(rig.body(episode_id=OLD_EPISODE, answers=[ANSWER_FINE, " "])),
            (400, {"decision": 2}),
            id="plan: stale episode and blank answer: the answer",
        ),
        pytest.param(
            _run(),
            lambda rig: _unknown_run_with(rig, episode_id=OLD_EPISODE),
            (404, {}),
            id="unknown run and stale episode: the run",
        ),
        pytest.param(
            _run(),
            lambda rig: _unknown_run_with(rig, context_fingerprint="short"),
            (400, {}),
            id="unknown run and invalid body: the body",
        ),
        pytest.param(
            _run(),
            lambda rig: rig.approve(_oversized(rig), **{CONTENT_TYPE: TEXT_TYPE}),
            (415, {}),
            id="wrong content type and oversize length: the content type",
        ),
        pytest.param(
            _run(),
            lambda rig: rig.approve(_oversized(rig)),
            (413, {}),
            id="oversize length and valid headers: the length",
        ),
    ]


@pytest.mark.parametrize(("run", "send", "expected"), _precedence_cases())
def test_a_request_with_two_faults_is_refused_for_the_first_in_the_documented_order(
    make_rig: RigFactory,
    run: FactoryRun,
    send: Callable[[Rig], tuple[int, Any]],
    expected: tuple[int, dict[str, object]],
) -> None:
    rig = make_rig(run)
    with rig.assert_writes_nothing():
        status, payload = send(rig)

    actual = (status, {key: payload.get(key) for key in expected[1]})
    assert actual == expected


def test_a_run_without_an_escalation_is_not_waiting(make_rig: RigFactory) -> None:
    rig = make_rig(
        FactoryRun(id=RUN_ID, work_item_id=WORK_ITEM_ID, state=WorkflowState.NEEDS_HUMAN)
    )
    rig.run = _run()

    assert rig.approve() == (409, {"error": "conflict", REASON: NOT_WAITING})


def test_an_existing_request_for_the_episode_is_409(make_rig: RigFactory) -> None:
    rig = make_rig()
    assert rig.approve()[0] == 202
    with rig.assert_writes_nothing():
        status, payload = rig.approve()

    assert (status, payload[REASON]) == (409, "existing_request")


@pytest.mark.parametrize(
    ("max_reopens", "window_hours", "expected"),
    [
        pytest.param(4, 2, (202, None), id="both limits above what the run used"),
        pytest.param(3, 2, (409, "reopen_limit"), id="reopen limit equal to the reopens used"),
        pytest.param(4, 1, (409, EXPIRED), id="window shorter than the run's age"),
        pytest.param(None, None, (202, None), id="unknown limits are not checked"),
    ],
)
def test_the_limits_are_the_configured_ones(
    make_rig: RigFactory,
    max_reopens: int | None,
    window_hours: float | None,
    expected: tuple[int, str | None],
) -> None:
    # Three reopens used and escalated 90 minutes ago.
    run = _run(reopen_count=3, created_at=NOW - timedelta(minutes=90))
    rig = make_rig(run, max_reopens=max_reopens, reply_window_hours=window_hours)

    status, payload = rig.approve()

    actual = (status, payload.get(REASON))
    assert actual == expected


def test_two_approvals_at_once_make_one_request(make_rig: RigFactory) -> None:
    rig = make_rig()
    barrier = threading.Barrier(2)
    results: list[tuple[int, Any]] = []

    def send() -> None:
        barrier.wait(timeout=5)
        results.append(rig.approve())

    threads = [threading.Thread(target=send) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(status for status, _ in results) == [202, 409]
    assert [payload[REASON] for status, payload in results if status == 409] == ["existing_request"]
    assert len(rig.requests()) == 1
    assert len(list(rig.data_dir.rglob("dashboard-approval-*.json"))) == 1


def test_a_run_that_vanishes_before_the_write_is_404(make_rig: RigFactory) -> None:
    # double-waiver: B1 — a run file that vanishes between the read and the write
    def vanished(run_id: str, request: DashboardResumeRequest) -> ResumeRequestResult:
        return "run_missing"

    rig = make_rig(requester=vanished)

    assert rig.approve()[0] == 404


def _approval_request(run: FactoryRun) -> DashboardResumeRequest:
    return DashboardResumeRequest(
        run_id=RUN_ID,
        episode_id=EPISODE,
        context_fingerprint=_fingerprint(run),
        action=RISK,
        created_at=NOW,
    )


def test_the_production_reader_reads_a_stored_run(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    run = _run()
    store.save_run(run)

    assert build_resume_run_reader(store)(RUN_ID) == run


def test_the_production_reader_gives_none_for_an_unknown_run(tmp_path: Path) -> None:
    assert build_resume_run_reader(FileRunStore(tmp_path))(MISSING_RUN) is None


def test_the_production_reader_gives_none_for_a_corrupt_run_file(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    store.save_run(_run()).write_text("{not json", encoding=ENCODING)

    assert build_resume_run_reader(store)(RUN_ID) is None


def test_the_production_requester_reports_created_then_exists(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path)
    run = _run()
    store.save_run(run)
    request = _approval_request(run)
    requester = build_resume_requester(store)

    assert [requester(RUN_ID, request), requester(RUN_ID, request)] == ["created", "exists"]


def test_the_production_request_reader_lists_the_requests_of_one_episode_without_answers(
    tmp_path: Path,
) -> None:
    store = FileRunStore(tmp_path)
    run = _run(PLAN)
    store.save_run(run)
    marker = "private answer"
    request = DashboardResumeRequest(
        run_id=RUN_ID,
        episode_id=EPISODE,
        context_fingerprint=_fingerprint(run),
        action=PLAN,
        answers=[PlanDecisionAnswer(decision_number=1, answer=marker)],
        created_at=NOW,
    )
    assert build_resume_requester(store)(RUN_ID, request) == "created"
    read = build_resume_request_reader(store)

    requests = read(RUN_ID, EPISODE)

    assert [(r["action"], r["status"], r["context_fingerprint"]) for r in requests] == [
        ("PLAN_DECISION", "pending", _fingerprint(run))
    ]
    assert "answers" not in requests[0]
    assert marker not in repr(requests)
    assert read(RUN_ID, OLD_EPISODE) == []


def test_a_damaged_request_file_is_skipped_and_logged_by_type_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = FileRunStore(tmp_path)
    run = _run(PLAN)
    store.save_run(run)
    assert build_resume_requester(store)(RUN_ID, _approval_request(run)) == "created"
    (damaged,) = tmp_path.rglob("dashboard-approval-*.json")
    marker = "private answer"
    damaged.write_text(
        json.dumps({"action": "NOT_AN_ACTION", "answers": [{"answer": marker}]}), encoding="utf-8"
    )

    with caplog.at_level(logging.WARNING, logger="software_agent_factory.store"):
        requests = build_resume_request_reader(store)(RUN_ID, EPISODE)

    assert requests == []
    assert f"skipped dashboard request {damaged.name}: ValidationError" in caplog.text
    assert marker not in caplog.text


def test_the_production_requester_reports_a_missing_run(tmp_path: Path) -> None:
    run = _run()
    request = _approval_request(run)

    assert build_resume_requester(FileRunStore(tmp_path))(RUN_ID, request) == "run_missing"


# -- other writes stay blocked ------------------------------------------------------------


def _write_response(rig: Rig, method: str, path: str) -> tuple[int, str | None, str | None]:
    """The status, ``Allow`` and ``Connection`` of ``method path`` with a valid body and headers."""
    conn = http.client.HTTPConnection(LOOPBACK, rig.port, timeout=5)
    try:
        conn.request(method, path, body=json.dumps(rig.body()), headers=rig.headers())
        response = conn.getresponse()
        response.read()
        return response.status, response.getheader("Allow"), response.getheader("Connection")
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("method", "path", "allow"),
    [
        ("POST", "/api/runs", READ_ONLY_METHODS),
        ("POST", _path("retry"), READ_ONLY_METHODS),
        ("POST", RUN_PATH, READ_ONLY_METHODS),
        ("POST", f"{_path(APPROVE)}/extra", READ_ONLY_METHODS),
        ("PUT", _path(APPROVE), "POST"),
        ("PATCH", _path(ANSWER), "POST"),
        ("DELETE", RUN_PATH, READ_ONLY_METHODS),
        ("PATCH", "/api/summary", READ_ONLY_METHODS),
        ("OPTIONS", _path(APPROVE), "POST"),
        ("PUT", "//[::1", READ_ONLY_METHODS),
        ("POST", "//[::1", READ_ONLY_METHODS),
    ],
)
def test_every_other_write_is_405(make_rig: RigFactory, method: str, path: str, allow: str) -> None:
    rig = make_rig()
    with rig.assert_writes_nothing():
        assert _write_response(rig, method, path) == (405, allow, CLOSE)


@pytest.mark.parametrize("action", [APPROVE, ANSWER])
def test_without_resume_actions_the_routes_do_not_exist(make_rig: RigFactory, action: str) -> None:
    rig = make_rig(actions=False)
    with rig.assert_writes_nothing():
        assert _write_response(rig, "POST", _path(action)) == (405, READ_ONLY_METHODS, CLOSE)


# -- audit --------------------------------------------------------------------------------


AUDIT_LOGGER = "software_agent_factory.dashboard.audit"


def _audit(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == AUDIT_LOGGER]


def _event(action: str, result: str) -> str:
    return f"dashboard {action} run={RUN_ID} result={result}"


def test_an_accepted_request_writes_one_event_with_the_run_and_the_result(
    make_rig: RigFactory, caplog: pytest.LogCaptureFixture
) -> None:
    rig = make_rig()

    with caplog.at_level(logging.INFO):
        assert rig.approve()[0] == 202

    assert _audit(caplog) == [_event(APPROVE, "202")]


@pytest.mark.parametrize(
    ("overrides", "result"),
    [
        ({TOKEN_HEADER: WRONG_TOKEN}, "401"),
        ({ORIGIN_HEADER: None}, "403"),
        ({CONTENT_TYPE: TEXT_TYPE}, "415"),
        ({HOST_HEADER: EVIL_HOST}, "400"),
    ],
)
def test_a_rejected_request_writes_one_event_with_the_result(
    make_rig: RigFactory,
    caplog: pytest.LogCaptureFixture,
    overrides: dict[str, str | None],
    result: str,
) -> None:
    rig = make_rig()

    with caplog.at_level(logging.INFO):
        rig.approve(**overrides)

    assert _audit(caplog) == [_event(APPROVE, result)]


def test_a_conflict_event_carries_its_reason(
    make_rig: RigFactory, caplog: pytest.LogCaptureFixture
) -> None:
    rig = make_rig()

    with caplog.at_level(logging.INFO):
        rig.approve(rig.body(episode_id=OLD_EPISODE))

    assert _audit(caplog) == [_event(APPROVE, "409 stale_episode")]


def test_an_unexpected_failure_is_500_and_one_event(
    make_rig: RigFactory, caplog: pytest.LogCaptureFixture
) -> None:
    # double-waiver: B1 — a disk write that fails
    def failing(run_id: str, request: DashboardResumeRequest) -> ResumeRequestResult:
        raise RuntimeError("disk on fire")

    rig = make_rig(requester=failing)

    with caplog.at_level(logging.INFO):
        status, payload = rig.approve()

    assert (status, payload) == (500, {"error": "internal error"})
    assert _audit(caplog) == [_event(APPROVE, "500")]
    assert "disk on fire" not in json.dumps(payload)


def test_an_unexpected_failure_logs_the_exception_type_and_no_text(
    make_rig: RigFactory, caplog: pytest.LogCaptureFixture
) -> None:
    # double-waiver: B1 — a disk write that fails with text that could hold a plan answer
    def failing(run_id: str, request: DashboardResumeRequest) -> ResumeRequestResult:
        raise RuntimeError("private answer")

    rig = make_rig(requester=failing)

    with caplog.at_level(logging.ERROR, logger="software_agent_factory.dashboard"):
        rig.approve()

    assert "Unhandled dashboard error for an action on a run: RuntimeError" in caplog.text
    assert "private answer" not in caplog.text


def test_a_hostile_run_id_cannot_forge_a_log_line(
    make_rig: RigFactory, caplog: pytest.LogCaptureFixture
) -> None:
    rig = make_rig()

    with caplog.at_level(logging.INFO):
        rig.post(_path(ANSWER, "x%0Aresult=202%1B[2J"), rig.body(), headers=rig.headers())

    (event,) = _audit(caplog)
    assert "\n" not in event
    assert "\x1b" not in event
    assert event.endswith("result=400")


def test_the_logs_hold_neither_the_token_nor_the_body(
    make_rig: RigFactory, caplog: pytest.LogCaptureFixture
) -> None:
    rig = make_rig(_run(PLAN))
    secret_answer = "My private reasoning about the cache."

    with caplog.at_level(logging.DEBUG):
        accepted = rig.answer(rig.body(answers=[ANSWER_FORMAT, secret_answer]))
        refused = rig.post(
            f"{_path(ANSWER)}?token={rig.server.token}",
            rig.body(answers=["", secret_answer]),
            headers=rig.headers(**{TOKEN_HEADER: "wrong-but-secret-looking"}),
        )
        bad_answer = rig.answer(rig.body(answers=[ANSWER_FORMAT, ""]))

    assert (accepted[0], refused[0], bad_answer[0]) == (202, 401, 400)
    # The guard: the checks below prove nothing unless the three events were logged.
    assert _audit(caplog) == [
        _event(ANSWER, "202"),
        _event(ANSWER, "401"),
        _event(ANSWER, "400"),
    ]
    logged = "\n".join(r.getMessage() + repr(r.args) for r in caplog.records)
    assert rig.server.token not in logged
    assert "wrong-but-secret-looking" not in logged
    assert secret_answer not in logged
    assert ANSWER_FORMAT not in logged
    assert rig.run.escalation is not None
    assert _fingerprint(rig.run) not in logged


# -- imports ------------------------------------------------------------------------------

PACKAGE = "software_agent_factory"
#: The package-internal modules ``dashboard/`` may import. Each is a leaf for the dashboard:
#: none of them runs a workflow, calls GitHub, starts a process or writes a run.
ALLOWED_PACKAGE_MODULES = {"models", RESUME, "escalation_protocol", "redaction", "store"}
#: What ``dashboard/`` may take from ``resume``: the read functions it uses, and the type of
#: one of their results. A write function, or the whole module, is not on the list.
ALLOWED_RESUME_NAMES = {
    "RequestMismatch",
    "build_plan_answers",
    "clean_plan_answer",
    "request_refusal",
}
WHOLE_MODULE = "*"


def _package_imports(source: str) -> set[tuple[str, str]]:
    """The ``(module, name)`` pairs ``source`` imports from this package.

    ``module`` is the first segment after the package: ``resume`` for ``from ..resume import
    x`` and for ``import software_agent_factory.resume``. ``name`` is what is taken from it, or
    ``*`` for the module as a whole (``import ...resume``, ``from .. import resume``). Imports
    inside ``dashboard/`` itself (one dot) and from outside the package are not returned,
    except that a dotted import that climbs past the package is a failure of this test.
    """
    pairs: set[tuple[str, str]] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                head, _, rest = alias.name.partition(".")
                if head == PACKAGE and rest:
                    pairs.add((rest.split(".")[0], WHOLE_MODULE))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 2 and module:
                pairs.update((module.split(".")[0], alias.name) for alias in node.names)
            elif node.level == 2:
                pairs.update((alias.name, WHOLE_MODULE) for alias in node.names)
            elif node.level == 0 and module.split(".")[0] == PACKAGE:
                _, _, rest = module.partition(".")
                if rest:
                    pairs.update((rest.split(".")[0], alias.name) for alias in node.names)
                else:
                    pairs.update((alias.name, WHOLE_MODULE) for alias in node.names)
    return pairs


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "from ..resume import clean_plan_answer, request_refusal",
            {(RESUME, "clean_plan_answer"), (RESUME, "request_refusal")},
        ),
        ("from ..resume.sub import thing", {(RESUME, "thing")}),
        ("from .. import workflow", {("workflow", WHOLE_MODULE)}),
        ("import software_agent_factory.github", {("github", WHOLE_MODULE)}),
        ("from software_agent_factory.service import run", {("service", "run")}),
        ("from software_agent_factory import escalation", {("escalation", WHOLE_MODULE)}),
        ("from .snapshot import is_valid_run_id", set()),
        ("from . import assets", set()),
        ("import subprocess", set()),
        ("from http import HTTPStatus", set()),
    ],
    ids=[
        "names from a module",
        "names from a sub-module",
        "a module from the package root",
        "an absolute module import",
        "absolute names",
        "an absolute module from the package root",
        "a sibling module",
        "a sibling from the dashboard root",
        "the standard library",
        "the standard library with names",
    ],
)
def test_the_import_scan_sees_every_way_to_import_a_package_module(
    source: str, expected: set[tuple[str, str]]
) -> None:
    assert _package_imports(source) == expected


def _dashboard_sources() -> list[Path]:
    sources = sorted(Path(dashboard.__file__).parent.rglob("*.py"))
    assert sources, "the dashboard package moved; update this test"
    return sources


def test_the_dashboard_package_imports_only_the_allowed_package_modules() -> None:
    found = {
        path.name: {module for module, _ in _package_imports(path.read_text(encoding=ENCODING))}
        - ALLOWED_PACKAGE_MODULES
        for path in _dashboard_sources()
    }

    assert {name: modules for name, modules in found.items() if modules} == {}


def _imports_subprocess(source: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import) and any(
            alias.name.split(".")[0] == "subprocess" for alias in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "subprocess":
            return True
    return False


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import subprocess", True),
        ("import subprocess as sp", True),
        ("from subprocess import run", True),
        ("import os, subprocess", True),
        ("import os", False),
        ("from .snapshot import subprocess_free", False),
    ],
)
def test_the_subprocess_scan_sees_every_way_to_import_it(source: str, expected: bool) -> None:
    assert _imports_subprocess(source) is expected


def test_the_dashboard_package_imports_no_subprocess() -> None:
    importing = [
        path.name
        for path in _dashboard_sources()
        if _imports_subprocess(path.read_text(encoding=ENCODING))
    ]

    assert importing == []


def test_the_dashboard_package_takes_only_read_functions_from_resume() -> None:
    taken = {
        name
        for path in _dashboard_sources()
        for module, name in _package_imports(path.read_text(encoding=ENCODING))
        if module == RESUME
    }

    assert taken - ALLOWED_RESUME_NAMES == set()
