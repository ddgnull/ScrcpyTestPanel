# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（macOS arm64，windowed）。

使用：
    python scripts/gen_build_info.py --version 0.0.1
    pyinstaller build_mac.spec --noconfirm
产物：
    dist/ScrcpyTestPanel.app
"""
import os

ROOT = os.path.abspath(os.getcwd())


def read_version() -> str:
    """优先取构建信息，其次从 constants.py 兜底解析。"""
    build_info = os.path.join(ROOT, "_build_info.py")
    if os.path.isfile(build_info):
        try:
            namespace: dict = {}
            with open(build_info, encoding="utf-8") as f:
                exec(f.read(), namespace)
            version = namespace.get("VERSION")
            if version:
                return str(version)
        except Exception:
            pass
    try:
        with open(os.path.join(ROOT, "constants.py"), encoding="utf-8") as f:
            for line in f:
                if line.startswith("VERSION") and "=" in line:
                    return line.split("=", 1)[1].strip().strip("\"'")
    except Exception:
        pass
    return "0.0.0"


VERSION = read_version()
HIDDEN = ["_build_info"] if os.path.isfile(os.path.join(ROOT, "_build_info.py")) else []

a = Analysis(
    ["main_qt.py"],
    pathex=[ROOT],
    binaries=[],
    datas=[],
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ScrcpyTestPanel",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="arm64",
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ScrcpyTestPanel",
)

app = BUNDLE(
    coll,
    name="ScrcpyTestPanel.app",
    icon=None,
    bundle_identifier="com.ddgnull.scrcpytestpanel",
    info_plist={
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "CFBundleDisplayName": "ScrcpyTestPanel",
        "CFBundleName": "ScrcpyTestPanel",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
        "LSApplicationCategoryType": "public.app-category.developer-tools",
    },
)
