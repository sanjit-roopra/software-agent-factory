"""Theme tokens, contrast and toggle wiring for the dashboard assets.

The tests read ``style.css`` and ``app.js`` as plain text. Tokens must be
6-digit hex literals so the WCAG 2.1 contrast ratios can be computed here.
"""

from __future__ import annotations

import re

import pytest
from dashboard_js import function_source, strip_comments

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

# Text needs 4.5:1 and UI parts 3:1 (WCAG 2.1 AA).
TEXT_BACKGROUNDS = ("--bg", "--surface", "--surface-2")
#: Defined for status text, so checked even where no rule uses them yet.
RESERVED_TEXT_TOKENS = ("--ok",)
#: Sits on the accent fill, not on a page background, so it has its own pair.
ON_ACCENT_TOKEN = "--accent-contrast"
#: A disabled button or field: muted text on the raised surface, with no opacity to dim it.
DISABLED_PAIR = ("--text-muted", "--surface-2")
UI_PAIRS = (
    ("--focus", "--bg"),
    ("--accent", "--surface"),
)
_TEXT_COLOR_USE = re.compile(r"(?<![\w-])color:\s*var\((--[a-z0-9-]+)\)")
_COLOR_PROPERTY = re.compile(
    r"(?<![\w-])(color|background|background-color"
    r"|border(?:-(?:top|right|bottom|left))?(?:-color)?|outline(?:-color)?)\s*:\s*([^;}]+)"
)
_NOT_A_COLOR = re.compile(
    r"var\(--[a-z0-9-]+\)|\d+(?:\.\d+)?(?:px|rem|em)?|\b(?:solid|dashed|none)\b"
)


def _text_tokens(css: str) -> list[str]:
    """Every token a rule uses as text color, plus the reserved status tokens."""
    used = set(_TEXT_COLOR_USE.findall(css)) | set(RESERVED_TEXT_TOKENS)
    return sorted(used - {ON_ACCENT_TOKEN})


def _non_token_colors(css: str) -> list[str]:
    """Color-bearing declarations whose value is not made of tokens, widths and styles."""
    return [
        f"{prop}: {value.strip()}"
        for prop, value in _COLOR_PROPERTY.findall(css)
        if _NOT_A_COLOR.sub("", value).strip()
    ]


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


_TEXT_TOKENS = _text_tokens(dashboard_assets.STYLE_CSS)


def test_every_text_token_a_rule_uses_is_checked() -> None:
    assert {"--text", "--text-muted", "--accent", "--warn", "--error", "--ok"} <= set(_TEXT_TOKENS)
    assert ON_ACCENT_TOKEN not in _TEXT_TOKENS


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("foreground", _TEXT_TOKENS)
@pytest.mark.parametrize("background", TEXT_BACKGROUNDS)
def test_text_tokens_reach_4_5_to_1_on_every_background(
    css: str, theme: str, foreground: str, background: str
) -> None:
    tokens = _tokens(css, theme)
    assert _contrast(tokens[foreground], tokens[background]) >= 4.5


@pytest.mark.parametrize("theme", THEMES)
def test_text_on_the_accent_fill_reaches_4_5_to_1(css: str, theme: str) -> None:
    tokens = _tokens(css, theme)
    assert _contrast(tokens[ON_ACCENT_TOKEN], tokens["--accent"]) >= 4.5


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize(("foreground", "background"), UI_PAIRS)
def test_ui_pairs_reach_3_to_1(css: str, theme: str, foreground: str, background: str) -> None:
    tokens = _tokens(css, theme)
    assert _contrast(tokens[foreground], tokens[background]) >= 3.0


@pytest.mark.parametrize("theme", THEMES)
def test_a_disabled_control_keeps_4_5_to_1_text_contrast(css: str, theme: str) -> None:
    foreground, background = DISABLED_PAIR
    tokens = _tokens(css, theme)
    assert _contrast(tokens[foreground], tokens[background]) >= 4.5


@pytest.mark.parametrize("selector", ["button:disabled", r"\.answer-field input:disabled"])
def test_a_disabled_control_is_dimmed_by_its_colors_and_not_by_opacity(
    css: str, selector: str
) -> None:
    rule = _block(css, selector)
    foreground, background = DISABLED_PAIR
    assert f"color: var({foreground});" in rule
    assert f"background: var({background});" in rule
    assert "cursor: not-allowed;" in rule
    assert "dashed" in rule
    assert "opacity" not in rule


def test_contrast_helper_matches_known_wcag_values() -> None:
    assert _contrast("#000000", "#ffffff") == pytest.approx(21.0)
    assert _contrast("#777777", "#777777") == pytest.approx(1.0)


def test_no_hard_coded_colors_outside_token_blocks(css: str) -> None:
    body = css
    for selector in _BLOCK_SELECTORS.values():
        body = re.sub(selector + r"\s*\{[^}]*\}", "", body)
    assert _COLOR_LITERAL.findall(body) == []


def test_color_properties_use_tokens_and_never_named_colors(css: str) -> None:
    assert _non_token_colors(css) == []


def test_the_named_color_check_flags_named_colors_in_any_color_property() -> None:
    sample = (
        "a { color: white; } b { background: black; } c { border: 1px solid red; }"
        " d { border-bottom-color: transparent; } e { outline: 2px solid var(--focus); }"
        " f { border-right: none; color: var(--text); }"
    )
    assert _non_token_colors(sample) == [
        "color: white",
        "background: black",
        "border: 1px solid red",
        "border-bottom-color: transparent",
    ]


# --------------------------------------------------------------------------
# Toggle and storage wiring
# --------------------------------------------------------------------------


def test_index_html_has_a_native_theme_toggle_button_in_the_header() -> None:
    html = dashboard_assets.render_index_html(token="fixture-token")
    header = html.split("<header>")[1].split("</header>")[0]
    assert re.search(r'<button id="theme-toggle" type="button"', header)


STORAGE_CALLS = ("getItem", "setItem")


def test_the_theme_is_stored_under_one_key() -> None:
    assert 'const THEME_STORAGE_KEY = "factory-dashboard-theme";' in dashboard_assets.APP_JS


@pytest.mark.parametrize(
    ("call", "function"), list(zip(STORAGE_CALLS, ("readStoredTheme", "storeTheme")))
)
def test_each_local_storage_call_sits_inside_try_catch(call: str, function: str) -> None:
    source = function_source(dashboard_assets.APP_JS, function)
    assert re.fullmatch(
        rf"function {function}\([^)]*\) \{{ try \{{.*localStorage\.{call}\(THEME_STORAGE_KEY.*\}} "
        r"catch \{.*\} \}",
        source,
    )


def test_local_storage_is_touched_only_by_the_listed_calls() -> None:
    assert strip_comments(dashboard_assets.APP_JS).count("localStorage.") == len(STORAGE_CALLS)


def test_a_blocked_or_unreadable_store_means_no_remembered_theme() -> None:
    source = function_source(dashboard_assets.APP_JS, "readStoredTheme")
    assert source.endswith("catch { return null; } }")
    assert "stored === THEME_LIGHT || stored === THEME_DARK ? stored : null" in source


def test_a_blocked_store_keeps_the_choice_for_this_page_view() -> None:
    source = function_source(dashboard_assets.APP_JS, "storeTheme")
    assert source.endswith("catch { } }")


def test_theme_constants_name_the_values_and_the_attribute() -> None:
    js = dashboard_assets.APP_JS
    assert 'const THEME_LIGHT = "light";' in js
    assert 'const THEME_DARK = "dark";' in js
    assert 'const THEME_ATTRIBUTE = "data-theme";' in js
    assert 'DARK_QUERY = "(prefers-color-scheme: dark)";' in js


def test_the_stored_theme_is_applied_before_the_page_is_wired() -> None:
    js = dashboard_assets.APP_JS
    assert function_source(js, "applyStoredTheme") == (
        "function applyStoredTheme() { const initialTheme = readStoredTheme(); "
        "if (initialTheme) { "
        "document.documentElement.setAttribute(THEME_ATTRIBUTE, initialTheme); } }"
    )
    assert function_source(js, "start").startswith("function start() { applyStoredTheme();")


def test_the_toggle_applies_the_next_theme_and_then_stores_it() -> None:
    bindings = function_source(dashboard_assets.APP_JS, "bindControls")
    assert (
        'document.getElementById("theme-toggle").addEventListener("click", function () { '
        "const next = otherTheme(currentTheme()); applyTheme(next); storeTheme(next); });"
    ) in bindings


def test_without_a_pick_the_theme_follows_the_system() -> None:
    js = dashboard_assets.APP_JS
    assert (
        "return globalThis.matchMedia(DARK_QUERY).matches ? THEME_DARK : THEME_LIGHT;"
        in function_source(js, "systemTheme")
    )
    assert "document.documentElement.getAttribute(THEME_ATTRIBUTE) || systemTheme()" in (
        function_source(js, "currentTheme")
    )


def test_the_toggle_label_names_the_other_theme() -> None:
    source = function_source(dashboard_assets.APP_JS, "updateThemeToggle")
    assert 'toggle.textContent = "Switch to " + otherTheme(currentTheme()) + " theme";' in source


def test_apply_theme_sets_the_theme_attribute() -> None:
    source = function_source(dashboard_assets.APP_JS, "applyTheme")
    assert "document.documentElement.setAttribute(THEME_ATTRIBUTE, theme);" in source


def test_a_system_theme_change_refreshes_the_toggle_label() -> None:
    bindings = function_source(dashboard_assets.APP_JS, "bindControls")
    assert 'matchMedia(DARK_QUERY).addEventListener("change", updateThemeToggle)' in bindings
