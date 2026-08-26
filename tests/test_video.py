import pytest

import imu_data_collector.video as video_module
from imu_data_collector.config import VideoSettings
from imu_data_collector.video import (
    FFmpegVideoRecorder,
    discover_video_devices,
    normalize_video_timeline,
    select_video_device,
)


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
        assert timeout_seconds == 180.0
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

    devices = await discover_video_devices(VideoSettings())

    assert [item["device"] for item in devices] == ["/dev/video4"]
    assert devices[0]["camera_id"] == "logitech_123|if=00"
    assert devices[0]["integration"] == "external"
    assert devices[0]["supports_default_profile"] is True
