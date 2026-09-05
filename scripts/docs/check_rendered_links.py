"""Fail when rendered documentation contains visible Markdown links."""

from __future__ import annotations

import argparse
import re
from html.parser import HTMLParser
from pathlib import Path

MARKDOWN_LINK = re.compile(r"\[[^\]\n]+\]\([^\)\n]+\)")
IGNORED_ELEMENTS = {"code", "pre", "script", "style"}


class VisibleTextParser(HTMLParser):
    """Collect visible text while ignoring code and non-content elements."""

    def __init__(self) -> None:
        super().__init__()
        self._ignored_depth = 0
        self.visible_text: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del attrs
        if tag in IGNORED_ELEMENTS:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in IGNORED_ELEMENTS:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0:
            self.visible_text.append(data)


def find_literal_markdown_links(site_dir: Path) -> list[str]:
    failures: list[str] = []
    for html_path in sorted(site_dir.rglob("*.html")):
        parser = VisibleTextParser()
        parser.feed(html_path.read_text(encoding="utf-8"))
        for text in parser.visible_text:
            for match in MARKDOWN_LINK.finditer(text):
                failures.append(f"{html_path.relative_to(site_dir)}: {match.group()}")
    return failures


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("site_dir", type=Path)
    return parser.parse_args()


def main() -> int:
    failures = find_literal_markdown_links(parse_args().site_dir)
    if not failures:
        return 0

    print("Visible Markdown links found in rendered documentation:")
    for failure in failures:
        print(f"- {failure}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
