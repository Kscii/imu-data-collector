from pathlib import Path

import h5py

from imu_data_collector.config import Settings, StorageSettings
from imu_data_collector.models import DataTier, RecordingState, RecordingSummary
from imu_data_collector.publisher import prepare_publication, publish_recording
from imu_data_collector.storage import LocalFilesystemStore


async def test_publish_uses_h5_formal_start_and_repairs_stale_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    async def directly(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr("imu_data_collector.publisher.asyncio.to_thread", directly)
    recording_id = "20260827T014121.043236Z"
    formal_start = "2026-08-27T01:41:21.351792+00:00"
    directory = tmp_path / recording_id
    directory.mkdir()
    h5_path = directory / f"{recording_id}.h5"
    mkv_path = directory / f"{recording_id}.mkv"
    mkv_path.write_bytes(b"mkv")
    with h5py.File(h5_path, "w") as handle:
        handle.attrs.update(
            {
                "recording_id": recording_id,
                "collection_id": "20260827_session_01",
                "identity_contract_version": "2.0.0",
                "participant_assignment_source": "review",
                "data_tier": "prod",
                "body_location": "chest",
                "capture_schema_version": "1.8.0",
                "sensor_sn": "IMU-0002-R01",
                "device_profile_sha256": "b" * 64,
                "started_at_utc": formal_start,
                "duration_ns": 26_523_294_758,
                "calibration_verified": False,
            }
        )
        imu = handle.create_group("imu")
        imu.attrs.update(
            {
                "sensor_sn": "IMU-0002-R01",
                "device_profile_sha256": "b" * 64,
                "protocol": "acce_gyro_abf0_v1",
                "firmware_version": "stock-unknown",
                "firmware_evidence_status": "user_confirmed_not_v0.0.4",
            }
        )

    async def preview(_mkv_path: Path, output_path: Path, **_kwargs) -> Path:
        output_path.write_bytes(b"preview")
        return output_path

    monkeypatch.setattr("imu_data_collector.publisher.build_preview_mp4", preview)
    settings = Settings(
        storage=StorageSettings(backend="local", root=tmp_path / "objects")
    )
    store = LocalFilesystemStore(settings.storage.root)
    summary = RecordingSummary(
        recording_id=recording_id,
        collection_id="20260827_session_01",
        data_tier=DataTier.PROD,
        state=RecordingState.READY,
        started_at_utc="2026-08-27T01:41:21.043236+00:00",
        duration_ns=26_523_294_758,
        h5_path=str(h5_path),
        mkv_path=str(mkv_path),
    )

    manifest, first_generation = await publish_recording(summary, settings, store)
    assert manifest.captured_at_utc == formal_start
    assert manifest.schema_version == "3.1.0"
    assert manifest.sensor is not None
    assert manifest.sensor.sensor_sn == "IMU-0002-R01"
    assert manifest.sensor.protocol_id == "acce_gyro_abf0_v1"

    key = f"captures/{recording_id}/manifest.json"
    legacy, current_generation = store.read_json(key)
    legacy["captured_at_utc"] = summary.started_at_utc
    store.write_json(key, legacy, if_generation_match=current_generation)

    repaired, repaired_generation = await publish_recording(summary, settings, store)
    stored, _generation = store.read_json(key)

    assert repaired.captured_at_utc == formal_start
    assert stored["captured_at_utc"] == formal_start
    assert repaired_generation > first_generation


async def test_legacy_unassigned_capture_keeps_manifest_3_0(
    tmp_path: Path, monkeypatch
) -> None:
    async def directly(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr("imu_data_collector.publisher.asyncio.to_thread", directly)
    recording_id = "20260908T100000.000000Z"
    directory = tmp_path / recording_id
    directory.mkdir()
    h5_path = directory / f"{recording_id}.h5"
    mkv_path = directory / f"{recording_id}.mkv"
    mkv_path.write_bytes(b"mkv")
    with h5py.File(h5_path, "w") as handle:
        handle.attrs.update(
            {
                "recording_id": recording_id,
                "collection_id": "legacy-config",
                "identity_contract_version": "2.0.0",
                "participant_assignment_source": "review",
                "data_tier": "test",
                "body_location": "chest",
                "capture_schema_version": "1.8.0",
                "sensor_sn": "legacy-unassigned",
                "device_profile_sha256": "legacy",
                "started_at_utc": "2026-09-08T10:00:00+00:00",
                "duration_ns": 1,
                "calibration_verified": False,
            }
        )
        handle.create_group("imu")

    async def preview(_mkv_path: Path, output_path: Path, **_kwargs) -> Path:
        output_path.write_bytes(b"preview")
        return output_path

    monkeypatch.setattr("imu_data_collector.publisher.build_preview_mp4", preview)
    summary = RecordingSummary(
        recording_id=recording_id,
        collection_id="legacy-config",
        data_tier=DataTier.TEST,
        state=RecordingState.READY,
        started_at_utc="2026-09-08T10:00:00+00:00",
        duration_ns=1,
        h5_path=str(h5_path),
        mkv_path=str(mkv_path),
    )

    manifest, _paths = await prepare_publication(summary, Settings())

    assert manifest.schema_version == "3.0.0"
    assert manifest.sensor is None


async def test_configuration_backed_capture_publishes_manifest_3_2(
    tmp_path: Path, monkeypatch
) -> None:
    async def directly(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr("imu_data_collector.publisher.asyncio.to_thread", directly)
    recording_id = "20260909T010203.000000Z"
    directory = tmp_path / recording_id
    directory.mkdir()
    h5_path = directory / f"{recording_id}.h5"
    mkv_path = directory / f"{recording_id}.mkv"
    mkv_path.write_bytes(b"mkv")
    with h5py.File(h5_path, "w") as handle:
        handle.attrs.update(
            {
                "recording_id": recording_id,
                "collection_id": "configuration-v2",
                "identity_contract_version": "2.0.0",
                "participant_assignment_source": "review",
                "data_tier": "test",
                "body_location": "chest",
                "capture_schema_version": "1.9.0",
                "sensor_sn": "IMU-0002-R01",
                "device_profile_sha256": "a" * 64,
                "started_at_utc": "2026-09-09T01:02:03+00:00",
                "duration_ns": 1,
                "calibration_verified": False,
                "configuration_snapshot_id": "local-cfg-" + "b" * 24,
                "configuration_snapshot_sha256": "c" * 64,
                "configuration_content_sha256": "d" * 64,
                "configuration_source": "local",
                "configuration_approval_state": "local",
                "configuration_checked_at_utc": "2026-09-09T01:00:00+00:00",
                "si_profile_id": "si-" + "e" * 24,
            }
        )
        imu = handle.create_group("imu")
        imu.attrs.update(
            {
                "protocol": "acce_gyro_abf0_v1",
                "firmware_version": "stock-unknown",
                "firmware_evidence_status": "unreadable",
                "si_profile_id": "si-" + "e" * 24,
            }
        )

    async def preview(_mkv_path: Path, output_path: Path, **_kwargs) -> Path:
        output_path.write_bytes(b"preview")
        return output_path

    monkeypatch.setattr("imu_data_collector.publisher.build_preview_mp4", preview)
    summary = RecordingSummary(
        recording_id=recording_id,
        collection_id="configuration-v2",
        data_tier=DataTier.TEST,
        state=RecordingState.READY,
        started_at_utc="2026-09-09T01:02:03+00:00",
        duration_ns=1,
        h5_path=str(h5_path),
        mkv_path=str(mkv_path),
    )

    manifest, _paths = await prepare_publication(summary, Settings())

    assert manifest.schema_version == "3.2.0"
    assert manifest.sensor is not None
    assert manifest.configuration is not None
    assert manifest.configuration.snapshot_id == "local-cfg-" + "b" * 24
    assert manifest.configuration.approval_state_at_capture == "local"
    assert manifest.configuration.si_profile_id == "si-" + "e" * 24
