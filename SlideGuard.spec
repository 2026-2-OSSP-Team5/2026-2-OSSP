# -*- mode: python ; coding: utf-8 -*-

import os
import sys
from pathlib import Path

# Keep direct spec builds consistent with scripts/build.ps1. Qt must resolve
# Windows ICU dependencies from System32 instead of unrelated tools on PATH.
if sys.platform == 'win32':
    system_root = Path(os.environ['SystemRoot'])
    os.environ['PATH'] = os.pathsep.join(
        (str(Path(sys.executable).parent), str(system_root / 'System32'), str(system_root))
    )

a = Analysis(
    ['scripts/desktop_entry.py'],
    pathex=['src'],
    binaries=[],
    datas=[],
    hiddenimports=[],
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
    name='SlideGuard',
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
    name='SlideGuard',
)
