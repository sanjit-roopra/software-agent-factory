from __future__ import annotations

import json
from pathlib import Path

import pytest

from software_agent_factory.models import (
    ToolchainInventory,
    ToolchainLane,
    ToolchainProvider,
    ToolchainSlot,
    ToolchainSlotBinding,
)
from software_agent_factory.repository_profile import profile_repository
from software_agent_factory.toolchain import (
    LANE_SLOTS,
    PROVIDER_SIGNALS,
    inventory_toolchain,
)


def _inventory(root: Path) -> ToolchainInventory:
    return inventory_toolchain(root, profile_repository(root))


def _binding(
    inventory: ToolchainInventory, lane: ToolchainLane, slot: ToolchainSlot
) -> ToolchainSlotBinding:
    matches = [b for b in inventory.bindings if b.lane is lane and b.slot is slot]
    assert len(matches) == 1, inventory.bindings
    return matches[0]


def _write(root: Path, name: str, text: str) -> None:
    (root / name).write_text(text, encoding="utf-8")


def test_every_slot_default_is_one_of_its_providers_and_has_signals() -> None:
    for slots in LANE_SLOTS.values():
        for spec in slots.values():
            assert spec.default in spec.providers
            assert all(provider in PROVIDER_SIGNALS for provider in spec.providers)


def test_repository_without_a_known_stack_has_no_lanes(tmp_path: Path) -> None:
    _write(tmp_path, "README.md", "# docs\n")

    inventory = _inventory(tmp_path)

    assert inventory.lanes == ()
    assert inventory.bindings == ()


def test_bare_python_repository_marks_every_slot_missing_with_defaults(tmp_path: Path) -> None:
    _write(tmp_path, "main.py", "print('hi')\n")

    inventory = _inventory(tmp_path)

    assert inventory.lanes == (ToolchainLane.PYTHON,)
    assert all(binding.missing for binding in inventory.bindings)
    assert {b.slot: b.default_provider for b in inventory.bindings} == {
        ToolchainSlot.FORMAT: ToolchainProvider.RUFF,
        ToolchainSlot.LINT: ToolchainProvider.RUFF,
        ToolchainSlot.TYPECHECK: ToolchainProvider.MYPY,
        ToolchainSlot.TEST: ToolchainProvider.PYTEST,
    }


def test_configured_python_tools_are_kept(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "pyproject.toml",
        """
[project]
name = "example"

[dependency-groups]
dev = ["pytest>=9", "ruff>=0.12"]

[tool.ruff]
line-length = 100

[tool.mypy]
strict = true
""",
    )

    inventory = _inventory(tmp_path)

    lint = _binding(inventory, ToolchainLane.PYTHON, ToolchainSlot.LINT)
    assert lint.provider is ToolchainProvider.RUFF
    assert lint.evidence == ("dependency:ruff", "pyproject:tool.ruff")
    assert (
        _binding(inventory, ToolchainLane.PYTHON, ToolchainSlot.FORMAT).provider
        is ToolchainProvider.RUFF
    )
    typecheck = _binding(inventory, ToolchainLane.PYTHON, ToolchainSlot.TYPECHECK)
    assert typecheck.provider is ToolchainProvider.MYPY
    assert typecheck.evidence == ("pyproject:tool.mypy",)
    assert (
        _binding(inventory, ToolchainLane.PYTHON, ToolchainSlot.TEST).provider
        is ToolchainProvider.PYTEST
    )
    assert not any(binding.missing for binding in inventory.bindings)


def test_existing_black_and_flake8_are_not_replaced_by_ruff(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", '[project]\nname = "x"\n\n[tool.black]\n')
    _write(tmp_path, "setup.cfg", "[flake8]\nmax-line-length = 100\n")

    inventory = _inventory(tmp_path)

    format_binding = _binding(inventory, ToolchainLane.PYTHON, ToolchainSlot.FORMAT)
    assert format_binding.provider is ToolchainProvider.BLACK
    assert format_binding.default_provider is ToolchainProvider.RUFF
    lint = _binding(inventory, ToolchainLane.PYTHON, ToolchainSlot.LINT)
    assert lint.provider is ToolchainProvider.FLAKE8
    assert lint.evidence == ("ini:[flake8]",)


def test_javascript_without_typescript_has_no_typecheck_slot(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "package.json",
        json.dumps({"name": "x", "devDependencies": {"vitest": "^3.0.0"}, "prettier": {}}),
    )

    inventory = _inventory(tmp_path)

    assert inventory.lanes == (ToolchainLane.JAVASCRIPT,)
    assert ToolchainSlot.TYPECHECK not in {b.slot for b in inventory.bindings}
    format_binding = _binding(inventory, ToolchainLane.JAVASCRIPT, ToolchainSlot.FORMAT)
    assert format_binding.provider is ToolchainProvider.PRETTIER
    assert format_binding.evidence == ("package.json:prettier",)
    lint = _binding(inventory, ToolchainLane.JAVASCRIPT, ToolchainSlot.LINT)
    assert lint.missing
    assert lint.default_provider is ToolchainProvider.OXLINT
    assert (
        _binding(inventory, ToolchainLane.JAVASCRIPT, ToolchainSlot.TEST).provider
        is ToolchainProvider.VITEST
    )


def test_typescript_repository_binds_eslint_and_tsc_from_config_files(tmp_path: Path) -> None:
    _write(tmp_path, "package.json", json.dumps({"name": "x"}))
    _write(tmp_path, "tsconfig.json", "{}")
    _write(tmp_path, "eslint.config.mjs", "export default []\n")
    _write(tmp_path, "index.ts", "export const x = 1\n")

    inventory = _inventory(tmp_path)

    lint = _binding(inventory, ToolchainLane.JAVASCRIPT, ToolchainSlot.LINT)
    assert lint.provider is ToolchainProvider.ESLINT
    assert lint.evidence == ("file:eslint.config.mjs",)
    assert (
        _binding(inventory, ToolchainLane.JAVASCRIPT, ToolchainSlot.TYPECHECK).provider
        is ToolchainProvider.TSC
    )


def test_mixed_repository_gets_both_lanes(tmp_path: Path) -> None:
    _write(tmp_path, "app.py", "x = 1\n")
    _write(tmp_path, "package.json", json.dumps({"name": "x"}))

    inventory = _inventory(tmp_path)

    assert inventory.lanes == (ToolchainLane.PYTHON, ToolchainLane.JAVASCRIPT)


@pytest.mark.parametrize(
    ("name", "text", "fragment"),
    [
        ("pyproject.toml", "[project\n", "invalid pyproject.toml"),
        ("package.json", "{", "invalid package.json"),
        ("setup.cfg", "no section header\n", "invalid setup.cfg"),
    ],
)
def test_invalid_config_records_a_warning(
    tmp_path: Path, name: str, text: str, fragment: str
) -> None:
    _write(tmp_path, "app.py", "x = 1\n")
    _write(tmp_path, name, text)

    inventory = _inventory(tmp_path)

    assert any(fragment in warning for warning in inventory.warnings), inventory.warnings


def test_oversized_config_is_skipped_with_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from software_agent_factory import toolchain

    monkeypatch.setattr(toolchain, "MAX_CONFIG_BYTES", 4)
    _write(tmp_path, "app.py", "x = 1\n")
    _write(tmp_path, "setup.cfg", "[flake8]\n")

    inventory = _inventory(tmp_path)

    assert "skipped oversized config: setup.cfg" in inventory.warnings
    assert _binding(inventory, ToolchainLane.PYTHON, ToolchainSlot.LINT).missing


def test_symlinked_config_is_ignored(tmp_path: Path) -> None:
    outside = tmp_path / "outside.cfg"
    outside.write_text("[flake8]\n", encoding="utf-8")
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "app.py", "x = 1\n")
    (root / "setup.cfg").symlink_to(outside)

    inventory = _inventory(root)

    assert _binding(inventory, ToolchainLane.PYTHON, ToolchainSlot.LINT).missing


def test_missing_root_yields_bindings_without_file_evidence(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "app.py", "x = 1\n")
    profile = profile_repository(root)

    inventory = inventory_toolchain(tmp_path / "gone", profile)

    assert inventory.lanes == (ToolchainLane.PYTHON,)
    assert all(binding.missing for binding in inventory.bindings)


def test_inventory_round_trips_as_json(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", '[project]\nname = "x"\n\n[tool.ruff]\n')

    inventory = _inventory(tmp_path)

    assert ToolchainInventory.model_validate_json(inventory.model_dump_json()) == inventory
