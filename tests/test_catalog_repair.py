from __future__ import annotations

import json
from pathlib import Path

import h5py

from imu_data_collector.catalog import RecordingCatalog
from imu_data_collector.catalog_repair import (
    REPAIR_CONFIRMATION,
    apply_catalog_repair,
    build_catalog_repair_plan,
)
from imu_data_collector.models import (
    BackgroundJobKind,
    PublishTarget,
    RecordingState,
    RecordingSummary,
)
from imu_data_collector.storage import LocalFilesystemStore


def _local_capture(
    data_root: Path, recording_id: str, collection_id: str, residual_ns: int = 0
) -> tuple[str, str]:
    directory = data_root / collection_id / recording_id
    directory.mkdir(parents=True)
    h5_path = directory / f"{recording_id}.h5"
    mkv_path = directory / f"{recording_id}.mkv"
    with h5py.File(h5_path, "w") as handle:
        handle.attrs["recording_id"] = recording_id
        handle.attrs["collection_id"] = collection_id
        imu = handle.create_group("imu")
        imu.attrs["packet_end_fit_residual_max_abs_ns"] = residual_ns
    mkv_path.write_bytes(b"mkv")
    return str(h5_path), str(mkv_path)


def _remote_receipt(
    store: LocalFilesystemStore, recording_id: str, *, status: str = "indexed"
) -> None:
    manifest = store.write_json(
        f"captures/{recording_id}/manifest.json",
        {"recording_id": recording_id},
        if_generation_match=0,
    )
    store.write_json(
        f"index-receipts/{recording_id}.json",
        {
            "schema_version": "1.0.0",
            "recording_id": recording_id,
            "manifest_generation": manifest.generation,
            "status": status,
            "annotation_build_id": "test",
            "processed_at_utc": "2026-08-31T00:00:00+00:00",
            "code": status,
            "message": "indexed" if status == "indexed" else "calibration",
        },
        if_generation_match=0,
    )


def test_catalog_repair_classifies_remote_truth_and_restores_findings(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    current_catalog = RecordingCatalog(tmp_path / "current.sqlite3")
    backup_catalog = RecordingCatalog(tmp_path / "backup.sqlite3")
    store = LocalFilesystemStore(tmp_path / "objects")
    fixtures = (
        ("20260830T010000.000000Z_user001", "current", "uploaded", 219_253_000),
        ("20260830T020000.000000Z_user001", "legacy", "published", 0),
        ("20260830T030000.000000Z_user001", "missing", "uploaded", 0),
        ("20260830T040000.000000Z_user001", "never", "not_requested", 600_000_000),
    )
    mappings = []
    for old_id, kind, upload_state, residual_ns in fixtures:
        new_id = old_id.rsplit("_", 1)[0]
        collection_id = "20260830_session_01"
        h5_path, mkv_path = _local_capture(
            data_root, new_id, collection_id, residual_ns
        )
        current_catalog.upsert(
            RecordingSummary(
                recording_id=new_id,
                collection_id=collection_id,
                data_tier="prod",
                state=RecordingState.READY,
                started_at_utc="2026-08-30T00:00:00+00:00",
                h5_path=h5_path,
                mkv_path=mkv_path,
            )
        )
        warnings = []
        validation = []
        if kind == "current":
            warnings = [
                "IMU packet timestamp maximum residual is 219.253 ms; "
                "warning threshold is 200 ms"
            ]
        if kind == "never":
            validation = [
                "IMU packet timestamp maximum residual exceeds 0.5 seconds"
            ]
        backup_catalog.upsert(
            RecordingSummary(
                recording_id=old_id,
                collection_id="20260830_user001_01",
                participant_id="user001",
                data_tier="prod",
                state=(
                    RecordingState.NEEDS_ATTENTION
                    if validation
                    else RecordingState.READY
                ),
                started_at_utc="2026-08-30T00:00:00+00:00",
                quality_warnings=warnings,
                validation_issues=validation,
                upload_state=upload_state,
                publish_target=(
                    "direct_gcs" if upload_state != "not_requested" else "disabled"
                ),
                index_state=(
                    "indexed" if upload_state != "not_requested" else "not_requested"
                ),
                manifest_generation=(1 if upload_state != "not_requested" else None),
            )
        )
        mappings.append(
            {"old_recording_id": old_id, "new_recording_id": new_id}
        )
        if kind == "current":
            _remote_receipt(store, new_id)
        elif kind == "legacy":
            _remote_receipt(store, old_id, status="rejected")
    first_old_id = fixtures[0][0]
    backup_catalog.enqueue_job(first_old_id, BackgroundJobKind.PUBLISH)
    backup_catalog.complete_job(first_old_id, BackgroundJobKind.PUBLISH)
    migration_path = tmp_path / "migration" / "plan.json"
    migration_path.parent.mkdir()
    migration_path.write_text(
        json.dumps({"migration_id": "test-migration", "captures": mappings}),
        encoding="utf-8",
    )

    plan = build_catalog_repair_plan(
        data_root=data_root,
        catalog=current_catalog,
        backup_catalog_path=backup_catalog.path,
        migration_plan_path=migration_path,
        store=store,
        calibration_recording_ids={fixtures[1][0]},
        publish_target=PublishTarget.DIRECT_GCS,
    )

    assert plan["counts"] == {
        "current_published": 1,
        "legacy_calibration_published": 1,
        "remote_missing": 1,
        "not_requested": 1,
        "quality_warning_recordings": 1,
        "blocking_validation_recordings": 1,
        "recording_jobs": 1,
        "upload_jobs": 0,
    }
    receipt = apply_catalog_repair(
        catalog=current_catalog,
        migration_plan_path=migration_path,
        plan=plan,
        plan_token=plan["plan_token"],
        confirmation=REPAIR_CONFIRMATION,
    )
    assert receipt["status"] == "complete"
    by_kind = {
        kind: current_catalog.get(old_id.rsplit("_", 1)[0])
        for old_id, kind, _state, _residual in fixtures
    }
    assert by_kind["current"].upload_state == "uploaded"  # type: ignore[union-attr]
    assert by_kind["legacy"].upload_state == "legacy_published"  # type: ignore[union-attr]
    assert by_kind["missing"].upload_state == "remote_missing"  # type: ignore[union-attr]
    assert by_kind["never"].upload_state == "not_requested"  # type: ignore[union-attr]
    assert by_kind["current"].quality_warnings  # type: ignore[union-attr]
    assert by_kind["never"].validation_issues  # type: ignore[union-attr]
    assert by_kind["current"].upload_job is not None  # type: ignore[union-attr]
    repeated = apply_catalog_repair(
        catalog=current_catalog,
        migration_plan_path=migration_path,
        plan=plan,
        plan_token=plan["plan_token"],
        confirmation=REPAIR_CONFIRMATION,
    )
    assert repeated["first_completed_at_utc"] == receipt["completed_at_utc"]
