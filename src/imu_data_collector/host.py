"""跨平台主机信息、资源定位与默认数据目录。

采集时间轴仍统一使用 Python 的单调时钟；这里显式记录操作系统实际采用的
实现，避免把 Windows/macOS 采集误标成 Linux CLOCK_MONOTONIC。
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class HostRuntime:
    os_name: str
    os_version: str
    architecture: str
    monotonic_implementation: str
    clock_domain: str

    def as_h5_attributes(self) -> dict[str, str]:
        return asdict(self)


def platform_id() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def host_runtime() -> HostRuntime:
    clock = __import__("time").get_clock_info("monotonic")
    current = platform_id()
    domains = {
        "linux": "linux_clock_monotonic",
        "windows": "windows_query_performance_counter_monotonic",
        "macos": "darwin_mach_absolute_monotonic",
    }
    return HostRuntime(
        os_name=current,
        os_version=platform.platform(),
        architecture=platform.machine() or "unknown",
        monotonic_implementation=clock.implementation,
        clock_domain=domains.get(current, "python_monotonic"),
    )


def application_root() -> Path:
    """返回源码根目录或 PyInstaller 解包后的只读资源根目录。"""

    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root).resolve()
    return Path(__file__).resolve().parents[2]


def resource_path(relative: str | Path) -> Path:
    return application_root() / relative


def user_data_dir(app_name: str = "imu-data-collector") -> Path:
    current = platform_id()
    if current == "windows":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / app_name
    if current == "macos":
        return Path.home() / "Library" / "Application Support" / app_name
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / app_name


def user_cache_dir(app_name: str = "imu-data-collector") -> Path:
    current = platform_id()
    if current == "windows":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / app_name / "cache"
    if current == "macos":
        return Path.home() / "Library" / "Caches" / app_name
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / app_name


def find_executable(name: str) -> str | None:
    """查找安装包随附工具或 PATH 中的工具；不存在时返回 ``None``。"""

    executable = f"{name}.exe" if platform_id() == "windows" else name
    bundled = resource_path(Path("third_party") / "ffmpeg" / "bin" / executable)
    if bundled.is_file():
        return str(bundled)
    return shutil.which(executable) or shutil.which(name)


def resolve_executable(name: str) -> str:
    """优先使用安装包随附工具，开发环境则回退到 PATH。"""

    executable = f"{name}.exe" if platform_id() == "windows" else name
    return find_executable(name) or executable


def background_subprocess_kwargs() -> dict[str, int]:
    """返回后台工具的跨平台子进程参数。

    Windows 的托盘入口属于 windowed 程序；如果直接启动 FFmpeg、ffprobe 或
    rclone 这类 console 程序，系统会为每个子进程创建命令行窗口。这里集中使用
    ``CREATE_NO_WINDOW``，同时保留调用方已有的标准流重定向与退出管理。
    """

    if platform_id() != "windows":
        return {}
    return {
        "creationflags": int(
            getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
    }
