# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


project_root = Path(SPECPATH).parent
fixture = project_root / "examples" / "fixtures" / "yituliu_full_roster_maxed.xlsx"
data_cache = project_root / "data" / "cache"
required_data_files = (
    "building_data.json",
    "character_table.json",
    "item_table.json",
    "data_version.txt",
)

datas = []
if fixture.is_file():
    datas.append((str(fixture), "examples/fixtures"))
for file_name in required_data_files:
    data_file = data_cache / file_name
    if data_file.is_file():
        datas.append((str(data_file), "data/cache"))

a = Analysis(
    [str(project_root / "tools" / "pyinstaller_entry.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=collect_submodules("openpyxl"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ArknightsScheduleUI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="ArknightsScheduleUI",
)
