"""Add release instructions and optional notice files to a frozen bundle."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

INSTALL_FILENAME = "INSTALL.txt"
ROOT = Path(__file__).resolve().parents[2]
OPTIONAL_NOTICE_GLOBS = ("LICENSE", "LICENSE.*", "NOTICE", "NOTICE.*")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--architecture", required=True)
    return parser.parse_args()


def render_install_instructions(version: str, architecture: str) -> str:
    stable_location = (
        f"~/.local/opt/software-agent-factory/{version}-macos-{architecture}/software-agent-factory"
    )
    return f"""software-agent-factory {version} for macOS {architecture}

Quick start
- Move this extracted software-agent-factory directory to a stable location such as:
  {stable_location}
- From inside the directory, run: ./factory --version
- Then run: ./factory doctor
- Optional background service: ./factory service install

External prerequisites
- required: git
- optional: gh (required when pull_request.enabled, ci.enabled or scheduler.enabled)
- optional: copilot (only when using --runtime copilot)

Gatekeeper note
- This archive is unsigned or ad-hoc signed only.
- If macOS blocks it after download, remove quarantine with:
  xattr -dr com.apple.quarantine software-agent-factory
"""


def _copy_optional_notice_files(bundle_dir: Path) -> None:
    for pattern in OPTIONAL_NOTICE_GLOBS:
        for source in ROOT.glob(pattern):
            if source.is_file():
                shutil.copy2(source, bundle_dir / source.name)


def prepare_bundle(bundle_dir: Path, *, version: str, architecture: str) -> None:
    target = bundle_dir.resolve()
    if not target.is_dir():
        raise SystemExit(f"Bundle directory does not exist: {bundle_dir}")
    (target / INSTALL_FILENAME).write_text(
        render_install_instructions(version, architecture),
        encoding="utf-8",
    )
    _copy_optional_notice_files(target)


def main() -> int:
    args = parse_args()
    prepare_bundle(args.bundle_dir, version=args.version, architecture=args.architecture)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
