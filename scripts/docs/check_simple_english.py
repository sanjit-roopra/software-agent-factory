"""Deterministic SimpleEnglish prose checker for repository documentation."""

from __future__ import annotations

import argparse
import importlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_VENDOR = _ROOT / "src" / "software_agent_factory" / "_vendor"
sys.path.insert(0, str(_VENDOR))
_LINTER = importlib.import_module("simple_english.lint")

DASH = _LINTER.DASH
LATIN = _LINTER.LATIN
LIMITS = _LINTER.LIMITS
SLOP = _LINTER.SLOP
INSTRUCTION_START = re.compile(
    r"^(?:add|apply|build|check|choose|configure|confirm|copy|create|do|edit|"
    r"enable|examine|inspect|install|keep|make|open|operate|read|remove|replace|"
    r"report|run|select|set|start|stop|use|verify|write)\b",
    re.I,
)
CONTRACTION = re.compile(
    r"\b\w+n[’']t\b|"
    r"\b(?:I|you|he|she|it|we|they|that|there|here|what|who|where|when|why|how)"
    r"[’'](?:d|ll|m|re|s|ve)\b|"
    r"\blet[’']s\b",
    re.I,
)
BANNED_MODAL = re.compile(r"(?i:\b(?:should|would|might|could)\b)|\bmay\b")
PERFECT_TENSE = re.compile(
    r"\b(?:has|have|had)\s+(?:not\s+|never\s+|already\s+|just\s+)?"
    r"(?:been|\w+ed|built|done|found|given|gone|known|made|read|run|seen|shown|taken|written)\b",
    re.I,
)
COMMA_ING = re.compile(
    r",\s*\b(?:mak|allow|enabl|ensur|highlight|creat|provid|offer|help|"
    r"reduc|improv|lead|caus|result|writ|runn|chang|us)ing\b",
    re.I,
)
TRAILING_CONDITION = re.compile(r"\s(?:if|when)\s", re.I)
EMBEDDED_QUESTION_VERB = re.compile(
    r"\b(?:ask|control|decide|define|determine|explain|know|report|show|specify)\s*$",
    re.I,
)


@dataclass(frozen=True)
class Violation:
    """Actionable record of a documentation writing violation."""

    path: Path
    line: int
    rule: str
    message: str

    def format(self, root: Path | None = None) -> str:
        display_path = self.path
        if root is not None:
            try:
                display_path = self.path.relative_to(root)
            except ValueError:
                display_path = self.path
        return f"{display_path}:{self.line}: {self.rule}: {self.message}"


def mask_markdown(source: str) -> str:
    """Mask non-prose and protected technical spans while preserving newlines."""
    masked = list(source)

    def mask_span(start: int, end: int) -> None:
        for idx in range(start, end):
            if masked[idx] != "\n":
                masked[idx] = " "

    if source.startswith("---"):
        end_fm = source.find("\n---", 3)
        if end_fm == -1:
            end_fm = source.find("\n...", 3)
        if end_fm != -1:
            end_fm_line = source.find("\n", end_fm + 4)
            if end_fm_line == -1:
                end_fm_line = len(source)
            mask_span(0, end_fm_line)

    s = "".join(masked)

    for m in re.finditer(r"(?ms)^[ \t]*(```+|~~~+)[^\n]*\n.*?\n[ \t]*\1[ \t]*$", s):
        mask_span(m.start(), m.end())

    s = "".join(masked)

    for m in re.finditer(r"(?s)<!--.*?-->", s):
        mask_span(m.start(), m.end())

    s = "".join(masked)

    admonition_indent: int | None = None
    list_indent: int | None = None
    offset = 0
    for line in s.splitlines(keepends=True):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        list_item = re.match(r"^\s*(?:[-*+]|\d+[.)])\s+", line)
        if re.match(r"^\s*!!!\s", line):
            admonition_indent = indent + 4
        elif stripped:
            if admonition_indent is not None and indent < admonition_indent:
                admonition_indent = None
            if list_item is not None:
                list_indent = indent
            elif list_indent is not None and indent <= list_indent:
                list_indent = None

            if admonition_indent is not None and indent >= admonition_indent + 4:
                mask_span(offset, offset + len(line))
            elif admonition_indent is None and list_indent is not None:
                if indent >= list_indent + 8:
                    mask_span(offset, offset + len(line))
            elif admonition_indent is None and indent >= 4:
                mask_span(offset, offset + len(line))
        offset += len(line)

    s = "".join(masked)

    for m in re.finditer(r"!\[([^\]\n]*)\]\(([^)\n]+)\)", s):
        dest_start = m.start(2) - 1
        dest_end = m.end(2) + 1
        mask_span(dest_start, dest_end)

    s = "".join(masked)

    for m in re.finditer(r"\[([^\]\n]*)\]\(([^)\n]+)\)", s):
        dest_start = m.start(2) - 1
        dest_end = m.end(2) + 1
        mask_span(dest_start, dest_end)

    s = "".join(masked)

    for m in re.finditer(r"(?m)^[ \t]*\[[^\]\n]+\]:[ \t]+[^\n]*$", s):
        mask_span(m.start(), m.end())

    s = "".join(masked)

    for m in re.finditer(r"(?s)(?<!`)(`+)(?!`).*?\1(?!`)", s):
        mask_span(m.start(), m.end())

    s = "".join(masked)

    for m in re.finditer(r"<[a-zA-Z/][^>\n]*>", s):
        mask_span(m.start(), m.end())

    s = "".join(masked)

    for m in re.finditer(r"https?://\S+|<(?:https?|mailto):[^>\s]+>", s):
        mask_span(m.start(), m.end())

    s = "".join(masked)

    quoted_error = re.compile(
        r"(?i)\b(?:error|warning|message|output)(?:\s+\w+){0,3}\s*:\s*"
        r"(\"[^\"\n]*\"|“[^”\n]*”|(?<!\w)'[^'\n]+'(?!\w))"
    )
    for m in quoted_error.finditer(s):
        mask_span(m.start(1), m.end(1))

    s = "".join(masked)

    for m in re.finditer(r"(?m)^[ \t]*#+\s.*$", s):
        mask_span(m.start(), m.end())

    for m in re.finditer(r"(?m)^[ \t]*\|[\s:|-]+\|[ \t]*$", s):
        mask_span(m.start(), m.end())

    return "".join(masked)


def extract_sentences_with_spans(text: str) -> list[tuple[str, int, int]]:
    """Extract sentences from masked text along with start offsets and word counts."""
    lines = text.splitlines(keepends=True)
    line_offsets: list[int] = []
    curr = 0
    for line in lines:
        line_offsets.append(curr)
        curr += len(line)

    blocks: list[tuple[str, int]] = []
    curr_block: list[str] = []
    curr_block_start = 0
    in_block = False

    list_marker_re = re.compile(r"^[ \t]*([-*+]|\d+\.)[ \t]+")
    table_row_re = re.compile(r"^[ \t]*\|(.*)\|[ \t]*$")

    for i, line in enumerate(lines):
        stripped = line.strip()
        offset = line_offsets[i]

        if not stripped:
            if curr_block:
                blocks.append(("".join(curr_block), curr_block_start))
                curr_block = []
                in_block = False
            continue

        m_list = list_marker_re.match(line)
        if m_list:
            if curr_block:
                blocks.append(("".join(curr_block), curr_block_start))
                curr_block = []
            marker_len = m_list.end()
            curr_block_start = offset + marker_len
            curr_block = [line[marker_len:]]
            in_block = True
            continue

        m_table = table_row_re.match(line)
        if m_table:
            if curr_block:
                blocks.append(("".join(curr_block), curr_block_start))
                curr_block = []
                in_block = False
            for cell in m_table.group(1).split("|"):
                stripped_cell = cell.strip()
                if stripped_cell:
                    c_offset = offset + line.find(cell)
                    blocks.append((cell, c_offset))
            continue

        if not in_block:
            curr_block_start = offset
            curr_block = [line]
            in_block = True
        else:
            curr_block.append(line)

    if curr_block:
        blocks.append(("".join(curr_block), curr_block_start))

    sentence_splitter = re.compile(r"([^\s.!?].*?(?:[.!?](?=\s|$)|$))", re.S)
    results: list[tuple[str, int, int]] = []

    for block_text, block_start in blocks:
        for m in sentence_splitter.finditer(block_text):
            sent = m.group(1).strip()
            words = [w for w in sent.split() if any(c.isalnum() for c in w)]
            if len(words) >= 2:
                sent_offset = block_start + m.start(1)
                results.append((sent, sent_offset, len(words)))

    return results


def check_content(
    content: str,
    path: Path,
    root: Path | None = None,
) -> list[Violation]:
    """Check Markdown content and return ordered actionable violations."""
    masked = mask_markdown(content)
    violations: list[Violation] = []
    del root

    for m in re.finditer(r";", masked):
        line = content[: m.start()].count("\n") + 1
        violations.append(
            Violation(
                path=path,
                line=line,
                rule="semicolon",
                message="semicolons are not permitted",
            )
        )

    for m in DASH.finditer(masked):
        line = content[: m.start()].count("\n") + 1
        violations.append(
            Violation(
                path=path,
                line=line,
                rule="em_dash",
                message=f"disallowed dash: {m.group()!r}",
            )
        )

    for m in LATIN.finditer(masked):
        line = content[: m.start()].count("\n") + 1
        violations.append(
            Violation(
                path=path,
                line=line,
                rule="latin_abbrev",
                message=f"Latin abbreviation: {m.group()!r}",
            )
        )

    for m in SLOP.finditer(masked):
        line = content[: m.start()].count("\n") + 1
        violations.append(
            Violation(
                path=path,
                line=line,
                rule="slop_word",
                message=f"filler term: {m.group()!r}",
            )
        )

    for rule, message, pattern in (
        ("contraction", "contractions are not permitted", CONTRACTION),
        ("modal", "use can, will, or must instead", BANNED_MODAL),
        ("perfect_tense", "use a simple tense instead", PERFECT_TENSE),
        ("comma_ing", "start a new sentence instead", COMMA_ING),
    ):
        for m in pattern.finditer(masked):
            line = content[: m.start()].count("\n") + 1
            violations.append(
                Violation(
                    path=path,
                    line=line,
                    rule=rule,
                    message=f"{message}: {m.group()!r}",
                )
            )

    for sent, offset, count in extract_sentences_with_spans(masked):
        procedural = is_procedural_sentence(sent)
        sentence_limit = LIMITS["procedural" if procedural else "descriptive"]
        if count > sentence_limit:
            line = content[:offset].count("\n") + 1
            excerpt = sent.replace("\n", " ").strip()
            if len(excerpt) > 60:
                excerpt = excerpt[:57] + "..."
            violations.append(
                Violation(
                    path=path,
                    line=line,
                    rule="sentence_over_limit",
                    message=(
                        f"sentence has {count} words (limit is {sentence_limit}): {excerpt!r}"
                    ),
                )
            )
        condition = trailing_condition(sent) if procedural else None
        if condition is not None:
            line = content[: offset + condition.start()].count("\n") + 1
            violations.append(
                Violation(
                    path=path,
                    line=line,
                    rule="trailing_condition",
                    message="put the condition before the instruction",
                )
            )

    violations.sort(key=lambda v: (v.line, v.rule, v.message))
    return violations


def is_procedural_sentence(sentence: str) -> bool:
    """Return whether a sentence gives an instruction or starts with a condition."""
    prose = sentence.lstrip(" *_0123456789.)")
    if INSTRUCTION_START.match(prose) is not None:
        return True
    condition = re.match(r"^(?:if|when)\b[^,]*,\s*(.*)$", prose, re.I)
    return condition is not None and INSTRUCTION_START.match(condition.group(1)) is not None


def trailing_condition(sentence: str) -> re.Match[str] | None:
    """Find a trailing condition while excluding embedded questions."""
    for match in TRAILING_CONDITION.finditer(sentence):
        if match.start() < 4:
            continue
        if EMBEDDED_QUESTION_VERB.search(sentence[: match.start()]) is None:
            return match
    return None


def check_file(path: Path, root: Path | None = None) -> list[Violation]:
    """Check one Markdown file on disk."""
    content = path.read_text(encoding="utf-8")
    return check_content(content, path, root=root)


def find_documentation_files(root: Path) -> list[Path]:
    """Discover README.md and all Markdown files under docs/."""
    files: list[Path] = []
    readme = root / "README.md"
    if readme.is_file():
        files.append(readme)
    docs_dir = root / "docs"
    if docs_dir.is_dir():
        files.extend(sorted(docs_dir.rglob("*.md")))
    return files


def resolve_target_files(paths: list[Path], root: Path) -> list[Path]:
    """Resolve target Markdown files from command line inputs or defaults."""
    if not paths:
        return find_documentation_files(root)
    resolved: list[Path] = []
    for path in paths:
        target = path if path.is_absolute() else (root / path)
        if target.is_dir():
            resolved.extend(sorted(target.rglob("*.md")))
        elif target.is_file():
            resolved.append(target)
        else:
            raise FileNotFoundError(f"Documentation file or directory not found: {path}")
    return resolved


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check Markdown documentation for SimpleEnglish writing violations.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Specific Markdown files to check (defaults to README.md and docs/**/*.md).",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Repository root directory (defaults to auto-detected repository root).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.resolve() if args.root else _ROOT

    try:
        files = resolve_target_files(args.paths, root)
    except FileNotFoundError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 2

    if not files:
        print(f"No Markdown files found to check under {root}.")
        return 0

    all_violations: list[Violation] = []
    files_with_violations = 0

    for file_path in files:
        violations = check_file(file_path, root=root)
        if violations:
            files_with_violations += 1
            all_violations.extend(violations)
            for v in violations:
                print(v.format(root=root))

    if all_violations:
        print(
            f"\nSimpleEnglish prose check failed: {len(all_violations)} violation(s) "
            f"found across {files_with_violations} file(s).",
            file=sys.stderr,
        )
        return 1

    print(
        f"Checked {len(files)} file(s): "
        "all documentation satisfies SimpleEnglish controlled writing."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
