"""Real GitHub Copilot subprocess runtime.

This module provides the production ``AgentRuntime`` implementation needed for
the real Copilot-backed phases. It deliberately keeps workflow authority in the
controller: the runtime only builds role-scoped prompts, runs the Copilot CLI
with constrained permissions, and validates one typed artifact from the final
response.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
from dataclasses import dataclass
from json import JSONDecodeError
from pathlib import Path
from typing import Literal, TypeAlias

from pydantic import ValidationError

from .agents import AgentRequest, AgentResult, AgentRuntime
from .models import AgentRole, ModelBase
from .prompts import (
    RoleName,
    artifact_model_for_role,
    build_prompt,
    normalize_role,
)

ResultField: TypeAlias = Literal[
    "triage_result",
    "specification",
    "research_report",
    "execution_plan",
    "change_set",
    "test_report",
    "review_report",
]

READ_ONLY_TOOLS = ("glob", "grep", "view")
IMPLEMENTER_TOOLS = ("glob", "grep", "view", "create", "edit", "bash")
GITHUB_CREDENTIAL_ENV_VARS = frozenset(
    {
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        "ACTIONS_RUNTIME_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GH_TOKEN",
        "GIT_ASKPASS",
        "GITHUB_ENTERPRISE_TOKEN",
        "GITHUB_PAT",
        "GITHUB_TOKEN",
    }
)
TOKEN_PATTERNS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{8,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
)


@dataclass(frozen=True)
class _PermissionProfile:
    available_tools: tuple[str, ...]
    denied_permissions: tuple[str, ...]


@dataclass(frozen=True)
class _ArtifactSpec:
    model_class: type[ModelBase]
    result_field: ResultField


ARTIFACT_SPECS: dict[str, _ArtifactSpec] = {
    "TRIAGE": _ArtifactSpec(
        model_class=artifact_model_for_role("TRIAGE"),
        result_field="triage_result",
    ),
    "REFINER": _ArtifactSpec(
        model_class=artifact_model_for_role("REFINER"),
        result_field="specification",
    ),
    "RESEARCHER": _ArtifactSpec(
        model_class=artifact_model_for_role("RESEARCHER"),
        result_field="research_report",
    ),
    "PLANNER": _ArtifactSpec(
        model_class=artifact_model_for_role("PLANNER"),
        result_field="execution_plan",
    ),
    "IMPLEMENTER": _ArtifactSpec(
        model_class=artifact_model_for_role("IMPLEMENTER"),
        result_field="change_set",
    ),
    # The independent tester produces a TestReport (AI judgement), never a
    # VerificationReport -- deterministic evidence is factory-produced.
    "TESTER": _ArtifactSpec(
        model_class=artifact_model_for_role("TESTER"),
        result_field="test_report",
    ),
    "REVIEWER": _ArtifactSpec(
        model_class=artifact_model_for_role("REVIEWER"),
        result_field="review_report",
    ),
}


class CopilotAgentRuntime(AgentRuntime):
    """Invoke the installed ``copilot`` CLI and parse one typed artifact."""

    def __init__(self, *, executable: str = "copilot", max_error_chars: int = 4000) -> None:
        self._executable = executable
        self._max_error_chars = max_error_chars

    def run(self, request: AgentRequest) -> AgentResult:
        if request.role is AgentRole.IMPLEMENTER and not request.workspace_path:
            raise ValueError("IMPLEMENTER requests require workspace_path")
        if request.timeout_seconds < 1:
            raise ValueError("timeout_seconds must be at least 1")

        cwd = self._cwd_for(request)
        prompt = build_prompt(request)
        command = self._build_command(request, prompt=prompt, cwd=cwd)
        child_env, scrubbed_values = _build_child_env()

        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=request.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            stdout = stdout or _decode_timeout_text(exc.stdout)
            stderr = stderr or _decode_timeout_text(exc.stderr)
            reason = _format_failure_reason(
                role=request.role,
                message=f"copilot timed out after {request.timeout_seconds}s",
                stdout=stdout,
                stderr=stderr,
                scrubbed_values=scrubbed_values,
                limit=self._max_error_chars,
            )
            return AgentResult(role=request.role, success=False, failure_reason=reason)

        if process.returncode != 0:
            reason = _format_failure_reason(
                role=request.role,
                message=f"copilot exited with code {process.returncode}",
                stdout=stdout,
                stderr=stderr,
                scrubbed_values=scrubbed_values,
                limit=self._max_error_chars,
            )
            return AgentResult(role=request.role, success=False, failure_reason=reason)

        try:
            artifact = parse_copilot_artifact(request.role, stdout=stdout)
        except ValueError as exc:
            reason = _format_failure_reason(
                role=request.role,
                message=str(exc),
                stdout=stdout,
                stderr=stderr,
                scrubbed_values=scrubbed_values,
                limit=self._max_error_chars,
            )
            return AgentResult(role=request.role, success=False, failure_reason=reason)

        result_field = _artifact_spec(request.role).result_field
        return AgentResult(role=request.role, success=True, **{result_field: artifact})

    def _cwd_for(self, request: AgentRequest) -> Path:
        base = request.workspace_path if request.workspace_path is not None else os.getcwd()
        return Path(base).expanduser().resolve()

    def _build_command(self, request: AgentRequest, *, prompt: str, cwd: Path) -> list[str]:
        profile = _permission_profile(request.role)
        command = [
            self._executable,
            "-C",
            str(cwd),
            "--model",
            request.model,
            "--reasoning-effort",
            request.reasoning,
            "--output-format",
            "json",
            "--stream",
            "off",
            "--no-remote",
            "--no-remote-export",
            "--no-auto-update",
            "--no-ask-user",
            "--disable-builtin-mcps",
            "--disallow-temp-dir",
            "--allow-all-tools",
            "--available-tools",
            ",".join(profile.available_tools),
        ]
        for denied_permission in profile.denied_permissions:
            command.extend(["--deny-tool", denied_permission])
        command.extend(["-p", prompt])
        return command


def parse_copilot_artifact(role: RoleName, *, stdout: str) -> ModelBase:
    """Extract and validate a single typed artifact from Copilot output."""

    spec = _artifact_spec(role)
    candidates = _assistant_response_candidates(stdout)
    if not candidates:
        assistant_text = extract_assistant_text(stdout)
        candidates = _candidate_texts(assistant_text, stdout)

    found_object = False
    last_validation_error: ValidationError | None = None
    for candidate in candidates:
        for payload in _iter_json_objects(candidate):
            found_object = True
            try:
                return spec.model_class.model_validate(payload)
            except ValidationError as exc:
                last_validation_error = exc

    role_name = normalize_role(role)
    if last_validation_error is not None:
        raise ValueError(
            f"{role_name} response did not validate as {spec.model_class.__name__}: "
            f"{_summarize_validation_error(last_validation_error)}"
        )
    if not found_object:
        raise ValueError(
            f"{role_name} response did not contain a parseable JSON object for "
            f"{spec.model_class.__name__}"
        )
    raise ValueError(f"{role_name} response did not contain a valid {spec.model_class.__name__}")


def extract_assistant_text(stdout: str) -> str:
    """Collect assistant text fragments from Copilot JSONL output.

    If no recognized JSONL fragments are found, the plain stdout text is
    returned unchanged so direct-JSON and plain-text fallbacks still work.
    """

    fragments: list[str] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except JSONDecodeError:
            continue
        event_type = event.get("type")
        if (
            event_type
            in {
                "assistant.message",
                "assistant.message.delta",
                "model.response",
                "model.response.delta",
                "response.completed",
            }
            or event_type is None
        ):
            fragments.extend(_extract_text_fragments(event))

    cleaned = [fragment.strip() for fragment in fragments if fragment.strip()]
    if cleaned:
        return "\n".join(_dedupe_fragments(cleaned))
    return stdout.strip()


def _artifact_spec(role: RoleName) -> _ArtifactSpec:
    normalized_role = normalize_role(role)
    try:
        return ARTIFACT_SPECS[normalized_role]
    except KeyError as exc:  # pragma: no cover - defensive programmer error
        raise ValueError(f"unsupported agent role: {role!r}") from exc


def _permission_profile(role: AgentRole) -> _PermissionProfile:
    if role is AgentRole.IMPLEMENTER:
        return _PermissionProfile(
            available_tools=IMPLEMENTER_TOOLS,
            denied_permissions=("url", "shell(git push)", "shell(gh:*)"),
        )
    return _PermissionProfile(
        available_tools=READ_ONLY_TOOLS,
        denied_permissions=("url",),
    )


def _build_child_env() -> tuple[dict[str, str], set[str]]:
    env = dict(os.environ)
    scrubbed_values: set[str] = set()
    for name in GITHUB_CREDENTIAL_ENV_VARS:
        value = env.pop(name, None)
        if value:
            scrubbed_values.add(value)
    return env, scrubbed_values


def _decode_timeout_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _candidate_texts(assistant_text: str, stdout: str) -> list[str]:
    candidates: list[str] = []
    for text in (assistant_text.strip(), stdout.strip()):
        if text and text not in candidates:
            candidates.append(text)
    return candidates


def _assistant_response_candidates(stdout: str) -> list[str]:
    direct_candidates: list[str] = []
    fallback_candidates: list[str] = []

    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except JSONDecodeError:
            continue
        _collect_event_candidates(event, direct_candidates, fallback_candidates)

    ordered: list[str] = []
    for candidate in reversed(direct_candidates):
        text = candidate.strip()
        if text and text not in ordered:
            ordered.append(text)
    for candidate in reversed(fallback_candidates):
        text = candidate.strip()
        if text and text not in ordered:
            ordered.append(text)
    return ordered


def _iter_json_objects(text: str) -> list[dict[str, object]]:
    decoder = json.JSONDecoder()
    objects: list[dict[str, object]] = []
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(text[index:])
        except JSONDecodeError:
            continue
        if isinstance(payload, dict):
            objects.append(payload)
    return objects


def _collect_event_candidates(
    event: dict[str, object],
    direct_candidates: list[str],
    fallback_candidates: list[str],
) -> None:
    event_type = event.get("type")
    if event_type == "assistant.message":
        data = event.get("data")
        if isinstance(data, dict):
            content = data.get("content")
            if isinstance(content, str):
                direct_candidates.append(content)
                return
    if event_type == "model.response":
        data = event.get("data")
        if isinstance(data, dict):
            response = data.get("response")
            if isinstance(response, dict):
                content = response.get("content")
                if isinstance(content, str):
                    direct_candidates.append(content)
                    return
    if event_type == "response.completed":
        response = event.get("response")
        if isinstance(response, dict):
            content = response.get("content")
            if isinstance(content, str):
                direct_candidates.append(content)
                return
    if event_type in {"assistant.message.delta", "model.response.delta"}:
        fallback_candidates.extend(_extract_text_fragments(event))


def _extract_text_fragments(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        item_fragments: list[str] = []
        for item in value:
            item_fragments.extend(_extract_text_fragments(item))
        return item_fragments
    if not isinstance(value, dict):
        return []

    fragments: list[str] = []
    text = value.get("text")
    if isinstance(text, str):
        fragments.append(text)

    delta = value.get("delta")
    if isinstance(delta, str):
        fragments.append(delta)
    else:
        fragments.extend(_extract_text_fragments(delta))

    for key in (
        "content",
        "data",
        "message",
        "assistant",
        "response",
        "output",
        "item",
        "payload",
        "result",
    ):
        if key in value:
            fragments.extend(_extract_text_fragments(value[key]))
    return fragments


def _dedupe_fragments(fragments: list[str]) -> list[str]:
    deduped: list[str] = []
    previous = None
    for fragment in fragments:
        if fragment == previous:
            continue
        deduped.append(fragment)
        previous = fragment
    return deduped


def _summarize_validation_error(error: ValidationError) -> str:
    details = error.errors(include_url=False)
    if not details:
        return str(error)
    first = details[0]
    location = ".".join(str(part) for part in first.get("loc", ()))
    message = str(first.get("msg", "validation error"))
    return f"{location}: {message}" if location else message


def _format_failure_reason(
    *,
    role: AgentRole,
    message: str,
    stdout: str,
    stderr: str,
    scrubbed_values: set[str],
    limit: int,
) -> str:
    sections: list[str] = [f"{role.value}: {message}."]
    cleaned_stdout = _sanitize_output(stdout, scrubbed_values)
    cleaned_stderr = _sanitize_output(stderr, scrubbed_values)
    if cleaned_stdout:
        sections.append(f"stdout={cleaned_stdout}")
    if cleaned_stderr:
        sections.append(f"stderr={cleaned_stderr}")
    combined = " ".join(sections)
    if len(combined) <= limit:
        return combined
    return f"{combined[: limit - 12].rstrip()}...[truncated]"


def _sanitize_output(text: str, scrubbed_values: set[str]) -> str:
    sanitized = text
    for value in sorted(scrubbed_values, key=len, reverse=True):
        if len(value) >= 4:
            sanitized = sanitized.replace(value, "[REDACTED]")
    for pattern in TOKEN_PATTERNS:
        sanitized = pattern.sub("[REDACTED]", sanitized)
    sanitized = " ".join(sanitized.split())
    if len(sanitized) <= 600:
        return sanitized
    return f"{sanitized[:597].rstrip()}..."
