"""Generate build metadata from the authoritative project version."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from software_agent_factory.version import get_version


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tag")
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--runner-image", required=True)
    parser.add_argument("--python-version", required=True)
    parser.add_argument("--pyinstaller-version", default="")
    parser.add_argument("--target-architecture", required=True)
    parser.add_argument("--validate-tag-match", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    version = get_version()
    expected_tag = f"v{version}"
    if args.validate_tag_match and args.tag != expected_tag:
        raise SystemExit(
            f"Release tag mismatch: expected {expected_tag}, received {args.tag or '(none)'}"
        )

    payload = {
        "project": "software-agent-factory",
        "version": version,
        "tag": args.tag or "",
        "commit_sha": args.commit_sha,
        "build_timestamp": datetime.now(timezone.utc).isoformat(),
        "runner_image": args.runner_image,
        "python_version": args.python_version,
        "pyinstaller_version": args.pyinstaller_version,
        "target_architecture": args.target_architecture,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
