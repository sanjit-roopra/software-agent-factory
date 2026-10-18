"""Advisory scan for suppressions and skips that a change adds (ADR-056).

The implementer prompt forbids skipping tests or adding lint or type
suppressions to make checks pass. This scan counts such markers on the added
lines of the run's diff, so the tester, the reviewer and the operator see
them. It never fails a run: a marker can be correct, and only a person or the
reviewer can judge its reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import CommandResult
from .workspace import WorkspaceEvidence

SUPPRESSION_SCAN_COMMAND = "suppression scan"
MAX_LISTED = 50
_TEXT_LIMIT = 160
_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_MARKER = re.compile(
    r"#\s*noqa\b|type:\s*ignore\b|pragma:\s*no\s*cover\b|#\s*nosec\b|NOSONAR"
    r"|pytest\.mark\.(?:skip|skipif|xfail)\b|pytest\.(?:skip|xfail)\(|unittest\.skip"
    r"|\b(?:it|test|describe)\.skip\(|eslint-disable|@ts-(?:ignore|expect-error|nocheck)\b"
)


@dataclass(frozen=True)
class Suppression:
    """One marker on one added line."""

    path: str
    line: int
    marker: str
    text: str


@dataclass
class _Cursor:
    """Where the scan is in a unified diff: the file and the hunk line counts."""

    path: str | None = None
    old_left: int = 0
    new_left: int = 0
    new_line: int = 0

    def in_hunk(self) -> bool:
        return self.old_left > 0 or self.new_left > 0

    def read_header(self, line: str) -> None:
        if line.startswith("+++ "):
            self.path = line[6:] if line.startswith("+++ b/") else None
        elif match := _HUNK.match(line):
            self.old_left = int(match.group(1) or "1")
            self.new_line = int(match.group(2))
            self.new_left = int(match.group(3) or "1")

    def advance(self, kind: str) -> None:
        if kind in ("+", " "):
            self.new_line += 1
            self.new_left -= 1
        if kind in ("-", " "):
            self.old_left -= 1


def added_suppressions(diff: str) -> list[Suppression]:
    """Return the markers on the added lines of a unified diff, in diff order."""
    found: list[Suppression] = []
    cursor = _Cursor()
    for line in diff.splitlines():
        if not cursor.in_hunk():
            cursor.read_header(line)
            continue
        kind = line[:1]
        marker = _MARKER.search(line) if kind == "+" else None
        if marker is not None and cursor.path is not None:
            found.append(
                Suppression(cursor.path, cursor.new_line, marker.group(0), line[1:].strip())
            )
        cursor.advance(kind)
    return found


def suppression_items(evidence: WorkspaceEvidence | None) -> list[str]:
    """Markdown items for the PR body: the place and the marker of each one."""
    found = added_suppressions(evidence.diff) if evidence is not None else []
    items = [f"`{item.path}:{item.line}`: `{item.marker}`" for item in found[:MAX_LISTED]]
    if len(found) > MAX_LISTED:
        items.append(f"and {len(found) - MAX_LISTED} more")
    return items


def suppression_scan(diff: str) -> CommandResult | None:
    """Describe the markers as one passing check, or ``None`` when the diff adds none."""
    found = added_suppressions(diff)
    if not found:
        return None
    lines = [f"suppression scan: {len(found)} suppression or skip markers on added lines"]
    lines.extend(
        f"- {item.path}:{item.line}: {item.text[:_TEXT_LIMIT]}" for item in found[:MAX_LISTED]
    )
    if len(found) > MAX_LISTED:
        lines.append(f"- and {len(found) - MAX_LISTED} more")
    return CommandResult(
        command=SUPPRESSION_SCAN_COMMAND,
        exit_code=0,
        stdout="\n".join(lines),
        stderr="",
        duration_seconds=0.0,
    )
