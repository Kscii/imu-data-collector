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
    load_settings,
)
from imu_data_collector.hdf5_store import sha256_file
from imu_data_collector.models import (
    ActivitySegment,
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
from imu_data_collector.storage import LocalFilesystemStore


class FailOnceReleaseManifestStore(LocalFilesystemStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.fail_once = True

    def write_json(self, key, payload, *, if_generation_match):
        if (
            self.fail_once
            and key.startswith("releases/")
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
            review_policy="single_user",
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
                "capture_schema_version": "2.0.0",
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
        frames = handle.create_group("video").create_group("frames")
        frames.create_dataset(
            "recording_time_ns", data=np.arange(60, dtype=np.int64) * 33_333_333
        )

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
        source_h5_schema_version="2.0.0",
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


def _accepted_review(service, recording_id: str) -> None:
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
        taxonomy_version="1.0.0",
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
                    "state": ReviewWorkflowState.ACCEPTED,
                    "annotator_id": "xfan0282",
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
    app = create_annotation_app(settings, store)
    service = app.state.annotation_service
    assert service.refresh() == {"imported": 1, "skipped": 0}
    _accepted_review(service, recording_id)

    first_review = service.review(recording_id)
    first = service.export_training(recording_id, first_review.revision)
    reopened = service.update_workflow(
        recording_id,
        AnnotationReviewWorkflowRequest(
            action="reopen",
            expected_revision=service.review(recording_id).revision,
            comment="更正活动类别",
        ),
        "rkim6933",
    )
    assert reopened.active_export is None
    assert reopened.workflow.annotator_id == "rkim6933"

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
                    update={"state": ReviewWorkflowState.ACCEPTED}
                ),
            }
        )

    accepted = service.reviews.mutate(manifest, reopened.revision, accept_changed)
    second = service.export_training(recording_id, accepted.revision)

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
    _accepted_review(service, recording_id)
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
        service.export_training(recording_id, service.review(recording_id).revision)


def test_configured_evidence_hash_must_match_actual_file(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.imu.calibration_evidence_sha256 = "0" * 64
    store = LocalFilesystemStore(settings.storage.root)
    recording_id = _publish_calibrated_recording(settings, store, tmp_path)
    app = create_annotation_app(settings, store)
    service = app.state.annotation_service
    service.refresh()
    _accepted_review(service, recording_id)

    with pytest.raises(ValueError, match="实际证据文件"):
        service.export_training(recording_id, service.review(recording_id).revision)


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

    assert service.refresh() == {"imported": 0, "skipped": 1}
    assert service.list_recordings() == []


def test_release_retry_reuses_tar_after_manifest_write_failure(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = FailOnceReleaseManifestStore(settings.storage.root)
    recording_id = _publish_calibrated_recording(settings, store, tmp_path)
    service = create_annotation_app(settings, store).state.annotation_service
    service.refresh()
    _accepted_review(service, recording_id)
    service.export_training(recording_id, service.review(recording_id).revision)

    with pytest.raises(RuntimeError, match="manifest 写入中断"):
        service.create_release("xfan0282")
    assert len([item for item in store.list("releases/") if item.key.endswith(".tar")]) == 1

    recovered = service.create_release("xfan0282")
    repeated = service.create_release("xfan0282")
    assert recovered["created"] is True
    assert repeated["created"] is False
    assert recovered["release_id"] == repeated["release_id"]
    assert len([item for item in store.list("releases/") if item.key.endswith(".tar")]) == 1
    with pytest.raises(ValueError, match="只有管理员"):
        service.revoke_release(
            recovered["release_id"],
            actor_id="rkim6933",
            confirmation=f"REVOKE {recovered['release_id']}",
            reason="成员误操作",
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
    _accepted_review(service, recording_id)
    active = service.export_training(
        recording_id, service.review(recording_id).revision
    )
    orphan = tmp_path / "orphan.bin"
    orphan.write_bytes(b"orphan")
    orphan_export_key = "exports/unreferenced/review-1/aligned30-deadbeef.h5"
    orphan_release_key = "releases/release-orphan/archive.tar"
    store.put_file(orphan, orphan_export_key, content_type="application/x-hdf5")
    store.put_file(orphan, orphan_release_key, content_type="application/x-tar")
    now = datetime(2026, 8, 27, tzinfo=UTC)
    old = (now - timedelta(days=8)).timestamp()
    for key in (active.object_key, orphan_export_key, orphan_release_key):
        os.utime(store.resolve(key), (old, old))

    result = service.cleanup_orphan_uploads(now=now)

    assert result["orphan_exports"] == 1
    assert result["orphan_releases"] == 1
    assert store.stat(active.object_key) is not None
    assert store.stat(orphan_export_key) is None
    assert store.stat(orphan_release_key) is None
