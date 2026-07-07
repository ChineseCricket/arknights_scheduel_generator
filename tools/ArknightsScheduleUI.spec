# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys

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
binaries = []
if fixture.is_file():
    datas.append((str(fixture), "examples/fixtures"))
for file_name in required_data_files:
    data_file = data_cache / file_name
    if data_file.is_file():
        datas.append((str(data_file), "data/cache"))
python_library_bin = Path(sys.prefix) / "Library" / "bin"
for dll_name in ("libssl-3-x64.dll", "libcrypto-3-x64.dll"):
    dll_path = python_library_bin / dll_name
    if dll_path.is_file():
        binaries.append((str(dll_path), "."))

hiddenimports = [
    "http.client",
    "ssl",
    "urllib.error",
    "urllib.request",
]
hiddenimports.extend(collect_submodules("openpyxl"))

a = Analysis(
    [str(project_root / "tools" / "pyinstaller_entry.py")],
    pathex=[str(project_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
