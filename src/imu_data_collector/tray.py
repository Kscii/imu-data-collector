"""Windows 桌面托盘入口。

托盘只管理本机采集后端的生命周期，不复制采集状态机。浏览器关闭后后端继续运行；
只有托盘“退出”才请求 Uvicorn 优雅关闭并释放 BLE、摄像头与后台任务。
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from ctypes import wintypes
from logging.handlers import RotatingFileHandler
from typing import Any

import uvicorn
from PIL import Image, ImageDraw

from imu_data_collector.capture_api import create_capture_app
from imu_data_collector.config import Settings, load_settings
from imu_data_collector.host import platform_id, user_cache_dir

logger = logging.getLogger(__name__)

_MUTEX_NAME = "Local\\Kscii.IMUDataCollector.Tray"
_ERROR_ALREADY_EXISTS = 183


def _configure_logging() -> None:
    log_root = user_cache_dir() / "logs"
    log_root.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_root / "tray.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=2,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(
        isinstance(existing, RotatingFileHandler)
        and getattr(existing, "baseFilename", None) == handler.baseFilename
        for existing in root.handlers
    ):
        root.addHandler(handler)


def _show_message(title: str, message: str, *, error: bool = False) -> None:
    if os.name != "nt":
        if error:
            logger.error("%s: %s", title, message)
        else:
            logger.info("%s: %s", title, message)
        return
    style = 0x00000010 if error else 0x00000040
    ctypes.windll.user32.MessageBoxW(None, message, title, style)


def _confirm_exit() -> bool:
    if os.name != "nt":
        return True
    result = ctypes.windll.user32.MessageBoxW(
        None,
        "退出后将释放 IMU 和摄像头，浏览器页面也会停止工作。\n\n确定退出吗？",
        "退出 IMU 数采平台",
        0x00000004 | 0x00000020,
    )
    return result == 6


def _create_icon_image() -> Image.Image:
    """生成不依赖系统字体的托盘图标。"""

    image = Image.new("RGBA", (64, 64), (11, 29, 52, 255))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 4, 59, 59), radius=14, fill=(37, 99, 235, 255))
    draw.line(
        [(11, 35), (19, 35), (24, 20), (31, 47), (37, 27), (43, 35), (53, 35)],
        fill=(255, 255, 255, 255),
        width=5,
        joint="curve",
    )
    return image


def _health(url: str) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(f"{url}/api/v1/health", timeout=0.8) as response:
            payload = json.loads(response.read())
        return payload if payload.get("application") == "capture" else None
    except (
        OSError,
        TimeoutError,
        ValueError,
        json.JSONDecodeError,
        urllib.error.URLError,
    ):
        return None


def _acquire_windows_mutex() -> tuple[int | None, bool]:
    """返回 `(句柄, 已有实例)`；句柄必须在进程退出时释放。"""

    if os.name != "nt":
        return None, False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel32.CreateMutexW(None, True, _MUTEX_NAME)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None, True
    return int(handle), False


def _release_windows_mutex(handle: int | None) -> None:
    if os.name != "nt" or handle is None:
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.ReleaseMutex(wintypes.HANDLE(handle))
    kernel32.CloseHandle(wintypes.HANDLE(handle))


class TrayApplication:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.url = f"http://{settings.server_host}:{settings.server_port}"
        self.server: uvicorn.Server | None = None
        self.server_thread: threading.Thread | None = None
        self.owns_server = False
        self.icon: Any | None = None

    def open_webui(self, _icon: Any = None, _item: Any = None) -> None:
        webbrowser.open(self.url)

    def _start_server(self) -> None:
        if _health(self.url):
            logger.info("复用已经运行的数采后端：%s", self.url)
            return
        config = uvicorn.Config(
            create_capture_app(self.settings),
            host=self.settings.server_host,
            port=self.settings.server_port,
            log_level="info",
            # Windows windowed EXE 没有 sys.stdout/sys.stderr。Uvicorn 默认
            # formatter 会调用 stderr.isatty()，因此托盘统一写上方滚动日志。
            log_config=None,
            access_log=False,
            timeout_graceful_shutdown=5,
        )
        self.server = uvicorn.Server(config)
        self.server_thread = threading.Thread(
            target=self.server.run,
            name="capture-api",
            daemon=True,
        )
        self.owns_server = True
        self.server_thread.start()
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if _health(self.url):
                return
            if not self.server_thread.is_alive():
                break
            time.sleep(0.2)
        raise RuntimeError(
            f"本地后端未能在 20 秒内启动。请确认端口 {self.settings.server_port} 未被其他程序占用。"
        )

    def _exit(self, icon: Any, _item: Any = None) -> None:
        health = _health(self.url)
        if health and health.get("state") in {"arming", "recording"}:
            _show_message(
                "正在录制",
                "当前录制尚未停止。请先在 WebUI 点击“结束录制”，等待进入后台收尾后再退出。",
                error=True,
            )
            return
        if not _confirm_exit():
            return
        if self.owns_server and self.server is not None:
            self.server.should_exit = True
        icon.stop()

    def run(self) -> None:
        import pystray

        self._start_server()
        menu = pystray.Menu(
            pystray.MenuItem("打开数采页面", self.open_webui, default=True),
            pystray.MenuItem("退出", self._exit),
        )
        self.icon = pystray.Icon(
            "imu-data-collector",
            _create_icon_image(),
            "CW12EU-T IMU 数采平台",
            menu,
        )
        self.open_webui()
        self.icon.run()
        if self.server_thread is not None:
            self.server_thread.join(timeout=12.0)
            if self.server_thread.is_alive() and self.server is not None:
                logger.warning("后端未在宽限期内退出，发送强制退出请求")
                self.server.force_exit = True
                self.server_thread.join(timeout=3.0)


def main() -> None:
    _configure_logging()
    if platform_id() != "windows":
        _show_message("不支持的平台", "托盘入口目前只支持 Windows。", error=True)
        return
    mutex: int | None = None
    try:
        settings = load_settings()
        url = f"http://{settings.server_host}:{settings.server_port}"
        mutex, already_running = _acquire_windows_mutex()
        if already_running:
            webbrowser.open(url)
            return
        TrayApplication(settings).run()
    except Exception as error:
        logger.exception("托盘应用启动或运行失败")
        _show_message(
            "IMU 数采平台启动失败",
            f"{error}\n\n详细日志位于本机应用缓存目录的 logs/tray.log。",
            error=True,
        )
    finally:
        _release_windows_mutex(mutex)
