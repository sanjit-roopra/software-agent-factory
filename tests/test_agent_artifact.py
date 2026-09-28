from __future__ import annotations

import json

from software_agent_factory.agent_artifact import parse_agent_artifact
from software_agent_factory.models import AgentRole, TriageResult


def _triage_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "factory_eligible": True,
        "complexity": "L0",
        "risk": "R0",
        "requirements_quality": "clear",
        "needs_research": False,
        "dependencies": [],
        "unknowns": [],
        "confidence": 1.0,
    }


def test_parse_agent_artifact_extracts_typed_artifact_from_final_text() -> None:
    text = json.dumps(_triage_payload())

    artifact = parse_agent_artifact(AgentRole.TRIAGE, text=text)

    assert isinstance(artifact, TriageResult)
    assert artifact.complexity == "L0"
    assert artifact.risk == "R0"


def test_parse_agent_artifact_no_json_object_wording() -> None:
    try:
        parse_agent_artifact(AgentRole.TRIAGE, text="not any JSON here at all")
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected ValueError")

    assert "did not contain a parseable JSON object for TriageResult" in message


def test_parse_agent_artifact_validation_error_wording() -> None:
    payload = _triage_payload()
    del payload["complexity"]
    text = json.dumps(payload)

    try:
        parse_agent_artifact(AgentRole.TRIAGE, text=text)
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected ValueError")

    assert "did not validate as TriageResult" in message
    assert "complexity: Field required" in message


def test_parse_agent_artifact_empty_text_is_no_parseable_object() -> None:
    try:
        parse_agent_artifact(AgentRole.TRIAGE, text="   ")
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected ValueError")

    assert "did not contain a parseable JSON object for TriageResult" in message
