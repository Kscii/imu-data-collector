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
