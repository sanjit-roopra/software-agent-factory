"""Mechanical prose checks adapted from SimpleEnglish v2.0.2.

This file keeps the deterministic checks that fit factory artifacts. The
factory does not use the upstream benchmark runner, chat reply rules, modal
rules, tense rules, trailing-condition rule, or synonym-rotation rule.

The checks cannot prove ASD-STE100 compliance.
"""

from __future__ import annotations

import re
from importlib import resources
from typing import Literal, TypedDict

TextType = Literal["procedural", "descriptive"]

LATIN = re.compile(r"\b(e\.g\.|i\.e\.|etc\.?)(?=[\s,)]|$)", re.I)
SLOP_CORE = re.compile(
    r"\b(simply|seamlessly|effortlessly|robust|leverag\w*|utiliz\w*|"
    r"comprehensive|powerful|blazingly|streamlin\w*|facilitat\w*|"
    r"performant|plethora|myriad|delve|crucial|pivotal)\b",
    re.I,
)
DASH = re.compile(r"—|(?<!\d)–(?!\d)|(?<= )--(?= )|(?<=[^\s\d]{2}) - (?=[^\s\d]{2})")
LIMITS: dict[TextType, int] = {"procedural": 20, "descriptive": 25}


class LintReport(TypedDict):
    type: TextType
    words: int
    sentences: int
    mean_sentence_words: float
    longest_sentence_words: int
    violations: dict[str, int]
    violations_total: int
    violations_per_100w: float


def _slop_pattern() -> re.Pattern[str]:
    resource = resources.files(__package__).joinpath("slop.tsv")
    if not resource.is_file():
        raise RuntimeError("the bundled SimpleEnglish slop.tsv file is missing")
    terms = []
    for line in resource.read_text(encoding="utf-8").splitlines():
        term = line.split("\t")[0].strip().lower()
        if term:
            terms.append(re.escape(term).replace(r"\ ", r"\s+") + r"\w*")
    if not terms:
        raise RuntimeError("the bundled SimpleEnglish slop.tsv file is empty")
    return re.compile(
        SLOP_CORE.pattern[: -len(r")\b")] + "|" + "|".join(terms) + r")\b",
        re.I,
    )


SLOP = _slop_pattern()


def _strip_protected_text(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"`[^`\n]+`", " ", text)
    text = re.sub(r'"[^"\n]*"', " ", text)
    text = re.sub(r"“[^”\n]*”", " ", text)
    text = re.sub(r"(?<!\w)'[^'\n]+'(?!\w)", " ", text)
    text = re.sub(r"^#+\s.*$", " ", text, flags=re.M)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"^(?: {4}|\t).*$", " ", text, flags=re.M)
    text = re.sub(r"^\s*\|[\s:|-]+\|\s*$", " ", text, flags=re.M)
    return re.sub(
        r"^\s*\|(.*)\|\s*$",
        lambda match: (
            ". ".join(cell.strip() for cell in match.group(1).split("|") if cell.strip()) + ". "
        ),
        text,
        flags=re.M,
    )


def prose_word_count(text: str) -> int:
    """Count prose words after exact technical spans are removed."""

    return len(_strip_protected_text(text).split())


def _sentences(text: str) -> list[str]:
    text = re.sub(
        r"^\s*([-*]|\d+\.)\s+(.*?)([.!?:])?\s*$",
        lambda match: match.group(2) + (match.group(3) or ".") + " ",
        text,
        flags=re.M,
    )
    text = re.sub(r"\n+", ". ", text)
    parts = re.split(r"(?<=[.!?:])\s+", text)
    return [part.strip() for part in parts if len(part.strip().split()) >= 2]


def lint(text: str, text_type: TextType) -> LintReport:
    """Return the selected SimpleEnglish mechanical findings for ``text``."""

    body = _strip_protected_text(text)
    sentence_list = _sentences(body)
    limit = LIMITS[text_type]
    lengths = [len(sentence.split()) for sentence in sentence_list]
    counts = {
        "sentence_over_limit": sum(1 for length in lengths if length > limit),
        "semicolon": body.count(";"),
        "em_dash": len(DASH.findall(body)),
        "latin_abbrev": len(LATIN.findall(body)),
        "slop_word": len(SLOP.findall(body)),
    }
    words = max(1, prose_word_count(text))
    total = sum(counts.values())
    return {
        "type": text_type,
        "words": words,
        "sentences": len(sentence_list),
        "mean_sentence_words": round(sum(lengths) / max(1, len(lengths)), 1),
        "longest_sentence_words": max(lengths, default=0),
        "violations": counts,
        "violations_total": total,
        "violations_per_100w": round(100.0 * total / words, 2),
    }
