"""Mechanical checks for the code rules in AGENTS.md."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "software_agent_factory"
MAX_MODULE_LINES = 800
MAX_AGENTS_MD_LINES = 100

# Modules already over the limit, at their line count when the rule started.
# A listed module may shrink but never grow. Lower its number when you shrink
# it, and remove it once it is under the limit.
OVERSIZED_MODULES = {
    "workflow.py": 2795,
    "github.py": 1979,
    "observability.py": 1605,
    "cli.py": 1806,
    "escalation.py": 1501,
    "projects.py": 1696,
    "dashboard/sanitize.py": 1068,
    "service.py": 951,
    "scheduler.py": 961,
    "repository_profile.py": 970,
    "dashboard/handler.py": 901,
    "doctor.py": 881,
    "prompts.py": 865,
    "config.py": 860,
    "pi_runtime.py": 864,
}

_PATH_TOKEN = re.compile(
    r"^(src|docs|tests|scripts|\.github|\.claude)/|^[^\s]+/[^\s]+\.(py|md|ya?ml|toml)$"
)


def _line_count(path: Path) -> int:
    if not path.is_file():
        return 0
    with path.open(encoding="utf-8") as handle:
        return sum(1 for _ in handle)


def test_modules_stay_under_the_size_limit() -> None:
    problems = []
    for path in sorted(PACKAGE.rglob("*.py")):
        name = path.relative_to(PACKAGE).as_posix()
        limit = OVERSIZED_MODULES.get(name, MAX_MODULE_LINES)
        lines = _line_count(path)
        if lines > limit:
            problems.append(
                f"{name}: {lines} lines, limit {limit}. Split it before adding more; "
                "if a listed module shrank, lower its number in OVERSIZED_MODULES."
            )
    assert problems == []


def test_oversized_list_only_shrinks() -> None:
    fixed = [name for name in OVERSIZED_MODULES if _line_count(PACKAGE / name) <= MAX_MODULE_LINES]
    assert fixed == [], f"remove these from OVERSIZED_MODULES: {fixed}"


def test_agents_md_paths_exist() -> None:
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    tokens = {token for token in re.findall(r"`([^`\s]+)`", text) if _PATH_TOKEN.search(token)}
    missing = sorted(
        token for token in tokens if not (ROOT / token).exists() and not (PACKAGE / token).exists()
    )
    assert tokens
    assert missing == [], f"AGENTS.md names paths that do not exist: {missing}"


def test_agents_md_stays_short() -> None:
    lines = _line_count(ROOT / "AGENTS.md")
    assert lines <= MAX_AGENTS_MD_LINES, (
        f"AGENTS.md has {lines} lines, limit {MAX_AGENTS_MD_LINES}. "
        "Keep it short and move detail to docs/."
    )


def test_claude_md_imports_resolve() -> None:
    # Claude Code resolves an @import relative to the file that contains it.
    claude_md = ROOT / ".claude" / "CLAUDE.md"
    imports = re.findall(r"^@(\S+)$", claude_md.read_text(encoding="utf-8"), re.MULTILINE)
    assert imports
    missing = [path for path in imports if not (claude_md.parent / path).is_file()]
    assert missing == [], f".claude/CLAUDE.md imports files that do not exist: {missing}"
