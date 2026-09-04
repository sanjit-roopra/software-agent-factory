# -*- mode: python ; coding: utf-8 -*-

from __future__ import annotations

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

# PyInstaller sets ``SPECPATH`` to the directory containing this spec file.
project_root = Path(SPECPATH).resolve().parent
package_root = project_root / "src" / "software_agent_factory"

bundle_name = os.environ.get("SOFTWARE_AGENT_FACTORY_BUNDLE_NAME", "software-agent-factory")
executable_name = os.environ.get("SOFTWARE_AGENT_FACTORY_EXECUTABLE_NAME", "factory")

package_data = [
    (str(package_root / "default_config.yaml"), "software_agent_factory"),
]

build_info_path = package_root / "build-info.json"
if not build_info_path.is_file():
    raise SystemExit(
        "Expected src/software_agent_factory/build-info.json before running PyInstaller."
    )
package_data.append((str(build_info_path), "software_agent_factory"))

# The read-only dashboard (Phase 15.11) is deliberately asset-free: its HTML,
# CSS and JS are Python string constants in ``dashboard/assets.py``, so there
# is no static directory, no bundler and no build step to bundle here.
hiddenimports = collect_submodules("software_agent_factory")

analysis = Analysis(
    [str(package_root / "__main__.py")],
    pathex=[str(project_root / "src")],
    binaries=[],
    datas=package_data,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name=executable_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=bundle_name,
)
