"""基于 FFmpeg/V4L2 的录制、本地预览、进度监控与逐帧 PTS 提取。"""

from __future__ import annotations

import asyncio
import glob
import re
import shutil
import signal
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from imu_data_collector.config import VideoSettings


async def _run_capture(
    *args: str, timeout_seconds: float = 10.0
) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        close_fds=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout_seconds
        )
    except asyncio.CancelledError:
        process.kill()
        await process.communicate()
        raise
    except TimeoutError:
        process.kill()
        stdout, stderr = await process.communicate()
        message = f"命令超过 {timeout_seconds:.0f} 秒未完成：{' '.join(args[:2])}"
        stderr = stderr + message.encode()
        return 124, stdout.decode(errors="replace"), stderr.decode(errors="replace")
    return process.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")


def _supports_profile(formats: str, settings: VideoSettings) -> bool:
    pixel = (
        "MJPG"
        if settings.input_format.lower() in {"mjpeg", "mjpg"}
        else settings.input_format.upper()
    )
    resolution = f"{settings.width}x{settings.height}"
    fps_patterns = (
        f"{float(settings.requested_fps):.3f} fps",
        f"{settings.requested_fps}.000 fps",
    )
    return (
        pixel in formats
        and resolution in formats
        and any(item in formats for item in fps_patterns)
    )


def _stable_camera_id(props: dict[str, str], device: str) -> str:
    serial = props.get("ID_SERIAL", "")
    path = props.get("ID_PATH", "")
    interface = props.get("ID_USB_INTERFACE_NUM", "")
    stable = serial or path
    return f"{stable}|if={interface}" if stable else f"path={device}"


async def discover_video_devices(
    settings: VideoSettings | None = None,
) -> list[dict[str, Any]]:
    profile = settings or VideoSettings()
    devices = sorted(glob.glob("/dev/video*"))

    async def inspect(device: str) -> dict[str, Any] | None:
        code, properties, _ = await _run_capture(
            "udevadm",
            "info",
            "--query=property",
            f"--name={device}",
            timeout_seconds=3.0,
        )
        if code:
            return None
        props = dict(
            line.split("=", 1) for line in properties.splitlines() if "=" in line
        )
        if ":capture:" not in props.get("ID_V4L_CAPABILITIES", ""):
            return None
        code, formats, _ = await _run_capture(
            "v4l2-ctl",
            "--list-formats-ext",
            "-d",
            device,
            timeout_seconds=3.0,
        )
        # metadata 节点常能出现在 --all 中，但没有实际像素格式。
        if code or not re.search(r"\[\d+\]:", formats):
            return None
        return {
            "device": device,
            "product": props.get("ID_V4L_PRODUCT", "Unknown camera"),
            "serial": props.get("ID_SERIAL", ""),
            "path": props.get("ID_PATH", ""),
            "interface": props.get("ID_USB_INTERFACE_NUM", ""),
            "integration": props.get("ID_INTEGRATION", ""),
            "camera_id": _stable_camera_id(props, device),
            "formats": formats,
            "supports_default_profile": _supports_profile(formats, profile),
            "color_capture": "MJPG" in formats or "YUYV" in formats,
        }

    inspected = await asyncio.gather(*(inspect(device) for device in devices))
    return [item for item in inspected if item is not None]


def select_video_device(
    devices: list[dict[str, Any]],
    settings: VideoSettings,
    requested_camera_id: str | None = None,
) -> dict[str, Any]:
    if settings.device:
        selected = next((item for item in devices if item["device"] == settings.device), None)
        if selected is None:
            raise RuntimeError(f"配置的视频节点不可用或不是采集节点：{settings.device}")
    else:
        camera_id = requested_camera_id or settings.camera_id
        if camera_id:
            selected = next((item for item in devices if item["camera_id"] == camera_id), None)
            if selected is None:
                raise RuntimeError("所选摄像头当前不可用；请重新扫描并选择")
        else:
            compatible = [
                item
                for item in devices
                if item["supports_default_profile"] and item["color_capture"]
            ]
            # 同时存在笔记本内置相机与 USB 相机时，默认选择外接相机。
            # 显式 camera_id 或配置 device 始终具有更高优先级。
            selected = next(
                (item for item in compatible if item.get("integration") == "external"),
                compatible[0] if compatible else None,
            )
    if selected is None:
        raise RuntimeError("没有摄像头支持配置的 1080p30 MJPEG 彩色视频模式")
    if not selected["supports_default_profile"]:
        raise RuntimeError("所选摄像头不支持当前配置的视频分辨率、帧率或像素格式")
    if not selected["color_capture"]:
        raise RuntimeError("所选节点不是彩色视频采集节点")
    return selected


@dataclass(slots=True)
class VideoProgress:
    frame: int = 0
    fps: float = 0.0
    bitrate: str = "0"
    out_time_us: int = 0
    speed: str = "0x"
    errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CameraControlState:
    requested: dict[str, int] = field(default_factory=dict)
    effective: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.errors and self.requested == self.effective


def _parse_controls(text: str) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in text.splitlines():
        match = re.match(r"\s*([a-zA-Z0-9_]+)\s*:\s*(-?\d+)", line)
        if match:
            values[match.group(1)] = int(match.group(2))
    return values


async def apply_video_controls(
    settings: VideoSettings, device: str
) -> CameraControlState:
    """按确定顺序锁定曝光并读回有效值；不支持的设备保留明确诊断。"""

    state = CameraControlState()
    if not settings.manual_controls_enabled:
        return state
    state.requested = {
        "auto_exposure": settings.auto_exposure,
        "exposure_time_absolute": settings.exposure_time_absolute,
        "gain": settings.gain,
        "exposure_dynamic_framerate": settings.exposure_dynamic_framerate,
        "power_line_frequency": settings.power_line_frequency,
    }
    # 先退出自动曝光，随后设置的曝光时间和增益才不会被驱动忽略。
    commands = [
        f"auto_exposure={settings.auto_exposure}",
        ",".join(
            f"{name}={value}"
            for name, value in state.requested.items()
            if name != "auto_exposure"
        ),
    ]
    for controls in commands:
        code, _stdout, stderr = await _run_capture(
            "v4l2-ctl",
            "-d",
            device,
            f"--set-ctrl={controls}",
            timeout_seconds=3.0,
        )
        if code:
            state.errors.append(stderr.strip() or f"设置摄像头控制失败：{controls}")
    names = ",".join(state.requested)
    code, stdout, stderr = await _run_capture(
        "v4l2-ctl",
        "-d",
        device,
        f"--get-ctrl={names}",
        timeout_seconds=3.0,
    )
    if code:
        state.errors.append(stderr.strip() or "无法读回摄像头控制")
    else:
        state.effective = _parse_controls(stdout)
        mismatched = {
            name: (wanted, state.effective.get(name))
            for name, wanted in state.requested.items()
            if state.effective.get(name) != wanted
        }
        if mismatched:
            state.errors.append(f"摄像头控制读回不一致：{mismatched}")
    return state


@dataclass(frozen=True, slots=True)
class VideoFrameTable:
    pts_monotonic_ns: np.ndarray
    duration_ns: np.ndarray
    key_frame: np.ndarray
    codec: str
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class PreviewFrameSnapshot:
    """浏览器预览通道的不可变快照。"""

    session_id: int
    generation: int
    active: bool
    jpeg: bytes | None


class PreviewFrameHub:
    """让一个浏览器 MJPEG 通道跨越多个 FFmpeg 进程持续存在。"""

    def __init__(self) -> None:
        self._session_id = 0
        self._generation = 0
        self._active = False
        self._jpeg: bytes | None = None
        self._changed = asyncio.Event()

    def _signal(self) -> None:
        changed = self._changed
        self._changed = asyncio.Event()
        changed.set()

    def activate(self) -> int:
        """开始一次新的用户预览会话；录制切换不会再次调用。"""

        if self._active:
            return self._session_id
        self._session_id += 1
        self._generation += 1
        self._active = True
        self._jpeg = None
        self._signal()
        return self._session_id

    def deactivate(self) -> None:
        """显式释放设备时关闭通道并丢弃最后一帧。"""

        if not self._active and self._jpeg is None:
            return
        self._generation += 1
        self._active = False
        self._jpeg = None
        self._signal()

    def publish(self, jpeg: bytes) -> None:
        if not self._active:
            return
        self._jpeg = jpeg
        self._generation += 1
        self._signal()

    def snapshot(self) -> PreviewFrameSnapshot:
        return PreviewFrameSnapshot(
            session_id=self._session_id,
            generation=self._generation,
            active=self._active,
            jpeg=self._jpeg,
        )

    async def wait_for_change(
        self,
        session_id: int,
        generation: int,
        *,
        timeout: float = 0.5,
    ) -> PreviewFrameSnapshot:
        """等待新帧、会话切换或显式释放，避免返回 200 空流。"""

        # 先抓取事件引用再读快照：publish() 若夹在两者之间，会唤醒这个旧事件；
        # 反过来先读快照可能错过一次信号，只能等到超时再看到新帧。
        changed = self._changed
        current = self.snapshot()
        if (
            current.session_id != session_id
            or current.generation != generation
            or not current.active
        ):
            return current
        try:
            await asyncio.wait_for(changed.wait(), timeout=timeout)
        except TimeoutError:
            pass
        return self.snapshot()


class FFmpegVideoRecorder:
    def __init__(
        self,
        settings: VideoSettings,
        device: str,
        output_path: Path | None,
        preview_hub: PreviewFrameHub | None = None,
    ) -> None:
        self.settings = settings
        self.device = device
        self.output_path = output_path
        self.process: asyncio.subprocess.Process | None = None
        self.progress = VideoProgress()
        self.latest_jpeg: bytes | None = None
        self.preview_generation = 0
        self.started_monotonic_ns: int | None = None
        self.control_state = CameraControlState()
        self.preview_hub = preview_hub
        self._source_pts_seconds: deque[float] = deque()
        self._preview_times: deque[float] = deque()
        self._tasks: list[asyncio.Task[Any]] = []

    @staticmethod
    def _rolling_fps(values: deque[float]) -> float:
        if len(values) < 2 or values[-1] <= values[0]:
            return 0.0
        return (len(values) - 1) / (values[-1] - values[0])

    def _trim_window(self, values: deque[float]) -> None:
        if not values:
            return
        cutoff = values[-1] - self.settings.source_fps_window_seconds
        while len(values) > 2 and values[0] < cutoff:
            values.popleft()

    @property
    def source_fps(self) -> float:
        return self._rolling_fps(self._source_pts_seconds)

    @property
    def preview_fps(self) -> float:
        return self._rolling_fps(self._preview_times)

    @property
    def source_frame_count(self) -> int:
        return len(self._source_pts_seconds)

    @property
    def frame(self) -> int:
        return self.progress.frame

    @property
    def fps(self) -> float:
        """旧 API 兼容别名；新界面应分别读取 source_fps/preview_fps。"""

        return self.source_fps

    @property
    def bitrate(self) -> str:
        return self.progress.bitrate

    @property
    def speed(self) -> str:
        return self.progress.speed

    def command(self) -> list[str]:
        settings = self.settings
        use_vaapi = bool(settings.vaapi_device and Path(settings.vaapi_device).exists())
        split = "[0:v]split=2[record][preview];"
        if use_vaapi:
            record_filter = "[record]format=nv12,hwupload[record_out];"
            encoder = [
                "-map",
                "[record_out]",
                "-c:v",
                "h264_vaapi",
                "-profile:v",
                "high",
            ]
        else:
            record_filter = "[record]format=yuv420p[record_out];"
            encoder = [
                "-map",
                "[record_out]",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-profile:v",
                "high",
            ]
        preview_filter = (
            f"[preview]fps={settings.preview_fps},scale={settings.preview_width}:-2[preview_out]"
        )
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-progress",
            "pipe:2",
            "-stats_period",
            "1",
        ]
        if use_vaapi:
            command.extend(["-vaapi_device", str(settings.vaapi_device)])
        command.extend(
            [
                "-copyts",
                "-f",
                "video4linux2",
                "-timestamps",
                "default",
                "-input_format",
                settings.input_format,
                "-video_size",
                f"{settings.width}x{settings.height}",
                "-framerate",
                str(settings.requested_fps),
                "-i",
                self.device,
            ]
        )
        source_stats_output = [
            "-map",
            "0:v:0",
            "-c:v",
            "wrapped_avframe",
            "-stats_enc_pre",
            "pipe:2",
            "-stats_enc_pre_fmt",
            "source {n} {pts} {tb}",
            "-f",
            "null",
            "/dev/null",
        ]
        if self.output_path is None:
            command.extend(
                [
                    *source_stats_output,
                    "-vf",
                    f"fps={settings.preview_fps},scale={settings.preview_width}:-2",
                    "-c:v",
                    "mjpeg",
                    "-q:v",
                    "7",
                    "-fps_mode",
                    "vfr",
                    "-f",
                    "image2pipe",
                    "pipe:1",
                ]
            )
            return command
        command.extend(
            [
                *source_stats_output,
                "-filter_complex",
                split + record_filter + preview_filter,
                *encoder,
                "-b:v",
                settings.bitrate,
                "-maxrate",
                settings.maxrate,
                "-bufsize",
                "12M",
                "-g",
                str(settings.requested_fps * 2),
                "-bf",
                "0",
                "-fps_mode",
                "vfr",
                "-cluster_time_limit",
                "1000",
                "-f",
                "matroska",
                str(self.output_path),
                "-map",
                "[preview_out]",
                "-c:v",
                "mjpeg",
                "-q:v",
                "7",
                "-f",
                "image2pipe",
                "pipe:1",
            ]
        )
        return command

    async def start(self) -> None:
        if self.output_path is not None:
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.control_state = await apply_video_controls(self.settings, self.device)
        self.started_monotonic_ns = time.monotonic_ns()
        self.process = await asyncio.create_subprocess_exec(
            *self.command(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            close_fds=True,
        )
        self._tasks = [
            asyncio.create_task(self._read_preview()),
            asyncio.create_task(self._read_progress()),
        ]
        await asyncio.sleep(0.25)
        if self.process.returncode is not None:
            raise RuntimeError(
                "FFmpeg exited during camera startup: " + "; ".join(self.progress.errors[-5:])
            )

    async def stop(self, timeout: float = 15.0) -> None:
        process = self.process
        if process is None:
            return
        if process.returncode is None:
            process.send_signal(signal.SIGINT)
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout)
            except TimeoutError:
                process.terminate()
                await process.wait()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if process.returncode not in (0, 255):
            raise RuntimeError(
                f"FFmpeg recording failed with code {process.returncode}: "
                + "; ".join(self.progress.errors[-5:])
            )

    async def _read_preview(self) -> None:
        assert self.process and self.process.stdout
        buffer = bytearray()
        while chunk := await self.process.stdout.read(64 * 1024):
            buffer.extend(chunk)
            while True:
                start = buffer.find(b"\xff\xd8")
                if start < 0:
                    if len(buffer) > 2:
                        del buffer[:-2]
                    break
                end = buffer.find(b"\xff\xd9", start + 2)
                if end < 0:
                    if start:
                        del buffer[:start]
                    break
                self.latest_jpeg = bytes(buffer[start : end + 2])
                self.preview_generation += 1
                if self.preview_hub is not None:
                    self.preview_hub.publish(self.latest_jpeg)
                self._preview_times.append(time.monotonic())
                self._trim_window(self._preview_times)
                del buffer[: end + 2]

    async def _read_progress(self) -> None:
        assert self.process and self.process.stderr
        while line := await self.process.stderr.readline():
            text = line.decode(errors="replace").strip()
            if text.startswith("source "):
                fields = text.split()
                if len(fields) == 4:
                    try:
                        pts = float(fields[2])
                        numerator, denominator = fields[3].split("/", 1)
                        seconds = pts * float(numerator) / float(denominator)
                        self._source_pts_seconds.append(seconds)
                        self._trim_window(self._source_pts_seconds)
                    except (ValueError, ZeroDivisionError):
                        self.progress.errors.append(text)
                else:
                    self.progress.errors.append(text)
                continue
            if "=" not in text:
                if text:
                    self.progress.errors.append(text)
                continue
            key, value = text.split("=", 1)
            try:
                if key == "frame":
                    self.progress.frame = int(value)
                elif key == "fps":
                    self.progress.fps = float(value)
                elif key == "bitrate":
                    self.progress.bitrate = value
                elif key == "out_time_us":
                    self.progress.out_time_us = int(value)
                elif key == "speed":
                    self.progress.speed = value
            except ValueError:
                self.progress.errors.append(text)


async def probe_video_frames(
    path: Path,
    recording_start_monotonic_ns: int,
    *,
    pts_are_monotonic: bool = True,
    timeout_seconds: float = 600.0,
    stall_timeout_seconds: float = 90.0,
    nice_value: int = 0,
) -> VideoFrameTable:
    command: list[str] = []
    if shutil.which("ionice"):
        command.extend(("ionice", "-c", "2", "-n", "7"))
    if nice_value and shutil.which("nice"):
        command.extend(("nice", "-n", str(nice_value)))
    command.extend((
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_streams",
        "-show_frames",
        "-show_entries",
        "stream=codec_name,width,height:frame=pts_time,best_effort_timestamp_time,pkt_duration_time,key_frame",
        "-of",
        "compact=p=1:nk=0",
        str(path),
    ))
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        close_fds=True,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    pts_seconds: list[float] = []
    durations: list[float] = []
    keys: list[bool] = []
    stream: dict[str, str] | None = None
    try:
        async with asyncio.timeout(timeout_seconds):
            while True:
                line = await asyncio.wait_for(
                    process.stdout.readline(), timeout=stall_timeout_seconds
                )
                if not line:
                    break
                fields = line.decode(errors="replace").strip().split("|")
                if not fields:
                    continue
                values = {
                    key: value
                    for field in fields[1:]
                    if "=" in field
                    for key, value in (field.split("=", 1),)
                }
                if fields[0] == "stream":
                    stream = values
                    continue
                if fields[0] != "frame":
                    continue
                pts_value = values.get(
                    "pts_time", values.get("best_effort_timestamp_time")
                )
                if pts_value is None:
                    continue
                pts_seconds.append(float(pts_value))
                durations.append(float(values.get("pkt_duration_time", 0.0)))
                keys.append(bool(int(values.get("key_frame", 0))))
            stderr = (await process.stderr.read()).decode(errors="replace")
            code = await process.wait()
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise
    except TimeoutError as error:
        process.kill()
        await process.wait()
        raise RuntimeError(
            f"ffprobe 超时：{path.name}，总时限 {timeout_seconds:.0f} 秒，"
            f"无进展时限 {stall_timeout_seconds:.0f} 秒"
        ) from error
    if code:
        raise RuntimeError(f"ffprobe failed: {stderr.strip()}")
    if stream is None:
        raise ValueError("recorded MKV has no video stream")
    pts = np.rint(np.asarray(pts_seconds, dtype=np.float64) * 1e9).astype(np.int64)
    if len(pts) and not pts_are_monotonic:
        pts = pts - pts[0] + int(recording_start_monotonic_ns)
    duration_ns = np.rint(np.asarray(durations, dtype=np.float64) * 1e9).astype(np.int64)
    if len(duration_ns):
        positive = duration_ns[duration_ns > 0]
        fallback = int(np.median(np.diff(pts))) if len(pts) > 1 else 0
        duration_ns[duration_ns <= 0] = int(np.median(positive)) if len(positive) else fallback
    return VideoFrameTable(
        pts_monotonic_ns=pts,
        duration_ns=duration_ns,
        key_frame=np.asarray(keys, dtype=np.bool_),
        codec=str(stream.get("codec_name", "unknown")),
        width=int(stream.get("width", 0)),
        height=int(stream.get("height", 0)),
    )


async def normalize_video_timeline(
    source_path: Path,
    output_path: Path,
    *,
    timeout_seconds: float = 300.0,
    nice_value: int = 0,
) -> None:
    """无损重封装视频码流，使交付 MKV 的首帧媒体 PTS 从零开始。"""

    if source_path == output_path:
        raise ValueError("源 MKV 与规范化输出 MKV 不能是同一路径")
    command: list[str] = []
    if shutil.which("ionice"):
        command.extend(("ionice", "-c", "2", "-n", "7"))
    if nice_value and shutil.which("nice"):
        command.extend(("nice", "-n", str(nice_value)))
    command.extend((
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source_path),
        "-map",
        "0:v:0",
        "-c",
        "copy",
        "-y",
        str(output_path),
    ))
    code, _stdout, stderr = await _run_capture(
        *command, timeout_seconds=timeout_seconds
    )
    if code:
        raise RuntimeError(f"MKV 时间轴无损重封装失败：{stderr}")
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError("MKV 时间轴无损重封装没有产生有效输出")
