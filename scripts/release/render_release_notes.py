"""Render GitHub Release notes for macOS artifacts."""

from __future__ import annotations

import argparse
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    notes = f"""software-agent-factory {args.tag}

Release artifacts:
- software-agent-factory-{args.version}-macos-arm64.tar.gz
- software-agent-factory-{args.version}-macos-x86_64.tar.gz
- software_agent_factory-{args.version}-py3-none-any.whl
- software_agent_factory-{args.version}.tar.gz
- SHA256SUMS
- build-info.json

Gatekeeper notice:
These macOS archives are unsigned or ad-hoc signed only. macOS may quarantine the
extracted directory. If that happens, remove the quarantine attribute manually,
for example:

  xattr -dr com.apple.quarantine software-agent-factory

External prerequisites:
- required: git
- optional: gh (required when pull_request.enabled, ci.enabled or scheduler.enabled)
- optional: copilot (only when you opt into --runtime copilot)

This release does not auto-install, auto-update, notarize, deploy or publish to a
package index.

Verify provenance after this repository is public:

  gh attestation verify <artifact> --repo sanjit-roopra/software-agent-factory
"""
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(notes, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
