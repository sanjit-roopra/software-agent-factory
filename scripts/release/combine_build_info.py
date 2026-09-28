"""Combine per-artifact build metadata into the published release manifest."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("build_info", nargs="+", type=Path)
    return parser.parse_args()


def _within_cwd(path: Path) -> Path:
    """Resolve a CLI-supplied path and refuse it if it escapes the working directory."""
    resolved = os.path.realpath(path)
    base_dir = os.path.realpath(os.getcwd())
    if resolved != base_dir and not resolved.startswith(base_dir + os.sep):
        raise ValueError(f"path {str(path)!r} is outside the working directory {base_dir!r}")
    return Path(resolved)


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(_within_cwd(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Build info must be a JSON object: {path}")
    return payload


def _consistent_value(entries: list[dict[str, Any]], key: str) -> str:
    values = {entry.get(key) for entry in entries}
    if len(values) != 1:
        rendered = sorted(repr(value) for value in values)
        raise SystemExit(f"Expected exactly one release {key}, found: {rendered}")
    value = values.pop()
    if not isinstance(value, str) or not value:
        raise SystemExit(f"Release {key} must be a non-empty string")
    return value


def main() -> int:
    args = parse_args()
    entries = [_load(path) for path in args.build_info]
    payload: dict[str, Any] = {
        key: _consistent_value(entries, key) for key in ("project", "version", "tag", "commit_sha")
    }
    payload["artifacts"] = sorted(
        entries,
        key=lambda entry: (
            str(entry.get("target_architecture", "")),
            str(entry.get("runner_image", "")),
        ),
    )
    output = _within_cwd(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
