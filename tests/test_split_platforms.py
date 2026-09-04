import hashlib
import io
import json
import os
import sqlite3
import time
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import h5py
import numpy as np
import pytest
from fastapi.testclient import TestClient

from imu_data_collector.annotation_api import create_annotation_app
from imu_data_collector.annotation_catalog import AnnotationCatalog
from imu_data_collector.annotation_service import _training_snapshot_fingerprint
from imu_data_collector.capture_api import create_capture_app
from imu_data_collector.config import (
    AnnotationSettings,
    AuthSettings,
    IdentitySettings,
    Settings,
    StorageSettings,
)
from imu_data_collector.constants import (
    ANNOTATION_ACCEPTED_CAPTURE_SCHEMA_VERSIONS,
    CAPTURE_SCHEMA_VERSION,
)
from imu_data_collector.coordinator import RecordingCoordinator
from imu_data_collector.dataset_catalog import DATASET_HANDOFF_VERSION
from imu_data_collector.hdf5_store import sha256_file
from imu_data_collector.models import (
    ArtifactDescriptor,
    CalibrationProfile,
    CaptureManifestV2,
    DataTier,
    IndexReceipt,
    PublishTarget,
    RecordingState,
    RecordingSummary,
    ReviewWorkflowState,
    TrainingExportReference,
)
from imu_data_collector.publisher import _require_annotation_capabilities
from imu_data_collector.storage import LocalFilesystemStore, ObjectConflictError


class FailOnceDeleteStore(LocalFilesystemStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.fail_once = True

    def delete(self, key: str, *, if_generation_match: int | None) -> bool:
        if self.fail_once and key.endswith("/preview.mp4"):
            self.fail_once = False
            raise ObjectConflictError("模拟 generation 冲突")
        return super().delete(key, if_generation_match=if_generation_match)


class FailOnceSnapshotDeleteStore(LocalFilesystemStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.fail_once = True

    def delete(self, key: str, *, if_generation_match: int | None) -> bool:
        if self.fail_once and key.endswith(".tar"):
            self.fail_once = False
            raise ObjectConflictError("模拟训练快照 TAR generation 冲突")
        return super().delete(key, if_generation_match=if_generation_match)


def _settings(tmp_path: Path) -> Settings:
    data_root = tmp_path / "data"
    data_root.mkdir()
    return Settings(
        data_root=data_root,
        catalog_path=tmp_path / "capture.sqlite3",
        activity_taxonomy_path=Path("configs/activities.yaml").resolve(),
        identity=IdentitySettings(subject_ids={"xfan0282": "cw12eu:subject-001"}),
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


def _publish_fixture(
    store: LocalFilesystemStore,
    tmp_path: Path,
    *,
    recording_id: str = "fixture_001",
    data_tier: DataTier = DataTier.TEST,
    manifest_schema_version: str = "2.1.0",
    source_h5_schema_version: str = CAPTURE_SCHEMA_VERSION,
) -> str:
    source = tmp_path / f"source-{recording_id}"
    source.mkdir()
    files = {
        "capture_h5": source / "capture.h5",
        "video_mkv": source / "video.mkv",
        "preview_mp4": source / "preview.mp4",
    }
    if data_tier == DataTier.PROD:
        with h5py.File(files["capture_h5"], "w") as handle:
            frames = handle.create_group("video/frames")
            frame_times = np.asarray([0, 500_000_000, 1_000_000_000], dtype=np.int64)
            frames.create_dataset("recording_time_ns", data=frame_times)
            frames.create_dataset("media_time_ns", data=frame_times)
    else:
        files["capture_h5"].write_bytes(b"hdf5-fixture")
    files["video_mkv"].write_bytes(b"matroska-fixture")
    files["preview_mp4"].write_bytes(b"0123456789")
    descriptors = []
    for role, path in files.items():
        filename = path.name
        key = f"captures/{recording_id}/{filename}"
        digest = sha256_file(path)
        content_type = {
            "capture_h5": "application/x-hdf5",
            "video_mkv": "video/x-matroska",
            "preview_mp4": "video/mp4",
        }[role]
        store.put_file(
            path,
            key,
            content_type=content_type,
            metadata={"sha256": digest},
        )
        descriptors.append(
            ArtifactDescriptor(
                role=role,
                object_key=key,
                filename=filename,
                size_bytes=path.stat().st_size,
                sha256=digest,
                content_type=content_type,
            )
        )
    manifest = CaptureManifestV2(
        recording_id=recording_id,
        collection_id="pilot",
        participant_id="xfan0282",
        data_tier=data_tier,
        captured_at_utc="2026-08-26T00:00:00+00:00",
        duration_ns=1_000_000_000,
        source_h5_schema_version=source_h5_schema_version,
        software_revision="test",
        calibration=CalibrationProfile(),
        artifacts=descriptors,
    )
    payload = manifest.model_dump(mode="json")
    payload["schema_version"] = manifest_schema_version
    store.write_json(
        f"captures/{recording_id}/manifest.json",
        payload,
        if_generation_match=0,
    )
    return recording_id


def _install_completed_review(app, store, tmp_path: Path, recording_id: str) -> None:
    logical_digest = "b" * 64
    aligned = tmp_path / f"{recording_id}.aligned.h5"
    text = h5py.string_dtype(encoding="utf-8")
    sequence_dtype = np.dtype(
        [
            ("sample_start", "<i8"),
            ("sample_stop", "<i8"),
            ("source_file", text),
            ("participant_id", text),
            ("recording_id", text),
            ("body_location", text),
            ("activity_code", text),
            ("is_fall", "?"),
            ("supervision_kind", text),
            ("source_sampling_rate_hz", "<f8"),
        ]
    )
    annotation_dtype = np.dtype(
        [
            ("sequence_index", "<i4"),
            ("kind", text),
            ("start_sample", "<i8"),
            ("stop_sample", "<i8"),
            ("code", text),
        ]
    )
    with h5py.File(aligned, "w") as handle:
        handle.attrs.update(
            {
                "imu_schema_version": "3.1.0",
                "sampling_rate_hz": 25.0,
                "evaluation_role": "training_only",
                "logical_content_sha256": logical_digest,
                "grid_origin_recording_time_ns": 0,
            }
        )
        handle.create_dataset("samples", data=np.zeros((2, 6), dtype=np.float32))
        handle.create_dataset(
            "sequences",
            data=np.asarray(
                [
                    (
                        0,
                        2,
                        "capture.h5",
                        "cw12eu:xfan0282",
                        f"cw12eu:{recording_id}",
                        "chest",
                        "walking",
                        False,
                        "temporal",
                        25.0,
                    )
                ],
                dtype=sequence_dtype,
            ),
        )
        handle.create_dataset(
            "annotations",
            data=np.asarray([(0, "activity", 0, 2, "walking")], dtype=annotation_dtype),
        )
    digest = sha256_file(aligned)
    key = f"exports/{recording_id}/review-0/aligned-{logical_digest[:16]}.h5"
    info = store.put_file(
        aligned,
        key,
        content_type="application/x-hdf5",
        metadata={
            "sha256": digest,
            "logical_content_sha256": logical_digest,
            "recording_id": recording_id,
            "source_review_revision": "0",
            "hdf5_schema_version": "3.1.0",
            "sampling_rate_hz": "25",
        },
    )
    service = app.state.annotation_service
    manifest = service.required_manifest(recording_id)
    review, generation = service.reviews.load(manifest)
    completed = review.model_copy(
        update={
            "workflow": review.workflow.model_copy(
                update={
                    "state": ReviewWorkflowState.COMPLETED,
                    "annotator_id": "xfan0282",
                    "last_editor_id": "xfan0282",
                }
            ),
            "active_export": TrainingExportReference(
                export_schema_version="2.0.0",
                hdf5_schema_version="3.1.0",
                sampling_rate_hz=25.0,
                filename="aligned.h5",
                source_review_revision=0,
                object_key=key,
                sha256=digest,
                logical_content_sha256=logical_digest,
                size_bytes=info.size_bytes,
                calibration_profile_id="fixture",
                calibration_evidence_sha256="c" * 64,
                created_at_utc="2026-08-26T00:00:00+00:00",
            ),
        }
    )
    store.write_json(
        f"reviews/{recording_id}/review.json",
        completed.model_dump(mode="json"),
        if_generation_match=generation,
    )


def test_capture_app_does_not_expose_annotation_or_export_routes(
    tmp_path: Path,
) -> None:
    app = create_capture_app(_settings(tmp_path))
    paths = {route.path for route in app.routes}

    assert "/api/v1/recordings/{recording_id}/publish" in paths
    assert "/api/v1/recordings/{recording_id}/annotations" not in paths
    assert "/api/v1/training-releases" not in paths


def test_annotation_catalog_migrates_existing_rows_to_active(tmp_path: Path) -> None:
    path = tmp_path / "annotation.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE recordings (
                recording_id TEXT PRIMARY KEY,
                participant_id TEXT NOT NULL,
                collection_id TEXT NOT NULL,
                data_tier TEXT NOT NULL,
                captured_at_utc TEXT NOT NULL,
                manifest_generation INTEGER NOT NULL,
                manifest_json TEXT NOT NULL
            )
            """
        )

    AnnotationCatalog(path)

    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(recordings)")}
    assert "deletion_state" in columns


def test_annotation_app_indexes_manifest_and_supports_video_range(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = LocalFilesystemStore(settings.storage.root)
    recording_id = _publish_fixture(store, tmp_path)
    app = create_annotation_app(settings, store)
    paths = {route.path for route in app.routes}

    assert "/api/v1/recordings/start" not in paths
    assert "/api/v1/recordings/{recording_id}/publish" not in paths
    assert "/api/v1/recordings/{recording_id}" in paths
    assert "/api/v1/recordings/{recording_id}/capture-h5/download" in paths
    assert "/api/v1/sync-experiments/{experiment_id}" not in paths

    with TestClient(app) as client:
        refreshed = client.post("/api/v1/index/refresh")
        assert refreshed.json() == {
            "imported": 1,
            "unchanged": 0,
            "skipped": 0,
            "issues": [],
        }

        recordings = client.get("/api/v1/recordings").json()
        assert recordings[0]["recording_id"] == recording_id
        assert recordings[0]["state"] == "published"
        assert recordings[0]["upload_state"] == "published"
        assert recordings[0]["workflow_state"] == "unassigned"
        assert recordings[0]["annotator_id"] is None

        partial = client.get(
            f"/api/v1/recordings/{recording_id}/video",
            headers={"Range": "bytes=2-5"},
        )
        assert partial.status_code == 206
        assert partial.content == b"2345"
        assert partial.headers["content-range"] == "bytes 2-5/10"

        review = client.get(f"/api/v1/recordings/{recording_id}/review").json()
        assert review["workflow"] == {
            "state": "unassigned",
            "annotator_id": None,
            "last_editor_id": None,
            "updated_at_utc": None,
        }

        assigned = client.post(
            f"/api/v1/recordings/{recording_id}/workflow",
            json={"action": "assign", "expected_revision": 0, "comment": ""},
        )
        assert assigned.status_code == 200
        refreshed_recording = client.get(f"/api/v1/recordings/{recording_id}").json()
        assert refreshed_recording["workflow_state"] == "in_progress"
        assert refreshed_recording["annotator_id"] == "xfan0282"

        review_download = client.get(f"/api/v1/recordings/{recording_id}/review/download")
        assert review_download.status_code == 200
        assert review_download.json()["recording_id"] == recording_id
        assert "attachment" in review_download.headers["content-disposition"]

        capture_download = client.get(f"/api/v1/recordings/{recording_id}/capture-h5/download")
        assert capture_download.status_code == 200
        assert capture_download.content == b"hdf5-fixture"
        assert capture_download.headers["content-type"] == "application/x-hdf5"
        assert capture_download.headers["content-length"] == str(len(b"hdf5-fixture"))
        assert capture_download.headers["content-disposition"] == (
            f'attachment; filename="{recording_id}.capture.h5"'
        )
        assert capture_download.headers["cache-control"] == "private, no-store"

        missing_export = client.get(f"/api/v1/recordings/{recording_id}/aligned30/download")
        assert missing_export.status_code == 403

        aligned = tmp_path / "aligned30.h5"
        aligned.write_bytes(b"aligned-hdf5-fixture")
        store.put_file(
            aligned,
            f"exports/{recording_id}/aligned30.h5",
            content_type="application/x-hdf5",
        )
        aligned_download = client.get(f"/api/v1/recordings/{recording_id}/aligned30/download")
        assert aligned_download.status_code == 403
        assert "test 数据永久禁止" in aligned_download.json()["detail"]["message"]

        aligned30_write_path = f"/api/v1/recordings/{recording_id}/aligned30"
        assert not any(
            route.path == "/api/v1/recordings/{recording_id}/aligned30"
            and "POST" in (getattr(route, "methods", None) or set())
            for route in app.routes
        )
        removed_write = client.post(
            aligned30_write_path,
            json={"expected_revision": 0},
        )
        # 未构建静态前端时没有匹配路由，返回 404；生产包带有仅 GET 的
        # SPA catch-all，Starlette 对同一路径的 POST 返回 405。两者都证明
        # 标注服务没有重新暴露 aligned30 写接口。
        assert removed_write.status_code in {404, 405}


def test_annotation_reads_legacy_manifests_and_publishes_v3_capability(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = LocalFilesystemStore(settings.storage.root)
    recording_id = _publish_fixture(store, tmp_path, manifest_schema_version="2.0.0")
    app = create_annotation_app(settings, store)

    with TestClient(app) as client:
        capabilities, _generation = store.read_json("contracts/annotation-capabilities.json")
        assert capabilities["accepted_manifest_schema_versions"] == [
            "2.0.0",
            "2.1.0",
            "3.0.0",
        ]
        assert capabilities["accepted_capture_h5_schema_versions"] == list(
            ANNOTATION_ACCEPTED_CAPTURE_SCHEMA_VERSIONS
        )
        result = client.post("/api/v1/index/refresh").json()

    assert result["imported"] == 1
    assert result["skipped"] == 0
    assert result["issues"] == []
    receipt, _generation = store.read_json(f"index-receipts/{recording_id}.json")
    assert receipt["status"] == "indexed"
    assert receipt["manifest_generation"] > 0


def test_annotation_rejection_has_structured_issue_and_receipt(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = LocalFilesystemStore(settings.storage.root)
    recording_id = _publish_fixture(
        store,
        tmp_path,
        source_h5_schema_version="1.4.0",
    )
    app = create_annotation_app(settings, store)

    with TestClient(app) as client:
        result = client.post("/api/v1/index/refresh").json()

    assert result["imported"] == 0
    assert result["skipped"] == 1
    assert result["issues"][0]["recording_id"] == recording_id
    assert result["issues"][0]["stage"] == "manifest"
    assert result["issues"][0]["code"] == "unsupported_h5_schema"
    receipt, _generation = store.read_json(f"index-receipts/{recording_id}.json")
    assert receipt["status"] == "rejected"
    assert receipt["code"] == "unsupported_h5_schema"


def test_missing_capabilities_blocks_before_any_capture_object(tmp_path: Path) -> None:
    store = LocalFilesystemStore(tmp_path / "objects")

    with pytest.raises(RuntimeError, match="上传任何对象前停止"):
        _require_annotation_capabilities(store, CAPTURE_SCHEMA_VERSION)

    assert store.list("captures/") == []


@pytest.mark.asyncio
async def test_capture_keeps_pending_when_receipt_generation_is_stale(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    coordinator = RecordingCoordinator(settings)
    summary = RecordingSummary(
        recording_id="generation-check",
        collection_id="pilot",
        participant_id="xfan0282",
        data_tier=DataTier.TEST,
        state=RecordingState.READY,
        started_at_utc="2026-08-27T00:00:00+00:00",
        upload_state="uploaded",
        publish_target=PublishTarget.DIRECT_GCS,
        index_state="pending",
        manifest_generation=200,
    )
    coordinator.catalog.upsert(summary)
    coordinator.object_store.write_json(
        "index-receipts/generation-check.json",
        IndexReceipt(
            recording_id="generation-check",
            manifest_generation=199,
            status="indexed",
            annotation_build_id="test",
            processed_at_utc="2026-08-27T00:00:00+00:00",
        ).model_dump(mode="json"),
        if_generation_match=0,
    )

    updated = await coordinator.refresh_publish_status("generation-check")
    await coordinator.shutdown()

    assert updated.index_state == "pending"
    assert "另一版 manifest" in updated.index_message


def test_annotation_background_refresh_discovers_new_manifest(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.annotation.catalog_refresh_interval_s = 0.02
    store = LocalFilesystemStore(settings.storage.root)
    app = create_annotation_app(settings, store)

    with TestClient(app) as client:
        assert client.get("/api/v1/recordings").json() == []
        recording_id = _publish_fixture(store, tmp_path)
        deadline = time.monotonic() + 1.0
        recordings: list[dict[str, object]] = []
        while time.monotonic() < deadline:
            recordings = client.get("/api/v1/recordings").json()
            if recordings:
                break
            time.sleep(0.02)

    assert [item["recording_id"] for item in recordings] == [recording_id]


def test_calibration_evidence_archive_is_independent_and_can_remove_source(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = LocalFilesystemStore(settings.storage.root)
    service = create_annotation_app(settings, store).state.annotation_service
    recording_id = next(iter(service.calibration_recording_ids))
    _publish_fixture(store, tmp_path, recording_id=recording_id)
    service.refresh()

    planned = service.archive_calibration_evidence()
    plan = next(item for item in planned["recordings"] if item["recording_id"] == recording_id)
    assert plan["status"] == "ready"
    assert plan["artifact_count"] == 4

    result = service.archive_calibration_evidence(apply=True, delete_source=True)
    archived = next(item for item in result["recordings"] if item["recording_id"] == recording_id)
    assert archived["status"] == "archived"
    assert archived["artifact_count"] == 4
    assert service.catalog.get(recording_id) is None
    assert store.list(f"captures/{recording_id}/") == []
    for role in ("capture_h5", "video_mkv", "preview_mp4", "capture_manifest"):
        artifact, info = service.calibration_evidence_artifact(recording_id, role)
        assert artifact["sha256"] == info.metadata["sha256"]


def test_annotation_refresh_skips_unchanged_manifest_generation(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = LocalFilesystemStore(settings.storage.root)
    _publish_fixture(store, tmp_path)
    app = create_annotation_app(settings, store)

    with TestClient(app) as client:
        assert client.post("/api/v1/index/refresh").json() == {
            "imported": 1,
            "unchanged": 0,
            "skipped": 0,
            "issues": [],
        }
        assert client.post("/api/v1/index/refresh").json() == {
            "imported": 0,
            "unchanged": 1,
            "skipped": 0,
            "issues": [],
        }


def test_iap_identity_is_required_mapped_and_cannot_claim_admin(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    settings.auth = AuthSettings(mode="iap", iap_audience="/projects/123/apps/app")
    settings.identity = IdentitySettings(
        email_to_unikey={
            "owner@example.com": "xfan0282",
            "member@example.com": "rkim6933",
        }
    )

    def verify(token: str, audience: str) -> dict[str, str]:
        assert audience == "/projects/123/apps/app"
        email = {
            "owner-token": "owner@example.com",
            "member-token": "member@example.com",
            "unknown-token": "unknown@example.com",
        }[token]
        return {"email": email, "sub": f"subject-{email}"}

    app = create_annotation_app(
        settings,
        LocalFilesystemStore(settings.storage.root),
        token_verifier=verify,
    )
    with TestClient(app) as client:
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/v1/session").status_code == 401
        unknown = client.get(
            "/api/v1/session",
            headers={"X-Goog-IAP-JWT-Assertion": "unknown-token"},
        )
        assert unknown.status_code == 403

        member_headers = {"X-Goog-IAP-JWT-Assertion": "member-token"}
        member = client.get("/api/v1/session", headers=member_headers)
        assert member.json() == {
            "unikey": "rkim6933",
            "email": "member@example.com",
            "is_admin": False,
            "auth_mode": "iap",
        }
        assert client.post("/api/v1/index/refresh", headers=member_headers).status_code == 403

        owner_headers = {"X-Goog-IAP-JWT-Assertion": "owner-token"}
        owner = client.get("/api/v1/session", headers=owner_headers)
        assert owner.json()["is_admin"] is True
        assert client.post("/api/v1/index/refresh", headers=owner_headers).status_code == 200


def test_annotation_delete_removes_unreleased_recording_and_shared_references(
    tmp_path: Path,
    caplog,
) -> None:
    settings = _settings(tmp_path)
    store = LocalFilesystemStore(settings.storage.root)
    recording_id = _publish_fixture(store, tmp_path)
    app = create_annotation_app(settings, store)
    cache = settings.storage.cache_root / recording_id
    cache.mkdir(parents=True)
    (cache / "capture.h5").write_bytes(b"cached")
    store.write_json(
        f"reviews/{recording_id}/review.json",
        {"temporary": True},
        if_generation_match=0,
    )
    export = tmp_path / "aligned.h5"
    export.write_bytes(b"aligned")
    store.put_file(
        export,
        f"exports/{recording_id}/aligned30.h5",
        content_type="application/x-hdf5",
    )

    with TestClient(app) as client:
        client.post("/api/v1/index/refresh")
        wrong = client.request(
            "DELETE",
            f"/api/v1/recordings/{recording_id}",
            json={"actor_id": "xfan0282", "confirmation": recording_id},
        )
        assert wrong.status_code == 422
        deleted = client.request(
            "DELETE",
            f"/api/v1/recordings/{recording_id}",
            json={
                "actor_id": "rkim6933",
                "confirmation": f"DELETE {recording_id}",
            },
        )
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True
        assert client.get(f"/api/v1/recordings/{recording_id}").status_code == 404

    assert f"recording_id={recording_id}" in caplog.text
    assert "actor_id=xfan0282" in caplog.text
    assert "actor_id=rkim6933" not in caplog.text


def test_annotation_delete_keeps_self_contained_training_snapshot(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = LocalFilesystemStore(settings.storage.root)
    recording_id = _publish_fixture(store, tmp_path)
    archive = tmp_path / "snapshot.tar"
    archive.write_bytes(b"snapshot")
    archive_info = store.put_file(
        archive,
        "training-snapshots/snapshot-000000000000000000000000/snapshot.tar",
        content_type="application/x-tar",
        metadata={
            "sha256": sha256_file(archive),
            "content_fingerprint": "f" * 64,
        },
    )
    store.write_json(
        "training-snapshots/snapshot-000000000000000000000000/manifest.json",
        {
            "schema_version": "2.0.0",
            "snapshot_id": "snapshot-000000000000000000000000",
            "archive_object_key": archive_info.key,
            "archive_sha256": sha256_file(archive),
            "archive_size_bytes": archive_info.size_bytes,
            "content_fingerprint": "f" * 64,
            "recordings": [{"recording_id": recording_id}],
        },
        if_generation_match=0,
    )
    app = create_annotation_app(settings, store)

    with TestClient(app) as client:
        client.post("/api/v1/index/refresh")
        deleted = client.request(
            "DELETE",
            f"/api/v1/recordings/{recording_id}",
            json={
                "actor_id": "xfan0282",
                "confirmation": f"DELETE {recording_id}",
            },
        )

    assert deleted.status_code == 200
    assert store.stat(f"captures/{recording_id}/manifest.json") is None
    payload, info = app.state.annotation_service.training_snapshot_download(
        "snapshot-000000000000000000000000"
    )
    assert payload["recordings"][0]["recording_id"] == recording_id
    assert info.key == archive_info.key


def test_annotation_delete_can_retry_after_partial_generation_conflict(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = FailOnceDeleteStore(settings.storage.root)
    recording_id = _publish_fixture(store, tmp_path)
    app = create_annotation_app(settings, store)
    payload = {
        "actor_id": "rkim6933",
        "confirmation": f"DELETE {recording_id}",
    }

    with TestClient(app) as client:
        client.post("/api/v1/index/refresh")
        conflicted = client.request("DELETE", f"/api/v1/recordings/{recording_id}", json=payload)
        assert conflicted.status_code == 409
        assert client.get(f"/api/v1/recordings/{recording_id}").status_code == 404
        client.post("/api/v1/index/refresh")
        assert client.get("/api/v1/recordings").json() == []

        retried = client.request("DELETE", f"/api/v1/recordings/{recording_id}", json=payload)

    assert retried.status_code == 200
    assert store.list(f"captures/{recording_id}/") == []


def test_training_snapshot_writes_queryable_sidecar_manifest(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = LocalFilesystemStore(settings.storage.root)
    recording_id = _publish_fixture(store, tmp_path, data_tier=DataTier.PROD)
    app = create_annotation_app(settings, store)

    with TestClient(app) as client:
        client.post("/api/v1/index/refresh")
        _install_completed_review(app, store, tmp_path, recording_id)
        created = client.post("/api/v1/training-snapshots")
        repeated = client.post("/api/v1/training-snapshots")
        current_key = "benchmark-datasets/team/cw12eu/current.json"
        assert store.stat(current_key) is None
        activated = client.post(
            f"/api/v1/training-snapshots/{created.json()['snapshot_id']}/activate-benchmark"
        )
        listed = client.get("/api/v1/training-snapshots")

    assert created.status_code == 200
    assert created.json()["created"] is True
    assert repeated.json()["created"] is False
    assert repeated.json()["snapshot_id"] == created.json()["snapshot_id"]
    assert activated.status_code == 200
    assert activated.json()["benchmark"]["is_current"] is True
    current, _generation = store.read_json(current_key)
    assert current["handoff_contract_version"] == DATASET_HANDOFF_VERSION
    assert [item["snapshot_id"] for item in listed.json()] == [created.json()["snapshot_id"]]
    archive_key = created.json()["archive_object_key"]
    sidecar_key = f"{archive_key.rsplit('/', 1)[0]}/manifest.json"
    sidecar, _generation = store.read_json(sidecar_key)
    assert sidecar["archive_object_key"] == archive_key
    assert [item["recording_id"] for item in sidecar["recordings"]] == [recording_id]


def test_training_snapshot_contract_version_avoids_legacy_id_collision(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = LocalFilesystemStore(settings.storage.root)
    recording_id = _publish_fixture(store, tmp_path, data_tier=DataTier.PROD)
    app = create_annotation_app(settings, store)
    service = app.state.annotation_service

    with TestClient(app) as client:
        client.post("/api/v1/index/refresh")
        _install_completed_review(app, store, tmp_path, recording_id)
        manifest = service.required_manifest(recording_id)
        reference, _info = service.active_export(recording_id)
        recordings = [
            {
                "participant_id": f"cw12eu:{manifest.participant_id}",
                "recording_id": recording_id,
                "source_review_revision": reference.source_review_revision,
                "export_schema_version": reference.export_schema_version,
                "hdf5_schema_version": reference.hdf5_schema_version,
                "sampling_rate_hz": reference.sampling_rate_hz,
                "aligned_object_key": reference.object_key,
                "aligned_sha256": reference.sha256,
                "logical_content_sha256": reference.logical_content_sha256,
            }
        ]
        legacy_fingerprint = hashlib.sha256(
            json.dumps(
                recordings,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        legacy_id = f"snapshot-{legacy_fingerprint[:24]}"
        store.write_json(
            f"training-snapshots/{legacy_id}/manifest.json",
            {"schema_version": "2.0.0", "created_at_utc": "2026-08-28T00:00:00+00:00"},
            if_generation_match=0,
        )
        store.write_json(
            f"benchmark-datasets/team/cw12eu/{legacy_id}/manifest.json",
            {
                "schema_version": "imu_benchmark_dataset_manifest_v1",
                "created_at_utc": "2026-08-28T00:00:00+00:00",
            },
            if_generation_match=0,
        )

        created = client.post("/api/v1/training-snapshots")

    assert created.status_code == 200
    created_manifest, _generation = store.read_json(
        f"training-snapshots/{created.json()['snapshot_id']}/manifest.json"
    )
    expected_fingerprint = _training_snapshot_fingerprint(created_manifest["recordings"])
    assert created.json()["content_fingerprint"] == expected_fingerprint
    assert created.json()["snapshot_id"] == f"snapshot-{expected_fingerprint[:24]}"
    assert created.json()["snapshot_id"] != legacy_id
    legacy, _generation = store.read_json(
        f"benchmark-datasets/team/cw12eu/{legacy_id}/manifest.json"
    )
    assert "handoff_contract_version" not in legacy


def test_training_snapshot_range_download_and_admin_cleanup(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = LocalFilesystemStore(settings.storage.root)
    recording_id = _publish_fixture(store, tmp_path, data_tier=DataTier.PROD)
    app = create_annotation_app(settings, store)

    with TestClient(app) as client:
        client.post("/api/v1/index/refresh")
        _install_completed_review(app, store, tmp_path, recording_id)
        snapshot = client.post("/api/v1/training-snapshots").json()
        snapshot_id = snapshot["snapshot_id"]
        partial = client.get(
            f"/api/v1/training-snapshots/{snapshot_id}/download",
            headers={"Range": "bytes=0-9"},
        )
        assert partial.status_code == 206
        assert len(partial.content) == 10
        assert partial.headers["content-range"].startswith("bytes 0-9/")
        suffix = client.get(
            f"/api/v1/training-snapshots/{snapshot_id}/download",
            headers={"Range": "bytes=-10"},
        )
        assert suffix.status_code == 206
        assert len(suffix.content) == 10
        head = client.head(f"/api/v1/training-snapshots/{snapshot_id}/download")
        assert head.status_code == 200
        assert head.content == b""
        assert head.headers["accept-ranges"] == "bytes"
        assert head.headers["x-content-sha256"] == snapshot["archive_sha256"]

        wrong = client.request(
            "DELETE",
            f"/api/v1/training-snapshots/{snapshot_id}",
            json={"confirmation": snapshot_id},
        )
        assert wrong.status_code == 422
        deleted = client.request(
            "DELETE",
            f"/api/v1/training-snapshots/{snapshot_id}",
            json={"confirmation": f"DELETE {snapshot_id}"},
        )
        assert deleted.json()["deleted"] is True
        assert client.get("/api/v1/training-snapshots").json() == []
        assert client.get(f"/api/v1/training-snapshots/{snapshot_id}/download").status_code == 404

        deleted = client.request(
            "DELETE",
            f"/api/v1/recordings/{recording_id}",
            json={"confirmation": f"DELETE {recording_id}"},
        )
        assert deleted.status_code == 200


def test_snapshot_customer_delivery_and_read_only_viewer(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = LocalFilesystemStore(settings.storage.root)
    recording_id = _publish_fixture(
        store,
        tmp_path,
        recording_id="20260826T000000.000000Z",
        data_tier=DataTier.PROD,
    )
    app = create_annotation_app(settings, store)

    with TestClient(app) as client:
        client.post("/api/v1/index/refresh")
        _install_completed_review(app, store, tmp_path, recording_id)
        snapshot = client.post("/api/v1/training-snapshots").json()
        snapshot_id = snapshot["snapshot_id"]
        store.write_json(
            f"client-deliveries/{snapshot_id}/manifest.json",
            {
                "schema_version": "cw12eu_client_delivery_v1",
                "snapshot_id": snapshot_id,
                "archive_object_key": "client-deliveries/legacy.zip",
            },
            if_generation_match=0,
        )
        assert client.get(
            f"/api/v1/training-snapshots/{snapshot_id}/delivery"
        ).json()["state"] == "not_created"
        deleted_source = client.request(
            "DELETE",
            f"/api/v1/recordings/{recording_id}",
            json={"confirmation": f"DELETE {recording_id}"},
        )
        assert deleted_source.status_code == 200
        queued = client.post(f"/api/v1/training-snapshots/{snapshot_id}/delivery")
        assert queued.status_code == 200
        for _attempt in range(200):
            status = client.get(f"/api/v1/training-snapshots/{snapshot_id}/delivery").json()
            if status["state"] in {"ready", "failed"}:
                break
            time.sleep(0.01)
        assert status["state"] == "ready", status

        downloaded = client.get(f"/api/v1/training-snapshots/{snapshot_id}/delivery/download")
        partial = client.get(
            f"/api/v1/training-snapshots/{snapshot_id}/delivery/download",
            headers={"Range": "bytes=0-9"},
        )
        suffix = client.get(
            f"/api/v1/training-snapshots/{snapshot_id}/delivery/download",
            headers={"Range": "bytes=-10"},
        )
        head = client.head(
            f"/api/v1/training-snapshots/{snapshot_id}/delivery/download"
        )
        viewer = client.get(f"/api/v1/training-snapshots/{snapshot_id}/viewer")
        overview = client.get(
            f"/api/v1/training-snapshots/{snapshot_id}/viewer/{recording_id}/timeline"
        )
        video = client.get(
            f"/api/v1/training-snapshots/{snapshot_id}/viewer/{recording_id}/video",
            headers={"Range": "bytes=2-5"},
        )

    assert downloaded.status_code == 200
    assert partial.status_code == 206
    assert len(partial.content) == 10
    assert suffix.status_code == 206
    assert len(suffix.content) == 10
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["accept-ranges"] == "bytes"
    assert head.headers["x-content-sha256"] == status["archive_sha256"]
    assert viewer.json()["recordings"][0]["recording_id"] == recording_id
    assert overview.json()["values"] == [[0.0] * 6, [0.0] * 6]
    assert video.status_code == 206
    assert video.content == b"2345"
    with zipfile.ZipFile(io.BytesIO(downloaded.content)) as archive:
        names = set(archive.namelist())
        assert "dataset/cw12eu.h5" in names
        assert "recordings/0000/video.mp4" in names
        assert "recordings/0000/view.json" in names
        assert {
            "manifest.json",
            "README.md",
            "DATASET_CARD.md",
            "SHA256SUMS",
        } <= names
        assert all(info.compress_type == zipfile.ZIP_STORED for info in archive.infolist())
        assert not any("capture.h5" in name or "raw" in name for name in names)
        package_manifest = json.loads(archive.read("manifest.json"))
        assert package_manifest["schema_version"] == "cw12eu_client_delivery_v2"
        assert package_manifest["contract_version"] == "2.0.0"
        assert package_manifest["snapshot_id"] == snapshot_id
        assert package_manifest["hdf5_schema_version"] == "3.1.0"
        assert package_manifest["recordings"][0]["recording_id"] == recording_id
        assert package_manifest["recordings"][0]["video_path"] == (
            "recordings/0000/video.mp4"
        )
        assert len(package_manifest["taxonomies"]) == 1
        taxonomy_path = package_manifest["taxonomies"][0]["path"]
        taxonomy = json.loads(archive.read(taxonomy_path))
        assert set(taxonomy) == {
            "schema_version",
            "taxonomy_id",
            "version",
            "fall",
            "non_fall",
        }
        assert all(set(item) == {"code", "name", "active"} for item in taxonomy["fall"])
        assert "xfan0282" not in archive.read(taxonomy_path).decode()
        checksums = {
            name: digest
            for digest, name in (
                line.split("  ", 1) for line in archive.read("SHA256SUMS").decode().splitlines()
            )
        }
        for name, digest in checksums.items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == digest


def test_training_snapshot_cleanup_retries_after_generation_conflict(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = FailOnceSnapshotDeleteStore(settings.storage.root)
    recording_id = _publish_fixture(store, tmp_path, data_tier=DataTier.PROD)
    app = create_annotation_app(settings, store)
    with TestClient(app) as client:
        client.post("/api/v1/index/refresh")
        _install_completed_review(app, store, tmp_path, recording_id)
        snapshot_id = client.post("/api/v1/training-snapshots").json()["snapshot_id"]
        body = {"confirmation": f"DELETE {snapshot_id}"}
        first = client.request("DELETE", f"/api/v1/training-snapshots/{snapshot_id}", json=body)
        assert first.status_code == 409
        assert len(client.get("/api/v1/training-snapshots").json()) == 1

        second = client.request("DELETE", f"/api/v1/training-snapshots/{snapshot_id}", json=body)
        assert second.status_code == 200
        assert second.json()["deleted"] is True


def test_cleanup_only_old_capture_groups_without_manifest(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = LocalFilesystemStore(settings.storage.root)
    for ordinal, key in enumerate(
        (
            "captures/old-orphan/chunk.bin",
            "captures/recent-orphan/chunk.bin",
            "captures/complete/chunk.bin",
        )
    ):
        source = tmp_path / f"chunk-{ordinal}.bin"
        source.write_bytes(f"chunk-{ordinal}".encode())
        store.put_file(source, key, content_type="application/octet-stream")
    store.write_json(
        "captures/complete/manifest.json",
        {"recording_id": "complete"},
        if_generation_match=0,
    )
    store.write_json(
        "index-receipts/old-orphan.json",
        {"recording_id": "old-orphan"},
        if_generation_match=0,
    )
    store.write_json(
        "index-receipts/complete.json",
        {"recording_id": "complete"},
        if_generation_match=0,
    )
    store.write_json(
        "_upload_sessions/expired.json",
        {"upload_id": "expired"},
        if_generation_match=0,
    )
    now = datetime(2026, 8, 26, tzinfo=UTC)
    old = (now - timedelta(days=8)).timestamp()
    os.utime(store.resolve("captures/old-orphan/chunk.bin"), (old, old))
    os.utime(store.resolve("captures/complete/chunk.bin"), (old, old))
    os.utime(store.resolve("captures/complete/manifest.json"), (old, old))
    os.utime(store.resolve("index-receipts/old-orphan.json"), (old, old))
    os.utime(store.resolve("index-receipts/complete.json"), (old, old))
    os.utime(store.resolve("_upload_sessions/expired.json"), (old, old))
    recent = (now - timedelta(days=1)).timestamp()
    os.utime(store.resolve("captures/recent-orphan/chunk.bin"), (recent, recent))
    service = create_annotation_app(settings, store).state.annotation_service

    preview = service.cleanup_orphan_uploads(now=now, dry_run=True)
    assert preview["orphan_recordings"] == 1
    assert preview["orphan_receipts"] == 1
    assert preview["orphan_upload_sessions"] == 1
    assert preview["candidate_objects"] == 3
    assert store.stat("captures/old-orphan/chunk.bin") is not None
    assert store.stat("index-receipts/old-orphan.json") is not None

    deleted = service.cleanup_orphan_uploads(now=now)
    assert deleted["deleted_objects"] == 3
    assert store.stat("captures/old-orphan/chunk.bin") is None
    assert store.stat("index-receipts/old-orphan.json") is None
    assert store.stat("_upload_sessions/expired.json") is None
    assert store.stat("captures/recent-orphan/chunk.bin") is not None
    assert store.stat("captures/complete/chunk.bin") is not None
    assert store.stat("index-receipts/complete.json") is not None
