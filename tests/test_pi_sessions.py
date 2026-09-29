"""Session store for pi: which session file a call continues or starts."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from software_agent_factory.models import AgentRole
from software_agent_factory.pi_sessions import (
    Continue,
    Fresh,
    PiSessionStore,
    SessionSettings,
    _is_session_file_name,
    _next_session_path,
    persists_session,
)

MAX_AGE = 3600
START = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
SETTINGS = SessionSettings(model="claude-sonnet-5", provider="github-copilot", reasoning="medium")
IMPLEMENTER_FILE = "implementer.jsonl"
IMPLEMENTER_SIDECAR = "implementer.meta.json"
REVIEWER_FILE = "reviewer.jsonl"
NOT_A_SESSION_FILE = "not a session file"
UTF_8 = "utf-8"


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
    assert decision.path.name == IMPLEMENTER_FILE


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


def sidecar_name(role: AgentRole) -> str:
    return f"{role.value.lower()}.meta.json"


def stamp(path: Path, when: datetime) -> None:
    """Set the modification time of ``path``, as pi's last write would."""
    os.utime(path, (when.timestamp(), when.timestamp()))


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
    with decision.path.open("a", encoding=UTF_8) as session_file:
        session_file.write('{"type":"message"}\n')
    store.record(work_item_id, role, decision.path, settings, success=success)
    ended_at = json.loads((decision.path.parent / sidecar_name(role)).read_text(encoding=UTF_8))
    stamp(decision.path, datetime.fromisoformat(ended_at["last_ended_at"]))
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
    "changed",
    [
        SessionSettings("claude-opus-5", "github-copilot", "medium"),
        SessionSettings("claude-sonnet-5", "anthropic", "medium"),
        SessionSettings("claude-sonnet-5", "github-copilot", "high"),
    ],
    ids=["model changed", "provider changed", "reasoning changed"],
)
def test_changed_settings_start_a_new_session_and_keep_the_old_file(
    store: PiSessionStore, clock: Clock, changed: SessionSettings
) -> None:
    first = run_call(store, "W1", AgentRole.IMPLEMENTER)
    clock.advance(minutes=10)

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


def point_sidecar_at(directory: Path, session_file: str) -> None:
    """Rewrite the implementer sidecar, valid in every other way, to name ``session_file``."""
    sidecar = directory / IMPLEMENTER_SIDECAR
    tampered = json.loads(sidecar.read_text(encoding=UTF_8))
    tampered["session_file"] = session_file
    sidecar.write_text(json.dumps(tampered), encoding=UTF_8)


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


def run_three_expired_reviewer_calls(store: PiSessionStore, clock: Clock) -> list[Path]:
    """Run three reviewer calls, each after the previous session expired."""
    paths = []
    for _ in range(3):
        paths.append(run_call(store, "W1", AgentRole.REVIEWER).path)
        clock.advance(seconds=MAX_AGE)
    return paths


def test_fresh_sessions_use_numbered_files_and_keep_every_earlier_live_file(
    store: PiSessionStore,
) -> None:
    efforts = ["low", "medium", "high"]
    paths = [
        run_call(store, "W1", AgentRole.REVIEWER, SessionSettings("m", "p", effort)).path
        for effort in efforts
    ]

    assert [path.name for path in paths] == [REVIEWER_FILE, "reviewer-2.jsonl", "reviewer-3.jsonl"]
    assert [path.exists() for path in paths] == [True, True, True]


def test_the_sidecar_tracks_the_latest_numbered_file(store: PiSessionStore, clock: Clock) -> None:
    paths = run_three_expired_reviewer_calls(store, clock)
    clock.advance(seconds=60 - MAX_AGE)

    assert store.resolve("W1", AgentRole.REVIEWER, SETTINGS) == Continue(paths[-1])


def test_interleaved_work_items_use_only_their_own_files(store: PiSessionStore) -> None:
    w1 = store.resolve("W1", AgentRole.IMPLEMENTER, SETTINGS)
    w2 = store.resolve("W2", AgentRole.IMPLEMENTER, SETTINGS)
    for decision in (w1, w2):
        decision.path.touch()
    store.record("W1", AgentRole.IMPLEMENTER, w1.path, SETTINGS, success=True)
    store.record("W2", AgentRole.IMPLEMENTER, w2.path, SETTINGS, success=True)

    assert w1.path != w2.path
    assert store.resolve("W1", AgentRole.IMPLEMENTER, SETTINGS) == Continue(w1.path)
    assert store.resolve("W2", AgentRole.IMPLEMENTER, SETTINGS) == Continue(w2.path)


def test_record_uses_the_given_end_time(store: PiSessionStore) -> None:
    decision = store.resolve("W1", AgentRole.IMPLEMENTER, SETTINGS)
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
    with pytest.raises(ValueError, match=NOT_A_SESSION_FILE):
        store.record(
            "W1", AgentRole.IMPLEMENTER, tmp_path / "elsewhere.jsonl", SETTINGS, success=True
        )


def test_record_rejects_a_file_that_is_not_a_session_file(store: PiSessionStore) -> None:
    directory = store.resolve("W1", AgentRole.IMPLEMENTER, SETTINGS).path.parent

    with pytest.raises(ValueError, match=NOT_A_SESSION_FILE):
        store.record(
            "W1", AgentRole.IMPLEMENTER, directory / IMPLEMENTER_SIDECAR, SETTINGS, success=True
        )


@pytest.mark.parametrize(
    "name",
    [REVIEWER_FILE, "reviewer-2.jsonl", "implementer-1.jsonl", "implementer-02.jsonl"],
)
def test_record_rejects_another_roles_or_a_malformed_session_file_name(
    store: PiSessionStore, name: str
) -> None:
    directory = store.resolve("W1", AgentRole.IMPLEMENTER, SETTINGS).path.parent

    with pytest.raises(ValueError, match=NOT_A_SESSION_FILE):
        store.record("W1", AgentRole.IMPLEMENTER, directory / name, SETTINGS, success=True)


def test_record_leaves_no_temporary_files_behind(store: PiSessionStore) -> None:
    decision = run_call(store, "W1", AgentRole.IMPLEMENTER)

    names = sorted(entry.name for entry in decision.path.parent.iterdir())

    assert names == [IMPLEMENTER_FILE, IMPLEMENTER_SIDECAR]


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
        IMPLEMENTER_FILE,
        IMPLEMENTER_SIDECAR,
    ]
    assert store.resolve("W1", AgentRole.IMPLEMENTER, SETTINGS) == Continue(first.path)


@pytest.mark.parametrize("content", ["not json", "{}"], ids=["garbage", "missing fields"])
def test_an_unusable_sidecar_starts_a_new_session(store: PiSessionStore, content: str) -> None:
    first = run_call(store, "W1", AgentRole.IMPLEMENTER)
    (first.path.parent / IMPLEMENTER_SIDECAR).write_text(content, encoding=UTF_8)

    assert isinstance(store.resolve("W1", AgentRole.IMPLEMENTER, SETTINGS), Fresh)


@pytest.mark.parametrize(
    "named",
    ["../../outside.jsonl", "../x.jsonl", "sub/implementer.jsonl"],
    ids=["two levels up", "one level up", "subdirectory"],
)
def test_a_valid_sidecar_naming_a_file_outside_the_directory_is_not_followed(
    store: PiSessionStore, named: str
) -> None:
    first = run_call(store, "W1", AgentRole.IMPLEMENTER)
    target = first.path.parent / named
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}\n", encoding=UTF_8)
    point_sidecar_at(first.path.parent, named)

    assert isinstance(store.resolve("W1", AgentRole.IMPLEMENTER, SETTINGS), Fresh)


@pytest.mark.parametrize(
    "named",
    [REVIEWER_FILE, "implementer-1.jsonl", "implementer-0.jsonl", "implementer-02.jsonl"],
)
def test_a_sidecar_naming_another_roles_or_a_malformed_file_is_not_followed(
    store: PiSessionStore, named: str
) -> None:
    first = run_call(store, "W1", AgentRole.IMPLEMENTER)
    (first.path.parent / named).write_text("{}\n", encoding=UTF_8)
    point_sidecar_at(first.path.parent, named)

    assert isinstance(store.resolve("W1", AgentRole.IMPLEMENTER, SETTINGS), Fresh)


def test_a_sidecar_naming_a_numbered_file_of_its_own_role_is_followed(
    store: PiSessionStore,
) -> None:
    first = run_call(store, "W1", AgentRole.IMPLEMENTER)
    numbered = first.path.parent / "implementer-12.jsonl"
    numbered.write_text("{}\n", encoding=UTF_8)
    point_sidecar_at(first.path.parent, numbered.name)

    assert store.resolve("W1", AgentRole.IMPLEMENTER, SETTINGS) == Continue(numbered)


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
    "v1.2",
    "a.b",
    "\ud800",
    "x" * 100 + "\ud800",
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


def directory_name_of(store: PiSessionStore, work_item_id: str) -> str:
    return store.resolve(work_item_id, AgentRole.IMPLEMENTER, SETTINGS).path.parent.name


@pytest.mark.parametrize(
    ("work_item_id", "expected"),
    [
        ("W1", "+w1"),
        ("gh/12", "gh%2F12"),
        (".x", "%2Ex"),
        ("v1.2", "v1.2"),
        ("caf\u00e9", "caf%C3%A9"),
        ("a+b", "a%2Bb"),
    ],
)
def test_directory_names_use_the_documented_encoding(
    store: PiSessionStore, work_item_id: str, expected: str
) -> None:
    assert directory_name_of(store, work_item_id) == expected


def test_a_name_of_the_longest_allowed_length_is_kept(store: PiSessionStore) -> None:
    assert directory_name_of(store, "x" * 93) == "x" * 93


def test_a_name_one_character_over_the_limit_is_cut_and_ends_in_a_digest(
    store: PiSessionStore,
) -> None:
    name = directory_name_of(store, "x" * 94)

    assert re.fullmatch(r"x{60}~[0-9a-f]{32}", name)


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


@pytest.mark.parametrize("role", [AgentRole.IMPLEMENTER, AgentRole.REVIEWER])
def test_every_session_file_name_the_store_makes_is_accepted_as_one(
    tmp_path: Path, role: AgentRole
) -> None:
    for _ in range(12):
        path = _next_session_path(tmp_path, role)
        assert _is_session_file_name(path.name, role)
        path.touch()


def test_a_session_file_name_with_an_absurdly_long_number_is_rejected() -> None:
    assert not _is_session_file_name(f"implementer-{'9' * 5000}.jsonl", AgentRole.IMPLEMENTER)


NO_GROUP_OR_OTHER_ACCESS = 0o077


@pytest.fixture
def open_umask() -> Iterator[None]:
    """Run with umask 0, so only the code under test can make a file private."""
    previous = os.umask(0)
    try:
        yield
    finally:
        os.umask(previous)


def is_private(path: Path) -> bool:
    return path.stat().st_mode & NO_GROUP_OR_OTHER_ACCESS == 0


@pytest.mark.usefixtures("open_umask")
def test_resolve_creates_the_root_and_work_item_directories_private(
    store: PiSessionStore, tmp_path: Path
) -> None:
    decision = store.resolve("W1", AgentRole.IMPLEMENTER, SETTINGS)

    assert is_private(tmp_path / "pi-sessions")
    assert is_private(decision.path.parent)


def test_resolve_makes_directories_left_open_by_an_earlier_version_private(
    store: PiSessionStore, tmp_path: Path
) -> None:
    root = tmp_path / "pi-sessions"
    directory = root / "+w1"
    directory.mkdir(parents=True)
    root.chmod(0o755)
    directory.chmod(0o755)

    store.resolve("W1", AgentRole.IMPLEMENTER, SETTINGS)

    assert is_private(root)
    assert is_private(directory)


@pytest.mark.usefixtures("open_umask")
def test_record_writes_a_private_sidecar_and_makes_the_session_file_private(
    store: PiSessionStore,
) -> None:
    decision = store.resolve("W1", AgentRole.IMPLEMENTER, SETTINGS)
    decision.path.touch(mode=0o644)

    store.record("W1", AgentRole.IMPLEMENTER, decision.path, SETTINGS, success=True)

    assert is_private(decision.path.parent / IMPLEMENTER_SIDECAR)
    assert is_private(decision.path)


def test_record_of_a_call_that_wrote_no_session_file_still_writes_the_sidecar(
    store: PiSessionStore,
) -> None:
    decision = store.resolve("W1", AgentRole.IMPLEMENTER, SETTINGS)

    store.record("W1", AgentRole.IMPLEMENTER, decision.path, SETTINGS, success=False)

    assert (decision.path.parent / IMPLEMENTER_SIDECAR).exists()


def test_continuing_makes_a_session_file_left_open_by_a_crash_private(
    store: PiSessionStore,
) -> None:
    first = run_call(store, "W1", AgentRole.IMPLEMENTER)
    first.path.chmod(0o644)

    decision = store.resolve("W1", AgentRole.IMPLEMENTER, SETTINGS)

    assert decision == Continue(first.path)
    assert is_private(first.path)


def test_a_fresh_session_removes_the_roles_expired_files(
    store: PiSessionStore, clock: Clock
) -> None:
    old = run_call(store, "W1", AgentRole.REVIEWER)
    clock.advance(seconds=MAX_AGE)

    fresh = store.resolve("W1", AgentRole.REVIEWER, SETTINGS)

    assert isinstance(fresh, Fresh)
    assert not old.path.exists()


def test_a_fresh_session_keeps_a_file_that_can_still_be_continued_later(
    store: PiSessionStore, clock: Clock
) -> None:
    recent = run_call(store, "W1", AgentRole.REVIEWER)
    clock.advance(seconds=MAX_AGE - 1)
    changed = SessionSettings("claude-opus-5", "github-copilot", "medium")

    fresh = store.resolve("W1", AgentRole.REVIEWER, changed)

    assert isinstance(fresh, Fresh)
    assert recent.path.exists()


def test_a_fresh_session_leaves_other_roles_and_other_files_alone(
    store: PiSessionStore, clock: Clock
) -> None:
    implementer = run_call(store, "W1", AgentRole.IMPLEMENTER)
    old = run_call(store, "W1", AgentRole.REVIEWER)
    notes = old.path.parent / "notes.jsonl"
    notes.write_text("{}\n", encoding=UTF_8)
    for path in (implementer.path, notes):
        stamp(path, START)
    clock.advance(seconds=MAX_AGE * 2)

    store.resolve("W1", AgentRole.REVIEWER, SETTINGS)

    assert sorted(entry.name for entry in old.path.parent.iterdir()) == [
        IMPLEMENTER_FILE,
        IMPLEMENTER_SIDECAR,
        "notes.jsonl",
        "reviewer.meta.json",
    ]


def test_a_fresh_session_leaves_other_work_items_alone(store: PiSessionStore, clock: Clock) -> None:
    other = run_call(store, "W2", AgentRole.REVIEWER)
    clock.advance(seconds=MAX_AGE * 2)

    store.resolve("W1", AgentRole.REVIEWER, SETTINGS)

    assert other.path.exists()


def test_a_continued_session_removes_nothing(store: PiSessionStore, clock: Clock) -> None:
    current = run_call(store, "W1", AgentRole.REVIEWER)
    clock.advance(seconds=MAX_AGE - 1)
    stale = current.path.parent / "reviewer-7.jsonl"
    stale.write_text("{}\n", encoding=UTF_8)
    stamp(stale, START - timedelta(seconds=MAX_AGE))

    decision = store.resolve("W1", AgentRole.REVIEWER, SETTINGS)

    assert decision == Continue(current.path)
    assert stale.exists()


def test_a_file_that_cannot_be_removed_does_not_fail_the_new_session(
    store: PiSessionStore, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_call(store, "W1", AgentRole.REVIEWER)
    clock.advance(seconds=MAX_AGE)

    def refuse(_self: Path, missing_ok: bool = False) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "unlink", refuse)

    assert isinstance(store.resolve("W1", AgentRole.REVIEWER, SETTINGS), Fresh)
