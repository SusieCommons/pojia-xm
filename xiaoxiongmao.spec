# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

build/ 目录被 .gitignore 排除（构建产物不该进库），所以 icon 和
version_info 在这里按「存在才用」处理 —— 直接写死路径的话，clone 下来
第一次打包会因为找不到 build/icon.ico 直接失败，而报错信息是
"Unable to open icon file"，看不出是缺资源还是路径写错。
"""
import os

_HERE = os.path.abspath(os.getcwd())
_ICON = os.path.join(_HERE, "build", "icon.ico")
_VER = os.path.join(_HERE, "build", "version_info.txt")

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[('ui/index.html', 'ui'), ('persona.md', '.')],
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
    name='xiaoxiongmao',
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
    # 缺资源就不传，PyInstaller 用默认图标 / 不写版本资源，打包照样能过
    version=_VER if os.path.exists(_VER) else None,
    icon=[_ICON] if os.path.exists(_ICON) else None,
)
