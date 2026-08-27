from pathlib import Path

import h5py

from imu_data_collector.config import Settings, StorageSettings
from imu_data_collector.models import DataTier, RecordingState, RecordingSummary
from imu_data_collector.publisher import publish_recording
from imu_data_collector.storage import LocalFilesystemStore


async def test_publish_uses_h5_formal_start_and_repairs_legacy_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    recording_id = "20260827T014121.043236Z_xfan0282"
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
                "collection_id": "xfan0282_test_01",
                "participant_id": "xfan0282",
                "data_tier": "prod",
                "body_location": "chest",
                "capture_schema_version": "1.5.0",
                "started_at_utc": formal_start,
                "duration_ns": 26_523_294_758,
                "calibration_verified": False,
            }
        )
        handle.create_group("imu")

    async def preview(_mkv_path: Path, output_path: Path) -> Path:
        output_path.write_bytes(b"preview")
        return output_path

    monkeypatch.setattr("imu_data_collector.publisher.build_preview_mp4", preview)
    settings = Settings(
        storage=StorageSettings(backend="local", root=tmp_path / "objects")
    )
    store = LocalFilesystemStore(settings.storage.root)
    summary = RecordingSummary(
        recording_id=recording_id,
        collection_id="xfan0282_test_01",
        participant_id="xfan0282",
        data_tier=DataTier.PROD,
        state=RecordingState.READY,
        started_at_utc="2026-08-27T01:41:21.043236+00:00",
        duration_ns=26_523_294_758,
        h5_path=str(h5_path),
        mkv_path=str(mkv_path),
    )

    manifest, first_generation = await publish_recording(summary, settings, store)
    assert manifest.captured_at_utc == formal_start

    key = f"captures/{recording_id}/manifest.json"
    legacy, current_generation = store.read_json(key)
    legacy["captured_at_utc"] = summary.started_at_utc
    store.write_json(key, legacy, if_generation_match=current_generation)

    repaired, repaired_generation = await publish_recording(summary, settings, store)
    stored, _generation = store.read_json(key)

    assert repaired.captured_at_utc == formal_start
    assert stored["captured_at_utc"] == formal_start
    assert repaired_generation > first_generation
