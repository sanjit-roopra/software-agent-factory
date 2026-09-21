from __future__ import annotations

import hashlib
import http.client
import json
import logging
import math
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any, Literal, Protocol, runtime_checkable

import pydantic
from pydantic import ConfigDict, Field

from .config import FactoryConfig, RoleModelConfig, RoutingConfig
from .governance import find_protected_matches
from .models import (
    AgentRole,
    Complexity,
    ExecutionRoute,
    ModelBase,
    RepositoryProfile,
    Risk,
    RouteDecision,
    RouteOption,
    WorkItem,
)
from .verification import redact_secrets

logger = logging.getLogger(__name__)

COMPLEXITY_ORDER = [Complexity.L0, Complexity.L1, Complexity.L2, Complexity.L3]
RISK_ORDER = [Risk.R0, Risk.R1, Risk.R2, Risk.R3]


class ModelRouter:
    def __init__(self, config: FactoryConfig):
        self._config = config

    def model_for_role(
        self,
        role: AgentRole,
        *,
        model_profile: str | None = None,
    ) -> RoleModelConfig:
        if role is AgentRole.IMPLEMENTER:
            raise ValueError("Implementer routing requires complexity and attempt_number")

        models = (
            self._config.models
            if model_profile is None
            else self._config.model_profiles[model_profile]
        )
        if role is AgentRole.TRIAGE:
            return models.triage
        if role is AgentRole.REFINER:
            return models.refiner
        if role is AgentRole.RESEARCHER:
            return models.researcher
        if role is AgentRole.PLANNER:
            return models.planner
        if role is AgentRole.TESTER:
            return models.tester
        if role is AgentRole.REVIEWER:
            return models.reviewer
        raise ValueError(f"No fixed model is configured for role {role}")

    def model_for_researcher(self) -> RoleModelConfig:
        return self._config.models.researcher

    def model_for_implementer(
        self,
        starting_complexity: Complexity,
        attempt_number: int,
        *,
        model_profile: str | None = None,
    ) -> RoleModelConfig | None:
        """Select the worker model for one implementation/repair attempt.

        Escalation walks the distinct configured worker models from
        ``starting_complexity`` upwards, spending ``same_model_attempts`` on
        each. Once the strongest distinct configured model is reached the
        selection plateaus there, so every complexity yields exactly
        ``max_total_attempts`` usable attempts; ``None`` means the global
        budget itself is exhausted, never that routing ran out of models.
        """
        if attempt_number < 1:
            raise ValueError("attempt_number must be 1 or greater")
        if attempt_number > self._config.retries.max_total_attempts:
            return None

        distinct_models = self._distinct_worker_models(
            starting_complexity, model_profile=model_profile
        )
        model_index = (attempt_number - 1) // self._config.retries.same_model_attempts
        return distinct_models[min(model_index, len(distinct_models) - 1)]

    def requires_human_approval(self, risk: Risk) -> bool:
        return self._config.risk[risk].human_approval

    def _distinct_worker_models(
        self,
        starting_complexity: Complexity,
        *,
        model_profile: str | None = None,
    ) -> list[RoleModelConfig]:
        models = (
            self._config.models
            if model_profile is None
            else self._config.model_profiles[model_profile]
        )
        start_index = COMPLEXITY_ORDER.index(starting_complexity)
        ordered_configs = []
        seen_configs: set[tuple[str, str, str]] = set()

        for complexity in COMPLEXITY_ORDER[start_index:]:
            config = models.workers[complexity]
            key = (config.model, config.reasoning, str(config.context_tier))
            if key in seen_configs:
                continue
            seen_configs.add(key)
            ordered_configs.append(config)

        return ordered_configs


# ---------------------------------------------------------------------------
# Route Advisor Protocol & Models
# ---------------------------------------------------------------------------


class RouteRequest(ModelBase):
    """Sanitized, minimal context sent to the route advisor.

    Explicitly excludes source code, diffs, command logs, credentials,
    remote URLs, local filesystem paths, and repository instructions.
    """

    work_item_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    repository_markers: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)
    package_managers: list[str] = Field(default_factory=list)
    has_verify_commands: bool = False
    options: list[RouteOption] = Field(min_length=1)


class RouteResponse(ModelBase):
    """Validated, typed outcome from a RouteAdvisor."""

    selected_option: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    probabilities: dict[str, float]
    model_id: str = Field(min_length=1)
    latency_ms: float = Field(default=0.0, ge=0.0)
    usage: dict[str, Any] | None = None


@runtime_checkable
class RouteAdvisor(Protocol):
    def decide_route(self, request: RouteRequest) -> RouteResponse: ...


class JevRouteError(RuntimeError):
    """Raised when Jev router execution or strict validation fails."""


class JevResponseValidationError(JevRouteError):
    """Raised when Jev router strict response validation fails."""


# ---------------------------------------------------------------------------
# Strict Jev System One Choice Schema
# ---------------------------------------------------------------------------


class JevUsage(ModelBase):
    """Typed usage counts with non-negative integer values or null."""

    model_config = ConfigDict(strict=True, extra="forbid")

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


class JevChoiceAnswer(ModelBase):
    """Typed answer for a single Choice question from System One."""

    model_config = ConfigDict(strict=True, extra="forbid")

    type: Literal["choice"] = "choice"
    choice: str = Field(min_length=1)
    probabilities: dict[str, float]
    confidence: float = Field(ge=0.0, le=1.0)


class JevAnswers(ModelBase):
    """Answers mapping requiring the exact route_selection key."""

    model_config = ConfigDict(strict=True, extra="forbid")

    route_selection: JevChoiceAnswer


class JevResponse(ModelBase):
    """Typed, strict response model from System One API."""

    model_config = ConfigDict(strict=True, extra="forbid")

    model: str = Field(min_length=1)
    answers: JevAnswers
    usage: JevUsage


# ---------------------------------------------------------------------------
# Jev Implementation
# ---------------------------------------------------------------------------


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Prevent HTTP redirects so the one-call invariant is strictly maintained."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            f"HTTP redirect ({code}) to {newurl} forbidden by Jev router policy",
            headers,
            fp,
        )


JevTransport = Callable[[urllib.request.Request, float], tuple[int, bytes]]


def default_https_transport(
    req: urllib.request.Request,
    timeout: float,
    max_response_bytes: int,
) -> tuple[int, bytes]:
    """Default transport making exactly one HTTPS POST with total deadline and bounded read."""
    parsed = urllib.parse.urlsplit(req.full_url)
    if parsed.scheme != "https":
        raise ValueError(f"HTTPS required, got {parsed.scheme}")

    host = parsed.hostname
    if not host:
        raise ValueError(f"Invalid host in URL: {req.full_url}")
    port = parsed.port or 443
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    deadline = time.monotonic() + timeout

    def remaining_timeout() -> float:
        rem = deadline - time.monotonic()
        if rem <= 0:
            raise TimeoutError("Total request deadline exceeded")
        return rem

    ssl_ctx = ssl.create_default_context()
    conn = http.client.HTTPSConnection(
        host,
        port=port,
        timeout=remaining_timeout(),
        context=ssl_ctx,
    )
    try:
        headers = dict(req.headers)
        data = req.data
        conn.timeout = remaining_timeout()
        conn.request(req.get_method(), path, body=data, headers=headers)

        if conn.sock:
            conn.sock.settimeout(remaining_timeout())
        response = conn.getresponse()
        status = response.status

        if 300 <= status < 400:
            raise JevRouteError(f"HTTP redirect ({status}) forbidden by Jev router policy")

        read_limit = max_response_bytes + 1
        chunks: list[bytes] = []
        bytes_read = 0
        chunk_size = 4096

        while bytes_read < read_limit:
            if conn.sock:
                conn.sock.settimeout(remaining_timeout())
            to_read = min(chunk_size, read_limit - bytes_read)
            chunk = response.read(to_read)
            if not chunk:
                break
            chunks.append(chunk)
            bytes_read += len(chunk)

        if bytes_read > max_response_bytes:
            raise JevRouteError(
                f"Response exceeded maximum allowed size of {max_response_bytes} bytes"
            )

        return status, b"".join(chunks)
    finally:
        conn.close()


class JevRouteAdvisor:
    """Makes exactly one HTTPS POST to Jev / System One API."""

    def __init__(
        self,
        config: RoutingConfig,
        transport: JevTransport | None = None,
    ):
        self._config = config
        self._transport = transport
        if not self._config.api_url.startswith("https://"):
            raise ValueError(f"JevRouteAdvisor requires HTTPS URL, got {self._config.api_url!r}")

    def decide_route(self, request: RouteRequest) -> RouteResponse:
        api_key = os.environ.get(self._config.api_key_env_var)
        if not api_key:
            raise JevRouteError(
                f"routing API key environment variable {self._config.api_key_env_var!r} is not set"
            )

        prompt_text = self._build_prompt_text(request)
        if len(prompt_text) > self._config.max_prompt_chars:
            prompt_text = prompt_text[: self._config.max_prompt_chars]

        payload = {
            "state": prompt_text,
            "model": self._config.model,
            "questions": {
                "route_selection": {
                    "type": "choice",
                    "instructions": (
                        "Select the most appropriate execution route option for this task."
                    ),
                    "criteria": {
                        opt.id: (
                            f"{opt.route.value} (complexity={opt.complexity or 'none'}): "
                            f"{opt.description}".strip()
                        )
                        for opt in request.options
                    },
                }
            },
        }

        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self._config.api_url,
            data=body_bytes,
            headers={
                "Content-Type": "application/json",
                "Authorization": api_key,
                "User-Agent": "software-agent-factory/1.0",
            },
            method="POST",
        )

        start_time = time.perf_counter()
        if self._transport is not None:
            try:
                status, response_bytes = self._transport(req, self._config.timeout_seconds)
                if len(response_bytes) > self._config.max_response_bytes:
                    max_bytes = self._config.max_response_bytes
                    raise JevRouteError(
                        f"Response exceeded maximum allowed size of {max_bytes} bytes"
                    )
                response_body = response_bytes.decode("utf-8")
            except Exception as exc:
                if isinstance(exc, JevRouteError):
                    raise
                raise JevRouteError(f"Transport error contacting Jev router: {exc}") from exc
        else:
            try:
                status, response_bytes = default_https_transport(
                    req, self._config.timeout_seconds, self._config.max_response_bytes
                )
                response_body = response_bytes.decode("utf-8")
            except TimeoutError as exc:
                raise JevRouteError("Timeout contacting Jev router") from exc
            except http.client.HTTPException as exc:
                raise JevRouteError(f"HTTP connection error from Jev router: {exc}") from exc
            except Exception as exc:
                if isinstance(exc, JevRouteError):
                    raise
                raise JevRouteError(f"Error contacting Jev router: {exc}") from exc

        latency_ms = (time.perf_counter() - start_time) * 1000.0

        if status != 200:
            raise JevRouteError(f"Unexpected status code {status} from Jev router")

        try:
            data = json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise JevRouteError("Malformed JSON response from Jev router") from exc

        return self._validate_response(data, request.options, latency_ms)

    def _validate_response(
        self, data: Any, offered_options: list[RouteOption], latency_ms: float
    ) -> RouteResponse:
        try:
            parsed = JevResponse.model_validate(data)
        except Exception as exc:
            raise JevResponseValidationError(f"Invalid Jev response schema: {exc}") from exc

        # 1. Exact expected model version
        if parsed.model != self._config.model:
            raise JevResponseValidationError(
                f"Model version mismatch: expected {self._config.model!r}, got {parsed.model!r}"
            )

        answer = parsed.answers.route_selection

        offered_ids = {opt.id for opt in offered_options}

        # 3. Option ID in offered set
        if answer.choice not in offered_ids:
            raise JevResponseValidationError(
                f"Selected option {answer.choice!r} is not in offered set: {offered_ids}"
            )

        # 4. All and only offered probability keys
        prob_keys = set(answer.probabilities.keys())
        if prob_keys != offered_ids:
            raise JevResponseValidationError(
                f"Probability keys {prob_keys} do not match offered options {offered_ids}"
            )

        # 5. Finite values in [0, 1]
        for opt_id, prob in answer.probabilities.items():
            if not math.isfinite(prob) or prob < 0.0 or prob > 1.0:
                raise JevResponseValidationError(
                    f"Probability for {opt_id} ({prob}) is not a finite value in [0, 1]"
                )

        # 6. Sum near 1
        prob_sum = sum(answer.probabilities.values())
        if not math.isclose(prob_sum, 1.0, rel_tol=1e-2, abs_tol=1e-2):
            raise JevResponseValidationError(f"Sum of probabilities ({prob_sum}) is not near 1.0")

        # 7. Selected option is argmax
        max_prob_opt = max(answer.probabilities.items(), key=lambda kv: kv[1])[0]
        if answer.probabilities[answer.choice] < answer.probabilities[max_prob_opt]:
            raise JevResponseValidationError(
                f"Selected option {answer.choice!r} is not argmax (argmax is {max_prob_opt!r})"
            )

        # 8. Selected probability and confidence meet configured thresholds
        selected_prob = answer.probabilities[answer.choice]
        if selected_prob < self._config.min_probability:
            raise JevResponseValidationError(
                f"Selected probability {selected_prob:.3f} below threshold "
                f"{self._config.min_probability}"
            )
        if answer.confidence < self._config.min_confidence:
            raise JevResponseValidationError(
                f"Confidence {answer.confidence:.3f} below threshold {self._config.min_confidence}"
            )

        return RouteResponse(
            selected_option=answer.choice,
            confidence=answer.confidence,
            probabilities=answer.probabilities,
            model_id=parsed.model,
            latency_ms=latency_ms,
            usage={
                "input_tokens": parsed.usage.input_tokens,
                "output_tokens": parsed.usage.output_tokens,
            },
        )

    def _build_prompt_text(self, request: RouteRequest) -> str:
        lines = [
            f"Task: {sanitize_outbound_text(request.title)}",
            f"Description: {sanitize_outbound_text(request.description)}",
        ]
        if request.acceptance_criteria:
            lines.append("Acceptance Criteria:")
            for ac in request.acceptance_criteria:
                lines.append(f"- {sanitize_outbound_text(ac)}")
        if request.constraints:
            lines.append("Constraints:")
            for c in request.constraints:
                lines.append(f"- {sanitize_outbound_text(c)}")
        if request.labels:
            lines.append(
                f"Labels: {', '.join(sanitize_outbound_text(lbl) for lbl in request.labels)}"
            )
        lines.append(
            f"Repository Facts: tech={request.technologies or ['unknown']}, "
            f"pkg_managers={request.package_managers or ['unknown']}, "
            f"has_verify_commands={request.has_verify_commands}"
        )
        lines.append(
            "Select the most appropriate execution route and worker complexity for this task."
        )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Test Doubles
# ---------------------------------------------------------------------------


class FakeRouteAdvisor:
    """Deterministic test double for RouteAdvisor with no network access."""

    def __init__(
        self,
        *,
        canned_response: RouteResponse | None = None,
        canned_option_id: str | None = None,
        chosen_option: str | None = None,
        error: Exception | None = None,
        confidence: float = 0.95,
        latency_ms: float = 12.0,
    ):
        self._canned_response = canned_response
        self._canned_option_id = canned_option_id or chosen_option
        self._error = error
        self._confidence = confidence
        self._latency_ms = latency_ms
        self.requests: list[RouteRequest] = []

    def decide_route(self, request: RouteRequest) -> RouteResponse:
        self.requests.append(request)
        if self._error is not None:
            raise self._error

        if self._canned_response is not None:
            return self._canned_response

        selected = self._canned_option_id
        if selected is None:
            selected = request.options[0].id

        probs: dict[str, float] = {}
        n = len(request.options)
        if n == 1:
            probs[selected] = 1.0
        else:
            remaining = 1.0 - self._confidence
            other_prob = remaining / (n - 1)
            for opt in request.options:
                probs[opt.id] = self._confidence if opt.id == selected else other_prob

        return RouteResponse(
            selected_option=selected,
            confidence=self._confidence,
            probabilities=probs,
            model_id="fake-jev-1.13.0",
            latency_ms=self._latency_ms,
        )


class NoOpRouteAdvisor:
    """Always raises to simulate unconfigured or unavailable routing."""

    def decide_route(self, request: RouteRequest) -> RouteResponse:
        raise JevRouteError("routing advisor is not configured or disabled")


# ---------------------------------------------------------------------------
# Deterministic Safety Floors & Route Determination
# ---------------------------------------------------------------------------

_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?:/Users/|/home/|/private/|/tmp/|/var/|/etc/|/usr/)[^\s\"':;()]+",
    re.IGNORECASE,
)
_PROHIBITED_INSTRUCTION_PATTERNS = [
    re.compile(r"\$\{var@P\}", re.IGNORECASE),
    re.compile(r"\$\{[!A-Za-z0-9_]+\}", re.IGNORECASE),
    re.compile(r"(?i)\bignore (?:all |previous |prior )?instructions\b"),
    re.compile(r"(?i)\bsystem prompt\b"),
]

_CODE_FENCE_PATTERN = re.compile(r"```[\w\s]*\n[\s\S]*?```", re.MULTILINE)
_TILDE_FENCE_PATTERN = re.compile(r"~~~[\w\s]*\n[\s\S]*?~~~", re.MULTILINE)
_DIFF_BLOCK_PATTERN = re.compile(
    r"(?m)(?:^(?:diff --git|--- [ab]/|\+\+\+ [ab]/|@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@)[^\n]*\n?)"
    r"(?:^[ +-][^\n]*\n?)*"
)
_DIFF_HEADER_OR_HUNK_PATTERN = re.compile(
    r"(?m)^(?:diff --git[^\n]+|index\s+[0-9a-fA-F]+\.\.[0-9a-fA-F]+[^\n]*|"
    r"---(?:\s+[ab]/|\s+/dev/null)[^\n]*|\+\+\+(?:\s+[ab]/|\s+/dev/null)[^\n]*|"
    r"@@\s+-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?\s+@@[^\n]*)$"
)
_PYTHON_TRACEBACK_PATTERN = re.compile(
    r"(?m)^Traceback \(most recent call last\):(?:\n(?:\s+File\s+[^\n]+|\s+[^\n]+))+"
)
_JS_JAVA_STACK_TRACE_PATTERN = re.compile(
    r"(?m)^[a-zA-Z0-9_.]*(?:Error|Exception|Throwable):[^\n]*(?:\n\s+at\s+[^\n]+)+"
)


def strip_pasted_code_and_diffs(text: str) -> str:
    """Strip fenced code blocks, unified diff blocks/lines, and stack traces."""
    if not text:
        return text
    text = _CODE_FENCE_PATTERN.sub("", text)
    text = _TILDE_FENCE_PATTERN.sub("", text)
    text = _DIFF_BLOCK_PATTERN.sub("", text)
    text = _DIFF_HEADER_OR_HUNK_PATTERN.sub("", text)
    text = _PYTHON_TRACEBACK_PATTERN.sub("", text)
    text = _JS_JAVA_STACK_TRACE_PATTERN.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def sanitize_outbound_text(text: str) -> str:
    """Sanitize outbound text sent to Jev.

    Strips code fences, diffs, stack traces, and redacts credentials,
    secrets, absolute local paths, remote URLs, and prohibited instructions.
    """
    if not text:
        return text
    # 1. Strip code fences, diffs, stack traces
    text = strip_pasted_code_and_diffs(text)
    if not text:
        return text
    # 2. Standard secret redaction
    sanitized = redact_secrets(text)
    # 3. Remote URLs
    sanitized = _URL_PATTERN.sub("[URL]", sanitized)
    # 4. Absolute local paths
    sanitized = _ABSOLUTE_PATH_PATTERN.sub("[PATH]", sanitized)
    # Generic root paths with 2+ segments
    sanitized = re.sub(
        r"(?:^|(?<=[\s\"'(:;<]))/(?:[a-zA-Z0-9_\.\-]+/)+[a-zA-Z0-9_\.\-]+",
        "[PATH]",
        sanitized,
    )
    # Windows drive paths
    sanitized = re.sub(r"[a-zA-Z]:\\[^\s\"';()]+", "[PATH]", sanitized)
    # 5. Prohibited instructions
    for pat in _PROHIBITED_INSTRUCTION_PATTERNS:
        sanitized = pat.sub("[REDACTED_INSTRUCTION]", sanitized)
    return sanitized


def derive_named_paths(work_item: WorkItem) -> list[str]:
    """Derive validated repository-relative paths explicitly named in the work item."""
    prose_sources = [
        work_item.title,
        work_item.description,
        *work_item.acceptance_criteria,
        *work_item.constraints,
    ]
    full_text = " ".join(prose_sources)

    # 1. Backtick and quote tokens: `path/to/file` or "path/to/file" or 'path/to/file'
    quoted = re.findall(r"[`'\"]([^`'\"]+)[`'\"]", full_text)

    # 2. Whitespace-separated tokens
    words = full_text.split()

    known_extensions = (
        ".py",
        ".md",
        ".txt",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ts",
        ".js",
        ".tsx",
        ".jsx",
        ".html",
        ".css",
        ".sh",
        ".rs",
        ".go",
        ".c",
        ".h",
        ".cpp",
        ".sql",
        ".lock",
    )

    seen: set[str] = set()
    candidates: list[str] = []
    for raw in [*quoted, *words]:
        token = raw.strip("`'\",:;()[]{}<>").strip()
        if not token:
            continue
        if token.startswith("/") or "://" in token or token.startswith("-"):
            continue
        if any(ch in token for ch in "*?[]{}") or any(ch.isspace() for ch in token):
            continue
        is_dotfile = (
            token.startswith(".")
            and len(token) > 1
            and not token.startswith("..")
            and not any(ch in token for ch in "/\\")
        )
        if (
            "/" in token
            or token.lower().endswith(known_extensions)
            or is_dotfile
            or token.upper() in {"README", "MAKEFILE", "DOCKERFILE", "LICENSE"}
        ):
            parts = PurePosixPath(token).parts
            if not parts or any(p in {".", ".."} for p in parts):
                continue
            normalized = str(PurePosixPath(token))
            if normalized not in seen:
                seen.add(normalized)
                candidates.append(normalized)

    return candidates


def find_legal_full_fallback(
    legal_options: list[RouteOption],
) -> RouteOption | None:
    """Select the first legal option with route=FULL, or None if none survived."""
    for opt in legal_options:
        if opt.route is ExecutionRoute.FULL:
            return opt
    return None


def get_configured_full_fallback(
    options: list[RouteOption],
) -> RouteOption:
    """Select the configured FULL fallback option from the provided options.

    Raises RuntimeError if no option with route=FULL exists.
    """
    fallback = find_legal_full_fallback(options)
    if fallback is not None:
        return fallback
    raise RuntimeError("No configured FULL route option available for fallback")


def assess_safety_floors(
    work_item: WorkItem,
    repository_profile: RepositoryProfile,
    config: FactoryConfig,
) -> set[str]:
    """Return the subset of configured option IDs that satisfy safety floors.

    Constraints:
    - Explicit WorkItem risk requiring human approval (R2, R3): disallow SINGLE and CRITIQUE.
    - Explicit WorkItem complexity (L2, L3): disallow SINGLE. Also disallow options
      whose worker complexity is strictly lower than work_item.complexity.
    - Missing acceptance criteria: disallow SINGLE.
    - Missing verification commands: disallow SINGLE.
    - Research-required labels: disallow SINGLE and CRITIQUE (FULL or MANUAL only).
    - Forbidden labels (e.g. 'full-only', 'no-single'): filter accordingly.
    - Protected/sensitive/manifest/version patterns mentioned in task prose:
      disallow SINGLE and CRITIQUE.
    """
    legal_ids: set[str] = set()

    requires_human = False
    if work_item.risk is not None:
        requires_human = config.risk[work_item.risk].human_approval

    has_acceptance_criteria = bool(work_item.acceptance_criteria)
    has_verify_commands = bool(config.repository.commands.verify)
    named_paths = derive_named_paths(work_item)
    has_narrow_scope = bool(named_paths)

    normalized_labels = {lbl.casefold().strip() for lbl in work_item.labels}
    research_required = bool(
        normalized_labels.intersection({"research-required", "research", "needs-research"})
    )
    full_only = bool(normalized_labels.intersection({"full-only", "full"}))
    no_single = bool(normalized_labels.intersection({"no-single", "critique-or-full"}))

    touches_protected = bool(
        find_protected_matches(named_paths, config.repository.protected_file_patterns)
    )

    version_files = set(repository_profile.version_files)
    manifest_names = {"pyproject.toml", "package.json", "requirements.txt", "uv.lock"}
    touches_manifest_or_version = any(
        path in version_files or PurePosixPath(path).name in manifest_names for path in named_paths
    )

    sensitive_found = touches_protected or touches_manifest_or_version

    # Check governance-sensitive terms across all prose and labels
    prose_sources = [
        work_item.title,
        work_item.description,
        *work_item.acceptance_criteria,
        *work_item.constraints,
        *work_item.labels,
    ]
    full_text = " ".join(prose_sources)
    governance_sensitive = any(
        re.search(r"\b" + re.escape(term) + r"\b", full_text, re.IGNORECASE)
        for term in config.routing.full_only_terms
    )

    for opt_cfg in config.routing.options:
        route = opt_cfg.route

        # MANUAL_TRIAGE is always a safe floor option
        if route is ExecutionRoute.MANUAL_TRIAGE:
            legal_ids.add(opt_cfg.id)
            continue

        # Check complexity lower-bound floor
        if work_item.complexity is not None and opt_cfg.complexity is not None:
            if COMPLEXITY_ORDER.index(opt_cfg.complexity) < COMPLEXITY_ORDER.index(
                work_item.complexity
            ):
                continue

        # Check risk lower-bound floor: explicit WorkItem risk is a lower floor
        if work_item.risk is not None and opt_cfg.risk is not None:
            if RISK_ORDER.index(opt_cfg.risk) < RISK_ORDER.index(work_item.risk):
                continue

        # Configured risk policy decides which routes are legal:
        # An option whose risk requires human approval cannot run via SINGLE or CRITIQUE
        option_requires_human = (
            opt_cfg.risk is not None and config.risk[opt_cfg.risk].human_approval
        )

        # FULL route is allowed unless constrained by complexity above
        if route is ExecutionRoute.FULL:
            legal_ids.add(opt_cfg.id)
            continue

        # CRITIQUE route checks
        if route is ExecutionRoute.CRITIQUE:
            if (
                requires_human
                or option_requires_human
                or research_required
                or full_only
                or sensitive_found
                or governance_sensitive
            ):
                continue
            if not has_acceptance_criteria or not has_verify_commands:
                continue
            legal_ids.add(opt_cfg.id)
            continue

        # SINGLE route checks
        if route is ExecutionRoute.SINGLE:
            if not has_narrow_scope:
                continue
            if requires_human or option_requires_human:
                continue
            if not has_acceptance_criteria:
                continue
            if not has_verify_commands:
                continue
            if (
                research_required
                or full_only
                or no_single
                or sensitive_found
                or governance_sensitive
            ):
                continue
            if work_item.complexity in {Complexity.L2, Complexity.L3}:
                continue
            legal_ids.add(opt_cfg.id)
            continue

    return legal_ids


def map_advisor_exception_to_categorical_reason(exc: Exception) -> tuple[str, str]:
    """Map any advisor exception to a safe categorical code and sanitized message.

    Never includes raw exception text or response payloads.
    """
    if isinstance(exc, (JevResponseValidationError, pydantic.ValidationError)):
        return "ADVISOR_SCHEMA_ERROR", "Route advisor response failed strict schema validation"
    if isinstance(exc, json.JSONDecodeError):
        return "ADVISOR_JSON_ERROR", "Route advisor response was not valid JSON"
    if isinstance(exc, TimeoutError):
        return "ADVISOR_TIMEOUT", "Route advisor request timed out"
    if isinstance(exc, (urllib.error.HTTPError, http.client.HTTPException)):
        return "ADVISOR_HTTP_ERROR", "Route advisor returned an HTTP error"
    if isinstance(exc, (urllib.error.URLError, ConnectionError, OSError)):
        return "ADVISOR_NETWORK_ERROR", "Route advisor network connection failed"

    exc_str = str(exc).lower()
    if "unoffered option" in exc_str:
        return "ADVISOR_UNOFFERED_OPTION", "Route advisor selected an unoffered option"
    if "timeout" in exc_str or "deadline" in exc_str:
        return "ADVISOR_TIMEOUT", "Route advisor request timed out"
    if "exceeded maximum" in exc_str or "oversized" in exc_str:
        return "ADVISOR_RESPONSE_OVERSIZED", "Route advisor response exceeded size limit"
    if "redirect" in exc_str:
        return "ADVISOR_REDIRECT_ERROR", "Route advisor encountered forbidden HTTP redirect"
    if "api key" in exc_str:
        return "ADVISOR_CONFIG_ERROR", "Route advisor API key not configured"

    return "ADVISOR_CALL_FAILED", "Route advisor execution failed"


def determine_route(
    work_item: WorkItem,
    repository_profile: RepositoryProfile,
    config: FactoryConfig,
    advisor: RouteAdvisor | None,
) -> RouteDecision:
    """Compute legal options, run RouteAdvisor if appropriate, and return RouteDecision."""
    request_hash = hashlib.sha256(
        f"{work_item.id}:{work_item.title}:{work_item.description}".encode("utf-8")
    ).hexdigest()

    all_options = [
        RouteOption(
            id=opt.id,
            route=opt.route,
            complexity=opt.complexity,
            risk=opt.risk,
            model_profile=opt.model_profile,
            description=opt.description,
        )
        for opt in config.routing.options
    ]
    options_by_id = {opt.id: opt for opt in all_options}

    legal_ids = assess_safety_floors(work_item, repository_profile, config)
    offered_options = [opt for opt in all_options if opt.id in legal_ids]
    legal_full_fallback = find_legal_full_fallback(offered_options)

    # 1. When routing is disabled or not configured
    if not config.routing.enabled:
        fallback_option = find_legal_full_fallback(
            [opt for opt in all_options if opt.id in legal_ids]
        )
        if fallback_option is None:
            return RouteDecision(
                work_item_id=work_item.id,
                offered_options=all_options,
                selected_option="manual_triage",
                initial_route=ExecutionRoute.MANUAL_TRIAGE,
                effective_route=ExecutionRoute.MANUAL_TRIAGE,
                selected_worker_complexity=None,
                selected_risk=None,
                selected_model_profile=None,
                source="disabled",
                confidence=1.0,
                probabilities=None,
                model_id=None,
                protocol_version=config.routing.rubric_version,
                latency_ms=0.0,
                request_hash=request_hash,
                fallback_reason=(
                    "routing is disabled and no legal FULL option survived safety floors"
                ),
            )
        return RouteDecision(
            work_item_id=work_item.id,
            offered_options=all_options,
            selected_option=fallback_option.id,
            initial_route=fallback_option.route,
            effective_route=fallback_option.route,
            selected_worker_complexity=None,
            selected_risk=None,
            selected_model_profile=None,
            source="disabled",
            confidence=1.0,
            probabilities=None,
            model_id=None,
            protocol_version=config.routing.rubric_version,
            latency_ms=0.0,
            request_hash=request_hash,
            fallback_reason="routing is disabled by configuration",
        )

    # 2. When routing is enabled:
    # If no legal options exist, return MANUAL_TRIAGE
    if not offered_options:
        return RouteDecision(
            work_item_id=work_item.id,
            offered_options=all_options,
            selected_option="manual_triage",
            initial_route=ExecutionRoute.MANUAL_TRIAGE,
            effective_route=ExecutionRoute.MANUAL_TRIAGE,
            selected_worker_complexity=None,
            selected_risk=None,
            selected_model_profile=None,
            source="safety_floor",
            confidence=1.0,
            protocol_version=config.routing.rubric_version,
            latency_ms=0.0,
            request_hash=request_hash,
            fallback_reason="no legal options satisfied safety floors",
        )

    # If only one legal option exists, skip Jev!
    if len(offered_options) == 1:
        chosen = offered_options[0]
        return RouteDecision(
            work_item_id=work_item.id,
            offered_options=offered_options,
            selected_option=chosen.id,
            initial_route=chosen.route,
            effective_route=chosen.route,
            selected_worker_complexity=chosen.complexity,
            selected_risk=chosen.risk,
            selected_model_profile=chosen.model_profile,
            source="single_option",
            confidence=1.0,
            probabilities={chosen.id: 1.0},
            model_id=None,
            protocol_version=config.routing.rubric_version,
            latency_ms=0.0,
            request_hash=request_hash,
        )

    # Build RouteRequest with sanitized outbound state
    clean_title = sanitize_outbound_text(work_item.title)
    clean_description = sanitize_outbound_text(work_item.description)
    clean_ac = [sanitize_outbound_text(ac) for ac in work_item.acceptance_criteria]
    clean_constraints = [sanitize_outbound_text(c) for c in work_item.constraints]
    clean_labels = [sanitize_outbound_text(label) for label in work_item.labels]

    route_request = RouteRequest(
        work_item_id=work_item.id,
        title=clean_title,
        description=clean_description,
        acceptance_criteria=clean_ac,
        constraints=clean_constraints,
        labels=clean_labels,
        repository_markers=list(repository_profile.markers),
        technologies=[t.value for t in repository_profile.technologies],
        package_managers=[pm.value for pm in repository_profile.package_managers],
        has_verify_commands=bool(config.repository.commands.verify),
        options=offered_options,
    )

    request_payload_bytes = route_request.model_dump_json().encode("utf-8")
    request_hash = hashlib.sha256(request_payload_bytes).hexdigest()

    active_advisor = advisor if advisor is not None else JevRouteAdvisor(config.routing)

    try:
        response = active_advisor.decide_route(route_request)
        offered_ids = {opt.id for opt in offered_options}
        if response.selected_option not in offered_ids:
            raise JevRouteError(
                f"Advisor selected unoffered option: {response.selected_option!r} "
                f"(offered: {sorted(offered_ids)})"
            )
        chosen = options_by_id[response.selected_option]
        return RouteDecision(
            work_item_id=work_item.id,
            offered_options=offered_options,
            selected_option=chosen.id,
            initial_route=chosen.route,
            effective_route=chosen.route,
            selected_worker_complexity=chosen.complexity,
            selected_risk=chosen.risk,
            selected_model_profile=chosen.model_profile,
            source="jev",
            confidence=response.confidence,
            probabilities=response.probabilities,
            model_id=response.model_id,
            protocol_version=config.routing.rubric_version,
            latency_ms=response.latency_ms,
            usage=response.usage,
            request_hash=request_hash,
        )
    except Exception as exc:
        cat_code, cat_msg = map_advisor_exception_to_categorical_reason(exc)
        logger.warning(
            "route advisor failed [%s], falling back to deterministic route",
            cat_code,
        )
        if legal_full_fallback is None:
            safe_reason = redact_secrets(
                f"{cat_code}: {cat_msg} (no legal FULL option survived safety floors)"
            )[:200]
            return RouteDecision(
                work_item_id=work_item.id,
                offered_options=offered_options,
                selected_option="manual_triage",
                initial_route=ExecutionRoute.MANUAL_TRIAGE,
                effective_route=ExecutionRoute.MANUAL_TRIAGE,
                selected_worker_complexity=None,
                selected_risk=None,
                selected_model_profile=None,
                source="fallback",
                confidence=None,
                probabilities=None,
                model_id=None,
                protocol_version=config.routing.rubric_version,
                latency_ms=0.0,
                request_hash=request_hash,
                fallback_reason=safe_reason,
            )
        safe_reason = redact_secrets(f"{cat_code}: {cat_msg} (fell back to legal FULL route)")[:200]
        return RouteDecision(
            work_item_id=work_item.id,
            offered_options=offered_options,
            selected_option=legal_full_fallback.id,
            initial_route=legal_full_fallback.route,
            effective_route=legal_full_fallback.route,
            selected_worker_complexity=legal_full_fallback.complexity,
            selected_risk=legal_full_fallback.risk,
            selected_model_profile=legal_full_fallback.model_profile,
            source="fallback",
            confidence=None,
            probabilities=None,
            model_id=None,
            protocol_version=config.routing.rubric_version,
            latency_ms=0.0,
            request_hash=request_hash,
            fallback_reason=safe_reason,
        )
