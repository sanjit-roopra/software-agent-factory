"""Entrypoint for ``python -m software_agent_factory`` and frozen builds.

``PLAN.md`` Phase 15.2: the frozen bundle and the installed ``factory``
console script share this entry point. ``--version`` is answered here without
importing the Typer application at all, but the answer is identical to the
one the Typer ``--version`` option prints, because both render the same
``<program> <version>`` line from :mod:`software_agent_factory.version`.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

import typer
from typer.main import get_command

from software_agent_factory.version import get_program_name, get_version

VERSION_FLAGS = frozenset({"--version", "-V"})

__all__ = ["main"]


def _program_name() -> str:
    return get_program_name()


def _should_print_version(arguments: Sequence[str]) -> bool:
    return len(arguments) == 1 and arguments[0] in VERSION_FLAGS


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if _should_print_version(arguments):
        typer.echo(f"{_program_name()} {get_version()}")
        return 0

    from software_agent_factory.cli import app

    command = get_command(app)
    command.main(
        args=arguments,
        prog_name=_program_name(),
        standalone_mode=argv is None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
