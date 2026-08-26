import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from imu_data_collector.ble import NotificationPacket
from imu_data_collector.config import Settings
from imu_data_collector.coordinator import RecordingCoordinator
from imu_data_collector.cw12eu import pack_test_frame
from imu_data_collector.models import PreviewStartRequest, RecordingStartRequest


class _FakeVideo:
    def __init__(self, device: str) -> None:
        self.device = device
        self.started_monotonic_ns: int | None = None
        self.starts = 0
        self.stops = 0
        self.progress = SimpleNamespace(frame=0, fps=0.0, bitrate="0", speed="0x")

    async def start(self) -> None:
        self.starts += 1
        self.started_monotonic_ns = time.monotonic_ns()

    async def stop(self) -> None:
        self.stops += 1


class _FakeBle:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[NotificationPacket] = asyncio.Queue()
        self.connected = True
        self.last_packet_ns = None
        self.dropped_callback_packets = 7
        self.starts = 0

    async def start(self) -> None:
        self.starts += 1

    async def stop(self) -> None:
        self.connected = False


@pytest.mark.asyncio
async def test_imu_preview_parses_in_memory_without_creating_capture_files(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    coordinator = RecordingCoordinator(
        Settings(
            data_root=data_root,
            catalog_path=tmp_path / "catalog.sqlite3",
            activity_taxonomy_path=Path("configs/activities.yaml").resolve(),
            minimum_free_gib=0,
        )
    )
    queue: asyncio.Queue[NotificationPacket] = asyncio.Queue()
    start_ns = time.monotonic_ns() - 1_000_000_000
    payload_a = b"".join(
        (
            pack_test_frame((1, 2, 3, 4, 5, 6), b"\x00\x00\x00\x01"),
            pack_test_frame((7, 8, 9, 10, 11, 12), b"\x00\x00\x00\x02"),
        )
    )
    payload_b = b"".join(
        (
            pack_test_frame((13, 14, 15, 16, 17, 18), b"\x00\x00\x00\x03"),
            pack_test_frame((19, 20, 21, 22, 23, 24), b"\x00\x00\x00\x04"),
        )
    )
    queue.put_nowait(NotificationPacket(payload_a, start_ns))
    queue.put_nowait(NotificationPacket(payload_b, start_ns + 1_000_000_000))
    coordinator.ble = SimpleNamespace(
        queue=queue,
        connected=True,
        last_packet_ns=start_ns + 1_000_000_000,
        dropped_callback_packets=0,
    )
    coordinator.mode = "devices_preview"
    coordinator._stop_consumer.set()

    await coordinator._consume_imu_preview()

    snapshot = coordinator.snapshot()
    assert snapshot["session_type"] == "devices_preview"
    assert snapshot["imu"]["packet_count"] == 2
    assert snapshot["imu"]["sample_count"] == 4
    assert snapshot["imu"]["notification_rate_hz"] == pytest.approx(1.0)
    assert snapshot["imu"]["estimated_sample_rate_hz"] == pytest.approx(2.0)
    assert np.array_equal(coordinator.latest_raw, [19, 20, 21, 22, 23, 24])
    assert list(data_root.iterdir()) == []


@pytest.mark.asyncio
async def test_switch_preview_camera_keeps_ble_connected(tmp_path: Path) -> None:
    coordinator = RecordingCoordinator(
        Settings(
            data_root=tmp_path / "data",
            catalog_path=tmp_path / "catalog.sqlite3",
            activity_taxonomy_path=Path("configs/activities.yaml").resolve(),
        )
    )
    (tmp_path / "data").mkdir()
    ble = _FakeBle()
    old_video = _FakeVideo("/dev/video-old")
    new_video = _FakeVideo("/dev/video-new")
    coordinator.mode = "devices_preview"
    coordinator.ble = ble  # type: ignore[assignment]
    coordinator.video = old_video  # type: ignore[assignment]
    coordinator._preview_camera_id = "old"

    async def resolve(_camera_id: str | None = None) -> dict[str, str]:
        return {"camera_id": "new", "device": "/dev/video-new"}

    coordinator._resolve_camera = resolve  # type: ignore[method-assign]
    coordinator._new_video_recorder = lambda _device, _path: new_video  # type: ignore[method-assign]

    snapshot = await coordinator.switch_preview_camera(
        PreviewStartRequest(camera_id="new")
    )

    assert old_video.stops == 1
    assert new_video.starts == 1
    assert coordinator.ble is ble
    assert ble.connected
    assert ble.starts == 0
    assert snapshot["session_type"] == "devices_preview"


@pytest.mark.asyncio
async def test_start_recording_reuses_preview_ble(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    coordinator = RecordingCoordinator(
        Settings(
            data_root=data_root,
            catalog_path=tmp_path / "catalog.sqlite3",
            activity_taxonomy_path=Path("configs/activities.yaml").resolve(),
            minimum_free_gib=0,
        )
    )
    ble = _FakeBle()
    preview_video = _FakeVideo("/dev/video-preview")
    capture_video = _FakeVideo("/dev/video-capture")
    coordinator.mode = "devices_preview"
    coordinator.ble = ble  # type: ignore[assignment]
    coordinator.video = preview_video  # type: ignore[assignment]

    async def resolve(_camera_id: str | None = None) -> dict[str, str]:
        return {
            "camera_id": "camera-1",
            "device": "/dev/video-capture",
            "product": "fixture",
            "interface": "00",
        }

    coordinator._resolve_camera = resolve  # type: ignore[method-assign]
    coordinator._new_video_recorder = lambda _device, _path: capture_video  # type: ignore[method-assign]

    try:
        summary = await coordinator.start(
            RecordingStartRequest(
                collection_id="reuse_ble",
                participant_id="xfan0282",
                camera_id="camera-1",
            )
        )
        assert summary.state.value == "recording"
        assert coordinator.ble is ble
        assert ble.connected
        assert ble.starts == 0
        assert ble.dropped_callback_packets == 0
        assert preview_video.stops == 1
        assert capture_video.starts == 1
        assert coordinator.writer is not None
        events = coordinator.writer.handle["imu/connection_events/event"][:].astype(str)
        assert "ble_reused_from_preview" in events
    finally:
        coordinator._stop_consumer.set()
        if coordinator._consumer:
            coordinator._consumer.cancel()
            await asyncio.gather(coordinator._consumer, return_exceptions=True)
        if coordinator.writer:
            coordinator.writer.abort_close()
