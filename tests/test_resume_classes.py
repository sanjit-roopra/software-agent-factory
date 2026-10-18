"""The resumable class table names each resumable class once, and the tables on it agree."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import get_args

from software_agent_factory import halt_contexts, resume_classes, run_reopen
from software_agent_factory.dashboard import handler, next_step
from software_agent_factory.escalation_models import DashboardRequestAction
from software_agent_factory.models import (
    AcceptedReplyReceipt,
    EscalationRecord,
    ResumeClassification,
)
from software_agent_factory.resume_classes import GITHUB_REPLY_CLASSES, RESUME_CLASSES

RESUMABLE = set(ResumeClassification) - {ResumeClassification.NOT_RESUMABLE}


def test_every_resumable_class_has_an_entry_and_not_resumable_has_none() -> None:
    assert set(RESUME_CLASSES) == RESUMABLE


def test_every_entry_names_real_fields_and_has_every_value() -> None:
    for kind, spec in RESUME_CLASSES.items():
        assert spec.context_field in EscalationRecord.model_fields, kind
        assert spec.receipt_field in AcceptedReplyReceipt.model_fields, kind
        assert spec.command("run-1", "episode-1"), kind
        assert spec.dashboard_action, kind
        assert spec.needs_you_action, kind
        assert spec.needs_you_label, kind
        assert "{moment}" in spec.queued_sentence, kind
        assert spec.expired_sentence, kind


def test_context_and_receipt_fields_and_dashboard_actions_are_unique() -> None:
    specs = list(RESUME_CLASSES.values())
    for field in ("context_field", "receipt_field", "dashboard_action"):
        values = [getattr(spec, field) for spec in specs]
        assert len(set(values)) == len(values), field


def test_every_table_keyed_on_the_resumable_classes_covers_all_of_them() -> None:
    assert set(get_args(DashboardRequestAction)) == RESUMABLE
    assert set(halt_contexts._CONTEXT_BUILDERS) == RESUMABLE
    assert set(run_reopen._REOPEN_CHECKS) == RESUMABLE
    assert set(next_step._STALE_SENTENCES_BY_ACTION) == RESUMABLE
    assert set(next_step._STEPS) == {spec.dashboard_action for spec in RESUME_CLASSES.values()}
    assert set(handler._ACTION_KINDS.values()) == RESUMABLE


def test_a_publish_retry_takes_no_github_reply() -> None:
    # ADR-042: a publish retry is a dashboard action only.
    assert ResumeClassification.DELIVERY_RETRY not in GITHUB_REPLY_CLASSES
    assert GITHUB_REPLY_CLASSES == {
        ResumeClassification.RISK_APPROVAL,
        ResumeClassification.PLAN_DECISION,
    }


def test_the_table_module_imports_only_pure_package_modules() -> None:
    tree = ast.parse(Path(resume_classes.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module
    }
    assert imported <= {"models", "escalation_protocol", "redaction"}
