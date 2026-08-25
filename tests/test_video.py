import pytest

from imu_data_collector.config import VideoSettings
from imu_data_collector.video import select_video_device


def cameras() -> list[dict]:
    return [
        {
            "device": "/dev/video0",
            "camera_id": "bison|if=00",
            "supports_default_profile": True,
            "color_capture": True,
        },
        {
            "device": "/dev/video2",
            "camera_id": "bison|if=02",
            "supports_default_profile": False,
            "color_capture": False,
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
