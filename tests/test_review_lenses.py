from __future__ import annotations

import pytest
from prompt_fixtures import first_implementer_request, first_review_request

from software_agent_factory.prompts import build_prompt
from software_agent_factory.review_lenses import (
    ALWAYS,
    REVIEW_LENSES,
    render_review_lenses,
    select_review_lenses,
)


def _names(changed_files: list[str]) -> list[str]:
    return [lens.name for lens in select_review_lenses(changed_files)]


def test_no_changed_files_selects_no_lens() -> None:
    assert select_review_lenses([]) == ()


def test_backend_python_change_gets_no_ui_lens() -> None:
    assert _names(["src/app/service.py", "tests/test_service.py"]) == [
        "correctness",
        "security",
        "tests",
        "python",
    ]


@pytest.mark.parametrize(
    ("path", "lens"),
    [
        ("web/App.tsx", "typescript"),
        ("web/App.tsx", "ui"),
        ("web/index.js", "javascript"),
        ("web/page.vue", "ui"),
        ("db/migrations/0002_add_index.py", "data"),
        ("schema.sql", "data"),
        ("pyproject.toml", "dependencies"),
        ("web/package-lock.json", "dependencies"),
        (".github/workflows/ci.yml", "ci"),
        ("Dockerfile", "containers"),
        ("docs/guide.md", "docs"),
        ("README.md", "docs"),
    ],
)
def test_lens_scope_matches_its_files(path: str, lens: str) -> None:
    assert lens in _names([path])


def test_always_lenses_come_first_and_lenses_keep_registry_order() -> None:
    names = _names(["README.md", "src/a.py"])

    assert names == ["correctness", "security", "tests", "python", "docs"]
    assert [lens.scope for lens in REVIEW_LENSES[:3]] == [ALWAYS, ALWAYS, ALWAYS]


def test_every_lens_has_a_unique_name_and_a_checklist() -> None:
    names = [lens.name for lens in REVIEW_LENSES]

    assert len(names) == len(set(names))
    assert all(lens.checklist for lens in REVIEW_LENSES)


def test_rendered_lenses_map_names_to_items() -> None:
    rendered = render_review_lenses(select_review_lenses(["Dockerfile"]))

    assert [item["lens"] for item in rendered] == ["correctness", "security", "tests", "containers"]
    assert rendered[3]["check"] == [
        "Images that run as root or use a floating tag.",
        "Secrets copied into an image layer.",
    ]


def test_reviewer_prompt_lists_the_selected_lenses() -> None:
    prompt = build_prompt(first_review_request(changed_files=["src/app/service.py"]))

    assert "Review lenses for the changed files" in prompt
    assert "Mutable default arguments." in prompt
    assert "List items without a stable key." not in prompt


def test_implementer_prompt_gets_no_lenses() -> None:
    prompt = build_prompt(first_implementer_request(changed_files=["src/app/service.py"]))

    assert "Review lenses for the changed files" not in prompt
