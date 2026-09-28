from __future__ import annotations

import signal
import subprocess

import pytest

from software_agent_factory.subprocess_utils import (
    GITHUB_CREDENTIAL_ENV_VARS,
    TOKEN_PATTERNS,
    build_child_env,
    kill_process_group,
    parse_version,
    sanitize_output,
)


class _FakeProcess:
    def __init__(
        self,
        *,
        pid: int = 43210,
        timeout_calls: int = 0,
    ) -> None:
        self.pid = pid
        self._timeout_calls = timeout_calls
        self.communicate_calls = 0

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        self.communicate_calls += 1
        if timeout is not None and self._timeout_calls > 0:
            self._timeout_calls -= 1
            raise subprocess.TimeoutExpired(cmd=["pi"], timeout=timeout)
        return "out", "err"


def test_kill_process_group_sends_sigterm_and_returns_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        "software_agent_factory.subprocess_utils.os.killpg",
        lambda pid, sig: killed.append((pid, sig)),
    )

    stdout, stderr = kill_process_group(_FakeProcess())

    assert stdout == "out"
    assert stderr == "err"
    assert killed == [(43210, signal.SIGTERM)]


def test_kill_process_group_escalates_to_sigkill_when_uncooperative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        "software_agent_factory.subprocess_utils.os.killpg",
        lambda pid, sig: killed.append((pid, sig)),
    )

    stdout, stderr = kill_process_group(_FakeProcess(timeout_calls=1), grace_seconds=0.01)

    assert stdout == "out"
    assert stderr == "err"
    assert killed == [(43210, signal.SIGTERM), (43210, signal.SIGKILL)]


def test_kill_process_group_ignores_process_already_gone_during_sigkill_escalation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[signal.Signals] = []

    def killpg(pid: int, sig: signal.Signals) -> None:
        calls.append(sig)
        if sig is signal.SIGKILL:
            raise ProcessLookupError("No such process")

    monkeypatch.setattr("software_agent_factory.subprocess_utils.os.killpg", killpg)

    stdout, stderr = kill_process_group(_FakeProcess(timeout_calls=1), grace_seconds=0.01)

    assert stdout == "out"
    assert stderr == "err"
    assert calls == [signal.SIGTERM, signal.SIGKILL]


def test_kill_process_group_handles_process_already_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_killpg(pid: int, sig: signal.Signals) -> None:
        raise ProcessLookupError("No such process")

    monkeypatch.setattr("software_agent_factory.subprocess_utils.os.killpg", fail_killpg)

    stdout, stderr = kill_process_group(_FakeProcess())

    assert stdout == "out"
    assert stderr == "err"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("v22.19.0", (22, 19, 0)),
        ("0.84.4", (0, 84, 4)),
        ("V1.2", (1, 2)),
        ("22.19.0 (arm64)", (22, 19, 0)),
        ("0.84.4\n", (0, 84, 4)),
    ],
)
def test_parse_version_parses_tolerant_of_prefix_and_trailing_text(
    text: str, expected: tuple[int, ...]
) -> None:
    assert parse_version(text) == expected


@pytest.mark.parametrize("text", ["", "garbage", "not a version", "vX.Y.Z"])
def test_parse_version_returns_none_for_malformed_input(text: str) -> None:
    assert parse_version(text) is None


def test_parse_version_tuples_compare_directly() -> None:
    assert parse_version("0.84.4") >= parse_version("0.84.0")
    assert parse_version("0.84.0") < parse_version("0.84.4")
    assert parse_version("22.19.0") >= parse_version("22.19.0")


def test_build_child_env_scrubs_github_credential_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "gho_secret_value")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_other_secret")

    env, scrubbed_values = build_child_env()

    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
    assert scrubbed_values == {"gho_secret_value", "ghp_other_secret"}


def test_build_child_env_preserves_unrelated_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_UNRELATED_VAR", "keep-me")

    env, _ = build_child_env()

    assert env["SOME_UNRELATED_VAR"] == "keep-me"


def test_build_child_env_omits_unset_credential_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in GITHUB_CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    _, scrubbed_values = build_child_env()

    assert scrubbed_values == set()


def test_sanitize_output_redacts_scrubbed_values() -> None:
    result = sanitize_output("leaked: abc123secret in the log", {"abc123secret"})

    assert "abc123secret" not in result
    assert "[REDACTED]" in result


def test_sanitize_output_ignores_scrubbed_values_shorter_than_four_chars() -> None:
    result = sanitize_output("the pin is abc", {"abc"})

    assert result == "the pin is abc"


@pytest.mark.parametrize(
    "token",
    [
        "ghp_1234567890abcdef",
        "gho_1234567890abcdef",
        "github_pat_12345678901234567890",
    ],
)
def test_sanitize_output_redacts_token_shaped_substrings_not_in_scrubbed_values(
    token: str,
) -> None:
    result = sanitize_output(f"token={token}", set())

    assert token not in result
    assert "[REDACTED]" in result


def test_sanitize_output_collapses_whitespace_and_truncates_long_text() -> None:
    long_text = "line\n" * 200

    result = sanitize_output(long_text, set())

    assert len(result) <= 600
    assert result.endswith("...")
    assert "\n" not in result


def test_token_patterns_match_known_github_token_shapes() -> None:
    assert TOKEN_PATTERNS[0].search("ghp_1234567890abcdef") is not None
    assert TOKEN_PATTERNS[1].search("github_pat_12345678901234567890") is not None
