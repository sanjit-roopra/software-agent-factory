"""Focused tests for the deterministic documentation SimpleEnglish prose gate."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_script_module(name: str, relative_path: str) -> ModuleType:
    script_path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_script = _load_script_module("check_simple_english", "scripts/docs/check_simple_english.py")
check_content = _script.check_content
check_file = _script.check_file
find_documentation_files = _script.find_documentation_files
is_procedural_sentence = _script.is_procedural_sentence
main = _script.main
resolve_target_files = _script.resolve_target_files


def test_clean_prose_passes_without_violations() -> None:
    clean_markdown = (
        "# Clean Documentation\n\n"
        "This is a concise technical guide. It explains one fact per sentence.\n\n"
        "## Next steps\n\n"
        "- Run the test suite.\n"
        "- Verify your changes.\n"
    )
    violations = check_content(clean_markdown, Path("guide.md"))
    assert violations == []

    # Real repository files that are already compliant
    readme_violations = check_file(ROOT / "README.md")
    assert readme_violations == []

    policy_doc = ROOT / "docs" / "reference" / "writing-policy.md"
    assert check_file(policy_doc) == []


def test_protected_technical_spans_are_not_flagged() -> None:
    markdown = (
        "---\n"
        "title: Configuration; guide -- options\n"
        "description: A robust; comprehensive -- tool.\n"
        "---\n\n"
        "# Technical Spans\n\n"
        "Here is valid prose before code.\n\n"
        "```bash\n"
        "# Semicolons and dashes in fenced code are protected\n"
        'uv sync --locked --no-default-groups; echo "robust" --flag; run e.g. command\n'
        "```\n\n"
        "~~~python\n"
        "# Tilde fences are also protected\n"
        "value = 1; name = 'robust'; # e.g. comment -- with dash\n"
        "~~~\n\n"
        "Here is inline code: `uv run factory --flag; echo robust — e.g. text`.\n\n"
        "    # Indented code block is protected\n"
        "    indented_command --flag; echo 'robust' — e.g.\n\n"
        "Bare URL: https://example.com/api?q=1;opt=2--flag&filter=robust\n"
        "Autolink: <https://example.com/path;opt=1--dash>\n"
        "Markdown link: [clean anchor text](https://example.com/page;foo=bar--baz).\n"
        "Markdown image: ![clean alt text](https://example.com/img.png;foo=bar--baz).\n"
        "[ref]: https://example.com/reference;id=1--flag\n\n"
        "<!-- HTML comments; with -- dashes and robust slop words -->\n"
        '<div class="saf-hero" data-info="robust; dash -- here">\n'
        'This sentence includes a protected error message: "connection timeout; retry -- now".\n'
        "This sentence has warning text: “error; robust -- flag”.\n"
        "This sentence has output text: 'invalid token; retry -- later'.\n"
        "</div>\n"
    )

    violations = check_content(markdown, Path("protected.md"))
    assert violations == []


def test_wrapped_inline_code_does_not_hide_following_prose() -> None:
    markdown = (
        "Use `alpha` and a wrapped `beta\nspan` here. Then simply leverage a robust `x` design.\n"
    )

    findings = check_content(markdown, Path("docs/example.md"))

    assert [finding.rule for finding in findings].count("slop_word") == 3


def test_wrapped_inline_code_protects_flags() -> None:
    markdown = "Use `factory run --may-skip\nverify` for this operation.\n"

    assert check_content(markdown, Path("docs/example.md")) == []


def test_admonition_prose_is_checked() -> None:
    markdown = '!!! warning "Risk"\n\n    You should never run this robust command in production.\n'

    findings = check_content(markdown, Path("docs/example.md"))

    assert {finding.rule for finding in findings} == {"modal", "slop_word"}


def test_code_nested_in_admonition_is_protected() -> None:
    markdown = '!!! warning "Risk"\n\n    Read the warning.\n\n        printf "value; other"\n'

    assert check_content(markdown, Path("docs/example.md")) == []


def test_list_continuation_prose_is_checked() -> None:
    markdown = "- First item\n    This should use no robust filler.\n"

    findings = check_content(markdown, Path("docs/example.md"))

    assert {finding.rule for finding in findings} == {"modal", "slop_word"}


def test_violation_reporting_reports_actionable_path_and_lines() -> None:
    target_path = Path("docs/guides/example.md")
    content = (
        "Line 1 is clean prose.\n"
        "Line 2 has a semicolon; here in prose.\n"
        "Line 3 has an em dash — here in prose.\n"
        "Line 4 has a Latin abbreviation e.g. here.\n"
        "Line 5 has a robust filler term here.\n"
        "Line 6 has a sentence that contains far too many words in it so that it "
        "easily exceeds the descriptive limit of twenty-five words and triggers "
        "a sentence over limit finding.\n"
        "Line 7 should not use a banned modal.\n"
        "Line 8 has been written with a perfect tense.\n"
        "Line 9 is unclear, making the result difficult to read.\n"
        "Line 10 isn't permitted because it contains a contraction.\n"
        "Start the service when the flag is set.\n"
    )

    violations = check_content(content, target_path)

    assert len(violations) == 10

    v_semi = next(v for v in violations if v.rule == "semicolon")
    assert v_semi.path == target_path
    assert v_semi.line == 2
    assert "semicolon" in v_semi.message

    v_dash = next(v for v in violations if v.rule == "em_dash")
    assert v_dash.path == target_path
    assert v_dash.line == 3
    assert "—" in v_dash.message

    v_latin = next(v for v in violations if v.rule == "latin_abbrev")
    assert v_latin.path == target_path
    assert v_latin.line == 4
    assert "e.g." in v_latin.message

    v_slop = next(v for v in violations if v.rule == "slop_word")
    assert v_slop.path == target_path
    assert v_slop.line == 5
    assert "robust" in v_slop.message

    v_sent = next(v for v in violations if v.rule == "sentence_over_limit")
    assert v_sent.path == target_path
    assert v_sent.line == 6
    assert "limit is 25" in v_sent.message

    assert next(v for v in violations if v.rule == "modal").line == 7
    assert next(v for v in violations if v.rule == "perfect_tense").line == 8
    assert next(v for v in violations if v.rule == "comma_ing").line == 9
    assert next(v for v in violations if v.rule == "contraction").line == 10
    assert next(v for v in violations if v.rule == "trailing_condition").line == 11

    formatted = v_semi.format()
    assert formatted == "docs/guides/example.md:2: semicolon: semicolons are not permitted"

    formatted_with_root = v_semi.format(root=Path.cwd())
    assert "docs/guides/example.md:2: semicolon" in formatted_with_root


def test_procedural_sentences_use_the_twenty_word_limit() -> None:
    sentence = "Use " + " ".join(["word"] * 20) + "."

    assert is_procedural_sentence(sentence)
    findings = check_content(sentence, Path("docs/guides/example.md"))

    assert len(findings) == 1
    assert findings[0].rule == "sentence_over_limit"
    assert "limit is 20" in findings[0].message


@pytest.mark.parametrize(
    "sentence",
    [
        "Run " + " ".join(["word"] * 20) + ".",
        "Verify " + " ".join(["word"] * 20) + ".",
        "If the build fails, run " + " ".join(["word"] * 16) + ".",
    ],
)
def test_common_instructions_use_the_twenty_word_limit(sentence: str) -> None:
    findings = check_content(sentence, Path("docs/example.md"))

    assert any(
        finding.rule == "sentence_over_limit" and "limit is 20" in finding.message
        for finding in findings
    )


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("We've finished the task.", "contraction"),
        ("You’ll operate the command.", "contraction"),
        ("Here's the result.", "contraction"),
        ("Here’s the result.", "contraction"),
        ("The agent has completed the task.", "perfect_tense"),
        ("The agent had written the file.", "perfect_tense"),
        (
            "The factory starts the command, writing output to the log.",
            "comma_ing",
        ),
    ],
)
def test_grammar_checks_cover_common_forms(text: str, rule: str) -> None:
    findings = check_content(text, Path("docs/example.md"))

    assert [finding.rule for finding in findings] == [rule]


def test_month_name_is_not_a_banned_modal() -> None:
    assert check_content("The release starts in May.", Path("docs/example.md")) == []


def test_nonverb_ing_words_after_commas_are_not_flagged() -> None:
    text = "The types are integer, string, and boolean. The list includes tools, including Git."

    assert check_content(text, Path("docs/example.md")) == []


def test_embedded_question_is_not_a_trailing_condition() -> None:
    text = "Use this setting to decide when jobs start."

    assert check_content(text, Path("docs/example.md")) == []


def test_file_discovery_finds_readme_and_docs(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("# Readme\n", encoding="utf-8")

    docs = tmp_path / "docs"
    docs.mkdir()
    doc1 = docs / "index.md"
    doc1.write_text("# Index\n", encoding="utf-8")

    sub = docs / "sub"
    sub.mkdir()
    doc2 = sub / "guide.md"
    doc2.write_text("# Guide\n", encoding="utf-8")

    # Non-documentation files should be ignored
    (tmp_path / "CONTRIBUTING.md").write_text("# Contributing\n", encoding="utf-8")
    (docs / "image.png").write_bytes(b"\x89PNG")

    discovered = find_documentation_files(tmp_path)
    assert discovered == [readme, doc1, doc2]


def test_resolve_target_files_supports_explicit_paths_and_dirs(tmp_path: Path) -> None:
    file1 = tmp_path / "file1.md"
    file1.write_text("# File 1\n", encoding="utf-8")

    dir1 = tmp_path / "dir1"
    dir1.mkdir()
    file2 = dir1 / "file2.md"
    file2.write_text("# File 2\n", encoding="utf-8")

    resolved = resolve_target_files([file1, dir1], tmp_path)
    assert resolved == [file1, file2]

    with pytest.raises(FileNotFoundError, match="not found"):
        resolve_target_files([Path("nonexistent.md")], tmp_path)


def test_main_exit_status_zero_on_clean_prose(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = tmp_path / "README.md"
    doc.write_text("# Title\n\nShort clean sentence.\n", encoding="utf-8")

    exit_code = main(["--root", str(tmp_path), str(doc)])
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "all documentation satisfies SimpleEnglish controlled writing" in captured.out
    assert captured.err == ""


def test_main_exit_status_nonzero_on_violations(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = tmp_path / "README.md"
    doc.write_text("# Title\n\nSentence with a semicolon; violation.\n", encoding="utf-8")

    exit_code = main(["--root", str(tmp_path), str(doc)])
    assert exit_code == 1

    captured = capsys.readouterr()
    assert "README.md:3: semicolon: semicolons are not permitted" in captured.out
    assert "SimpleEnglish prose check failed: 1 violation(s)" in captured.err


def test_main_exit_status_two_on_missing_file(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["missing-file-xyz.md"])
    assert exit_code == 2

    captured = capsys.readouterr()
    assert "Error: Documentation file or directory not found" in captured.err
