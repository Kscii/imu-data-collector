"""基于 FFmpeg/V4L2 的录制、本地预览、进度监控与逐帧 PTS 提取。"""

from __future__ import annotations

import asyncio
import glob
import json
import re
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from imu_data_collector.config import VideoSettings


async def _run_capture(*args: str) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
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
    output: list[dict[str, Any]] = []
    for device in sorted(glob.glob("/dev/video*")):
        code, capabilities, _ = await _run_capture("v4l2-ctl", "--all", "-d", device)
        if code or "Video Capture" not in capabilities:
            continue
        code, formats, _ = await _run_capture(
            "v4l2-ctl", "--list-formats-ext", "-d", device
        )
        # metadata 节点常能出现在 --all 中，但没有实际像素格式。
        if code or not re.search(r"\[\d+\]:", formats):
            continue
        _, properties, _ = await _run_capture(
            "udevadm", "info", "--query=property", f"--name={device}"
        )
        props = dict(
            line.split("=", 1) for line in properties.splitlines() if "=" in line
        )
        output.append(
            {
                "device": device,
                "product": props.get("ID_V4L_PRODUCT", "Unknown camera"),
                "serial": props.get("ID_SERIAL", ""),
                "path": props.get("ID_PATH", ""),
                "interface": props.get("ID_USB_INTERFACE_NUM", ""),
                "camera_id": _stable_camera_id(props, device),
                "formats": formats,
                "supports_default_profile": _supports_profile(formats, profile),
                "color_capture": "MJPG" in formats or "YUYV" in formats,
            }
        )
    return output


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
            selected = next(
                (
                    item
                    for item in devices
                    if item["supports_default_profile"] and item["color_capture"]
                ),
                None,
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


@dataclass(frozen=True, slots=True)
class VideoFrameTable:
    pts_monotonic_ns: np.ndarray
    duration_ns: np.ndarray
    key_frame: np.ndarray
    codec: str
    width: int
    height: int


class FFmpegVideoRecorder:
    def __init__(self, settings: VideoSettings, device: str, output_path: Path) -> None:
        self.settings = settings
        self.device = device
        self.output_path = output_path
        self.process: asyncio.subprocess.Process | None = None
        self.progress = VideoProgress()
        self.latest_jpeg: bytes | None = None
        self.preview_generation = 0
        self.started_monotonic_ns: int | None = None
        self._tasks: list[asyncio.Task[Any]] = []

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
                "-fps_mode",
                "passthrough",
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
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.started_monotonic_ns = __import__("time").monotonic_ns()
        self.process = await asyncio.create_subprocess_exec(
            *self.command(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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
                del buffer[: end + 2]

    async def _read_progress(self) -> None:
        assert self.process and self.process.stderr
        while line := await self.process.stderr.readline():
            text = line.decode(errors="replace").strip()
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


async def probe_video_frames(path: Path, recording_start_monotonic_ns: int) -> VideoFrameTable:
    code, stdout, stderr = await _run_capture(
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
        "json",
        str(path),
    )
    if code:
        raise RuntimeError(f"ffprobe failed: {stderr}")
    payload = json.loads(stdout)
    streams = payload.get("streams", [])
    if not streams:
        raise ValueError("recorded MKV has no video stream")
    frames = payload.get("frames", [])
    pts_seconds: list[float] = []
    durations: list[float] = []
    keys: list[bool] = []
    for frame in frames:
        pts_value = frame.get("pts_time", frame.get("best_effort_timestamp_time"))
        if pts_value is None:
            continue
        pts_seconds.append(float(pts_value))
        durations.append(float(frame.get("pkt_duration_time", 0.0)))
        keys.append(bool(int(frame.get("key_frame", 0))))
    pts = np.rint(np.asarray(pts_seconds, dtype=np.float64) * 1e9).astype(np.int64)
    if len(pts) and pts[0] < 1_000_000_000_000:
        pts = pts - pts[0] + int(recording_start_monotonic_ns)
    duration_ns = np.rint(np.asarray(durations, dtype=np.float64) * 1e9).astype(np.int64)
    if len(duration_ns):
        positive = duration_ns[duration_ns > 0]
        fallback = int(np.median(np.diff(pts))) if len(pts) > 1 else 0
        duration_ns[duration_ns <= 0] = int(np.median(positive)) if len(positive) else fallback
    stream = streams[0]
    return VideoFrameTable(
        pts_monotonic_ns=pts,
        duration_ns=duration_ns,
        key_frame=np.asarray(keys, dtype=np.bool_),
        codec=str(stream.get("codec_name", "unknown")),
        width=int(stream.get("width", 0)),
        height=int(stream.get("height", 0)),
    )
