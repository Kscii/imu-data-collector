# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import os
import sys

from PyInstaller.utils.hooks import collect_submodules


root = Path(SPEC).resolve().parents[1]
config_dir = Path(os.environ.get("IMU_DESKTOP_CONFIG_DIR", root / "configs")).resolve()
datas = [
    (str(root / "frontend" / "dist-capture"), "frontend/dist-capture"),
    (str(config_dir), "configs"),
    (str(root / "LICENSE"), "."),
    (str(root / "THIRD_PARTY_NOTICES.md"), "."),
]
if sys.platform == "darwin":
    datas.extend(
        [
            (str(root / "packaging" / "macos" / "zh-Hans.lproj" / "InfoPlist.strings"), "zh-Hans.lproj"),
            (str(root / "packaging" / "macos" / "en.lproj" / "InfoPlist.strings"), "en.lproj"),
        ]
    )
ffmpeg = root / "third_party" / "ffmpeg"
if ffmpeg.is_dir():
    datas.append((str(ffmpeg), "third_party/ffmpeg"))

# build_info.py 需要与 Vite 构建时读取完全相同的源文件，才能继续阻止前后端混版。
for source in (
    "capture_api.py",
    "coordinator.py",
    "models.py",
    "annotation_api.py",
    "annotation_service.py",
    "annotation_catalog.py",
    "taxonomy_store.py",
):
    datas.append((str(root / "src" / "imu_data_collector" / source), "imu_data_collector"))

bleak_backend = (
    "bleak.backends.winrt"
    if sys.platform.startswith("win")
    else "bleak.backends.corebluetooth"
    if sys.platform == "darwin"
    else "bleak.backends.bluezdbus"
)
hiddenimports = collect_submodules(bleak_backend)
# h5py 使用 PyInstaller 官方 hook，避免把它自己的测试套件打入安装包。
# keyring 同样已有官方 hook；只有各平台实际可导入的凭据后端会进入安装包。

a_cli = Analysis(
    [str(root / "packaging" / "entrypoint.py")],
    pathex=[str(root / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz_cli = PYZ(a_cli.pure)

cli_exe = EXE(
    pyz_cli,
    a_cli.scripts,
    [],
    exclude_binaries=True,
    name="imu-collector",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=sys.platform.startswith("win"),
    console=True,
    disable_windowed_traceback=False,
)

collect_items = [cli_exe, a_cli.binaries, a_cli.datas]

if sys.platform.startswith("win") or sys.platform == "darwin":
    a_tray = Analysis(
        [str(root / "packaging" / "tray_entrypoint.py")],
        pathex=[str(root / "src")],
        binaries=[],
        datas=datas,
        hiddenimports=[
            *hiddenimports,
            "pystray._win32" if sys.platform.startswith("win") else "pystray._darwin",
        ],
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[],
        excludes=[],
        noarchive=False,
        optimize=0,
    )
    pyz_tray = PYZ(a_tray.pure)
    tray_exe = EXE(
        pyz_tray,
        a_tray.scripts,
        [],
        exclude_binaries=True,
        name="imu-data-collector" if sys.platform.startswith("win") else "IMU Data Collector",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=sys.platform.startswith("win"),
        console=False,
        disable_windowed_traceback=False,
    )
    # 两个 onedir 程序共用一个 _internal，避免重复打包 Python、HDF5 和 FFmpeg。
    tray_items = [tray_exe, a_tray.binaries, a_tray.datas]
    collect_items = (
        [*tray_items, *collect_items]
        if sys.platform == "darwin"
        else [*collect_items, *tray_items]
    )

coll = COLLECT(
    *collect_items,
    strip=False,
    upx=sys.platform.startswith("win"),
    name="IMU Data Collector" if sys.platform == "darwin" else "imu-collector",
)

if sys.platform == "darwin":
    bundle_version = os.environ.get("APP_BUNDLE_VERSION", "0.2.0")
    app = BUNDLE(
        coll,
        name="IMU Data Collector.app",
        icon=str(root / "packaging" / "macos" / "imu-data-collector.icns"),
        bundle_identifier="tech.kscii.imu-data-collector",
        info_plist={
            "CFBundleDisplayName": "IMU Data Collector",
            "CFBundleName": "IMU Data Collector",
            # Apple 的展示版本要求点分整数；开发构建的完整版本仍保留在 DMG 文件名。
            "CFBundleShortVersionString": bundle_version,
            "CFBundleVersion": bundle_version,
            "LSMinimumSystemVersion": "13.0",
            "LSUIElement": True,
            "NSHighResolutionCapable": True,
            "NSCameraUsageDescription": (
                "IMU Data Collector needs camera access to record video aligned with IMU samples."
            ),
            "NSBluetoothAlwaysUsageDescription": (
                "IMU Data Collector needs Bluetooth access to receive CW12EU-T sensor samples."
            ),
            "NSBluetoothPeripheralUsageDescription": (
                "IMU Data Collector needs Bluetooth access to receive CW12EU-T sensor samples."
            ),
        },
    )
