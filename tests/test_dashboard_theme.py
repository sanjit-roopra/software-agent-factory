"""Theme tokens, contrast and toggle wiring for the dashboard assets.

The tests read ``style.css`` and ``app.js`` as plain text. Tokens must be
6-digit hex literals so the WCAG 2.1 contrast ratios can be computed here.
"""

from __future__ import annotations

import re

import pytest

from software_agent_factory.dashboard import assets as dashboard_assets

THEMES = ("light", "dark")

TOKEN_NAMES = (
    "--bg",
    "--surface",
    "--surface-2",
    "--border",
    "--text",
    "--text-muted",
    "--accent",
    "--accent-contrast",
    "--ok",
    "--warn",
    "--error",
    "--focus",
)

_BLOCK_SELECTORS = {
    "light": r":root",
    "dark": r':root\[data-theme="dark"\]',
    "dark-system": r":root:not\(\[data-theme\]\)",
}
_TOKEN = re.compile(r"(--[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{6})\s*;")
_COLOR_LITERAL = re.compile(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgb|rgba|hsl|hsla)\(")

# Text pairs need 4.5:1 and UI pairs 3:1 (WCAG 2.1 AA).
TEXT_PAIRS = (
    ("--text", "--bg"),
    ("--text", "--surface"),
    ("--text", "--surface-2"),
    ("--text-muted", "--bg"),
    ("--text-muted", "--surface"),
    ("--text-muted", "--surface-2"),
    ("--accent-contrast", "--accent"),
    ("--ok", "--surface"),
    ("--warn", "--surface"),
    ("--error", "--surface"),
)
UI_PAIRS = (
    ("--focus", "--bg"),
    ("--accent", "--surface"),
)


def _block(css: str, selector: str) -> str:
    match = re.search(selector + r"\s*\{([^}]*)\}", css)
    assert match, f"no CSS rule for {selector}"
    return match.group(1)


def _tokens(css: str, name: str) -> dict[str, str]:
    return dict(_TOKEN.findall(_block(css, _BLOCK_SELECTORS[name])))


def _channel(value: int) -> float:
    c = value / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(color: str) -> float:
    r, g, b = (_channel(int(color[i : i + 2], 16)) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: str, b: str) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.fixture(scope="module")
def css() -> str:
    return dashboard_assets.STYLE_CSS


@pytest.mark.parametrize("theme", [*THEMES, "dark-system"])
def test_theme_block_defines_every_token_as_hex(css: str, theme: str) -> None:
    assert set(_tokens(css, theme)) == set(TOKEN_NAMES)


def test_system_dark_block_matches_the_explicit_dark_block(css: str) -> None:
    assert _tokens(css, "dark-system") == _tokens(css, "dark")


def test_dark_system_block_sits_inside_the_prefers_color_scheme_query(css: str) -> None:
    assert re.search(
        r"@media\s*\(prefers-color-scheme:\s*dark\)\s*\{\s*:root:not\(\[data-theme\]\)", css
    )


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize(("foreground", "background"), TEXT_PAIRS)
def test_text_pairs_reach_4_5_to_1(css: str, theme: str, foreground: str, background: str) -> None:
    tokens = _tokens(css, theme)
    assert _contrast(tokens[foreground], tokens[background]) >= 4.5


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize(("foreground", "background"), UI_PAIRS)
def test_ui_pairs_reach_3_to_1(css: str, theme: str, foreground: str, background: str) -> None:
    tokens = _tokens(css, theme)
    assert _contrast(tokens[foreground], tokens[background]) >= 3.0


def test_contrast_helper_matches_known_wcag_values() -> None:
    assert _contrast("#000000", "#ffffff") == pytest.approx(21.0)
    assert _contrast("#777777", "#777777") == pytest.approx(1.0)


def test_no_hard_coded_colors_outside_token_blocks(css: str) -> None:
    body = css
    for selector in _BLOCK_SELECTORS.values():
        body = re.sub(selector + r"\s*\{[^}]*\}", "", body)
    assert _COLOR_LITERAL.findall(body) == []


# --------------------------------------------------------------------------
# Toggle and storage wiring
# --------------------------------------------------------------------------


def test_index_html_has_a_native_theme_toggle_button_in_the_header() -> None:
    html = dashboard_assets.render_index_html(token="fixture-token")
    header = html.split("<header>")[1].split("</header>")[0]
    assert re.search(r'<button id="theme-toggle" type="button"', header)


@pytest.mark.parametrize("call", ["getItem", "setItem"])
def test_app_js_touches_local_storage_only_inside_try(call: str) -> None:
    js = dashboard_assets.APP_JS
    assert 'THEME_STORAGE_KEY = "factory-dashboard-theme"' in js
    assert re.search(
        r"try\s*\{[^}]*localStorage\." + call + r"\(THEME_STORAGE_KEY[^}]*\}\s*catch", js
    )
    assert js.count("localStorage.") == 2


def test_app_js_accepts_only_light_or_dark_and_falls_back_to_the_system() -> None:
    js = dashboard_assets.APP_JS
    assert 'stored === "light" || stored === "dark"' in js
    assert "(prefers-color-scheme: dark)" in js
    assert 'setAttribute("data-theme"' in js
