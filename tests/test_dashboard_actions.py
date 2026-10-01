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
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from software_agent_factory import dashboard
from software_agent_factory.dashboard import DashboardConfig, DashboardServer, create_server
from software_agent_factory.dashboard.actions import ResumeActions
from software_agent_factory.dashboard.handler import MAX_BODY_BYTES
from software_agent_factory.dashboard.security import TOKEN_HEADER
from software_agent_factory.dashboard.snapshot import ResumeRequestResult
from software_agent_factory.models import (
    Complexity,
    DashboardResumeRequest,
    EscalationRecord,
    EscalationStatus,
    FactoryRun,
    PlanDecisionContext,
    ResumeClassification,
    Risk,
    RiskApprovalContext,
    RiskRationale,
    WorkflowState,
)
from software_agent_factory.resume import (
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
        work_item_id="task-1",
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
            work_item_id="task-1",
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
        "episode_id": EPISODE,
        "status": EscalationStatus.NOTIFIED,
        "resume_classification": kind,
        "created_at": NOW - timedelta(hours=1),
        "approval_context": _risk_context() if kind is RISK else None,
        "plan_decision_context": _plan_context() if kind is PLAN else None,
    }
    escalation = EscalationRecord.model_validate({**fields, **record})
    return FactoryRun(id=RUN_ID, work_item_id="task-1", state=state, escalation=escalation)


def _fingerprint(run: FactoryRun) -> str:
    assert run.escalation is not None
    context = run.escalation.approval_context or run.escalation.plan_decision_context
    assert context is not None
    return context.context_fingerprint


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
            "episode_id": EPISODE,
            "context_fingerprint": _fingerprint(self.run),
        }
        if self.run.escalation is not None and self.run.escalation.resume_classification is PLAN:
            body["answers"] = ["JSON files.", "Ten entries."]
        body.update(overrides)
        return {key: value for key, value in body.items() if value is not None}

    def headers(self, **overrides: str | None) -> dict[str, str]:
        """The headers of a valid write, with ``overrides`` replacing or (as ``None``) dropping."""
        headers: dict[str, str | None] = {
            TOKEN_HEADER: self.server.token,
            ORIGIN_HEADER: f"http://127.0.0.1:{self.port}",
            "Content-Type": "application/json",
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
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("POST", path, body=payload, headers=headers or self.headers())
            response = conn.getresponse()
            return response.status, json.loads(response.read() or b"null")
        finally:
            conn.close()

    def approve(self, body: object = None, **header_overrides: str | None) -> tuple[int, Any]:
        return self.post(
            f"/api/runs/{RUN_ID}/approve",
            self.body() if body is None else body,
            headers=self.headers(**header_overrides),
        )

    def answer(self, body: object = None, **header_overrides: str | None) -> tuple[int, Any]:
        return self.post(
            f"/api/runs/{RUN_ID}/answer",
            self.body() if body is None else body,
            headers=self.headers(**header_overrides),
        )

    def requests(self) -> list[DashboardResumeRequest]:
        return self.store.list_dashboard_requests(RUN_ID, EPISODE)


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
    ) -> Rig:
        stored = run if run is not None else _run()
        store = FileRunStore(tmp_path)
        store.save_run(stored)

        def run_reader(run_id: str) -> FactoryRun | None:
            try:
                return store.load_run(run_id)
            except (OSError, ValueError):
                return None

        def create(run_id: str, request: DashboardResumeRequest) -> ResumeRequestResult:
            try:
                return "created" if store.create_dashboard_request(run_id, request) else "exists"
            except FileNotFoundError:
                return "run_missing"

        server = create_server(
            DashboardConfig(
                snapshot_provider=lambda *, limit, offset: {},
                run_detail_provider=lambda run_id: None,
                resume_actions=ResumeActions(
                    run_reader=run_reader,
                    requester=requester or create,
                    reply_window_hours=reply_window_hours,
                    max_reopens=max_reopens,
                    clock=lambda: NOW,
                )
                if actions
                else None,
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
        (1, "JSON files."),
        (2, "Ten entries."),
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

    assert rig.approve(**{"Content-Type": content_type})[0] == 202


def test_a_body_of_exactly_16_kb_is_accepted(make_rig: RigFactory) -> None:
    rig = make_rig()
    body = json.dumps(rig.body()).encode()
    padded = body + b" " * (MAX_BODY_BYTES - len(body))
    assert len(padded) == 16 * 1024

    assert rig.approve(padded)[0] == 202


def test_a_500_character_plan_answer_is_accepted(make_rig: RigFactory) -> None:
    rig = make_rig(_run(PLAN))

    assert rig.answer(rig.body(answers=["x" * 500, "Ten entries."]))[0] == 202
    assert len(rig.requests()[0].answers[0].answer) == 500


# -- rejected: transport ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("problem", "overrides", "status"),
    [
        ("no token header", {TOKEN_HEADER: None}, 401),
        ("a wrong token header", {TOKEN_HEADER: "wrong"}, 401),
        ("no Origin header", {ORIGIN_HEADER: None}, 403),
        ("a foreign Origin", {ORIGIN_HEADER: "http://evil.example"}, 403),
        ("the localhost alias as Origin", {ORIGIN_HEADER: "http://localhost:1"}, 403),
        ("a form-encoded body", {"Content-Type": "application/x-www-form-urlencoded"}, 415),
        ("no content type", {"Content-Type": None}, 415),
        ("a look-alike content type", {"Content-Type": "text/json"}, 415),
        (
            "a parameter that hides the type",
            {"Content-Type": "text/plain; x=application/json"},
            415,
        ),
        ("a wrong Host", {"Host": "evil.example:80"}, 400),
    ],
)
def test_a_rejected_request_changes_nothing(
    make_rig: RigFactory, problem: str, overrides: dict[str, str | None], status: int
) -> None:
    rig = make_rig()
    before = _tree(rig.data_dir)

    assert rig.approve(**overrides)[0] == status, problem

    assert _tree(rig.data_dir) == before


def test_the_token_only_in_the_query_string_is_not_enough(make_rig: RigFactory) -> None:
    rig = make_rig()
    before = _tree(rig.data_dir)

    status, _ = rig.post(
        f"/api/runs/{RUN_ID}/approve?token={rig.server.token}",
        rig.body(),
        headers=rig.headers(**{TOKEN_HEADER: None}),
    )

    assert status == 401
    assert _tree(rig.data_dir) == before


def test_a_body_over_16_kb_is_413(make_rig: RigFactory) -> None:
    rig = make_rig()
    before = _tree(rig.data_dir)
    body = json.dumps(rig.body()).encode()

    status, _ = rig.approve(body + b" " * (MAX_BODY_BYTES - len(body) + 1))

    assert status == 413
    assert _tree(rig.data_dir) == before


def test_the_checks_run_in_the_documented_order(make_rig: RigFactory) -> None:
    rig = make_rig()
    everything_wrong = {
        "Host": "evil.example:80",
        ORIGIN_HEADER: "http://evil.example",
        TOKEN_HEADER: "wrong",
        "Content-Type": "text/plain",
    }
    order = [("Host", 400), (ORIGIN_HEADER, 403), (TOKEN_HEADER, 401), ("Content-Type", 415)]
    for name, status in order:
        assert rig.approve(b"not json", **everything_wrong)[0] == status
        del everything_wrong[name]
    assert rig.approve(b"not json", **everything_wrong)[0] == 400


@pytest.mark.parametrize(
    ("length", "status"), [("abc", 400), ("-1", 400), ("", 400), ("9" * 40, 413)]
)
def test_a_content_length_that_is_not_a_size(
    make_rig: RigFactory, length: str, status: int
) -> None:
    rig = make_rig()
    conn = http.client.HTTPConnection("127.0.0.1", rig.port, timeout=5)
    conn.putrequest("POST", f"/api/runs/{RUN_ID}/approve")
    for name, value in rig.headers().items():
        conn.putheader(name, value)
    conn.putheader("Content-Length", length)
    conn.endheaders()
    response = conn.getresponse()
    response.read()
    conn.close()

    assert response.status == status
    assert response.getheader("Connection") == "close"


def test_a_refused_write_leaves_the_connection_usable(make_rig: RigFactory) -> None:
    rig = make_rig()
    conn = http.client.HTTPConnection("127.0.0.1", rig.port, timeout=5)
    body = json.dumps(rig.body()).encode()
    conn.request(
        "POST",
        f"/api/runs/{RUN_ID}/approve",
        body=body,
        headers=rig.headers(**{TOKEN_HEADER: "wrong"}),
    )
    refused = conn.getresponse()
    refused.read()
    assert refused.status == 401

    conn.request("GET", "/healthz", headers={TOKEN_HEADER: rig.server.token})
    follow_up = conn.getresponse()
    follow_up.read()
    conn.close()

    assert follow_up.status == 200


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
    before = _tree(rig.data_dir)

    assert rig.approve(body)[0] == 400
    assert _tree(rig.data_dir) == before


@pytest.mark.parametrize(
    "overrides",
    [
        {"episode_id": None},
        {"episode_id": 7},
        {"episode_id": "has space"},
        {"context_fingerprint": None},
        {"context_fingerprint": "short"},
        {"context_fingerprint": 7},
    ],
    ids=["no episode", "number episode", "bad episode", "no print", "short print", "number print"],
)
def test_a_missing_or_malformed_field_is_400(
    make_rig: RigFactory, overrides: dict[str, object]
) -> None:
    rig = make_rig()
    before = _tree(rig.data_dir)

    assert rig.approve(rig.body(**overrides))[0] == 400
    assert _tree(rig.data_dir) == before


@pytest.mark.parametrize(
    ("answers", "number"),
    [
        (["Fine.", "   "], 2),
        (["x" * 501, "Fine."], 1),
        (["Fine.", "two\nlines"], 2),
        ([7, "Fine."], 1),
        (["https://example.com/leak", "Fine."], 1),
    ],
    ids=["blank", "501 characters", "two lines", "not text", "a link"],
)
def test_an_invalid_answer_is_400_and_names_its_decision(
    make_rig: RigFactory, answers: list[object], number: int
) -> None:
    rig = make_rig(_run(PLAN))
    before = _tree(rig.data_dir)

    status, payload = rig.answer(rig.body(answers=answers))

    assert (status, payload["decision"]) == (400, number)
    assert _tree(rig.data_dir) == before


@pytest.mark.parametrize(
    "answers", [None, [], "text", ["Only one."], ["a", "b", "c"]], ids=lambda value: repr(value)
)
def test_answers_that_do_not_cover_the_decisions_are_400(
    make_rig: RigFactory, answers: object
) -> None:
    rig = make_rig(_run(PLAN))
    before = _tree(rig.data_dir)

    status, payload = rig.answer(rig.body(answers=answers))

    assert status == 400
    assert "decision" not in payload
    assert _tree(rig.data_dir) == before


# -- rejected: run ------------------------------------------------------------------------


def test_an_unknown_run_is_404(make_rig: RigFactory) -> None:
    rig = make_rig()
    before = _tree(rig.data_dir)

    status, _ = rig.post("/api/runs/run-missing/approve", rig.body(), headers=rig.headers())

    assert status == 404
    assert _tree(rig.data_dir) == before


@pytest.mark.parametrize("run_id", ["../etc", "..%2Fetc", "a%2Fb", "run id", "x" * 129])
def test_a_run_id_that_is_not_shaped_like_one_is_400(make_rig: RigFactory, run_id: str) -> None:
    rig = make_rig()
    before = _tree(rig.data_dir)

    status, _ = rig.post(f"/api/runs/{run_id}/approve".replace(" ", "%20"), rig.body())

    assert status == 400
    assert _tree(rig.data_dir) == before


# -- rejected: 409 ------------------------------------------------------------------------


def _conflict_cases() -> list[Any]:
    other_episode = "ep-old"
    return [
        pytest.param(
            lambda rig: rig.approve(rig.body(episode_id=other_episode)),
            _run(),
            "stale_episode",
            id="an old episode id",
        ),
        pytest.param(
            lambda rig: rig.approve(rig.body(context_fingerprint="b" * 64)),
            _run(),
            "stale_fingerprint",
            id="a changed context fingerprint",
        ),
        pytest.param(
            lambda rig: rig.answer(rig.body(answers=["One.", "Two."])),
            _run(RISK),
            "wrong_action",
            id="the answer action on a risk approval",
        ),
        pytest.param(
            lambda rig: rig.approve(),
            _run(PLAN),
            "wrong_action",
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
            "expired",
            id="the reply window ended",
        ),
        pytest.param(
            lambda rig: rig.approve(),
            _run(state=WorkflowState.IMPLEMENTING),
            "not_waiting",
            id="a run in IMPLEMENTING",
        ),
        pytest.param(
            lambda rig: rig.approve(),
            _run(status=EscalationStatus.REOPENED),
            "not_waiting",
            id="a run already reopened",
        ),
    ]


@pytest.mark.parametrize(("send", "run", "reason"), _conflict_cases())
def test_a_request_the_service_would_refuse_is_409_with_its_reason(
    make_rig: RigFactory, send: Callable[[Rig], tuple[int, Any]], run: FactoryRun, reason: str
) -> None:
    rig = make_rig(run)
    before = _tree(rig.data_dir)

    status, payload = send(rig)

    assert (status, payload["reason"]) == (409, reason)
    assert _tree(rig.data_dir) == before


def test_a_run_without_an_escalation_is_not_waiting(make_rig: RigFactory) -> None:
    rig = make_rig(FactoryRun(id=RUN_ID, work_item_id="task-1", state=WorkflowState.NEEDS_HUMAN))
    rig.run = _run()

    assert rig.approve() == (409, {"error": "conflict", "reason": "not_waiting"})


def test_an_existing_request_for_the_episode_is_409(make_rig: RigFactory) -> None:
    rig = make_rig()
    assert rig.approve()[0] == 202
    before = _tree(rig.data_dir)

    status, payload = rig.approve()

    assert (status, payload["reason"]) == (409, "existing_request")
    assert _tree(rig.data_dir) == before


def test_the_limits_are_the_configured_ones(make_rig: RigFactory) -> None:
    # Three reopens used: refused at a limit of 3, accepted at 4. A window of 2 hours has
    # ended for a run escalated 1 hour 30 minutes ago only if it is 1 hour.
    run = _run(reopen_count=3, created_at=NOW - timedelta(minutes=90))

    assert make_rig(run, max_reopens=4, reply_window_hours=2).approve()[0] == 202
    assert make_rig(run, max_reopens=3, reply_window_hours=2).approve()[1]["reason"] == (
        "reopen_limit"
    )
    assert make_rig(run, max_reopens=4, reply_window_hours=1).approve()[1]["reason"] == "expired"


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
    assert [payload["reason"] for status, payload in results if status == 409] == [
        "existing_request"
    ]
    assert len(rig.requests()) == 1
    assert len(list(rig.data_dir.rglob("dashboard-approval-*.json"))) == 1


def test_a_run_that_vanishes_before_the_write_is_404(make_rig: RigFactory) -> None:
    rig = make_rig(requester=lambda run_id, request: "run_missing")

    assert rig.approve()[0] == 404


# -- other writes stay blocked ------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "allow"),
    [
        ("POST", "/api/runs", "GET, HEAD"),
        ("POST", f"/api/runs/{RUN_ID}/retry", "GET, HEAD"),
        ("POST", f"/api/runs/{RUN_ID}", "GET, HEAD"),
        ("POST", f"/api/runs/{RUN_ID}/approve/extra", "GET, HEAD"),
        ("PUT", f"/api/runs/{RUN_ID}/approve", "POST"),
        ("PATCH", f"/api/runs/{RUN_ID}/answer", "POST"),
        ("DELETE", f"/api/runs/{RUN_ID}", "GET, HEAD"),
        ("PATCH", "/api/summary", "GET, HEAD"),
        ("OPTIONS", f"/api/runs/{RUN_ID}/approve", "POST"),
        ("PUT", "//[::1", "GET, HEAD"),
        ("POST", "//[::1", "GET, HEAD"),
    ],
)
def test_every_other_write_is_405(make_rig: RigFactory, method: str, path: str, allow: str) -> None:
    rig = make_rig()
    before = _tree(rig.data_dir)
    conn = http.client.HTTPConnection("127.0.0.1", rig.port, timeout=5)
    conn.request(method, path, body=json.dumps(rig.body()), headers=rig.headers())
    response = conn.getresponse()
    response.read()
    conn.close()

    assert response.status == 405
    assert response.getheader("Allow") == allow
    assert _tree(rig.data_dir) == before


@pytest.mark.parametrize("action", ["approve", "answer"])
def test_without_resume_actions_the_routes_do_not_exist(make_rig: RigFactory, action: str) -> None:
    rig = make_rig(actions=False)
    before = _tree(rig.data_dir)

    status, _ = rig.post(f"/api/runs/{RUN_ID}/{action}", rig.body(), headers=rig.headers())

    assert status == 405
    assert _tree(rig.data_dir) == before


# -- audit --------------------------------------------------------------------------------


AUDIT_LOGGER = "software_agent_factory.dashboard.audit"


def _audit(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == AUDIT_LOGGER]


def test_an_accepted_request_writes_one_event_with_the_run_and_the_result(
    make_rig: RigFactory, caplog: pytest.LogCaptureFixture
) -> None:
    rig = make_rig()

    with caplog.at_level(logging.INFO):
        assert rig.approve()[0] == 202

    assert _audit(caplog) == [f"dashboard approve run={RUN_ID} result=202"]


@pytest.mark.parametrize(
    ("overrides", "result"),
    [
        ({TOKEN_HEADER: "wrong"}, "401"),
        ({ORIGIN_HEADER: None}, "403"),
        ({"Content-Type": "text/plain"}, "415"),
        ({"Host": "evil.example:80"}, "400"),
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

    assert _audit(caplog) == [f"dashboard approve run={RUN_ID} result={result}"]


def test_a_conflict_event_carries_its_reason(
    make_rig: RigFactory, caplog: pytest.LogCaptureFixture
) -> None:
    rig = make_rig()

    with caplog.at_level(logging.INFO):
        rig.approve(rig.body(episode_id="ep-old"))

    assert _audit(caplog) == [f"dashboard approve run={RUN_ID} result=409 stale_episode"]


def test_an_unexpected_failure_is_500_and_one_event(
    make_rig: RigFactory, caplog: pytest.LogCaptureFixture
) -> None:
    def failing(run_id: str, request: DashboardResumeRequest) -> ResumeRequestResult:
        raise RuntimeError("disk on fire")

    rig = make_rig(requester=failing)

    with caplog.at_level(logging.INFO):
        status, payload = rig.approve()

    assert (status, payload) == (500, {"error": "internal error"})
    assert _audit(caplog) == [f"dashboard approve run={RUN_ID} result=500"]
    assert "disk on fire" not in json.dumps(payload)


def test_a_hostile_run_id_cannot_forge_a_log_line(
    make_rig: RigFactory, caplog: pytest.LogCaptureFixture
) -> None:
    rig = make_rig()

    with caplog.at_level(logging.INFO):
        rig.post("/api/runs/x%0Aresult=202%1B[2J/answer", rig.body(), headers=rig.headers())

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
        accepted = rig.answer(rig.body(answers=["JSON files.", secret_answer]))
        refused = rig.post(
            f"/api/runs/{RUN_ID}/answer?token={rig.server.token}",
            rig.body(answers=["", secret_answer]),
            headers=rig.headers(**{TOKEN_HEADER: "wrong-but-secret-looking"}),
        )
        bad_answer = rig.answer(rig.body(answers=["JSON files.", ""]))

    assert (accepted[0], refused[0], bad_answer[0]) == (202, 401, 400)
    logged = "\n".join(r.getMessage() + repr(r.args) for r in caplog.records)
    assert rig.server.token not in logged
    assert "wrong-but-secret-looking" not in logged
    assert secret_answer not in logged
    assert "JSON files." not in logged
    assert rig.run.escalation is not None
    assert _fingerprint(rig.run) not in logged


# -- imports ------------------------------------------------------------------------------


FORBIDDEN_IMPORTS = {"workflow", "service", "escalation", "github", "subprocess"}


def test_the_dashboard_package_imports_no_workflow_service_escalation_github_or_subprocess() -> (
    None
):
    found: dict[str, set[str]] = {}
    for path in sorted(Path(dashboard.__file__).parent.rglob("*.py")):
        names: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.update(alias.name.split("."))
            elif isinstance(node, ast.ImportFrom):
                names.update((node.module or "").split("."))
                if not node.module:
                    names.update(alias.name for alias in node.names)
        if names & FORBIDDEN_IMPORTS:
            found[path.name] = names & FORBIDDEN_IMPORTS
    assert found == {}
