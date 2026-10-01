"""Pure resume rules shared by the GitHub reply poller and the dashboard request path.

Both paths end in the same place: a receipt, a reopen count, and ``REOPENED``. The rules
that decide whether a run may resume, and what a valid answer looks like, live here once, so
the two paths cannot drift apart.

A leaf. It imports only :mod:`.models`, :mod:`.config`, :mod:`.escalation_protocol` and the
standard library: no GitHub client, no subprocess, no workflow and no service. A test checks
that.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Sequence

from .escalation_protocol import MAX_PLAN_DECISIONS
from .models import (
    PlanDecisionAnswer,
    PlanDecisionContext,
    Risk,
    RiskApprovalContext,
    WorkflowState,
)

MAX_PLAN_DECISION_ANSWER_CHARS = 500

# Absolute, network, and system file system paths
_ABSOLUTE_OR_NETWORK_PATH_PATTERN = re.compile(
    r"(?i)"
    r"(?:(?<![A-Za-z0-9.~/@\\<])(?<!&lt;)/(?:[A-Za-z0-9_.-]+)[^\s\"'`>)]*)"
    r"|(?:(?<![A-Za-z0-9_.-])~[\\/][^\s\"'`>)]+)"
    r"|(?:(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'`>)]*)"
    r"|(?:(?<![A-Za-z0-9_.-])\\\\[A-Za-z0-9_.-]+[\\/][A-Za-z0-9_.-]+[^\s\"'`>)]*)"
    r"|(?:(?<![A-Za-z0-9_.:])//[A-Za-z0-9_.-]+[\\/][A-Za-z0-9_.-]+[^\s\"'`>)]*)"
)

# Credentials embedded in URLs
_URL_CREDENTIAL_PATTERN = re.compile(
    r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s:@]+@[^\s/]+"
    r"|\b[a-z][a-z0-9+.-]*://[^/\s@]+@[^\s/]+"
)

# Tokens, API keys, credentials, and private keys
_TOKEN_AND_KEY_PATTERN = re.compile(
    r"(?i)\bxox[baprse]-[0-9A-Za-z-]{10,}\b"
    r"|\b(?:gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{22,}|glpat-[A-Za-z0-9_-]{20,})\b"
    r"|\b(?:AKIA|ABIA|ACCA|ASIA)[0-9A-Z]{16}\b"
    r"|\bsk-(?:proj-|ant-)?[0-9a-zA-Z_-]{20,}\b"
    r"|\b(?:authorization|proxy[_-]?authorization)\s*[:=]\s*[^\r\n]+"
    r"|\b(?:cookie|set[_-]?cookie|set[_-]?cookie2)\s*[:=]\s*[^\r\n]+"
    r"|\bBearer\s+[A-Za-z0-9_.\-/+=]{20,}"
    r"|\bBasic\s+[A-Za-z0-9+/]{8,}={1,2}(?!\S)"
    r"|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
    r"|\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|session[_-]?id|session[_-]?token|session[_-]?key)\s*[:=]\s*['\"]?[A-Za-z0-9_.-]{8,}"
    r"|-----BEGIN (?:[A-Z0-9_-]+ )?PRIVATE KEY-----"
)

# External URLs (http, https, ftp) and bare www. domains
_EXTERNAL_URL_PATTERN = re.compile(
    r"(?i)\b(?:https?|ftp)://[^\s\"'`<>)]+"
    r"|\bwww\.[A-Za-z0-9_.-]+\.[A-Za-z]{2,}[^\s\"'`<>)]*"
)

# Raw diagnostics / stack traces / diff output
_RAW_DIAGNOSTIC_PATTERN = re.compile(
    r"(?i)(?:traceback \(most recent call last\)|subprocess\.calledprocesserror|"
    r"file \"[^\"]+\", line \d+|diff --git|@@ -\d+,\d+ \+\d+,\d+ @@|\+[A-Z0-9_]+=[^\s]+)"
)


def contains_unsafe_content(text: str) -> tuple[bool, str]:
    """Check whether text contains paths, embedded credentials, tokens, URLs, or diagnostics."""
    if not text:
        return False, ""
    if _URL_CREDENTIAL_PATTERN.search(text):
        return True, "contains URL-embedded credentials"
    if _EXTERNAL_URL_PATTERN.search(text):
        return True, "contains external URL or link"
    if _TOKEN_AND_KEY_PATTERN.search(text):
        return True, "contains token or credential"
    if _ABSOLUTE_OR_NETWORK_PATH_PATTERN.search(text):
        return True, "contains local or network file system path"
    if _RAW_DIAGNOSTIC_PATTERN.search(text):
        return True, "contains raw diagnostic or diff output"
    return False, ""


def clean_plan_answer(text: str) -> str | None:
    """The answer to one plan decision once trimmed, or ``None`` when it breaks a rule.

    An answer is one line of 1 to :data:`MAX_PLAN_DECISION_ANSWER_CHARS` characters with no
    path, URL, credential or diagnostic text.
    """
    answer = text.strip()
    if not answer or len(answer) > MAX_PLAN_DECISION_ANSWER_CHARS:
        return None
    if "\r" in answer or "\n" in answer:
        return None
    if contains_unsafe_content(answer)[0]:
        return None
    return answer


def build_plan_answers(
    texts: Sequence[str], *, decision_count: int
) -> list[PlanDecisionAnswer] | None:
    """One numbered answer for each of ``decision_count`` decisions, in order, or ``None``.

    ``None`` means a wrong count, an unsafe answer or one that breaks :func:`clean_plan_answer`.
    """
    if not 1 <= decision_count <= MAX_PLAN_DECISIONS or len(texts) != decision_count:
        return None
    answers: list[PlanDecisionAnswer] = []
    for number, text in enumerate(texts, start=1):
        answer = clean_plan_answer(text)
        if answer is None:
            return None
        answers.append(PlanDecisionAnswer(decision_number=number, answer=answer))
    return answers


def compute_approval_context_fingerprint(
    *,
    run_id: str,
    episode_id: str,
    work_item_id: str,
    work_item_title: str,
    risk: str,
    complexity: str,
    intended_outcome: str,
    sensitive_boundary: str,
    necessity: str,
    credible_scenario: str,
    known_mitigations: Sequence[str],
    residual_risk: str,
    decision_requested: str,
    next_state: str,
    authorized_actions: Sequence[str],
    unauthorized_actions: Sequence[str],
    conditions_in_force: Sequence[str],
) -> str:
    """Compute deterministic SHA-256 binding displayed and authority fields to episode."""
    payload = json.dumps(
        {
            "run_id": run_id,
            "episode_id": episode_id,
            "work_item_id": work_item_id,
            "work_item_title": work_item_title,
            "risk": risk,
            "complexity": complexity,
            "intended_outcome": intended_outcome,
            "sensitive_boundary": sensitive_boundary,
            "necessity": necessity,
            "credible_scenario": credible_scenario,
            "known_mitigations": list(known_mitigations),
            "residual_risk": residual_risk,
            "decision_requested": decision_requested,
            "next_state": next_state,
            "authorized_actions": list(authorized_actions),
            "unauthorized_actions": list(unauthorized_actions),
            "conditions_in_force": list(conditions_in_force),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def is_valid_risk_approval_context(
    context: RiskApprovalContext | None,
    run_id: str,
    episode_id: str,
) -> bool:
    """Verify that an approval context is complete, safe, and bound to this run and episode."""
    if not isinstance(context, RiskApprovalContext):
        return False
    if context.risk not in {Risk.R2, Risk.R3}:
        return False
    if context.next_state is not WorkflowState.REFINING:
        return False

    rationale = context.risk_rationale
    fields = [
        context.work_item_id,
        context.work_item_title,
        context.decision_requested,
        rationale.intended_outcome,
        rationale.sensitive_boundary,
        rationale.necessity,
        rationale.credible_scenario,
        *rationale.known_mitigations,
        rationale.residual_risk,
        *context.authorized_actions,
        *context.unauthorized_actions,
        *context.conditions_in_force,
    ]
    for field_val in fields:
        is_unsafe, _ = contains_unsafe_content(field_val)
        if is_unsafe:
            return False

    expected_fp = compute_approval_context_fingerprint(
        run_id=run_id,
        episode_id=episode_id,
        work_item_id=context.work_item_id,
        work_item_title=context.work_item_title,
        risk=context.risk.value,
        complexity=context.complexity.value,
        intended_outcome=rationale.intended_outcome,
        sensitive_boundary=rationale.sensitive_boundary,
        necessity=rationale.necessity,
        credible_scenario=rationale.credible_scenario,
        known_mitigations=rationale.known_mitigations,
        residual_risk=rationale.residual_risk,
        decision_requested=context.decision_requested,
        next_state=context.next_state.value,
        authorized_actions=context.authorized_actions,
        unauthorized_actions=context.unauthorized_actions,
        conditions_in_force=context.conditions_in_force,
    )
    return secrets.compare_digest(context.context_fingerprint, expected_fp)


def compute_plan_decision_context_fingerprint(
    *,
    run_id: str,
    episode_id: str,
    plan_fingerprint: str,
    decisions: Sequence[str],
) -> str:
    """Bind a numbered decision set to one run and escalation episode."""
    payload = json.dumps(
        {
            "run_id": run_id,
            "episode_id": episode_id,
            "plan_fingerprint": plan_fingerprint,
            "decisions": list(decisions),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def is_valid_plan_decision_context(
    context: PlanDecisionContext | None,
    run_id: str,
    episode_id: str,
) -> bool:
    """Verify an answerable decision context is safe and bound to its episode."""
    if not isinstance(context, PlanDecisionContext):
        return False
    if not 1 <= len(context.decisions) <= MAX_PLAN_DECISIONS:
        return False
    if any(not value or contains_unsafe_content(value)[0] for value in context.decisions):
        return False
    expected = compute_plan_decision_context_fingerprint(
        run_id=run_id,
        episode_id=episode_id,
        plan_fingerprint=context.plan_fingerprint,
        decisions=context.decisions,
    )
    return secrets.compare_digest(context.context_fingerprint, expected)
