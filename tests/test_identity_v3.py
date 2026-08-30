from __future__ import annotations

from pathlib import Path

import h5py
import numpy as np
import pytest

from imu_data_collector.annotation_service import AnnotationService
from imu_data_collector.config import AnnotationSettings, IdentitySettings, Settings
from imu_data_collector.hdf5_store import sha256_file
from imu_data_collector.identity_migration import (
    CONFIRMATION,
    CloudIdentityMigration,
    auto_migrate_local_identity,
)
from imu_data_collector.models import (
    AnnotationReviewWorkflowRequest,
    ArtifactDescriptor,
    CalibrationProfile,
    CaptureManifestV2,
    DataTier,
    ParticipantConfirmRequest,
    ParticipantEvidence,
    ParticipantSelectRequest,
    RecordingStartRequest,
    ReviewWorkflowState,
)
from imu_data_collector.storage import LocalFilesystemStore


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_root=tmp_path / "data",
        catalog_path=tmp_path / "capture.sqlite3",
        activity_taxonomy_path=Path("configs/activities.yaml").resolve(),
        calibration_evidence_path=Path("configs/calibration-evidence.yaml").resolve(),
        annotation=AnnotationSettings(
            catalog_path=tmp_path / "annotation.sqlite3",
            catalog_refresh_interval_s=0,
        ),
        identity=IdentitySettings(
            subject_ids={"xfan0282": "cw12eu:subject-001"}
        ),
    )


def _publish_legacy_fixture(
    tmp_path: Path,
    store: LocalFilesystemStore,
    recording_id: str = "20260830T071943.008380Z_xfan0282",
) -> CaptureManifestV2:
    source = tmp_path / "source"
    source.mkdir()
    h5_path = source / f"{recording_id}.h5"
    mkv_path = source / f"{recording_id}.mkv"
    preview_path = source / "preview.mp4"
    with h5py.File(h5_path, "w") as handle:
        handle.attrs.update(
            {
                "capture_schema_name": "imu_capture_hdf5",
                "capture_schema_version": "1.6.0",
                "recording_id": recording_id,
                "collection_id": "20260830_xfan0282_02",
                "participant_id": "xfan0282",
                "data_tier": "prod",
                "body_location": "chest",
                "started_at_utc": "2026-08-30T07:19:43.008380+00:00",
                "duration_ns": 1_000_000_000,
            }
        )
        handle.create_dataset("imu/samples/raw_counts", data=np.arange(24).reshape(4, 6))
        handle.create_dataset(
            "imu/samples/empty_optional",
            shape=(0,),
            maxshape=(None,),
            chunks=(16,),
            dtype=np.int64,
        )
        handle.create_dataset(
            "imu/connection_events/event",
            data=np.asarray(["connected", "disconnected"], dtype=object),
            dtype=h5py.string_dtype(encoding="utf-8"),
            chunks=(16,),
            maxshape=(None,),
        )
        video = handle.create_group("video")
        video.attrs["path"] = mkv_path.name
    mkv_path.write_bytes(b"mkv")
    preview_path.write_bytes(b"mp4")
    artifacts = []
    for role, path, content_type in (
        ("capture_h5", h5_path, "application/x-hdf5"),
        ("video_mkv", mkv_path, "video/x-matroska"),
        ("preview_mp4", preview_path, "video/mp4"),
    ):
        key = f"captures/{recording_id}/{path.name}"
        digest = sha256_file(path)
        store.put_file(
            path,
            key,
            content_type=content_type,
            metadata={"sha256": digest},
        )
        artifacts.append(
            ArtifactDescriptor(
                role=role,
                object_key=key,
                filename=path.name,
                size_bytes=path.stat().st_size,
                sha256=digest,
                content_type=content_type,
            )
        )
    manifest = CaptureManifestV2(
        schema_version="2.1.0",
        recording_id=recording_id,
        collection_id="20260830_xfan0282_02",
        participant_id="xfan0282",
        identity_mode="capture_declared",
        data_tier=DataTier.PROD,
        captured_at_utc="2026-08-30T07:19:43.008380+00:00",
        duration_ns=1_000_000_000,
        source_h5_schema_version="1.6.0",
        software_revision="legacy",
        calibration=CalibrationProfile(),
        artifacts=artifacts,
    )
    store.write_json(
        f"captures/{recording_id}/manifest.json",
        manifest.model_dump(mode="json"),
        if_generation_match=0,
    )
    return manifest


def test_capture_request_rejects_removed_participant_identity() -> None:
    with pytest.raises(ValueError, match="Extra inputs"):
        RecordingStartRequest(collection_id="20260830_session_01", participant_id="xfan0282")


def test_participant_requires_evidence_selection_and_explicit_confirmation(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = LocalFilesystemStore(tmp_path / "objects")
    legacy = _publish_legacy_fixture(tmp_path, store)
    payload = legacy.model_dump(mode="json")
    payload.update(
        {
            "schema_version": "3.0.0",
            "recording_id": "20260830T071943.008380Z",
            "collection_id": "20260830_session_01",
            "participant_id": None,
            "identity_mode": "annotation_required",
        }
    )
    payload["artifacts"] = [
        {
            **item,
            "object_key": item["object_key"].replace(
                legacy.recording_id, payload["recording_id"]
            ),
        }
        for item in payload["artifacts"]
    ]
    for old, new in zip(legacy.artifacts, payload["artifacts"], strict=True):
        info = store.stat(old.object_key)
        assert info is not None
        store.copy(
            old.object_key,
            new["object_key"],
            if_source_generation_match=info.generation,
        )
    store.write_json(
        f"captures/{payload['recording_id']}/manifest.json",
        CaptureManifestV2.model_validate(payload).model_dump(
            mode="json", exclude_none=True
        ),
        if_generation_match=0,
    )
    service = AnnotationService(settings, store)
    service.refresh()
    recording_id = payload["recording_id"]
    review = service.review(recording_id)
    assert review.participant_assignment.status == "unassigned"
    review = service.update_workflow(
        recording_id,
        AnnotationReviewWorkflowRequest(
            action="assign", expected_revision=review.revision
        ),
        "xfan0282",
    )
    selected = service.select_participant(
        recording_id,
        ParticipantSelectRequest(
            participant_id="xfan0282",
            expected_revision=review.revision,
            evidence=ParticipantEvidence(
                video_frame_index=12,
                video_time_ns=400_000_000,
            ),
        ),
        "xfan0282",
    )
    assert selected.participant_assignment.status == "selected"
    assert selected.participant_assignment.evidence.video_frame_index == 12
    confirmed = service.confirm_participant(
        recording_id,
        ParticipantConfirmRequest(
            participant_id="xfan0282", expected_revision=selected.revision
        ),
        "xfan0282",
    )
    assert confirmed.participant_assignment.status == "confirmed"
    assert confirmed.participant_assignment.confirmed_by == "xfan0282"


def test_cloud_migration_archives_neutralizes_reopens_and_guards_rollback(
    tmp_path: Path,
) -> None:
    store = LocalFilesystemStore(tmp_path / "objects")
    manifest = _publish_legacy_fixture(tmp_path, store)
    service = AnnotationService(_settings(tmp_path), store)
    service.refresh()
    review = service.review(manifest.recording_id)
    store.write_json(
        f"reviews/{manifest.recording_id}/review.json",
        review.model_copy(
            update={
                "revision": 4,
                "workflow": review.workflow.model_copy(
                    update={
                        "state": ReviewWorkflowState.COMPLETED,
                        "annotator_id": "xfan0282",
                    }
                ),
            }
        ).model_dump(mode="json"),
        if_generation_match=store.stat(
            f"reviews/{manifest.recording_id}/review.json"
        ).generation,
    )

    migration = CloudIdentityMigration(store, set())
    plan = migration.build_plan()
    receipt = migration.apply(plan["plan_token"], CONFIRMATION)
    new_id = "20260830T071943.008380Z"
    assert receipt["recording_count"] == 1
    assert store.stat(f"captures/{manifest.recording_id}/manifest.json") is None
    assert store.stat(
        f"identity-migrations/{plan['migration_id']}/archive/"
        f"captures/{manifest.recording_id}/manifest.json"
    ) is not None
    migrated_payload, _ = store.read_json(f"captures/{new_id}/manifest.json")
    assert migrated_payload["schema_version"] == "3.0.0"
    assert "participant_id" not in migrated_payload
    assert migrated_payload["collection_id"] == "20260830_session_01"

    migrated_h5 = tmp_path / "migrated.h5"
    store.download_file(f"captures/{new_id}/{new_id}.h5", migrated_h5)
    with h5py.File(migrated_h5, "r") as handle:
        assert "participant_id" not in handle.attrs
        assert handle.attrs["recording_id"] == new_id
        assert handle.attrs["identity_contract_version"] == "2.0.0"
        np.testing.assert_array_equal(
            handle["imu/samples/raw_counts"], np.arange(24).reshape(4, 6)
        )
        assert handle["imu/samples/empty_optional"].shape == (0,)
        assert handle["imu/connection_events/event"].asstr()[:].tolist() == [
            "connected",
            "disconnected",
        ]
    migrated_review, generation = store.read_json(f"reviews/{new_id}/review.json")
    assert migrated_review["workflow"]["state"] == "in_progress"
    assert migrated_review["participant_assignment"]["status"] == "unassigned"
    assert migrated_review["active_export"] is None

    migrated_review["revision"] += 1
    store.write_json(
        f"reviews/{new_id}/review.json",
        migrated_review,
        if_generation_match=generation,
    )
    with pytest.raises(ValueError, match="新标注已经发生"):
        migration.rollback(
            plan["migration_id"], f"ROLLBACK {plan['migration_id']}"
        )


def test_local_upgrade_migration_is_archived_neutral_and_idempotent(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    old_id = "20260830T071943.008380Z_xfan0282"
    old_collection = "20260830_xfan0282_02"
    old_directory = data_root / old_collection / old_id
    old_directory.mkdir(parents=True)
    h5_path = old_directory / f"{old_id}.h5"
    mkv_path = old_directory / f"{old_id}.mkv"
    with h5py.File(h5_path, "w") as handle:
        handle.attrs.update(
            {
                "recording_id": old_id,
                "collection_id": old_collection,
                "participant_id": "xfan0282",
                "recording_kind": "capture",
                "started_at_utc": "2026-08-30T07:19:43.008380+00:00",
            }
        )
        handle.create_dataset("imu/samples/raw_counts", data=np.arange(24).reshape(4, 6))
        video = handle.create_group("video")
        video.attrs["path"] = mkv_path.name
    mkv_path.write_bytes(b"mkv")

    result = auto_migrate_local_identity(data_root)
    assert result is not None
    assert result["receipt"]["recording_count"] == 1
    new_id = "20260830T071943.008380Z"
    new_directory = data_root / "20260830_session_01" / new_id
    archived = (
        data_root
        / "_identity_migrations"
        / result["plan"]["migration_id"]
        / "archive"
        / old_collection
        / old_id
    )
    assert not old_directory.exists()
    assert (archived / f"{old_id}.h5").is_file()
    assert (new_directory / f"{new_id}.mkv").read_bytes() == b"mkv"
    with h5py.File(new_directory / f"{new_id}.h5", "r") as handle:
        assert "participant_id" not in handle.attrs
        assert handle.attrs["recording_id"] == new_id
        assert handle.attrs["collection_id"] == "20260830_session_01"
        assert handle["video"].attrs["path"] == f"{new_id}.mkv"
        np.testing.assert_array_equal(
            handle["imu/samples/raw_counts"], np.arange(24).reshape(4, 6)
        )

    assert auto_migrate_local_identity(data_root) is None


def test_local_collection_mapping_is_scoped_to_utc_day(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    old_collection = "xfan0282_test_01"
    for old_id, started_at in (
        ("20260826T235959.000000Z_xfan0282", "2026-08-26T23:59:59+00:00"),
        ("20260827T000001.000000Z_xfan0282", "2026-08-27T00:00:01+00:00"),
    ):
        directory = data_root / old_collection / old_id
        directory.mkdir(parents=True)
        with h5py.File(directory / f"{old_id}.h5", "w") as handle:
            handle.attrs.update(
                {
                    "recording_id": old_id,
                    "collection_id": old_collection,
                    "participant_id": "xfan0282",
                    "recording_kind": "capture",
                    "started_at_utc": started_at,
                }
            )
            handle.create_dataset("imu/samples/raw_counts", data=np.arange(6))

    result = auto_migrate_local_identity(data_root)
    assert result is not None
    mapped = {
        item["new_recording_id"]: item["new_collection_id"]
        for item in result["plan"]["captures"]
    }
    assert mapped == {
        "20260826T235959.000000Z": "20260826_session_01",
        "20260827T000001.000000Z": "20260827_session_01",
    }
