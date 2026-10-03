"""Select review lenses from the changed files (ADR-034).

A lens is a short checklist for one kind of change. The registry is data, and
a pure function selects the lenses whose scope matches at least one changed
file. No model selects lenses. The reviewer receives the selected checklists,
so a backend-only change gets no user interface checklist.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import PurePosixPath

ALWAYS: tuple[str, ...] = ("*",)


@dataclass(frozen=True)
class ReviewLens:
    """One review checklist and the file patterns it applies to."""

    name: str
    scope: tuple[str, ...]
    checklist: tuple[str, ...]

    def applies_to(self, path: str) -> bool:
        if self.scope == ALWAYS:
            return True
        posix = PurePosixPath(path)
        return any(
            fnmatchcase(posix.name, pattern) or fnmatchcase(posix.as_posix(), pattern)
            for pattern in self.scope
        )


#: The lens registry. Lenses with ``ALWAYS`` scope come first.
REVIEW_LENSES: tuple[ReviewLens, ...] = (
    ReviewLens(
        "correctness",
        ALWAYS,
        (
            "Inverted or missing conditions, off-by-one errors and unhandled empty input.",
            "Return values and errors that callers ignore.",
            "Behavior that differs from the acceptance criteria.",
        ),
    ),
    ReviewLens(
        "security",
        ALWAYS,
        (
            "Untrusted input that reaches a shell, a query, a path or a template.",
            "Secrets in code, logs or error messages.",
            "Missing authorization on a new entry point.",
        ),
    ),
    ReviewLens(
        "tests",
        ALWAYS,
        (
            "New behavior without a test that fails when the behavior breaks.",
            "Assertions that pass for wrong output, such as a check for any value.",
            "Tests that depend on time, order, randomness or the network.",
        ),
    ),
    ReviewLens(
        "python",
        ("*.py",),
        (
            "Missing type annotations on public functions.",
            "Bare except, or a broad except that hides the error.",
            "Mutable default arguments.",
        ),
    ),
    ReviewLens(
        "typescript",
        ("*.ts", "*.tsx", "*.mts", "*.cts"),
        (
            "Use of any, or a type assertion that hides a real type error.",
            "Possible null or undefined values without a check.",
            "Promises that nobody awaits.",
        ),
    ),
    ReviewLens(
        "javascript",
        ("*.js", "*.jsx", "*.mjs", "*.cjs"),
        (
            "Loose equality where the types can differ.",
            "Promises that nobody awaits.",
            "Mutation of shared objects or function arguments.",
        ),
    ),
    ReviewLens(
        "ui",
        ("*.tsx", "*.jsx", "*.vue", "*.svelte", "*.html", "*.css", "*.scss"),
        (
            "Interactive elements without a label or keyboard access.",
            "Effects or watchers with missing dependencies, or subscriptions that leak.",
            "List items without a stable key.",
        ),
    ),
    ReviewLens(
        "data",
        ("*.sql", "migrations/*", "*/migrations/*", "alembic/*", "*/alembic/*"),
        (
            "Migrations that lock large tables or cannot run twice.",
            "Schema changes without a way back.",
            "Queries inside loops.",
        ),
    ),
    ReviewLens(
        "dependencies",
        (
            "pyproject.toml",
            "requirements*.txt",
            "uv.lock",
            "poetry.lock",
            "package.json",
            "package-lock.json",
            "pnpm-lock.yaml",
            "yarn.lock",
        ),
        (
            "A new dependency that the change does not need.",
            "A manifest change without the matching lockfile change.",
            "Version ranges that are wider than the rest of the manifest.",
        ),
    ),
    ReviewLens(
        "ci",
        (".github/workflows/*", "*.yml", "*.yaml"),
        (
            "Third-party actions that are not pinned to a commit.",
            "Workflow permissions wider than the job needs.",
            "Untrusted event fields used in a run step.",
        ),
    ),
    ReviewLens(
        "containers",
        ("Dockerfile", "Dockerfile.*", "*.dockerfile", "compose.yml", "compose.yaml"),
        (
            "Images that run as root or use a floating tag.",
            "Secrets copied into an image layer.",
        ),
    ),
    ReviewLens(
        "docs",
        ("*.md", "*.rst", "docs/*"),
        (
            "Statements that the changed code no longer supports.",
            "Commands or options that do not exist.",
        ),
    ),
)


def select_review_lenses(changed_files: list[str] | tuple[str, ...]) -> tuple[ReviewLens, ...]:
    """Return the lenses that apply to at least one changed file, in registry order."""
    if not changed_files:
        return ()
    return tuple(
        lens for lens in REVIEW_LENSES if any(lens.applies_to(path) for path in changed_files)
    )


def render_review_lenses(lenses: tuple[ReviewLens, ...]) -> list[dict[str, object]]:
    """Return the checklists in selection order, for a prompt."""
    return [{"lens": lens.name, "check": list(lens.checklist)} for lens in lenses]
