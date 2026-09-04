"""Combine per-artifact build metadata into the published release manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("build_info", nargs="+", type=Path)
    return parser.parse_args()


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Build info must be a JSON object: {path}")
    return payload


def main() -> int:
    args = parse_args()
    entries = [_load(path) for path in args.build_info]
    versions = {entry.get("version") for entry in entries}
    if len(versions) != 1:
        raise SystemExit(f"Expected exactly one release version, found: {sorted(versions)}")

    first = entries[0]
    payload = {
        "project": first.get("project", "software-agent-factory"),
        "version": first["version"],
        "tag": first.get("tag", ""),
        "commit_sha": first.get("commit_sha", ""),
        "artifacts": sorted(
            entries,
            key=lambda entry: (
                str(entry.get("target_architecture", "")),
                str(entry.get("runner_image", "")),
            ),
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
