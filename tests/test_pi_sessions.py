"""Session store for pi: which session file a call continues or starts."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from software_agent_factory.models import AgentRole
from software_agent_factory.pi_sessions import (
    Continue,
    Fresh,
    PiSessionStore,
    SessionSettings,
    persists_session,
)

MAX_AGE = 3600
START = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
SETTINGS = SessionSettings(model="claude-sonnet-5", provider="github-copilot", reasoning="medium")


class Clock:
    """Injected clock the tests move by hand."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> PiSessionStore:
    return PiSessionStore(tmp_path / "pi-sessions", max_age_seconds=MAX_AGE, clock=clock)


def test_first_call_starts_a_fresh_session_file_for_the_work_item_and_role(
    store: PiSessionStore, tmp_path: Path
) -> None:
    decision = store.resolve("W1", AgentRole.IMPLEMENTER, SETTINGS)

    assert isinstance(decision, Fresh)
    assert decision.path.parent.parent == tmp_path / "pi-sessions"
    assert decision.path.name == "implementer.jsonl"


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (AgentRole.IMPLEMENTER, True),
        (AgentRole.REVIEWER, True),
        (AgentRole.TRIAGE, False),
        (AgentRole.REFINER, False),
        (AgentRole.RESEARCHER, False),
        (AgentRole.PLANNER, False),
        (AgentRole.TESTER, False),
    ],
)
def test_only_implementer_and_reviewer_persist_a_session(role: AgentRole, expected: bool) -> None:
    assert persists_session(role) is expected


def test_resolving_a_role_without_a_persisted_session_is_an_error(store: PiSessionStore) -> None:
    with pytest.raises(ValueError, match="TESTER"):
        store.resolve("W1", AgentRole.TESTER, SETTINGS)


def run_call(
    store: PiSessionStore,
    work_item_id: str,
    role: AgentRole,
    settings: SessionSettings = SETTINGS,
    *,
    success: bool = True,
) -> Continue | Fresh:
    """Resolve, let 'pi' write the session file, then record the outcome."""
    decision = store.resolve(work_item_id, role, settings)
    decision.path.parent.mkdir(parents=True, exist_ok=True)
    with decision.path.open("a", encoding="utf-8") as session_file:
        session_file.write('{"type":"message"}\n')
    store.record(work_item_id, role, decision.path, settings, success=success)
    return decision


def test_repair_round_within_the_limit_continues_the_same_file(
    store: PiSessionStore, clock: Clock
) -> None:
    first = run_call(store, "W1", AgentRole.IMPLEMENTER)
    clock.advance(minutes=10)

    second = store.resolve("W1", AgentRole.IMPLEMENTER, SETTINGS)

    assert second == Continue(first.path)


def test_reviewer_continues_its_own_file_not_the_implementers(
    store: PiSessionStore, clock: Clock
) -> None:
    implementer = run_call(store, "W1", AgentRole.IMPLEMENTER)
    reviewer = run_call(store, "W1", AgentRole.REVIEWER)
    clock.advance(minutes=5)

    decision = store.resolve("W1", AgentRole.REVIEWER, SETTINGS)

    assert decision == Continue(reviewer.path)
    assert reviewer.path != implementer.path


@pytest.mark.parametrize(
    ("changed", "minutes"),
    [
        (SessionSettings("claude-opus-5", "github-copilot", "medium"), 10),
        (SessionSettings("claude-sonnet-5", "anthropic", "medium"), 10),
        (SessionSettings("claude-sonnet-5", "github-copilot", "high"), 10),
    ],
    ids=["model changed", "provider changed", "reasoning changed"],
)
def test_changed_settings_start_a_new_session_and_keep_the_old_file(
    store: PiSessionStore, clock: Clock, changed: SessionSettings, minutes: int
) -> None:
    first = run_call(store, "W1", AgentRole.IMPLEMENTER)
    clock.advance(minutes=minutes)

    decision = store.resolve("W1", AgentRole.IMPLEMENTER, changed)

    assert isinstance(decision, Fresh)
    assert decision.path != first.path
    assert first.path.exists()


@pytest.mark.parametrize(
    ("elapsed", "reused"),
    [
        (timedelta(seconds=MAX_AGE - 1), True),
        (timedelta(seconds=MAX_AGE), False),
        (timedelta(minutes=61), False),
    ],
    ids=["just under 60 minutes", "exactly 60 minutes", "61 minutes"],
)
def test_session_age_limit_is_exclusive(
    store: PiSessionStore, clock: Clock, elapsed: timedelta, reused: bool
) -> None:
    first = run_call(store, "W1", AgentRole.IMPLEMENTER)
    clock.advance(seconds=elapsed.total_seconds())

    decision = store.resolve("W1", AgentRole.IMPLEMENTER, SETTINGS)

    assert isinstance(decision, Continue) is reused
    assert (decision.path == first.path) is reused


def test_a_session_last_used_in_the_future_is_not_continued(
    store: PiSessionStore, clock: Clock
) -> None:
    run_call(store, "W1", AgentRole.IMPLEMENTER)
    clock.advance(minutes=-1)

    assert isinstance(store.resolve("W1", AgentRole.IMPLEMENTER, SETTINGS), Fresh)


def test_a_missing_session_file_starts_a_new_session(store: PiSessionStore) -> None:
    first = run_call(store, "W1", AgentRole.IMPLEMENTER)
    first.path.unlink()

    assert isinstance(store.resolve("W1", AgentRole.IMPLEMENTER, SETTINGS), Fresh)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_an_unreadable_session_file_starts_a_new_session_and_is_kept(
    store: PiSessionStore,
) -> None:
    first = run_call(store, "W1", AgentRole.IMPLEMENTER)
    first.path.chmod(0o000)

    decision = store.resolve("W1", AgentRole.IMPLEMENTER, SETTINGS)

    assert isinstance(decision, Fresh)
    assert decision.path != first.path
    assert first.path.exists()


def test_a_failed_call_is_not_continued(store: PiSessionStore) -> None:
    first = run_call(store, "W1", AgentRole.IMPLEMENTER, success=False)

    decision = store.resolve("W1", AgentRole.IMPLEMENTER, SETTINGS)

    assert isinstance(decision, Fresh)
    assert decision.path != first.path


def test_a_success_after_a_failure_makes_the_new_session_reusable(store: PiSessionStore) -> None:
    run_call(store, "W1", AgentRole.IMPLEMENTER, success=False)
    second = run_call(store, "W1", AgentRole.IMPLEMENTER)

    assert store.resolve("W1", AgentRole.IMPLEMENTER, SETTINGS) == Continue(second.path)


def test_fresh_sessions_use_numbered_files_and_the_sidecar_tracks_the_latest(
    store: PiSessionStore, clock: Clock
) -> None:
    paths = []
    for _ in range(3):
        paths.append(run_call(store, "W1", AgentRole.REVIEWER).path)
        clock.advance(seconds=MAX_AGE)

    assert [path.name for path in paths] == [
        "reviewer.jsonl",
        "reviewer-2.jsonl",
        "reviewer-3.jsonl",
    ]
    assert all(path.exists() for path in paths)
    clock.advance(seconds=60 - MAX_AGE)
    assert store.resolve("W1", AgentRole.REVIEWER, SETTINGS) == Continue(paths[-1])


def test_interleaved_work_items_use_only_their_own_files(store: PiSessionStore) -> None:
    w1 = store.resolve("W1", AgentRole.IMPLEMENTER, SETTINGS)
    w2 = store.resolve("W2", AgentRole.IMPLEMENTER, SETTINGS)
    for decision in (w1, w2):
        decision.path.parent.mkdir(parents=True)
        decision.path.touch()
    store.record("W1", AgentRole.IMPLEMENTER, w1.path, SETTINGS, success=True)
    store.record("W2", AgentRole.IMPLEMENTER, w2.path, SETTINGS, success=True)

    assert w1.path != w2.path
    assert store.resolve("W1", AgentRole.IMPLEMENTER, SETTINGS) == Continue(w1.path)
    assert store.resolve("W2", AgentRole.IMPLEMENTER, SETTINGS) == Continue(w2.path)


def test_record_uses_the_given_end_time(store: PiSessionStore, clock: Clock) -> None:
    decision = store.resolve("W1", AgentRole.IMPLEMENTER, SETTINGS)
    decision.path.parent.mkdir(parents=True)
    decision.path.touch()
    store.record(
        "W1",
        AgentRole.IMPLEMENTER,
        decision.path,
        SETTINGS,
        success=True,
        ended_at=START - timedelta(seconds=MAX_AGE),
    )

    assert isinstance(store.resolve("W1", AgentRole.IMPLEMENTER, SETTINGS), Fresh)


def test_record_rejects_a_path_outside_the_work_item_and_role_directory(
    store: PiSessionStore, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="not a session file"):
        store.record(
            "W1", AgentRole.IMPLEMENTER, tmp_path / "elsewhere.jsonl", SETTINGS, success=True
        )


def test_record_leaves_no_temporary_files_behind(store: PiSessionStore) -> None:
    decision = run_call(store, "W1", AgentRole.IMPLEMENTER)

    names = sorted(entry.name for entry in decision.path.parent.iterdir())

    assert names == ["implementer.jsonl", "implementer.meta.json"]


def test_a_failed_atomic_write_keeps_the_previous_sidecar_and_cleans_up(
    store: PiSessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = run_call(store, "W1", AgentRole.IMPLEMENTER)

    def refuse(_source: object, _destination: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError, match="disk full"):
        store.record("W1", AgentRole.IMPLEMENTER, first.path, SETTINGS, success=False)
    monkeypatch.undo()

    assert sorted(entry.name for entry in first.path.parent.iterdir()) == [
        "implementer.jsonl",
        "implementer.meta.json",
    ]
    assert store.resolve("W1", AgentRole.IMPLEMENTER, SETTINGS) == Continue(first.path)


@pytest.mark.parametrize(
    "content",
    ["not json", "{}", '{"session_file": "../x.jsonl"}'],
    ids=["garbage", "missing fields", "escaping session file"],
)
def test_an_unusable_sidecar_starts_a_new_session(store: PiSessionStore, content: str) -> None:
    first = run_call(store, "W1", AgentRole.IMPLEMENTER)
    (first.path.parent / "implementer.meta.json").write_text(content, encoding="utf-8")

    assert isinstance(store.resolve("W1", AgentRole.IMPLEMENTER, SETTINGS), Fresh)


def test_a_sidecar_naming_a_file_outside_the_directory_is_not_followed(
    store: PiSessionStore, tmp_path: Path
) -> None:
    first = run_call(store, "W1", AgentRole.IMPLEMENTER)
    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}\n", encoding="utf-8")
    sidecar = first.path.parent / "implementer.meta.json"
    tampered = json.loads(sidecar.read_text(encoding="utf-8"))
    tampered["session_file"] = "../../outside.jsonl"
    sidecar.write_text(json.dumps(tampered), encoding="utf-8")

    decision = store.resolve("W1", AgentRole.IMPLEMENTER, SETTINGS)

    assert isinstance(decision, Fresh)


ODD_IDS = [
    "../../etc/passwd",
    "..",
    ".",
    "a/b",
    "a\\b",
    "/absolute",
    "C:\\windows",
    ".hidden",
    "nul\x00byte",
    "line\nbreak",
    "with space",
    "caf\u00e9",
    "\u202egnp.exe",
    "x" * 1000,
]


@pytest.mark.parametrize("work_item_id", ODD_IDS, ids=lambda item: repr(item)[:24])
def test_odd_work_item_ids_stay_inside_the_root(
    store: PiSessionStore, tmp_path: Path, work_item_id: str
) -> None:
    root = tmp_path / "pi-sessions"

    decision = run_call(store, work_item_id, AgentRole.IMPLEMENTER)

    assert decision.path.parent.parent == root
    assert decision.path.resolve().is_relative_to(root.resolve())
    assert len(decision.path.parent.name) <= 100
    assert not decision.path.parent.name.startswith(".")
    assert decision.path.exists()


@pytest.mark.parametrize("work_item_id", ODD_IDS, ids=lambda item: repr(item)[:24])
def test_odd_work_item_ids_are_continued_like_any_other(
    store: PiSessionStore, work_item_id: str
) -> None:
    first = run_call(store, work_item_id, AgentRole.IMPLEMENTER)

    assert store.resolve(work_item_id, AgentRole.IMPLEMENTER, SETTINGS) == Continue(first.path)


def test_distinct_work_item_ids_never_share_a_directory(store: PiSessionStore) -> None:
    ids = [
        "W1",
        "w1",
        "a/b",
        "a%2Fb",
        "a_b",
        "a+b",
        "..",
        "%2E%2E",
        "+w1",
        "x" * 200,
        "x" * 201,
        "x" * 199 + "y",
        *ODD_IDS,
    ]

    names = [store.resolve(item, AgentRole.IMPLEMENTER, SETTINGS).path.parent.name for item in ids]

    assert len({name.lower() for name in names}) == len(set(ids))


def test_the_same_work_item_id_always_maps_to_the_same_directory(tmp_path: Path) -> None:
    first = PiSessionStore(tmp_path, max_age_seconds=MAX_AGE)
    second = PiSessionStore(tmp_path, max_age_seconds=MAX_AGE)

    assert (
        first.resolve("gh/12", AgentRole.REVIEWER, SETTINGS).path
        == second.resolve("gh/12", AgentRole.REVIEWER, SETTINGS).path
    )


def test_an_empty_work_item_id_is_rejected(store: PiSessionStore) -> None:
    with pytest.raises(ValueError, match="work item id"):
        store.resolve("", AgentRole.IMPLEMENTER, SETTINGS)
