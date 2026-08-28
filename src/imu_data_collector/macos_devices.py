"""macOS 原生权限与 AVFoundation 摄像头能力枚举。

模块只能在 macOS 调用；PyObjC 导入保持在函数内部，使 Linux/Windows
开发、测试和打包不会意外加载 Apple 框架。
"""

from __future__ import annotations

import threading
from typing import Any

CAMERA_SETTINGS_PATH_ZH = "系统设置 → 隐私与安全性 → 相机"
BLUETOOTH_SETTINGS_PATH_ZH = "系统设置 → 隐私与安全性 → 蓝牙"


def _avfoundation() -> Any:
    try:
        import AVFoundation
    except ImportError as error:  # pragma: no cover - 仅真实 macOS 安装包触发
        raise RuntimeError(
            "macOS 摄像头支持组件未包含在应用中；请重新安装正式 DMG"
        ) from error
    return AVFoundation


def camera_permission_status() -> str:
    av = _avfoundation()
    status = int(av.AVCaptureDevice.authorizationStatusForMediaType_(av.AVMediaTypeVideo))
    return {
        int(av.AVAuthorizationStatusNotDetermined): "not_determined",
        int(av.AVAuthorizationStatusRestricted): "restricted",
        int(av.AVAuthorizationStatusDenied): "denied",
        int(av.AVAuthorizationStatusAuthorized): "authorized",
    }.get(status, "unknown")


def request_camera_permission() -> str:
    """仅在首次使用摄像头时触发系统权限弹窗。"""

    av = _avfoundation()
    status = camera_permission_status()
    if status != "not_determined":
        return status
    completed = threading.Event()

    def completion(_granted: bool) -> None:
        completed.set()

    av.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
        av.AVMediaTypeVideo,
        completion,
    )
    completed.wait(timeout=60.0)
    return camera_permission_status()


def _video_dimensions(format_description: Any) -> tuple[int, int]:
    try:
        import CoreMedia

        dimensions = CoreMedia.CMVideoFormatDescriptionGetDimensions(format_description)
        return int(dimensions.width), int(dimensions.height)
    except (ImportError, AttributeError, TypeError):
        return 0, 0


def _device_types(av: Any) -> list[str]:
    names = (
        "AVCaptureDeviceTypeExternal",
        # macOS 13 仍使用 ExternalUnknown；macOS 14 起改名为 External。
        # 两个常量按运行系统实际存在情况加入，保证最低支持版本能枚举 USB 摄像头。
        "AVCaptureDeviceTypeExternalUnknown",
        "AVCaptureDeviceTypeContinuityCamera",
        "AVCaptureDeviceTypeDeskViewCamera",
        "AVCaptureDeviceTypeBuiltInWideAngleCamera",
    )
    values: list[str] = []
    for name in names:
        value = getattr(av, name, None)
        if value and value not in values:
            values.append(value)
    return values


def enumerate_video_devices() -> list[dict[str, Any]]:
    """返回稳定 uniqueID、真实支持格式及外接属性。"""

    permission = request_camera_permission()
    if permission != "authorized":
        raise RuntimeError(
            f"摄像头权限为 {permission}；请在{CAMERA_SETTINGS_PATH_ZH}中允许 "
            "IMU Data Collector"
        )
    av = _avfoundation()
    create_discovery = getattr(  # noqa: B009
        av.AVCaptureDeviceDiscoverySession,
        "discoverySessionWithDeviceTypes_mediaType_position_",
    )
    discovery = create_discovery(
        _device_types(av),
        av.AVMediaTypeVideo,
        av.AVCaptureDevicePositionUnspecified,
    )
    output: list[dict[str, Any]] = []
    for device in discovery.devices():
        device_type = str(device.deviceType())
        profiles: list[dict[str, Any]] = []
        seen: set[tuple[int, int, float]] = set()
        for capture_format in device.formats():
            width, height = _video_dimensions(capture_format.formatDescription())
            if width <= 0 or height <= 0:
                continue
            for frame_range in capture_format.videoSupportedFrameRateRanges():
                fps = float(frame_range.maxFrameRate())
                key = (width, height, round(fps, 3))
                if fps <= 0 or key in seen:
                    continue
                seen.add(key)
                profiles.append(
                    {
                        "width": width,
                        "height": height,
                        "fps": fps,
                        "min_fps": float(frame_range.minFrameRate()),
                        "input_format": "backend_default",
                    }
                )
        lowered_type = device_type.lower()
        output.append(
            {
                "unique_id": str(device.uniqueID()),
                "name": str(device.localizedName()),
                "device_type": device_type,
                "integration": (
                    "external"
                    if "external" in lowered_type or "continuity" in lowered_type
                    else "integrated"
                ),
                "profiles": profiles,
            }
        )
    return output


def bluetooth_permission_status() -> str:
    """读取 CoreBluetooth 授权状态；真正的授权请求由 Bleak 首次连接触发。"""

    try:
        import CoreBluetooth
    except ImportError:
        return "unavailable"
    try:
        status = int(CoreBluetooth.CBManager.authorization())
    except (AttributeError, TypeError):
        return "unknown"
    return {
        0: "not_determined",
        1: "restricted",
        2: "denied",
        3: "authorized",
    }.get(status, "unknown")


def permission_statuses() -> dict[str, dict[str, str]]:
    return {
        "camera": {
            "state": camera_permission_status(),
            "settings_path": CAMERA_SETTINGS_PATH_ZH,
        },
        "bluetooth": {
            "state": bluetooth_permission_status(),
            "settings_path": BLUETOOTH_SETTINGS_PATH_ZH,
        },
    }
