import asyncio

import pytest

import imu_data_collector.macos_devices as macos_devices
import imu_data_collector.video as video_module
from imu_data_collector.config import VideoSettings
from imu_data_collector.video import (
    FFmpegVideoRecorder,
    PreviewFrameHub,
    _choose_profile,
    _parse_avfoundation_devices,
    _parse_dshow_profiles,
    _parse_v4l2_profiles,
    apply_video_controls,
    discover_video_devices,
    normalize_video_timeline,
    select_video_device,
)


def test_profile_selection_uses_highest_resolution_that_reaches_30_fps() -> None:
    profiles = [
        {"width": 3840, "height": 2160, "fps": 15.0, "input_format": "mjpeg"},
        {"width": 1920, "height": 1080, "fps": 30.0, "input_format": "mjpeg"},
        {"width": 1280, "height": 720, "fps": 60.0, "input_format": "mjpeg"},
    ]

    assert _choose_profile(profiles, VideoSettings()) == profiles[1]


def test_camera_profile_parsers_preserve_ffmpeg_pixel_format_names() -> None:
    v4l2 = _parse_v4l2_profiles(
        "[0]: 'YUYV'\nSize: Discrete 1280x720\n"
        "Interval: Discrete 0.033s (30.000 fps)\n"
    )
    assert v4l2[0]["input_format"] == "yuyv422"

    dshow = _parse_dshow_profiles(
        "pixel_format=yuyv422 min s=640x480 fps=30 max s=1920x1080 fps=30\n"
    )
    assert dshow[0] == {
        "width": 640,
        "height": 480,
        "fps": 30.0,
        "input_format": "yuyv422",
    }


def test_directshow_parser_keeps_none_typed_virtual_camera_for_diagnostics() -> None:
    devices = video_module._parse_dshow_devices(
        '[dshow] "OBS Virtual Camera" (none)\n'
        '[dshow]   Alternative name "@device_sw_{example}"\n'
        '[dshow] "Microphone" (audio)\n'
        '[dshow]   Alternative name "@device_cm_{audio}"\n'
    )

    assert devices == [
        {"name": "OBS Virtual Camera", "alternative": "@device_sw_{example}"}
    ]


def test_avfoundation_parser_excludes_audio_devices() -> None:
    devices = _parse_avfoundation_devices(
        "[AVFoundation indev] AVFoundation video devices:\n"
        "[AVFoundation indev] [0] FaceTime HD Camera\n"
        "[AVFoundation indev] [1] Logitech C93\n"
        "[AVFoundation indev] AVFoundation audio devices:\n"
        "[AVFoundation indev] [0] MacBook Microphone\n"
    )

    assert devices == [
        {"index": "0", "name": "FaceTime HD Camera"},
        {"index": "1", "name": "Logitech C93"},
    ]


def test_macos_13_device_types_include_legacy_external_camera() -> None:
    class FakeAVFoundation:
        AVCaptureDeviceTypeExternalUnknown = "legacy-external"
        AVCaptureDeviceTypeBuiltInWideAngleCamera = "built-in"

    assert macos_devices._device_types(FakeAVFoundation) == [
        "legacy-external",
        "built-in",
    ]


@pytest.mark.asyncio
async def test_preview_frame_hub_keeps_one_session_across_video_process_swaps() -> None:
    hub = PreviewFrameHub()

    session_id = hub.activate()
    assert hub.activate() == session_id
    initial = hub.snapshot()
    waiter = asyncio.create_task(
        hub.wait_for_change(session_id, initial.generation, timeout=1.0)
    )

    # 第一段 FFmpeg 进程留下最后一帧；替换进程期间浏览器通道仍保持活动。
    first_frame = b"\xff\xd8first\xff\xd9"
    hub.publish(first_frame)
    changed = await waiter
    assert changed.session_id == session_id
    assert changed.jpeg == first_frame
    assert changed.active

    # 第二段 FFmpeg 继续向同一通道发布，不产生新的浏览器 URL。
    second_frame = b"\xff\xd8second\xff\xd9"
    hub.publish(second_frame)
    swapped = hub.snapshot()
    assert swapped.session_id == session_id
    assert swapped.jpeg == second_frame

    hub.deactivate()
    closed = hub.snapshot()
    assert not closed.active
    assert closed.jpeg is None


def cameras() -> list[dict]:
    return [
        {
            "device": "/dev/video0",
            "camera_id": "bison|if=00",
            "supports_default_profile": True,
            "color_capture": True,
            "integration": "internal",
        },
        {
            "device": "/dev/video2",
            "camera_id": "bison|if=02",
            "supports_default_profile": False,
            "color_capture": False,
            "integration": "internal",
        },
    ]


def test_camera_selection_uses_stable_id_and_rejects_auxiliary_node() -> None:
    settings = VideoSettings()
    assert select_video_device(cameras(), settings, "bison|if=00")["device"] == "/dev/video0"
    with pytest.raises(RuntimeError, match="不支持"):
        select_video_device(cameras(), settings, "bison|if=02")


def test_camera_selection_rejects_stale_id() -> None:
    with pytest.raises(RuntimeError, match="当前不可用"):
        select_video_device(cameras(), VideoSettings(), "missing|if=00")


def test_camera_selection_prefers_compatible_external_camera() -> None:
    candidates = cameras() + [
        {
            "device": "/dev/video4",
            "camera_id": "logitech|if=00",
            "supports_default_profile": True,
            "color_capture": True,
            "integration": "external",
        }
    ]

    assert select_video_device(candidates, VideoSettings())["device"] == "/dev/video4"


def test_recorder_disables_b_frames_and_preserves_source_timestamps(
    tmp_path,
) -> None:
    command = FFmpegVideoRecorder(
        VideoSettings(vaapi_device=None), "/dev/video0", tmp_path / "capture.mkv"
    ).command()

    b_frame_index = command.index("-bf")
    assert command[b_frame_index + 1] == "0"
    index = command.index("-fps_mode")
    assert command[index + 1] == "vfr"


def test_preview_only_recorder_has_no_file_output(tmp_path) -> None:
    del tmp_path
    command = FFmpegVideoRecorder(
        VideoSettings(vaapi_device=None), "/dev/video0", None
    ).command()

    assert "matroska" not in command
    assert "libx264" not in command
    assert command[-1] == "pipe:1"
    assert "image2pipe" in command
    assert "-stats_enc_pre" in command
    assert "source {n} {pts} {tb}" in command


def test_windows_recorder_uses_directshow_and_selected_profile(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(video_module, "platform_id", lambda: "windows")
    monkeypatch.setattr(video_module, "resolve_executable", lambda name: f"{name}.exe")
    command = FFmpegVideoRecorder(
        VideoSettings(vaapi_device=None),
        "Integrated Camera",
        tmp_path / "capture.mkv",
        profile={
            "width": 1280,
            "height": 720,
            "fps": 30.0,
            "input_format": "mjpeg",
        },
    ).command()

    assert command[0] == "ffmpeg.exe"
    assert command[command.index("-f") + 1] == "dshow"
    assert "video=Integrated Camera" in command
    assert "1280x720" in command
    assert "/dev/video0" not in command


def test_macos_recorder_uses_avfoundation(monkeypatch) -> None:
    monkeypatch.setattr(video_module, "platform_id", lambda: "macos")
    monkeypatch.setattr(video_module, "resolve_executable", lambda name: name)
    command = FFmpegVideoRecorder(VideoSettings(vaapi_device=None), "0", None).command()

    assert command[command.index("-f") + 1] == "avfoundation"
    assert "0:none" in command


def test_macos_recording_prefers_verified_videotoolbox(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(video_module, "platform_id", lambda: "macos")
    recorder = FFmpegVideoRecorder(
        VideoSettings(vaapi_device=None), "1", tmp_path / "capture.mkv"
    )
    recorder.recording_encoder = "h264_videotoolbox"

    command = recorder.command()

    assert "h264_videotoolbox" in command
    assert "-allow_sw" in command
    assert "libx264" not in command


@pytest.mark.asyncio
async def test_manual_camera_controls_are_applied_and_read_back(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    async def fake_run(*args: str, timeout_seconds: float = 10.0):
        del timeout_seconds
        calls.append(args)
        if any(item.startswith("--get-ctrl=") for item in args):
            return (
                0,
                "auto_exposure: 1 (Manual Mode)\n"
                "exposure_time_absolute: 200\n"
                "gain: 192\n"
                "exposure_dynamic_framerate: 0\n"
                "power_line_frequency: 1 (50 Hz)\n",
                "",
            )
        return 0, "", ""

    monkeypatch.setattr(video_module, "_run_capture", fake_run)
    monkeypatch.setattr(video_module, "platform_id", lambda: "linux")
    state = await apply_video_controls(
        VideoSettings(manual_controls_enabled=True), "/dev/video4"
    )

    assert state.ready
    assert state.effective["auto_exposure"] == 1
    assert "--set-ctrl=auto_exposure=1" in calls[0]


@pytest.mark.asyncio
async def test_normalize_video_timeline_uses_lossless_remux(
    monkeypatch, tmp_path
) -> None:
    source = tmp_path / "raw.mkv"
    output = tmp_path / "normalized.mkv"
    source.write_bytes(b"raw")
    captured: tuple[str, ...] = ()

    async def fake_run(*args: str, timeout_seconds: float = 10.0):
        nonlocal captured
        captured = args
        assert timeout_seconds == 300.0
        output.write_bytes(b"normalized")
        return 0, "", ""

    monkeypatch.setattr(video_module, "_run_capture", fake_run)

    await normalize_video_timeline(source, output)

    assert "copy" in captured
    assert captured[-1] == str(output)
    assert output.read_bytes() == b"normalized"


@pytest.mark.asyncio
async def test_discovery_skips_metadata_and_keeps_capture_nodes(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        video_module.glob,
        "glob",
        lambda _pattern: ["/dev/video4", "/dev/video5"],
    )

    async def fake_run(*args: str, timeout_seconds: float = 10.0):
        del timeout_seconds
        device_arg = next(item for item in args if "/dev/video" in item)
        device = device_arg.removeprefix("--name=")
        if args[0] == "udevadm":
            capability = ":capture:" if device.endswith("4") else ":metadata:"
            return (
                0,
                f"ID_V4L_CAPABILITIES={capability}\n"
                "ID_V4L_PRODUCT=罗技摄像头\n"
                "ID_SERIAL=logitech_123\n"
                "ID_USB_INTERFACE_NUM=00\n"
                "ID_INTEGRATION=external\n",
                "",
            )
        return (
            0,
            "[0]: 'MJPG'\nSize: Discrete 1920x1080\n"
            "Interval: Discrete 0.033s (30.000 fps)\n",
            "",
        )

    monkeypatch.setattr(video_module, "_run_capture", fake_run)
    monkeypatch.setattr(video_module, "platform_id", lambda: "linux")

    devices = await discover_video_devices(VideoSettings())

    assert [item["device"] for item in devices] == ["/dev/video4"]
    assert devices[0]["camera_id"] == "logitech_123|if=00"
    assert devices[0]["integration"] == "external"
    assert devices[0]["supports_default_profile"] is True


@pytest.mark.asyncio
async def test_macos_discovery_uses_stable_native_id_and_real_profiles(
    monkeypatch,
) -> None:
    monkeypatch.setattr(video_module, "platform_id", lambda: "macos")
    monkeypatch.setattr(
        macos_devices,
        "enumerate_video_devices",
        lambda: [
            {
                "unique_id": "USB-LOGITECH-123",
                "name": "Logitech C93",
                "device_type": "AVCaptureDeviceTypeExternal",
                "integration": "external",
                "profiles": [
                    {
                        "width": 1920,
                        "height": 1080,
                        "fps": 30.0,
                        "min_fps": 5.0,
                        "input_format": "backend_default",
                    }
                ],
            }
        ],
    )

    async def fake_run(*args: str, timeout_seconds: float = 10.0):
        del args, timeout_seconds
        return (
            0,
            "",
            "[AVFoundation indev] AVFoundation video devices:\n"
            "[AVFoundation indev] [2] Logitech C93\n"
            "[AVFoundation indev] AVFoundation audio devices:\n",
        )

    monkeypatch.setattr(video_module, "_run_capture", fake_run)

    devices = await discover_video_devices(VideoSettings())

    assert devices[0]["device"] == "2"
    assert devices[0]["camera_id"] == "avfoundation:USB-LOGITECH-123"
    assert devices[0]["integration"] == "external"
    assert devices[0]["selected_profile"]["fps"] == 30.0
