"""Mechanical checks for the code rules in AGENTS.md."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "software_agent_factory"
MAX_MODULE_LINES = 800

# Modules already over the limit, at their line count when the rule started.
# A listed module may shrink but never grow. Lower its number when you shrink
# it, and remove it once it is under the limit.
OVERSIZED_MODULES = {
    "workflow.py": 4702,
    "github.py": 2825,
    "observability.py": 2503,
    "models.py": 2113,
    "cli.py": 1896,
    "escalation.py": 1784,
    "projects.py": 1715,
    "dashboard/sanitize.py": 1068,
    "service.py": 998,
    "scheduler.py": 996,
    "repository_profile.py": 970,
    "dashboard/handler.py": 904,
    "doctor.py": 881,
    "prompts.py": 866,
    "config.py": 864,
    "pi_runtime.py": 864,
    "workspace.py": 818,
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
