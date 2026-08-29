from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import h5py
import numpy as np
import pytest

from imu_data_collector.annotation_api import create_annotation_app
from imu_data_collector.config import (
    AnnotationSettings,
    Settings,
    StorageSettings,
    load_activity_taxonomy,
    load_settings,
)
from imu_data_collector.constants import CAPTURE_SCHEMA_VERSION
from imu_data_collector.hdf5_store import sha256_file
from imu_data_collector.models import (
    ActivitySegment,
    ActivityTaxonomyCreateRequest,
    ActivityTaxonomyDefinition,
    ActivityTaxonomyEntry,
    ActivityTaxonomyMigrationApplyRequest,
    ActivityTaxonomyMigrationPreviewRequest,
    ActivityTaxonomyUpdateRequest,
    AnnotationDocument,
    AnnotationReviewWorkflowRequest,
    ArtifactDescriptor,
    BinaryLabel,
    CalibrationProfile,
    CaptureManifestV2,
    DataTier,
    ReviewWorkflowState,
    SyncAnchor,
    SyncDocument,
)
from imu_data_collector.storage import LocalFilesystemStore, ObjectConflictError


class FailOnceSnapshotManifestStore(LocalFilesystemStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.fail_once = True

    def write_json(self, key, payload, *, if_generation_match):
        if (
            self.fail_once
            and key.startswith("training-snapshots/")
            and key.endswith("/manifest.json")
        ):
            self.fail_once = False
            raise RuntimeError("模拟 manifest 写入中断")
        return super().write_json(
            key, payload, if_generation_match=if_generation_match
        )


class CountingDownloadStore(LocalFilesystemStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.download_count = 0
        self._count_lock = threading.Lock()

    def download_file(self, key: str, destination: Path):
        with self._count_lock:
            self.download_count += 1
        time.sleep(0.02)
        return super().download_file(key, destination)


def _settings(tmp_path: Path) -> Settings:
    reference = load_settings()
    return Settings(
        data_root=tmp_path / "data",
        catalog_path=tmp_path / "capture.sqlite3",
        activity_taxonomy_path=reference.activity_taxonomy_path,
        calibration_evidence_path=reference.calibration_evidence_path,
        imu=reference.imu,
        storage=StorageSettings(
            backend="local",
            root=tmp_path / "objects",
            cache_root=tmp_path / "cache",
        ),
        annotation=AnnotationSettings(
            catalog_path=tmp_path / "annotation.sqlite3",
            catalog_refresh_interval_s=0,
        ),
    )


def _publish_calibrated_recording(
    settings: Settings, store: LocalFilesystemStore, tmp_path: Path
) -> str:
    recording_id = "calibrated_001"
    started_at = "2026-08-27T00:00:00+00:00"
    source = tmp_path / "source"
    source.mkdir()
    h5_path = source / "capture.h5"
    mkv_path = source / "video.mkv"
    preview_path = source / "preview.mp4"
    mkv_path.write_bytes(b"matroska")
    preview_path.write_bytes(b"preview")
    evidence_sha = settings.imu.calibration_evidence_sha256
    assert evidence_sha is not None

    with h5py.File(h5_path, "w") as handle:
        handle.attrs.update(
            {
                "capture_schema_version": CAPTURE_SCHEMA_VERSION,
                "recording_id": recording_id,
                "collection_id": "pilot",
                "participant_id": "xfan0282",
                "data_tier": "prod",
                "body_location": "chest",
                "started_at_utc": started_at,
                "duration_ns": 2_000_000_000,
                "recording_start_monotonic_ns": 10_000_000_000,
                "calibration_verified": True,
                "calibration_profile_id": settings.imu.calibration_profile_id,
            }
        )
        imu = handle.create_group("imu")
        imu.attrs.update(
            {
                "observed_rate_hz": 25.0,
                "calibration_profile_id": settings.imu.calibration_profile_id,
                "calibration_method": settings.imu.calibration_method,
                "calibration_evidence_sha256": evidence_sha,
                "accel_counts_per_g": settings.imu.accel_counts_per_g,
                "gyro_counts_per_dps": settings.imu.gyro_counts_per_dps,
                "accel_bias_counts_json": json.dumps(
                    settings.imu.accel_bias_counts
                ),
                "gyro_bias_counts_json": json.dumps(settings.imu.gyro_bias_counts),
                "raw_axis_order_json": json.dumps(settings.imu.raw_axis_order),
                "axis_signs_json": json.dumps(settings.imu.axis_signs),
            }
        )
        samples = imu.create_group("samples")
        times = np.arange(50, dtype=np.int64) * 40_000_000
        raw = np.column_stack(
            [
                np.arange(50),
                np.arange(50) + 1,
                np.full(50, 4096),
                np.arange(50) + 3,
                np.arange(50) + 4,
                np.arange(50) + 5,
            ]
        ).astype(np.int16)
        samples.create_dataset("recording_time_ns", data=times)
        samples.create_dataset("time_monotonic_ns", data=10_000_000_000 + times)
        samples.create_dataset("raw_counts", data=raw)
        samples.create_dataset("values_si", data=raw.astype(np.float32))
        samples.create_dataset("trailer", data=np.zeros((50, 4), dtype=np.uint8))
        frames = handle.create_group("video").create_group("frames")
        frame_times = np.arange(60, dtype=np.int64) * 33_333_333
        frames.create_dataset("recording_time_ns", data=frame_times)
        frames.create_dataset("media_time_ns", data=frame_times)

    paths = {
        "capture_h5": h5_path,
        "video_mkv": mkv_path,
        "preview_mp4": preview_path,
    }
    content_types = {
        "capture_h5": "application/x-hdf5",
        "video_mkv": "video/x-matroska",
        "preview_mp4": "video/mp4",
    }
    descriptors = []
    for role, path in paths.items():
        digest = sha256_file(path)
        key = f"captures/{recording_id}/{path.name}"
        store.put_file(
            path,
            key,
            content_type=content_types[role],
            metadata={"sha256": digest},
        )
        descriptors.append(
            ArtifactDescriptor(
                role=role,
                object_key=key,
                filename=path.name,
                size_bytes=path.stat().st_size,
                sha256=digest,
                content_type=content_types[role],
            )
        )
    calibration = CalibrationProfile(
        profile_id=settings.imu.calibration_profile_id,
        verified=True,
        accel_counts_per_g=settings.imu.accel_counts_per_g,
        gyro_counts_per_dps=settings.imu.gyro_counts_per_dps,
        accel_bias_counts=settings.imu.accel_bias_counts,
        gyro_bias_counts=settings.imu.gyro_bias_counts,
        raw_axis_order=settings.imu.raw_axis_order,
        axis_signs=settings.imu.axis_signs,
        method=settings.imu.calibration_method,
        evidence_sha256=evidence_sha,
    )
    manifest = CaptureManifestV2(
        recording_id=recording_id,
        collection_id="pilot",
        participant_id="xfan0282",
        data_tier=DataTier.PROD,
        captured_at_utc=started_at,
        duration_ns=2_000_000_000,
        source_h5_schema_version=CAPTURE_SCHEMA_VERSION,
        software_revision="test",
        calibration=calibration,
        artifacts=descriptors,
    )
    store.write_json(
        f"captures/{recording_id}/manifest.json",
        manifest.model_dump(mode="json"),
        if_generation_match=0,
    )
    return recording_id


def _ready_review(service, recording_id: str) -> None:
    manifest = service.required_manifest(recording_id)
    review, generation = service.reviews.load(manifest)
    sync = SyncDocument(
        anchors=[
            SyncAnchor(
                imu_time_ns=200_000_000,
                video_time_ns=200_000_000,
                role="start_tap",
                source_video_frame=6,
                source_imu_sample=5,
                video_interval_start_ns=166_666_665,
                imu_interval_start_ns=160_000_000,
                reviewer_id="xfan0282",
            ),
            SyncAnchor(
                imu_time_ns=1_600_000_000,
                video_time_ns=1_600_000_000,
                role="end_tap",
                source_video_frame=48,
                source_imu_sample=40,
                video_interval_start_ns=1_566_666_651,
                imu_interval_start_ns=1_560_000_000,
                reviewer_id="xfan0282",
            ),
        ]
    )
    annotations = AnnotationDocument(
        taxonomy_id="fall_binary_v1",
        taxonomy_version=service.taxonomy["version"],
        finalized=True,
        segments=[
            ActivitySegment(
                segment_id="seg_001",
                start_ns=0,
                end_ns=2_000_000_000,
                binary_label=BinaryLabel.NON_FALL,
                activity_code="standing",
                annotator_id="xfan0282",
            )
        ],
    )
    updated = review.model_copy(
        update={
            "sync": sync,
            "annotations": annotations,
            "workflow": review.workflow.model_copy(
                update={
                    "state": ReviewWorkflowState.IN_PROGRESS,
                    "annotator_id": "xfan0282",
                    "last_editor_id": "xfan0282",
                }
            ),
        }
    )
    store = service.store
    store.write_json(
        f"reviews/{recording_id}/review.json",
        updated.model_dump(mode="json"),
        if_generation_match=generation,
    )


def test_reopen_and_reexport_selects_new_immutable_object(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = LocalFilesystemStore(settings.storage.root)
    recording_id = _publish_calibrated_recording(settings, store, tmp_path)
    service = create_annotation_app(settings, store).state.annotation_service
    assert service.refresh() == {
        "imported": 1,
        "unchanged": 0,
        "skipped": 0,
        "issues": [],
    }
    _ready_review(service, recording_id)

    first_review = service.update_workflow(
        recording_id,
        AnnotationReviewWorkflowRequest(
            action="complete",
            expected_revision=service.review(recording_id).revision,
        ),
        "xfan0282",
    )
    assert first_review.active_export is not None
    first = first_review.active_export
    reopened = service.update_workflow(
        recording_id,
        AnnotationReviewWorkflowRequest(
            action="reopen",
            expected_revision=service.review(recording_id).revision,
            comment="更正活动类别",
        ),
        "xfan0282",
    )
    assert reopened.active_export is None
    taken = service.update_workflow(
        recording_id,
        AnnotationReviewWorkflowRequest(
            action="assign", expected_revision=reopened.revision
        ),
        "rkim6933",
    )
    assert taken.workflow.annotator_id == "rkim6933"

    manifest = service.required_manifest(recording_id)

    def accept_changed(review):
        segment = review.annotations.segments[0].model_copy(
            update={"activity_code": "walking", "annotator_id": "rkim6933"}
        )
        return review.model_copy(
            update={
                "annotations": review.annotations.model_copy(
                    update={"segments": [segment], "revision": 2}
                ),
                "workflow": review.workflow.model_copy(
                    update={"state": ReviewWorkflowState.IN_PROGRESS}
                ),
            }
        )

    changed = service.reviews.mutate(manifest, taken.revision, accept_changed)
    completed = service.update_workflow(
        recording_id,
        AnnotationReviewWorkflowRequest(
            action="complete", expected_revision=changed.revision
        ),
        "rkim6933",
    )
    assert completed.active_export is not None
    second = completed.active_export

    assert first.object_key != second.object_key
    assert first.logical_content_sha256 != second.logical_content_sha256
    assert store.stat(first.object_key) is not None
    active, _info = service.active_export(recording_id)
    assert active.object_key == second.object_key


def test_manifest_calibration_must_match_server_evidence(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = LocalFilesystemStore(settings.storage.root)
    recording_id = _publish_calibrated_recording(settings, store, tmp_path)
    app = create_annotation_app(settings, store)
    service = app.state.annotation_service
    service.refresh()
    _ready_review(service, recording_id)
    manifest = service.required_manifest(recording_id)
    service.catalog.upsert(
        manifest.model_copy(
            update={
                "calibration": manifest.calibration.model_copy(
                    update={"gyro_counts_per_dps": 16.4}
                )
            }
        ),
        999,
    )

    with pytest.raises(ValueError, match="gyro_counts_per_dps"):
        service.update_workflow(
            recording_id,
            AnnotationReviewWorkflowRequest(
                action="complete", expected_revision=service.review(recording_id).revision
            ),
            "xfan0282",
        )


def test_legacy_taxonomy_entry_without_name_uses_code_for_display() -> None:
    entry = ActivityTaxonomyEntry.model_validate({"code": "walking"})

    assert entry.name == "walking"


def test_legacy_taxonomy_version_without_change_metadata_remains_readable() -> None:
    definition = ActivityTaxonomyDefinition.model_validate(
        {
            "taxonomy_id": "fall_binary_v1",
            "version": "1.0.0",
            "fall": [{"code": "forward_fall", "name": "Forward fall"}],
            "non_fall": [{"code": "walking", "name": "Walking"}],
        }
    )

    assert definition.change is None


def test_existing_legacy_taxonomy_objects_are_not_rewritten(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = LocalFilesystemStore(settings.storage.root)
    legacy = load_activity_taxonomy(settings.activity_taxonomy_path)
    taxonomy_id = legacy["taxonomy_id"]
    safe_version = legacy["version"].replace("+", "_").replace("/", "_")
    current_key = f"taxonomies/{taxonomy_id}/current.json"
    version_key = f"taxonomies/{taxonomy_id}/versions/{safe_version}.json"
    store.write_json(current_key, legacy, if_generation_match=0)
    store.write_json(version_key, legacy, if_generation_match=0)

    service = create_annotation_app(settings, store).state.annotation_service

    assert service.taxonomy_definition()["change"] is None
    stored_current, _generation = store.read_json(current_key)
    stored_version, _generation = store.read_json(version_key)
    assert stored_current == legacy
    assert stored_version == legacy


def test_taxonomy_management_versions_and_protects_used_codes(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = LocalFilesystemStore(settings.storage.root)
    recording_id = _publish_calibrated_recording(settings, store, tmp_path)
    app = create_annotation_app(settings, store)
    service = app.state.annotation_service
    service.refresh()

    initial = service.taxonomy_definition()
    created = service.create_taxonomy_activity(
        ActivityTaxonomyCreateRequest(
            expected_version=initial["version"],
            binary_label="non_fall",
            code="stair_climbing",
            name="Stair climbing",
        ),
        "xfan0282",
    )
    assert created["version"] != initial["version"]
    assert next(
        item for item in created["non_fall"] if item["code"] == "stair_climbing"
    )["name"] == "Stair climbing"
    assert service.taxonomy_definition(initial["version"]) == initial

    review = service.update_workflow(
        recording_id,
        AnnotationReviewWorkflowRequest(
            action="assign",
            expected_revision=service.review(recording_id).revision,
        ),
        "xfan0282",
    )
    saved = service.save_annotations(
        recording_id,
        AnnotationDocument(
            taxonomy_id=initial["taxonomy_id"],
            taxonomy_version=initial["version"],
            revision=review.annotations.revision + 1,
            segments=[
                ActivitySegment(
                    segment_id="seg_001",
                    start_ns=0,
                    end_ns=1_000_000_000,
                    binary_label=BinaryLabel.NON_FALL,
                    activity_code="stair_climbing",
                    annotator_id="xfan0282",
                )
            ],
        ),
        "xfan0282",
        review.revision,
    )
    assert saved.taxonomy_version == created["version"]

    disabled = service.update_taxonomy_activity(
        "stair_climbing",
        ActivityTaxonomyUpdateRequest(
            expected_version=created["version"],
            name="Stairs",
            active=False,
        ),
        "xfan0282",
    )
    assert next(
        item for item in disabled["non_fall"] if item["code"] == "stair_climbing"
    )["name"] == "Stairs"
    current_review = service.review(recording_id)
    retained = saved.model_copy(update={"revision": saved.revision + 1})
    assert service.save_annotations(
        recording_id,
        retained,
        "xfan0282",
        current_review.revision,
    ).taxonomy_version == disabled["version"]

    with pytest.raises(ValueError, match="停用标签不能用于新标注"):
        service.save_annotations(
            recording_id,
            retained.model_copy(
                update={
                    "revision": retained.revision + 1,
                    "segments": [
                        *retained.segments,
                        retained.segments[0].model_copy(
                            update={
                                "segment_id": "seg_002",
                                "start_ns": 1_000_000_000,
                                "end_ns": 2_000_000_000,
                            }
                        ),
                    ],
                }
            ),
            "xfan0282",
            service.review(recording_id).revision,
        )

    with pytest.raises(ValueError, match="只能停用"):
        service.delete_taxonomy_activity(
            "stair_climbing", disabled["version"], "xfan0282"
        )


def test_taxonomy_management_api_allows_members_and_detects_conflict(
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient

    settings = _settings(tmp_path)
    store = LocalFilesystemStore(settings.storage.root)
    app = create_annotation_app(settings, store)
    with TestClient(app) as client:
        initial = client.get("/api/v1/taxonomy/manage")
        assert initial.status_code == 200
        version = initial.json()["version"]
        created = client.post(
            "/api/v1/taxonomy/activities",
            json={
                "expected_version": version,
                "binary_label": "non_fall",
                "code": "stair_climbing",
                "name": "Stair climbing",
            },
        )
        assert created.status_code == 200
        change = created.json()["change"]
        assert change["actor_unikey"] == "xfan0282"
        assert change["operation"] == "create"
        assert change["source_code"] == "stair_climbing"
        assert change["target_code"] is None
        assert datetime.fromisoformat(change["changed_at_utc"]).tzinfo == UTC
        preview = client.post(
            "/api/v1/taxonomy/migrations/preview",
            json={
                "expected_version": created.json()["version"],
                "source_code": "standing",
                "target_code": "stair_climbing",
            },
        )
        assert preview.status_code == 200
        assert preview.json()["affected_recordings"] == 0
        conflict = client.patch(
            "/api/v1/taxonomy/activities/stair_climbing",
            json={"expected_version": version, "active": False},
        )
        assert conflict.status_code == 409
        latest = client.get("/api/v1/taxonomy/manage").json()
        deleted = client.delete(
            "/api/v1/taxonomy/activities/stair_climbing",
            params={"expected_version": latest["version"]},
        )
        assert deleted.status_code == 200
        assert all(
            item["code"] != "stair_climbing"
            for item in deleted.json()["non_fall"]
        )
        assert "/api/v1/taxonomy/admin" not in app.openapi()["paths"]

    settings.auth.local_actor_id = "rkim6933"
    member_app = create_annotation_app(settings, store)
    with TestClient(member_app) as client:
        assert client.get("/api/v1/taxonomy").status_code == 200
        initial = client.get("/api/v1/taxonomy/manage")
        assert initial.status_code == 200
        created = client.post(
            "/api/v1/taxonomy/activities",
            json={
                "expected_version": initial.json()["version"],
                "binary_label": "non_fall",
                "code": "member_label",
                "name": "Member label",
            },
        )
        assert created.status_code == 200
        assert created.json()["change"]["actor_unikey"] == "rkim6933"
        updated = client.patch(
            "/api/v1/taxonomy/activities/member_label",
            json={
                "expected_version": created.json()["version"],
                "name": "Team label",
            },
        )
        assert updated.status_code == 200
        assert updated.json()["change"]["operation"] == "update"
        preview = client.post(
            "/api/v1/taxonomy/migrations/preview",
            json={
                "expected_version": updated.json()["version"],
                "source_code": "member_label",
                "target_code": "walking",
            },
        )
        assert preview.status_code == 200
        migrated = client.post(
            "/api/v1/taxonomy/migrations/apply",
            json={
                "expected_version": updated.json()["version"],
                "source_code": "member_label",
                "target_code": "walking",
                "plan_token": preview.json()["plan_token"],
                "confirmation": "MIGRATE member_label TO walking",
            },
        )
        assert migrated.status_code == 200
        assert migrated.json()["taxonomy"]["change"]["actor_unikey"] == "rkim6933"
        assert migrated.json()["taxonomy"]["change"]["operation"] == "migrate"
        deleted = client.delete(
            "/api/v1/taxonomy/activities/member_label",
            params={
                "expected_version": migrated.json()["taxonomy"]["version"]
            },
        )
        assert deleted.status_code == 200
        assert deleted.json()["change"]["operation"] == "delete"
        assert client.post("/api/v1/index/refresh").status_code == 403


def test_completed_review_exports_with_its_pinned_taxonomy_version(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = LocalFilesystemStore(settings.storage.root)
    recording_id = _publish_calibrated_recording(settings, store, tmp_path)
    service = create_annotation_app(settings, store).state.annotation_service
    service.refresh()
    _ready_review(service, recording_id)
    original_version = service.review(recording_id).annotations.taxonomy_version
    current = service.taxonomy_definition()
    service.create_taxonomy_activity(
        ActivityTaxonomyCreateRequest(
            expected_version=current["version"],
            binary_label="non_fall",
            code="stair_climbing",
            name="Stair climbing",
        ),
        "xfan0282",
    )

    completed = service.update_workflow(
        recording_id,
        AnnotationReviewWorkflowRequest(
            action="complete",
            expected_revision=service.review(recording_id).revision,
        ),
        "xfan0282",
    )

    assert completed.active_export is not None
    assert completed.annotations.taxonomy_version == original_version
    assert completed.annotations.taxonomy_version != service.taxonomy["version"]


def test_taxonomy_migration_rebuilds_completed_export_and_disables_source(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = LocalFilesystemStore(settings.storage.root)
    recording_id = _publish_calibrated_recording(settings, store, tmp_path)
    service = create_annotation_app(settings, store).state.annotation_service
    service.refresh()
    _ready_review(service, recording_id)
    current = service.taxonomy_definition()
    created = service.create_taxonomy_activity(
        ActivityTaxonomyCreateRequest(
            expected_version=current["version"],
            binary_label="non_fall",
            code="upright",
            name="Upright",
        ),
        "xfan0282",
    )
    review = service.review(recording_id)
    service.save_annotations(
        recording_id,
        review.annotations.model_copy(
            update={"revision": review.annotations.revision + 1}
        ),
        "xfan0282",
        review.revision,
    )
    completed = service.update_workflow(
        recording_id,
        AnnotationReviewWorkflowRequest(
            action="complete",
            expected_revision=service.review(recording_id).revision,
        ),
        "xfan0282",
    )
    assert completed.active_export is not None
    previous_export = completed.active_export

    preview_request = ActivityTaxonomyMigrationPreviewRequest(
        expected_version=created["version"],
        source_code="standing",
        target_code="upright",
    )
    preview = service.preview_taxonomy_migration(preview_request)
    assert preview["affected_recordings"] == 1
    assert preview["affected_segments"] == 1
    result = service.apply_taxonomy_migration(
        ActivityTaxonomyMigrationApplyRequest(
            **preview_request.model_dump(),
            plan_token=preview["plan_token"],
            confirmation="MIGRATE standing TO upright",
        ),
        "xfan0282",
    )

    migrated = service.review(recording_id)
    assert migrated.workflow.state == ReviewWorkflowState.COMPLETED
    assert migrated.annotations.segments[0].activity_code == "upright"
    assert migrated.active_export is not None
    assert migrated.active_export.object_key != previous_export.object_key
    assert migrated.active_export.logical_content_sha256 != (
        previous_export.logical_content_sha256
    )
    assert result["migrated"][0]["export_rebuilt"] is True
    assert result["remaining_usage"] == 0
    assert store.stat(result["receipt_object_key"]) is not None
    taxonomy = service.taxonomy_management_summary()
    standing = next(item for item in taxonomy["non_fall"] if item["code"] == "standing")
    assert standing["active"] is False


def test_taxonomy_migration_rejects_cross_category_and_stale_preview(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = LocalFilesystemStore(settings.storage.root)
    service = create_annotation_app(settings, store).state.annotation_service
    current = service.taxonomy_definition()
    with pytest.raises(ValueError, match="同一跌倒类型"):
        service.preview_taxonomy_migration(
            ActivityTaxonomyMigrationPreviewRequest(
                expected_version=current["version"],
                source_code="standing",
                target_code="forward_fall",
            )
        )

    created = service.create_taxonomy_activity(
        ActivityTaxonomyCreateRequest(
            expected_version=current["version"],
            binary_label="non_fall",
            code="upright",
            name="Upright",
        ),
        "xfan0282",
    )
    request = ActivityTaxonomyMigrationPreviewRequest(
        expected_version=created["version"],
        source_code="standing",
        target_code="upright",
    )
    preview = service.preview_taxonomy_migration(request)
    service.create_taxonomy_activity(
        ActivityTaxonomyCreateRequest(
            expected_version=created["version"],
            binary_label="non_fall",
            code="turning",
            name="Turning",
        ),
        "xfan0282",
    )
    with pytest.raises(ObjectConflictError, match="活动标签已经更新"):
        service.apply_taxonomy_migration(
            ActivityTaxonomyMigrationApplyRequest(
                **request.model_dump(),
                plan_token=preview["plan_token"],
                confirmation="MIGRATE standing TO upright",
            ),
            "xfan0282",
        )


def test_calibration_evidence_analysis_preserves_raw_and_derives_si(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = LocalFilesystemStore(settings.storage.root)
    recording_id = _publish_calibrated_recording(settings, store, tmp_path)
    app = create_annotation_app(settings, store)
    service = app.state.annotation_service
    profile_id = str(service.calibration_evidence["profile_id"])
    service.calibration_recording_ids.add(recording_id)
    source = tmp_path / "source" / "capture.h5"
    digest = sha256_file(source)
    object_key = f"calibration-evidence/{profile_id}/{recording_id}/capture.h5"
    info = store.put_file(
        source,
        object_key,
        content_type="application/x-hdf5",
        metadata={"sha256": digest},
    )
    store.write_json(
        service._calibration_manifest_key(profile_id, recording_id),
        {
            "schema_version": "1.0.0",
            "profile_id": profile_id,
            "recording_id": recording_id,
            "artifacts": [
                {
                    "role": "capture_h5",
                    "filename": "capture.h5",
                    "object_key": object_key,
                    "size_bytes": info.size_bytes,
                    "sha256": digest,
                    "content_type": "application/x-hdf5",
                }
            ],
        },
        if_generation_match=0,
    )

    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/calibration-evidence/{recording_id}/analysis"
        )
        assert response.status_code == 200
        result = response.json()

    assert result["conversion"]["available"] is True
    assert result["conversion"]["source"] == "runtime_authoritative_profile"
    assert result["imu"]["raw_counts"][0] == [0, 1, 4096, 3, 4, 5]
    assert len(result["imu"]["values_si"]) == 50
    assert result["imu"]["frame_hex"][0].startswith("00 00 00 01 10 00")
    assert result["video"]["media_time_ns"] == result["video"]["recording_time_ns"]

    settings.imu.calibration_evidence_sha256 = "0" * 64
    degraded = service.calibration_evidence_analysis(recording_id)
    assert degraded["conversion"]["available"] is False
    assert degraded["imu"]["values_si"] == []
    assert degraded["imu"]["raw_counts"] == result["imu"]["raw_counts"]


def test_configured_evidence_hash_must_match_actual_file(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.imu.calibration_evidence_sha256 = "0" * 64
    store = LocalFilesystemStore(settings.storage.root)
    recording_id = _publish_calibrated_recording(settings, store, tmp_path)
    app = create_annotation_app(settings, store)
    service = app.state.annotation_service
    service.refresh()
    _ready_review(service, recording_id)

    with pytest.raises(ValueError, match="实际证据文件"):
        service.update_workflow(
            recording_id,
            AnnotationReviewWorkflowRequest(
                action="complete", expected_revision=service.review(recording_id).revision
            ),
            "xfan0282",
        )


def test_takeover_changes_owner_and_old_owner_cannot_edit(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = LocalFilesystemStore(settings.storage.root)
    recording_id = _publish_calibrated_recording(settings, store, tmp_path)
    app = create_annotation_app(settings, store)
    service = app.state.annotation_service
    service.refresh()
    initial = service.review(recording_id)
    assigned = service.update_workflow(
        recording_id,
        AnnotationReviewWorkflowRequest(
            action="assign", expected_revision=initial.revision
        ),
        "xfan0282",
    )
    taken = service.update_workflow(
        recording_id,
        AnnotationReviewWorkflowRequest(
            action="assign", expected_revision=assigned.revision
        ),
        "rkim6933",
    )

    assert taken.workflow.annotator_id == "rkim6933"
    with pytest.raises(ValueError, match="其他成员"):
        service.save_annotations(
            recording_id,
            taken.annotations.model_copy(
                update={"revision": taken.annotations.revision + 1}
            ),
            "xfan0282",
            taken.revision,
        )


def test_annotation_public_writes_require_review_revision(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = LocalFilesystemStore(settings.storage.root)
    recording_id = _publish_calibrated_recording(settings, store, tmp_path)
    app = create_annotation_app(settings, store)

    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        client.post("/api/v1/index/refresh")
        review = client.get(f"/api/v1/recordings/{recording_id}/review").json()
        assert client.put(
            f"/api/v1/recordings/{recording_id}/annotations",
            json=review["annotations"],
        ).status_code == 422
        assert client.put(
            f"/api/v1/recordings/{recording_id}/sync",
            json=review["sync"],
        ).status_code == 422


def test_manifest_without_sha256_metadata_is_not_indexed(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = LocalFilesystemStore(settings.storage.root)
    recording_id = _publish_calibrated_recording(settings, store, tmp_path)
    store._metadata_path(
        f"captures/{recording_id}/preview.mp4"
    ).unlink()
    service = create_annotation_app(settings, store).state.annotation_service

    result = service.refresh()
    assert result["imported"] == 0
    assert result["skipped"] == 1
    assert result["issues"][0]["code"] == "artifact_sha_missing"
    assert service.list_recordings() == []


def test_snapshot_retry_reuses_tar_after_manifest_write_failure(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = FailOnceSnapshotManifestStore(settings.storage.root)
    recording_id = _publish_calibrated_recording(settings, store, tmp_path)
    service = create_annotation_app(settings, store).state.annotation_service
    service.refresh()
    _ready_review(service, recording_id)
    service.update_workflow(
        recording_id,
        AnnotationReviewWorkflowRequest(
            action="complete", expected_revision=service.review(recording_id).revision
        ),
        "xfan0282",
    )

    with pytest.raises(RuntimeError, match="manifest 写入中断"):
        service.create_training_snapshot("xfan0282")
    assert len(
        [
            item
            for item in store.list("training-snapshots/")
            if item.key.endswith(".tar")
        ]
    ) == 1

    recovered = service.create_training_snapshot("xfan0282")
    repeated = service.create_training_snapshot("xfan0282")
    assert recovered["created"] is True
    assert repeated["created"] is False
    assert recovered["snapshot_id"] == repeated["snapshot_id"]
    assert len(
        [
            item
            for item in store.list("training-snapshots/")
            if item.key.endswith(".tar")
        ]
    ) == 1
    with pytest.raises(ValueError, match="只有管理员"):
        service.delete_training_snapshot(
            recovered["snapshot_id"],
            actor_id="rkim6933",
            confirmation=f"DELETE {recovered['snapshot_id']}",
        )


def test_sha_addressed_cache_downloads_once_under_concurrency(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = CountingDownloadStore(settings.storage.root)
    recording_id = _publish_calibrated_recording(settings, store, tmp_path)
    service = create_annotation_app(settings, store).state.annotation_service
    service.refresh()
    manifest = service.required_manifest(recording_id)

    with ThreadPoolExecutor(max_workers=8) as executor:
        paths = list(executor.map(lambda _index: service.cached_h5(manifest), range(8)))

    assert len(set(paths)) == 1
    assert store.download_count == 1


def test_orphan_cleanup_preserves_active_export(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = LocalFilesystemStore(settings.storage.root)
    recording_id = _publish_calibrated_recording(settings, store, tmp_path)
    service = create_annotation_app(settings, store).state.annotation_service
    service.refresh()
    _ready_review(service, recording_id)
    completed = service.update_workflow(
        recording_id,
        AnnotationReviewWorkflowRequest(
            action="complete", expected_revision=service.review(recording_id).revision
        ),
        "xfan0282",
    )
    assert completed.active_export is not None
    active = completed.active_export
    orphan = tmp_path / "orphan.bin"
    orphan.write_bytes(b"orphan")
    orphan_export_key = "exports/unreferenced/review-1/aligned30-deadbeef.h5"
    orphan_snapshot_key = "training-snapshots/snapshot-000000000000000000000000/archive.tar"
    store.put_file(orphan, orphan_export_key, content_type="application/x-hdf5")
    store.put_file(orphan, orphan_snapshot_key, content_type="application/x-tar")
    now = datetime(2026, 8, 27, tzinfo=UTC)
    old = (now - timedelta(days=8)).timestamp()
    for key in (active.object_key, orphan_export_key, orphan_snapshot_key):
        os.utime(store.resolve(key), (old, old))

    result = service.cleanup_orphan_uploads(now=now)

    assert result["orphan_exports"] == 1
    assert result["orphan_snapshots"] == 1
    assert store.stat(active.object_key) is not None
    assert store.stat(orphan_export_key) is None
    assert store.stat(orphan_snapshot_key) is None
