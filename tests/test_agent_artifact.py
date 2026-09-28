from __future__ import annotations

import json

import pytest

from software_agent_factory.agent_artifact import (
    ScanStats,
    _iter_json_objects,
    parse_agent_artifact,
)
from software_agent_factory.models import AgentPurpose, AgentRole, TriageResult


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
    with pytest.raises(
        ValueError, match="did not contain a parseable JSON object for TriageResult"
    ):
        parse_agent_artifact(AgentRole.TRIAGE, text="not any JSON here at all")


def test_parse_agent_artifact_validation_error_wording() -> None:
    payload = _triage_payload()
    del payload["complexity"]
    text = json.dumps(payload)

    with pytest.raises(ValueError, match="did not validate as TriageResult") as exc_info:
        parse_agent_artifact(AgentRole.TRIAGE, text=text)

    assert "complexity: Field required" in str(exc_info.value)


def test_parse_agent_artifact_empty_text_is_no_parseable_object() -> None:
    with pytest.raises(
        ValueError, match="did not contain a parseable JSON object for TriageResult"
    ):
        parse_agent_artifact(AgentRole.TRIAGE, text="   ")


# ---------------------------------------------------------------------------
# _artifact_spec (via parse_agent_artifact): purpose-gated role mismatches
# ---------------------------------------------------------------------------


def test_decompose_project_purpose_requires_planner_role() -> None:
    with pytest.raises(ValueError, match="project decomposition requires the PLANNER role"):
        parse_agent_artifact(
            AgentRole.TRIAGE,
            text="{}",
            purpose=AgentPurpose.DECOMPOSE_PROJECT,
        )


def test_generate_repository_skill_purpose_requires_researcher_role() -> None:
    with pytest.raises(
        ValueError, match="repository skill generation requires the RESEARCHER role"
    ):
        parse_agent_artifact(
            AgentRole.TRIAGE,
            text="{}",
            purpose=AgentPurpose.GENERATE_REPOSITORY_SKILL,
        )


def test_correct_change_set_purpose_requires_implementer_role() -> None:
    with pytest.raises(ValueError, match="ChangeSet correction requires the IMPLEMENTER role"):
        parse_agent_artifact(
            AgentRole.TRIAGE,
            text="{}",
            purpose=AgentPurpose.CORRECT_CHANGE_SET,
        )


# ---------------------------------------------------------------------------
# _iter_json_objects: bounded-work JSON candidate scanner
# ---------------------------------------------------------------------------


def test_iter_json_objects_adversarial_unclosed_braces_linear_speed() -> None:
    text = "{" * 50000

    objects = _iter_json_objects(text)
    assert objects == []


def test_iter_json_objects_adversarial_unclosed_objects_speed() -> None:
    text = '{"key":' * 5000

    objects = _iter_json_objects(text)
    assert objects == []


def test_iter_json_objects_handles_braces_in_strings_and_escapes() -> None:
    text = (
        'prose before {"summary": "has {braces} and \\"escaped\\" quotes", '
        '"changed_files": ["foo.py"], "tests_added": [], "commands_run": []} prose after'
    )
    objects = _iter_json_objects(text)
    assert len(objects) == 1
    assert objects[0]["summary"] == 'has {braces} and "escaped" quotes'


def test_iter_json_objects_handles_multiple_valid_objects() -> None:
    text = '{"a": 1}\nsome text\n{"b": 2}\n{"c": {"nested": true}}'
    objects = _iter_json_objects(text)
    assert len(objects) == 3
    assert objects[0] == {"a": 1}
    assert objects[1] == {"b": 2}
    assert objects[2] == {"c": {"nested": True}}


def test_iter_json_objects_ignores_non_string_keys() -> None:
    text = '{123: "invalid json"} {"valid": true}'
    objects = _iter_json_objects(text)
    assert len(objects) == 1
    assert objects[0] == {"valid": True}


def test_iter_json_objects_malformed_prose_followed_by_valid_objects() -> None:
    text = (
        'Some text with { code: block } and { "bad": syntax, [1, } '
        'and unclosed { "string: unclosed '
        'and then valid: {"first": 123} and {"second": {"nested": 456}}'
    )
    objects = _iter_json_objects(text)
    assert len(objects) == 2
    assert objects[0] == {"first": 123}
    assert objects[1] == {"second": {"nested": 456}}


def test_iter_json_objects_deeply_nested_malformed_bounded_work() -> None:
    depth = 1000
    text = '{"a": ' * depth + "broken_payload" + "}" * depth
    stats = ScanStats()
    objects = _iter_json_objects(text, scan_stats=stats)

    assert objects == []
    # Verify deterministic near-linear work: characters scanned is bounded by 2 * len(text)
    assert stats.chars_scanned <= 2 * len(text)
    # Consecutive nested failures bound ensures only a small number of candidate decodes occur
    assert stats.candidates_tested <= 10


def test_iter_json_objects_unclosed_nested_braces_bounded_work() -> None:
    depth = 1000
    text = '{"a": ' * depth + "}"
    stats = ScanStats()
    objects = _iter_json_objects(text, scan_stats=stats)

    assert objects == []
    # Unclosed braces are tracked statefully so redundant scans are skipped in O(1)
    assert stats.chars_scanned <= 2 * len(text)
    assert stats.candidates_tested <= 2


def test_iter_json_objects_enforces_max_scan_work_limit() -> None:
    depth = 500
    text = '{"a": ' * depth + "}" * depth
    stats = ScanStats()
    limit = 250
    objects = _iter_json_objects(text, scan_stats=stats, max_scan_work=limit)

    assert objects == []
    assert stats.chars_scanned <= limit + 10


def test_iter_json_objects_skips_non_json_braces_without_suffix_scans() -> None:
    text = "{x" * 1000 + "}"
    stats = ScanStats()

    objects = _iter_json_objects(text, scan_stats=stats, max_scan_work=1)

    assert objects == []
    assert stats.chars_scanned == 0
    assert stats.candidates_tested == 0


def test_iter_json_objects_nested_envelope_with_broken_outer_recovers_inner() -> None:
    text = '{ broken: syntax, "result": {"valid": 123} }'
    objects = _iter_json_objects(text)
    assert objects == [{"valid": 123}]


def test_iter_json_objects_preserves_multiple_objects_and_escapes() -> None:
    text = (
        '{"first": "escaped \\" { and } braces", "val": 1} '
        '{"second": {"nested": "str \\\\ with \\" quote"}}'
    )
    objects = _iter_json_objects(text)
    assert len(objects) == 2
    assert objects[0] == {"first": 'escaped " { and } braces', "val": 1}
    assert objects[1] == {"second": {"nested": 'str \\ with " quote'}}
