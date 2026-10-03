from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from software_agent_factory.models import (
    DependencyEcosystem,
    RepositoryDependency,
    RepositoryProfile,
    RepositoryTechnology,
    ToolchainInventory,
    ToolchainLane,
    ToolchainProvider,
    ToolchainSlot,
    ToolchainSlotBinding,
)
from software_agent_factory.repository_profile import MAX_MANIFEST_BYTES, profile_repository
from software_agent_factory.toolchain import (
    LANE_SLOTS,
    PROVIDER_SIGNALS,
    degraded_toolchain_inventory,
    inventory_toolchain,
)

_POSIX_NON_ROOT = pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="needs POSIX permissions enforced for a non-root user",
)


def _inventory(root: Path) -> ToolchainInventory:
    return inventory_toolchain(root, profile_repository(root))


def _binding(
    inventory: ToolchainInventory, lane: ToolchainLane, slot: ToolchainSlot
) -> ToolchainSlotBinding:
    binding = inventory.binding(lane, slot)
    assert binding is not None, inventory.bindings
    return binding


def _write(root: Path, name: str, text: str) -> None:
    (root / name).write_text(text, encoding="utf-8")


@pytest.fixture
def restore_mode() -> Iterator[list[Path]]:
    paths: list[Path] = []
    yield paths
    for path in paths:
        path.chmod(0o755)


_REGISTRY_SLOTS = [(lane, slot) for lane, slots in LANE_SLOTS.items() for slot in slots]


@pytest.mark.parametrize(("lane", "slot"), _REGISTRY_SLOTS)
def test_registry_default_is_one_of_the_slot_providers(
    lane: ToolchainLane, slot: ToolchainSlot
) -> None:
    spec = LANE_SLOTS[lane][slot]

    assert spec.default_provider in spec.providers


@pytest.mark.parametrize(("lane", "slot"), _REGISTRY_SLOTS)
def test_registry_providers_all_have_detection_signals(
    lane: ToolchainLane, slot: ToolchainSlot
) -> None:
    missing = [p for p in LANE_SLOTS[lane][slot].providers if p not in PROVIDER_SIGNALS]

    assert missing == []


def test_repository_without_a_known_stack_has_no_lanes(tmp_path: Path) -> None:
    _write(tmp_path, "README.md", "# docs\n")

    inventory = _inventory(tmp_path)

    assert inventory.lanes == ()
    assert inventory.bindings == ()
    assert inventory.complete is True


def test_bare_python_repository_marks_every_slot_missing_with_defaults(tmp_path: Path) -> None:
    _write(tmp_path, "main.py", "print('hi')\n")

    inventory = _inventory(tmp_path)

    assert inventory.lanes == (ToolchainLane.PYTHON,)
    assert all(binding.is_missing for binding in inventory.bindings)
    assert {b.slot: b.default_provider for b in inventory.bindings} == {
        ToolchainSlot.FORMAT: ToolchainProvider.RUFF,
        ToolchainSlot.LINT: ToolchainProvider.RUFF,
        ToolchainSlot.TYPECHECK: ToolchainProvider.MYPY,
        ToolchainSlot.TEST: ToolchainProvider.PYTEST,
    }


def test_configured_python_tools_are_kept_with_their_evidence(tmp_path: Path) -> None:
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

    assert {b.slot: (b.provider, b.evidence) for b in inventory.bindings} == {
        ToolchainSlot.FORMAT: (
            ToolchainProvider.RUFF,
            ("dependency:ruff", "pyproject.toml:tool.ruff"),
        ),
        ToolchainSlot.LINT: (
            ToolchainProvider.RUFF,
            ("dependency:ruff", "pyproject.toml:tool.ruff"),
        ),
        ToolchainSlot.TYPECHECK: (ToolchainProvider.MYPY, ("pyproject.toml:tool.mypy",)),
        ToolchainSlot.TEST: (
            ToolchainProvider.PYTEST,
            ("dependency:pytest", "test-tool:pytest"),
        ),
    }


def test_existing_black_and_flake8_are_not_replaced_by_ruff(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", '[project]\nname = "x"\n\n[tool.black]\n')
    _write(tmp_path, "setup.cfg", "[flake8]\nmax-line-length = 100\n")

    inventory = _inventory(tmp_path)

    format_binding = _binding(inventory, ToolchainLane.PYTHON, ToolchainSlot.FORMAT)
    assert format_binding.provider is ToolchainProvider.BLACK
    assert format_binding.default_provider is ToolchainProvider.RUFF
    lint = _binding(inventory, ToolchainLane.PYTHON, ToolchainSlot.LINT)
    assert lint.provider is ToolchainProvider.FLAKE8
    assert lint.evidence == ("setup.cfg:[flake8]",)


@pytest.mark.parametrize(
    ("files", "lane", "slot", "expected"),
    [
        (
            {"pyproject.toml": '[project]\nname = "x"\n[tool.black]\n[tool.ruff]\n'},
            ToolchainLane.PYTHON,
            ToolchainSlot.FORMAT,
            ToolchainProvider.BLACK,
        ),
        (
            {"pyproject.toml": '[project]\nname = "x"\n[tool.ruff]\n', ".flake8": ""},
            ToolchainLane.PYTHON,
            ToolchainSlot.LINT,
            ToolchainProvider.RUFF,
        ),
        (
            {"package.json": '{"name": "x", "prettier": {}}', "biome.json": "{}"},
            ToolchainLane.JAVASCRIPT,
            ToolchainSlot.FORMAT,
            ToolchainProvider.BIOME,
        ),
        (
            {
                "package.json": '{"name": "x", "eslintConfig": {}}',
                "biome.json": "{}",
                "oxlint.json": "{}",
            },
            ToolchainLane.JAVASCRIPT,
            ToolchainSlot.LINT,
            ToolchainProvider.ESLINT,
        ),
        (
            {"package.json": '{"name": "x", "devDependencies": {"vitest": "3", "jest": "29"}}'},
            ToolchainLane.JAVASCRIPT,
            ToolchainSlot.TEST,
            ToolchainProvider.VITEST,
        ),
    ],
)
def test_first_provider_with_evidence_wins(
    tmp_path: Path,
    files: dict[str, str],
    lane: ToolchainLane,
    slot: ToolchainSlot,
    expected: ToolchainProvider,
) -> None:
    for name, text in files.items():
        _write(tmp_path, name, text)

    inventory = _inventory(tmp_path)

    assert _binding(inventory, lane, slot).provider is expected


@pytest.mark.parametrize(
    ("dev_dependency", "slot", "expected"),
    [
        ("@biomejs/biome", ToolchainSlot.FORMAT, ToolchainProvider.BIOME),
        ("prettier", ToolchainSlot.FORMAT, ToolchainProvider.PRETTIER),
        ("eslint", ToolchainSlot.LINT, ToolchainProvider.ESLINT),
        ("oxlint", ToolchainSlot.LINT, ToolchainProvider.OXLINT),
        ("jest", ToolchainSlot.TEST, ToolchainProvider.JEST),
    ],
)
def test_javascript_dev_dependency_is_evidence(
    tmp_path: Path, dev_dependency: str, slot: ToolchainSlot, expected: ToolchainProvider
) -> None:
    _write(
        tmp_path,
        "package.json",
        json.dumps({"name": "x", "devDependencies": {dev_dependency: "1.0.0"}}),
    )

    binding = _binding(_inventory(tmp_path), ToolchainLane.JAVASCRIPT, slot)

    assert binding.provider is expected
    assert binding.evidence == (f"dependency:{dev_dependency}",)


def test_javascript_without_typescript_has_no_typecheck_slot(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "package.json",
        json.dumps({"name": "x", "devDependencies": {"vitest": "^3.0.0"}, "prettier": {}}),
    )

    inventory = _inventory(tmp_path)

    assert inventory.lanes == (ToolchainLane.JAVASCRIPT,)
    assert inventory.binding(ToolchainLane.JAVASCRIPT, ToolchainSlot.TYPECHECK) is None
    format_binding = _binding(inventory, ToolchainLane.JAVASCRIPT, ToolchainSlot.FORMAT)
    assert format_binding.evidence == ("package.json:prettier",)
    lint = _binding(inventory, ToolchainLane.JAVASCRIPT, ToolchainSlot.LINT)
    assert lint.is_missing
    assert lint.default_provider is ToolchainProvider.OXLINT


def test_typescript_repository_binds_eslint_and_tsc_from_config_files(tmp_path: Path) -> None:
    _write(tmp_path, "package.json", json.dumps({"name": "x"}))
    _write(tmp_path, "tsconfig.json", "{}")
    _write(tmp_path, "eslint.config.mjs", "export default []\n")
    _write(tmp_path, "index.ts", "export const x = 1\n")

    inventory = _inventory(tmp_path)

    lint = _binding(inventory, ToolchainLane.JAVASCRIPT, ToolchainSlot.LINT)
    assert lint.provider is ToolchainProvider.ESLINT
    assert lint.evidence == ("file:eslint.config.mjs",)
    typecheck = _binding(inventory, ToolchainLane.JAVASCRIPT, ToolchainSlot.TYPECHECK)
    assert typecheck.evidence == ("file:tsconfig.json",)


def test_mixed_repository_gets_both_lanes(tmp_path: Path) -> None:
    _write(tmp_path, "app.py", "x = 1\n")
    _write(tmp_path, "package.json", json.dumps({"name": "x"}))

    inventory = _inventory(tmp_path)

    assert inventory.lanes == (ToolchainLane.PYTHON, ToolchainLane.JAVASCRIPT)


@pytest.mark.parametrize(
    ("name", "text", "warning"),
    [
        ("pyproject.toml", "[project\n", "invalid pyproject.toml: TOMLDecodeError"),
        ("package.json", "{", "invalid package.json: JSONDecodeError"),
        ("setup.cfg", "no section header\n", "invalid setup.cfg: MissingSectionHeaderError"),
        ("package.json", "1" * 5000, "invalid package.json: ValueError"),
    ],
)
def test_hostile_config_records_a_warning_without_its_content(
    tmp_path: Path, name: str, text: str, warning: str
) -> None:
    _write(tmp_path, "app.py", "x = 1\n")
    _write(tmp_path, name, text)

    inventory = inventory_toolchain(tmp_path, _profile_with_python())

    assert warning in inventory.warnings


def test_deeply_nested_config_never_raises(tmp_path: Path) -> None:
    # Python 3.13 raises RecursionError here; Python 3.14 parses it. Either way
    # the inventory must come back, with at most a type-only warning.
    _write(tmp_path, "package.json", "[" * 100_000 + "]" * 100_000)

    inventory = inventory_toolchain(tmp_path, _profile_with_python())

    assert inventory.warnings in ((), ("invalid package.json: RecursionError",))


def test_non_utf8_config_records_a_warning(tmp_path: Path) -> None:
    _write(tmp_path, "app.py", "x = 1\n")
    (tmp_path / "setup.cfg").write_bytes(b"\xff\xfe[flake8]\n")

    inventory = inventory_toolchain(tmp_path, _profile_with_python())

    assert "could not read setup.cfg: UnicodeDecodeError" in inventory.warnings


def test_package_json_that_is_not_an_object_gives_no_evidence(tmp_path: Path) -> None:
    _write(tmp_path, "package.json", "[]")

    inventory = _inventory(tmp_path)

    assert inventory.complete is False
    assert inventory.warnings == ("invalid manifest",)
    assert all(binding.is_missing for binding in inventory.bindings)


def test_oversized_config_is_skipped_with_a_warning(tmp_path: Path) -> None:
    _write(tmp_path, "app.py", "x = 1\n")
    _write(tmp_path, "setup.cfg", "[flake8]\n" + "#" * MAX_MANIFEST_BYTES)

    inventory = inventory_toolchain(tmp_path, _profile_with_python())

    assert "skipped oversized config: setup.cfg" in inventory.warnings
    assert inventory.complete is False
    assert _binding(inventory, ToolchainLane.PYTHON, ToolchainSlot.LINT).is_missing


def test_symlinked_config_is_not_evidence(tmp_path: Path) -> None:
    outside = tmp_path / "outside.cfg"
    outside.write_text("[flake8]\n", encoding="utf-8")
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "app.py", "x = 1\n")
    (root / "setup.cfg").symlink_to(outside)
    (root / ".flake8").symlink_to(outside)

    inventory = _inventory(root)

    assert _binding(inventory, ToolchainLane.PYTHON, ToolchainSlot.LINT).is_missing
    assert inventory.warnings == ()


@_POSIX_NON_ROOT
def test_unreadable_config_records_a_warning(tmp_path: Path, restore_mode: list[Path]) -> None:
    _write(tmp_path, "app.py", "x = 1\n")
    config = tmp_path / "setup.cfg"
    config.write_text("[flake8]\n", encoding="utf-8")
    config.chmod(0)
    restore_mode.append(config)

    inventory = inventory_toolchain(tmp_path, _profile_with_python())

    assert "could not read setup.cfg: PermissionError" in inventory.warnings


@_POSIX_NON_ROOT
def test_unlistable_root_records_a_warning_and_keeps_bindings(
    tmp_path: Path, restore_mode: list[Path]
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write(root, "app.py", "x = 1\n")
    profile = profile_repository(root)
    root.chmod(0o300)
    restore_mode.append(root)

    inventory = inventory_toolchain(root, profile)

    assert inventory.warnings == ("could not list repository root: PermissionError",)
    assert inventory.complete is False
    assert all(binding.is_missing for binding in inventory.bindings)


def test_missing_root_yields_bindings_without_file_evidence(tmp_path: Path) -> None:
    inventory = inventory_toolchain(tmp_path / "gone", _profile_with_python())

    assert inventory.lanes == (ToolchainLane.PYTHON,)
    assert all(binding.is_missing for binding in inventory.bindings)


@pytest.mark.parametrize(
    ("warning", "copied"),
    [
        ("scan limit reached after 20000 files", "scan limit reached"),
        ("dependency evidence limited to 200 declarations", "dependency evidence limited"),
        ("repository profiling degraded: <repo text>", "repository profiling degraded"),
        ("invalid manifest package.json: <repo text>", "invalid manifest"),
        ("could not read pyproject.toml: <repo text>", "could not read"),
        ("skipped oversized manifest: package.json", "skipped oversized manifest"),
        (
            "ignored dependency declaration outside profile limits in pyproject.toml: x",
            "ignored dependency declaration outside profile limits",
        ),
    ],
)
def test_incomplete_profile_evidence_marks_the_inventory_incomplete(
    tmp_path: Path, warning: str, copied: str
) -> None:
    profile = _profile_with_python().model_copy(update={"warnings": (warning, "other")})

    inventory = inventory_toolchain(tmp_path, profile)

    assert inventory.complete is False
    assert inventory.warnings == (copied,)


def test_degraded_inventory_is_empty_and_incomplete() -> None:
    inventory = degraded_toolchain_inventory("toolchain inventory degraded: OSError")

    assert inventory.lanes == ()
    assert inventory.complete is False


def test_bound_provider_without_evidence_is_rejected() -> None:
    with pytest.raises(ValidationError, match="needs evidence"):
        ToolchainSlotBinding(
            lane=ToolchainLane.PYTHON,
            slot=ToolchainSlot.LINT,
            provider=ToolchainProvider.RUFF,
            default_provider=ToolchainProvider.RUFF,
        )


def test_missing_provider_with_evidence_is_rejected() -> None:
    with pytest.raises(ValidationError, match="needs evidence"):
        ToolchainSlotBinding(
            lane=ToolchainLane.PYTHON,
            slot=ToolchainSlot.LINT,
            default_provider=ToolchainProvider.RUFF,
            evidence=("dependency:ruff",),
        )


def test_inventory_rejects_duplicate_slots_and_unknown_lanes() -> None:
    binding = ToolchainSlotBinding(
        lane=ToolchainLane.PYTHON,
        slot=ToolchainSlot.LINT,
        default_provider=ToolchainProvider.RUFF,
    )

    with pytest.raises(ValidationError, match="only one binding"):
        ToolchainInventory(lanes=(ToolchainLane.PYTHON,), bindings=(binding, binding))
    with pytest.raises(ValidationError, match="inventory lane"):
        ToolchainInventory(lanes=(), bindings=(binding,))


def test_inventory_round_trips_as_json(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", '[project]\nname = "x"\n\n[tool.ruff]\n')

    inventory = _inventory(tmp_path)

    assert ToolchainInventory.model_validate_json(inventory.model_dump_json()) == inventory


def _profile_with_python() -> RepositoryProfile:
    return RepositoryProfile(
        manifest_fingerprint="0" * 64,
        dependency_fingerprint="0" * 64,
        technologies=(RepositoryTechnology.PYTHON,),
        dependencies=(
            RepositoryDependency(
                ecosystem=DependencyEcosystem.PYTHON,
                name="requests",
                declared_version=">=2",
                manifest_path="pyproject.toml",
                group="runtime",
            ),
        ),
    )


@pytest.mark.parametrize(
    "warning",
    [
        "invalid lockfile uv.lock: <repo text>",
        "skipped oversized manifest: web/package-lock.json",
        "could not read pnpm-lock.yaml: <repo text>",
    ],
)
def test_lockfile_warning_keeps_the_inventory_complete(tmp_path: Path, warning: str) -> None:
    profile = _profile_with_python().model_copy(update={"warnings": (warning,)})

    inventory = inventory_toolchain(tmp_path, profile)

    assert inventory.complete is True
    assert inventory.warnings == ()


def test_only_known_package_scripts_are_recorded(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "package.json",
        json.dumps(
            {
                "name": "x",
                "scripts": {
                    "test": "vitest run",
                    "lint": 1,
                    "deploy": "rm -rf /",
                    "type-check": "tsc",
                },
            }
        ),
    )

    inventory = _inventory(tmp_path)

    assert inventory.package_json_scripts == ("test", "type-check")


@pytest.mark.parametrize(
    ("name", "text", "self_targeting"),
    [
        ("pyproject.toml", '[project]\nname = "x"\n[tool.mypy]\nfiles = ["src"]\n', True),
        ("pyproject.toml", '[project]\nname = "x"\n[tool.mypy]\nstrict = true\n', False),
        ("mypy.ini", "[mypy]\nfiles = src\n", True),
        ("setup.cfg", "[mypy]\nstrict = True\n", False),
        ("tox.ini", "[mypy]\nfiles = src\n", False),
    ],
)
def test_mypy_config_with_files_is_self_targeting(
    tmp_path: Path, name: str, text: str, self_targeting: bool
) -> None:
    _write(tmp_path, "app.py", "x = 1\n")
    _write(tmp_path, name, text)

    inventory = _inventory(tmp_path)

    assert (ToolchainProvider.MYPY in inventory.self_targeting_providers) is self_targeting
    assert _binding(inventory, ToolchainLane.PYTHON, ToolchainSlot.TYPECHECK).provider is (
        ToolchainProvider.MYPY
    )


@pytest.mark.parametrize("scripts", [None, [], "test"])
def test_package_json_without_a_script_table_records_no_scripts(
    tmp_path: Path, scripts: object
) -> None:
    payload: dict[str, object] = {"name": "x"}
    if scripts is not None:
        payload["scripts"] = scripts
    _write(tmp_path, "package.json", json.dumps(payload))

    assert _inventory(tmp_path).package_json_scripts == ()


def test_mypy_uses_only_its_first_configuration_file(tmp_path: Path) -> None:
    _write(tmp_path, "pyproject.toml", '[project]\nname = "x"\n[tool.mypy]\nstrict = true\n')
    _write(tmp_path, "setup.cfg", "[mypy]\nfiles = src\n")

    inventory = _inventory(tmp_path)

    assert inventory.self_targeting_providers == ()


def test_mypy_ini_without_a_mypy_section_still_wins(tmp_path: Path) -> None:
    _write(tmp_path, "app.py", "x = 1\n")
    _write(tmp_path, "mypy.ini", "[mypy-vendor.*]\nignore_errors = True\n")
    _write(tmp_path, "setup.cfg", "[mypy]\nfiles = src\n")

    assert _inventory(tmp_path).self_targeting_providers == ()


@pytest.mark.parametrize(
    ("files", "lanes"),
    [
        (
            {"pyproject.toml": '[project]\nname = "x"\n[tool.mutmut]\npaths_to_mutate = "src"\n'},
            (ToolchainLane.PYTHON,),
        ),
        (
            {"setup.cfg": "[mutmut]\npaths_to_mutate = src\n", "app.py": "x = 1\n"},
            (ToolchainLane.PYTHON,),
        ),
        (
            {"package.json": '{"name": "x"}', "stryker.config.mjs": "export default {}\n"},
            (ToolchainLane.JAVASCRIPT,),
        ),
        (
            {"package.json": '{"name": "x", "devDependencies": {"@stryker-mutator/core": "8"}}'},
            (ToolchainLane.JAVASCRIPT,),
        ),
        ({"app.py": "x = 1\n"}, ()),
        ({"package.json": '{"name": "x"}'}, ()),
        (
            {"pyproject.toml": '[project]\nname = "x"\n[dependency-groups]\ndev = ["mutmut"]\n'},
            (ToolchainLane.PYTHON,),
        ),
        (
            {"app.py": "x = 1\n", "package.json": '{"name": "x"}', "stryker.conf.json": "{}"},
            (ToolchainLane.JAVASCRIPT,),
        ),
    ],
)
def test_mutation_tool_is_found_by_dependency_or_configuration(
    tmp_path: Path, files: dict[str, str], lanes: tuple[ToolchainLane, ...]
) -> None:
    for name, text in files.items():
        _write(tmp_path, name, text)

    assert _inventory(tmp_path).mutation_tool_lanes == lanes


def test_pnpm_workspace_root_is_recorded(tmp_path: Path) -> None:
    _write(tmp_path, "package.json", '{"name": "x"}')
    _write(tmp_path, "pnpm-workspace.yaml", "packages: ['apps/*']\n")

    assert _inventory(tmp_path).pnpm_workspace is True


def test_package_json_alone_is_not_a_pnpm_workspace(tmp_path: Path) -> None:
    _write(tmp_path, "package.json", '{"name": "x"}')

    assert _inventory(tmp_path).pnpm_workspace is False


def test_yarnrc_yml_marks_a_yarn_berry_project(tmp_path: Path) -> None:
    _write(tmp_path, "package.json", '{"name": "x"}')
    _write(tmp_path, ".yarnrc.yml", "nodeLinker: node-modules\n")

    assert _inventory(tmp_path).yarn_berry is True


def test_a_plain_javascript_project_is_not_yarn_berry(tmp_path: Path) -> None:
    _write(tmp_path, "package.json", '{"name": "x"}')

    assert _inventory(tmp_path).yarn_berry is False
