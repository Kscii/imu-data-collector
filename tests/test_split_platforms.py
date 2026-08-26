import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from imu_data_collector.annotation_api import create_annotation_app
from imu_data_collector.annotation_catalog import AnnotationCatalog
from imu_data_collector.capture_api import create_capture_app
from imu_data_collector.config import (
    AnnotationSettings,
    AuthSettings,
    IdentitySettings,
    Settings,
    StorageSettings,
)
from imu_data_collector.hdf5_store import sha256_file
from imu_data_collector.models import (
    ArtifactDescriptor,
    CalibrationProfile,
    CaptureManifestV2,
    DataTier,
    ReviewWorkflowState,
    SyncExperimentDocument,
    SyncObservation,
)
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


class FailOnceReleaseDeleteStore(LocalFilesystemStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.fail_once = True

    def delete(self, key: str, *, if_generation_match: int | None) -> bool:
        if self.fail_once and key.endswith(".tar"):
            self.fail_once = False
            raise ObjectConflictError("模拟训练发布 TAR generation 冲突")
        return super().delete(key, if_generation_match=if_generation_match)


def _settings(tmp_path: Path) -> Settings:
    data_root = tmp_path / "data"
    data_root.mkdir()
    return Settings(
        data_root=data_root,
        catalog_path=tmp_path / "capture.sqlite3",
        activity_taxonomy_path=Path("configs/activities.yaml").resolve(),
        storage=StorageSettings(
            backend="local",
            root=tmp_path / "objects",
            cache_root=tmp_path / "cache",
        ),
        annotation=AnnotationSettings(
            catalog_path=tmp_path / "annotation.sqlite3",
            review_policy="single_user",
        ),
    )


def _publish_fixture(
    store: LocalFilesystemStore,
    tmp_path: Path,
    *,
    data_tier: DataTier = DataTier.TEST,
) -> str:
    recording_id = "fixture_001"
    source = tmp_path / "source"
    source.mkdir()
    files = {
        "capture_h5": source / "capture.h5",
        "video_mkv": source / "video.mkv",
        "preview_mp4": source / "preview.mp4",
    }
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
        source_h5_schema_version="1.0.0",
        software_revision="test",
        calibration=CalibrationProfile(),
        artifacts=descriptors,
    )
    store.write_json(
        f"captures/{recording_id}/manifest.json",
        manifest.model_dump(mode="json"),
        if_generation_match=0,
    )
    return recording_id


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
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(recordings)")
        }
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

    with TestClient(app) as client:
        refreshed = client.post("/api/v1/index/refresh")
        assert refreshed.json() == {"imported": 1, "skipped": 0}

        recordings = client.get("/api/v1/recordings").json()
        assert recordings[0]["recording_id"] == recording_id
        assert recordings[0]["state"] == "published"
        assert recordings[0]["upload_state"] == "published"

        partial = client.get(
            f"/api/v1/recordings/{recording_id}/video",
            headers={"Range": "bytes=2-5"},
        )
        assert partial.status_code == 206
        assert partial.content == b"2345"
        assert partial.headers["content-range"] == "bytes 2-5/10"

        review = client.get(f"/api/v1/recordings/{recording_id}/review").json()
        assert review["workflow"]["review_policy"] == "single_user"

        review_download = client.get(
            f"/api/v1/recordings/{recording_id}/review/download"
        )
        assert review_download.status_code == 200
        assert review_download.json()["recording_id"] == recording_id
        assert "attachment" in review_download.headers["content-disposition"]

        capture_download = client.get(
            f"/api/v1/recordings/{recording_id}/capture-h5/download"
        )
        assert capture_download.status_code == 200
        assert capture_download.content == b"hdf5-fixture"
        assert capture_download.headers["content-type"] == "application/x-hdf5"
        assert capture_download.headers["content-length"] == str(
            len(b"hdf5-fixture")
        )
        assert capture_download.headers["content-disposition"] == (
            f'attachment; filename="{recording_id}.capture.h5"'
        )
        assert capture_download.headers["cache-control"] == "private, no-store"

        missing_export = client.get(
            f"/api/v1/recordings/{recording_id}/aligned30/download"
        )
        assert missing_export.status_code == 403

        aligned = tmp_path / "aligned30.h5"
        aligned.write_bytes(b"aligned-hdf5-fixture")
        store.put_file(
            aligned,
            f"exports/{recording_id}/aligned30.h5",
            content_type="application/x-hdf5",
        )
        aligned_download = client.get(
            f"/api/v1/recordings/{recording_id}/aligned30/download"
        )
        assert aligned_download.status_code == 403
        assert "test 数据永久禁止" in aligned_download.json()["detail"]

        forbidden = client.post(
            f"/api/v1/recordings/{recording_id}/aligned30",
            json={"expected_revision": 0},
        )
        assert forbidden.status_code == 422
        assert "test 数据永久禁止" in forbidden.json()["detail"]


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
    experiment = SyncExperimentDocument(
        observations=[
            SyncObservation(
                observation_id="fixture_tap_01",
                recording_id=recording_id,
                video_frame_index=1,
                video_time_ns=1,
                imu_sample_index=1,
                imu_time_ns=1,
                reviewer_id="xfan0282",
            ),
            SyncObservation(
                observation_id="other_tap_01",
                recording_id="other_001",
                video_frame_index=1,
                video_time_ns=1,
                imu_sample_index=1,
                imu_time_ns=1,
                reviewer_id="xfan0282",
            ),
        ]
    )
    store.write_json(
        "diagnostics/sync-experiments/sync_validation_01.json",
        experiment.model_dump(mode="json"),
        if_generation_match=0,
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

    assert store.list(f"captures/{recording_id}/") == []
    assert store.list(f"reviews/{recording_id}/") == []
    assert store.list(f"exports/{recording_id}/") == []
    assert not cache.exists()
    saved_experiment, _generation = store.read_json(
        "diagnostics/sync-experiments/sync_validation_01.json"
    )
    assert [item["recording_id"] for item in saved_experiment["observations"]] == [
        "other_001"
    ]


def test_annotation_delete_rejects_recording_in_training_release(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = LocalFilesystemStore(settings.storage.root)
    recording_id = _publish_fixture(store, tmp_path)
    archive = tmp_path / "release.tar"
    archive.write_bytes(b"release")
    store.put_file(
        archive,
        "releases/release_001/release.tar",
        content_type="application/x-tar",
    )
    store.write_json(
        "releases/release_001/manifest.json",
        {
            "schema_version": "1.0.0",
            "release_id": "release_001",
            "recordings": [{"recording_id": recording_id}],
        },
        if_generation_match=0,
    )
    app = create_annotation_app(settings, store)

    with TestClient(app) as client:
        client.post("/api/v1/index/refresh")
        blocked = client.request(
            "DELETE",
            f"/api/v1/recordings/{recording_id}",
            json={
                "actor_id": "xfan0282",
                "confirmation": f"DELETE {recording_id}",
            },
        )

    assert blocked.status_code == 422
    assert "不可变训练发布" in blocked.json()["detail"]
    assert store.stat(f"captures/{recording_id}/manifest.json") is not None


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
        conflicted = client.request(
            "DELETE", f"/api/v1/recordings/{recording_id}", json=payload
        )
        assert conflicted.status_code == 409
        assert client.get(f"/api/v1/recordings/{recording_id}").status_code == 404
        client.post("/api/v1/index/refresh")
        assert client.get("/api/v1/recordings").json() == []

        retried = client.request(
            "DELETE", f"/api/v1/recordings/{recording_id}", json=payload
        )

    assert retried.status_code == 200
    assert store.list(f"captures/{recording_id}/") == []


def test_training_release_writes_queryable_sidecar_manifest(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = LocalFilesystemStore(settings.storage.root)
    recording_id = _publish_fixture(store, tmp_path, data_tier=DataTier.PROD)
    aligned = tmp_path / "aligned.h5"
    aligned.write_bytes(b"aligned")
    store.put_file(
        aligned,
        f"exports/{recording_id}/aligned30.h5",
        content_type="application/x-hdf5",
    )
    app = create_annotation_app(settings, store)

    with TestClient(app) as client:
        client.post("/api/v1/index/refresh")
        service = app.state.annotation_service
        manifest = service.required_manifest(recording_id)
        review, generation = service.reviews.load(manifest)
        exported = review.model_copy(
            update={
                "workflow": review.workflow.model_copy(
                    update={"state": ReviewWorkflowState.EXPORTED}
                )
            }
        )
        store.write_json(
            f"reviews/{recording_id}/review.json",
            exported.model_dump(mode="json"),
            if_generation_match=generation,
        )
        released = client.post("/api/v1/training-releases")
        repeated = client.post("/api/v1/training-releases")
        listed = client.get("/api/v1/training-releases")

    assert released.status_code == 200
    assert released.json()["created"] is True
    assert repeated.json()["created"] is False
    assert repeated.json()["release_id"] == released.json()["release_id"]
    assert [item["release_id"] for item in listed.json()] == [
        released.json()["release_id"]
    ]
    archive_key = released.json()["archive_object_key"]
    sidecar_key = f"{archive_key.rsplit('/', 1)[0]}/manifest.json"
    sidecar, _generation = store.read_json(sidecar_key)
    assert sidecar["archive_object_key"] == archive_key
    assert [item["recording_id"] for item in sidecar["recordings"]] == [recording_id]


def test_training_release_range_download_and_revoke(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = LocalFilesystemStore(settings.storage.root)
    recording_id = _publish_fixture(store, tmp_path, data_tier=DataTier.PROD)
    aligned = tmp_path / "aligned.h5"
    aligned.write_bytes(b"aligned")
    store.put_file(
        aligned,
        f"exports/{recording_id}/aligned30.h5",
        content_type="application/x-hdf5",
    )
    app = create_annotation_app(settings, store)

    with TestClient(app) as client:
        client.post("/api/v1/index/refresh")
        service = app.state.annotation_service
        manifest = service.required_manifest(recording_id)
        review, generation = service.reviews.load(manifest)
        store.write_json(
            f"reviews/{recording_id}/review.json",
            review.model_copy(
                update={
                    "workflow": review.workflow.model_copy(
                        update={"state": ReviewWorkflowState.EXPORTED}
                    )
                }
            ).model_dump(mode="json"),
            if_generation_match=generation,
        )
        release = client.post("/api/v1/training-releases").json()
        release_id = release["release_id"]
        partial = client.get(
            f"/api/v1/training-releases/{release_id}/download",
            headers={"Range": "bytes=0-9"},
        )
        assert partial.status_code == 206
        assert len(partial.content) == 10
        assert partial.headers["content-range"].startswith("bytes 0-9/")

        wrong = client.post(
            f"/api/v1/training-releases/{release_id}/revoke",
            json={"confirmation": release_id, "reason": "错误数据"},
        )
        assert wrong.status_code == 422
        revoked = client.post(
            f"/api/v1/training-releases/{release_id}/revoke",
            json={
                "confirmation": f"REVOKE {release_id}",
                "reason": "错误数据",
            },
        )
        assert revoked.json()["status"] == "revoked"
        assert client.get("/api/v1/training-releases").json() == []
        assert client.get(
            f"/api/v1/training-releases/{release_id}/download"
        ).status_code == 410
        status = client.get(
            f"/api/v1/training-releases/{release_id}/status"
        ).json()
        assert status["status"] == "revoked"
        assert status["reason"] == "错误数据"

        deleted = client.request(
            "DELETE",
            f"/api/v1/recordings/{recording_id}",
            json={"confirmation": f"DELETE {recording_id}"},
        )
        assert deleted.status_code == 200


def test_training_release_revoke_retries_after_partial_delete(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = FailOnceReleaseDeleteStore(settings.storage.root)
    recording_id = _publish_fixture(store, tmp_path, data_tier=DataTier.PROD)
    aligned = tmp_path / "aligned.h5"
    aligned.write_bytes(b"aligned")
    store.put_file(
        aligned,
        f"exports/{recording_id}/aligned30.h5",
        content_type="application/x-hdf5",
    )
    app = create_annotation_app(settings, store)
    with TestClient(app) as client:
        client.post("/api/v1/index/refresh")
        service = app.state.annotation_service
        manifest = service.required_manifest(recording_id)
        review, generation = service.reviews.load(manifest)
        store.write_json(
            f"reviews/{recording_id}/review.json",
            review.model_copy(
                update={
                    "workflow": review.workflow.model_copy(
                        update={"state": ReviewWorkflowState.EXPORTED}
                    )
                }
            ).model_dump(mode="json"),
            if_generation_match=generation,
        )
        release_id = client.post("/api/v1/training-releases").json()["release_id"]
        body = {
            "confirmation": f"REVOKE {release_id}",
            "reason": "需要重做",
        }
        first = client.post(
            f"/api/v1/training-releases/{release_id}/revoke", json=body
        )
        assert first.status_code == 409
        assert client.get("/api/v1/training-releases").json() == []
        assert client.get(
            f"/api/v1/training-releases/{release_id}/status"
        ).json()["status"] == "revoking"

        second = client.post(
            f"/api/v1/training-releases/{release_id}/revoke", json=body
        )
        assert second.status_code == 200
        assert second.json()["status"] == "revoked"


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
    now = datetime(2026, 8, 26, tzinfo=UTC)
    old = (now - timedelta(days=8)).timestamp()
    os.utime(store.resolve("captures/old-orphan/chunk.bin"), (old, old))
    os.utime(store.resolve("captures/complete/chunk.bin"), (old, old))
    os.utime(store.resolve("captures/complete/manifest.json"), (old, old))
    recent = (now - timedelta(days=1)).timestamp()
    os.utime(store.resolve("captures/recent-orphan/chunk.bin"), (recent, recent))
    service = create_annotation_app(settings, store).state.annotation_service

    preview = service.cleanup_orphan_uploads(now=now, dry_run=True)
    assert preview["orphan_recordings"] == 1
    assert preview["candidate_objects"] == 1
    assert store.stat("captures/old-orphan/chunk.bin") is not None

    deleted = service.cleanup_orphan_uploads(now=now)
    assert deleted["deleted_objects"] == 1
    assert store.stat("captures/old-orphan/chunk.bin") is None
    assert store.stat("captures/recent-orphan/chunk.bin") is not None
    assert store.stat("captures/complete/chunk.bin") is not None
