# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: PC-1251 エミュレータの単体実行バイナリ

使用方法:
    uv run pyinstaller sc61box.spec

出力:
    dist/sc61box  (Windows では dist/sc61box.exe)
"""

a = Analysis(
    ['tools/sc61box_entry.py'],
    pathex=[],
    binaries=[],
    datas=[
        # 筐体・液晶の画像とキー配置データ
        ('sc61box/assets', 'sc61box/assets'),
        # 付属の見本プログラム(.bas/.hex)
        ('programs', 'programs'),
    ],
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
    a.binaries,
    a.datas,
    [],
    name='sc61box',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
