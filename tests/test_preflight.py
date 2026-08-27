import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import h5py
import numpy as np
import pytest

from imu_data_collector.ble import NotificationPacket
from imu_data_collector.config import Settings
from imu_data_collector.coordinator import RecordingCoordinator
from imu_data_collector.cw12eu import pack_test_frame
from imu_data_collector.hdf5_store import CaptureH5Writer
from imu_data_collector.models import (
    BackgroundJobKind,
    DataTier,
    DeviceSessionState,
    PreviewStartRequest,
    RecordingStartRequest,
    RecordingState,
    RecordingSummary,
)
from imu_data_collector.validation import ValidationReport


class _FakeVideo:
    def __init__(self, device: str) -> None:
        self.device = device
        self.started_monotonic_ns: int | None = None
        self.starts = 0
        self.stops = 0
        self.progress = SimpleNamespace(frame=0, fps=0.0, bitrate="0", speed="0x")
        self.source_fps = 30.0
        self.preview_fps = 10.0
        self.source_frame_count = 30
        self.control_state = SimpleNamespace(ready=True, errors=[])
        self.frame = 0
        self.bitrate = "0"
        self.speed = "0x"

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
        self.stops = 0
        self.disconnect_reason = None

    async def start(self) -> None:
        self.starts += 1

    async def stop(self) -> None:
        self.stops += 1
        self.connected = False


def _coordinator(tmp_path: Path) -> RecordingCoordinator:
    data_root = tmp_path / "data"
    data_root.mkdir(exist_ok=True)
    return RecordingCoordinator(
        Settings(
            data_root=data_root,
            catalog_path=tmp_path / "catalog.sqlite3",
            activity_taxonomy_path=Path("configs/activities.yaml").resolve(),
            minimum_free_gib=0,
        )
    )


def test_startup_revalidation_allows_warning_and_preserves_operational_issue(
    tmp_path: Path,
    monkeypatch,
) -> None:
    coordinator = _coordinator(tmp_path)
    warning = (
        "IMU packet timestamp maximum residual is 219.253 ms; "
        "warning threshold is 200 ms"
    )
    for recording_id, operational_issues in (
        ("warning-only", []),
        ("operational-failure", ["摄像头收尾失败"]),
    ):
        directory = tmp_path / "data" / "pilot" / recording_id
        directory.mkdir(parents=True)
        h5_path = directory / f"{recording_id}.h5"
        mkv_path = directory / f"{recording_id}.mkv"
        h5_path.write_bytes(b"fixture")
        mkv_path.write_bytes(b"fixture")
        coordinator.catalog.upsert(
            RecordingSummary(
                recording_id=recording_id,
                collection_id="pilot",
                participant_id="xfan0282",
                data_tier="prod",
                state=RecordingState.NEEDS_ATTENTION,
                started_at_utc="2026-08-27T00:00:00Z",
                h5_path=str(h5_path),
                mkv_path=str(mkv_path),
                issues=operational_issues,
                validation_issues=[
                    "IMU packet timestamp maximum residual exceeds 0.2 seconds"
                ],
            )
        )

    monkeypatch.setattr(
        "imu_data_collector.coordinator.validate_capture_h5",
        lambda *_args, **_kwargs: ValidationReport(True, (), (warning,), {}),
    )

    result = coordinator.revalidate_unuploaded_recordings()

    allowed = coordinator.catalog.get("warning-only")
    blocked = coordinator.catalog.get("operational-failure")
    assert result == {
        "scanned": 2,
        "updated": 2,
        "ready": 1,
        "still_blocked": 1,
        "skipped": 0,
    }
    assert allowed is not None
    assert allowed.state == RecordingState.READY
    assert allowed.validation_issues == []
    assert allowed.quality_warnings == [warning]
    assert blocked is not None
    assert blocked.state == RecordingState.NEEDS_ATTENTION
    assert blocked.issues == ["摄像头收尾失败"]
    assert blocked.quality_warnings == [warning]


def test_startup_revalidation_does_not_touch_uploaded_recording(
    tmp_path: Path,
    monkeypatch,
) -> None:
    coordinator = _coordinator(tmp_path)
    summary = RecordingSummary(
        recording_id="uploaded-recording",
        collection_id="pilot",
        participant_id="xfan0282",
        data_tier="prod",
        state=RecordingState.NEEDS_ATTENTION,
        started_at_utc="2026-08-27T00:00:00Z",
        upload_state="uploaded",
        validation_issues=["旧验证结论"],
    )
    coordinator.catalog.upsert(summary)
    called = False

    def validate(*_args, **_kwargs):
        nonlocal called
        called = True
        return ValidationReport(True, (), (), {})

    monkeypatch.setattr(
        "imu_data_collector.coordinator.validate_capture_h5",
        validate,
    )

    result = coordinator.revalidate_unuploaded_recordings()

    assert result["scanned"] == 0
    assert result["skipped"] == 1
    assert not called
    assert coordinator.catalog.get("uploaded-recording") == summary


@pytest.mark.asyncio
async def test_release_preview_is_idempotent_and_cleans_stale_session(
    tmp_path: Path,
) -> None:
    coordinator = _coordinator(tmp_path)
    ble = _FakeBle()
    ble.connected = False
    video = _FakeVideo("/dev/video-stale")
    coordinator.mode = "devices_preview"
    coordinator.ble = ble  # type: ignore[assignment]
    coordinator.video = video  # type: ignore[assignment]
    coordinator._monitoring_requested = True

    first = await coordinator.stop_preview()
    second = await coordinator.stop_preview()

    assert first["session_type"] is None
    assert second["session_type"] is None
    assert second["device"]["state"] == "idle"
    assert coordinator.ble is None
    assert coordinator.video is None
    assert video.stops == 1


@pytest.mark.asyncio
async def test_release_can_cancel_preview_while_connection_is_pending(
    tmp_path: Path,
) -> None:
    coordinator = _coordinator(tmp_path)
    connecting = asyncio.Event()
    continue_connection = asyncio.Event()
    ble = _FakeBle()
    video = _FakeVideo("/dev/video-new")

    async def open_ble():
        connecting.set()
        await continue_connection.wait()
        return ble

    async def open_video(_camera_id: str | None):
        return {"camera_id": "fixture", "device": video.device}, video

    coordinator._open_preview_ble = open_ble  # type: ignore[method-assign]
    coordinator._open_preview_video = open_video  # type: ignore[method-assign]
    start_task = asyncio.create_task(
        coordinator.start_preview(PreviewStartRequest(camera_id="fixture"))
    )
    await asyncio.wait_for(connecting.wait(), timeout=1.0)

    with pytest.raises(RuntimeError, match="已有设备连接操作"):
        await coordinator.start_preview(PreviewStartRequest(camera_id="fixture"))

    released = await asyncio.wait_for(coordinator.stop_preview(), timeout=1.0)
    continue_connection.set()

    with pytest.raises(RuntimeError, match="释放操作取消"):
        await start_task
    assert released["device"]["state"] == "idle"
    assert coordinator.mode is None
    assert not ble.connected
    assert video.stops == 1


@pytest.mark.asyncio
async def test_preview_release_cannot_stop_formal_recording(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    coordinator.mode = "capture"
    coordinator.state = RecordingState.RECORDING

    with pytest.raises(RuntimeError, match="正式录制"):
        await coordinator.stop_preview()

    assert coordinator.mode == "capture"
    assert coordinator.state.value == "recording"


@pytest.mark.asyncio
async def test_failed_ble_arming_does_not_create_user_capture_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator = _coordinator(tmp_path)

    class FailingBle(_FakeBle):
        async def start(self) -> None:
            raise TimeoutError

    async def resolve(_camera_id: str | None = None) -> dict[str, str]:
        return {
            "camera_id": "fixture",
            "device": "/dev/video-fixture",
            "product": "fixture",
            "interface": "00",
        }

    coordinator._resolve_camera = resolve  # type: ignore[method-assign]
    monkeypatch.setattr(
        "imu_data_collector.coordinator.CW12EUBleSource", lambda _settings: FailingBle()
    )

    with pytest.raises(TimeoutError):
        await coordinator.start(
            RecordingStartRequest(
                collection_id="arming_failure",
                participant_id="xfan0282",
                camera_id="fixture",
            )
        )

    assert coordinator.mode is None
    assert coordinator.current is None
    assert coordinator.device_state.value == "error"
    assert list(coordinator.settings.data_root.iterdir()) == []


@pytest.mark.asyncio
async def test_prod_preflight_rejects_low_camera_source_fps(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    video = _FakeVideo("/dev/video-fixture")
    video.source_fps = 28.5

    with pytest.raises(RuntimeError, match="输入帧率不足"):
        await coordinator._require_prod_camera_preflight(video, DataTier.PROD)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_test_tier_does_not_apply_prod_camera_gate(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    video = _FakeVideo("/dev/video-fixture")
    video.source_fps = 1.0
    video.control_state.ready = False

    await coordinator._require_prod_camera_preflight(video, DataTier.TEST)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_preview_watchdog_reconnects_once_after_unexpected_disconnect(
    tmp_path: Path,
) -> None:
    coordinator = _coordinator(tmp_path)
    old_ble = _FakeBle()
    old_ble.connected = False
    old_ble.disconnect_reason = "bluez_disconnected"
    old_video = _FakeVideo("/dev/video-old")
    new_ble = _FakeBle()
    coordinator.mode = "devices_preview"
    coordinator.ble = old_ble  # type: ignore[assignment]
    coordinator.video = old_video  # type: ignore[assignment]
    coordinator._monitoring_requested = True
    coordinator._preview_camera_id = "fixture"
    operation_id = coordinator._next_device_operation(DeviceSessionState.CONNECTED)

    async def open_ble():
        return new_ble

    coordinator._open_preview_ble = open_ble  # type: ignore[method-assign]
    coordinator._start_preview_watchdog(operation_id)

    deadline = asyncio.get_running_loop().time() + 2.5
    while coordinator.ble is not new_ble and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)

    assert coordinator.ble is new_ble
    assert coordinator.video is old_video
    assert old_video.stops == 0
    assert coordinator.device_state.value == "connected"
    await coordinator.stop_preview()


@pytest.mark.asyncio
async def test_initial_ble_failure_keeps_camera_preview_and_schedules_retry(
    tmp_path: Path,
) -> None:
    coordinator = _coordinator(tmp_path)
    video = _FakeVideo("/dev/video-new")

    async def open_ble():
        raise RuntimeError("fixture BLE unavailable")

    async def open_video(_camera_id: str | None):
        return {"camera_id": "fixture", "device": video.device}, video

    coordinator._open_preview_ble = open_ble  # type: ignore[method-assign]
    coordinator._open_preview_video = open_video  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="fixture BLE unavailable"):
        await coordinator.start_preview(PreviewStartRequest(camera_id="fixture"))

    assert coordinator.mode == "devices_preview"
    assert coordinator.ble is None
    assert coordinator.video is video
    assert coordinator.device_state == DeviceSessionState.ERROR
    assert coordinator.device_error is not None
    assert coordinator.device_error["component"] == "ble"
    await coordinator.stop_preview()


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

        result = await coordinator.stop()

        assert result.state == RecordingState.FINALIZING
        assert result.finalization_job is not None
        assert result.finalization_job.state.value == "queued"
        assert coordinator.mode == "devices_preview"
        assert coordinator.ble is ble
        assert ble.connected
        assert ble.stops == 0
        assert capture_video.stops == 1
        assert capture_video.starts == 2
        with h5py.File(Path(result.h5_path or ""), "r") as handle:
            persisted_events = handle["imu/connection_events/event"][:].astype(str)
            assert handle.attrs["state"] == "pending_finalization"
        assert "ble_retained_after_capture" in persisted_events
    finally:
        if coordinator.mode == "devices_preview":
            await coordinator.stop_preview()
        else:
            coordinator._stop_consumer.set()
            if coordinator._consumer:
                coordinator._consumer.cancel()
                await asyncio.gather(coordinator._consumer, return_exceptions=True)
        if coordinator.writer:
            coordinator.writer.abort_close()


@pytest.mark.asyncio
async def test_stop_freezes_capture_and_queues_finalization_before_preview_restore(
    tmp_path: Path,
) -> None:
    coordinator = _coordinator(tmp_path)
    recording_id = "recording-validation-lock"
    directory = coordinator.settings.data_root / "validation_lock" / recording_id
    directory.mkdir(parents=True)
    partial_h5 = directory / f"{recording_id}.partial.h5"
    partial_mkv = directory / f"{recording_id}.partial.mkv"
    partial_mkv.write_bytes(b"fixture mkv")
    request = RecordingStartRequest(
        collection_id="validation_lock",
        participant_id="xfan0282",
    )
    writer = CaptureH5Writer(
        partial_h5,
        request,
        recording_id,
        1_000_000_000,
        coordinator.settings.imu,
        coordinator.taxonomy,
    )
    payload = pack_test_frame((1, 2, 3, 4, 5, 6), b"\x00\x00\x00\x01")
    writer.append_notification(payload, 1_100_000_000)
    writer.append_notification(payload, 1_140_000_000)

    capture_video = _FakeVideo("/dev/video-capture")
    capture_video.control_state = SimpleNamespace(
        ready=True,
        errors=[],
        requested={"gain": 192},
        effective={"gain": 192},
    )
    capture_video.progress.errors = []
    preview_video = _FakeVideo("/dev/video-preview")
    ble = _FakeBle()
    summary = RecordingSummary(
        recording_id=recording_id,
        collection_id=request.collection_id,
        participant_id=request.participant_id,
        data_tier=request.data_tier,
        state=RecordingState.RECORDING,
        started_at_utc="2026-08-27T00:00:00+00:00",
        h5_path=str(partial_h5),
        mkv_path=str(partial_mkv),
    )
    coordinator.current = summary
    coordinator.catalog.upsert(summary)
    coordinator.state = RecordingState.RECORDING
    coordinator.mode = "capture"
    coordinator.writer = writer
    coordinator.video = capture_video  # type: ignore[assignment]
    coordinator.ble = ble  # type: ignore[assignment]

    async def restore_preview(_camera_id: str | None):
        persisted = coordinator.catalog.get(recording_id)
        assert coordinator.writer is None
        assert persisted is not None
        assert persisted.state == RecordingState.FINALIZING
        assert persisted.finalization_job is not None
        assert persisted.finalization_job.state.value == "queued"
        with h5py.File(partial_h5, "r") as handle:
            assert handle.attrs["state"] == "pending_finalization"
        return {"camera_id": "fixture"}, preview_video

    coordinator._open_preview_video = restore_preview  # type: ignore[method-assign]

    result = await coordinator.stop()

    assert result.state == RecordingState.FINALIZING
    assert result.finalization_job is not None
    assert result.finalization_job.state.value == "queued"
    assert coordinator.state == RecordingState.IDLE
    assert coordinator.writer is None
    assert coordinator.video is preview_video
    assert Path(result.h5_path or "").name == f"{recording_id}.partial.h5"

    with pytest.raises(ValueError, match="仍在执行或等待自动重试"):
        await coordinator.retry_finalization(recording_id)

    claimed = coordinator.catalog.claim_next_job()
    assert claimed is not None
    failed = coordinator.catalog.fail_job(
        recording_id,
        BackgroundJobKind.FINALIZE,
        "fixture failure",
        (),
    )
    assert failed.state.value == "failed"
    await coordinator.retry_finalization(recording_id)
    retried = coordinator.catalog.get_job(recording_id, BackgroundJobKind.FINALIZE)
    assert retried is not None
    assert retried[0].state.value == "queued"
    assert retried[1]["camera_controls_requested"] == {"gain": 192}
